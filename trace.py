"""
trace.py —— 任务轨迹回放层（Trace Layer）

给每条任务/流水线一条**可回放、可审计**的轨迹：从触发输入、拆解、派单、
执行、审批、质检到最终状态，全程结构化事件留痕。定位是"在现有底座上
补观测"，不是重写架构——trace_id 只走任务 payload 的 `trace_id` 键，
存储复用现成 SQLite，零新增依赖。

设计纪律（改动不可破坏）：
1. **哈希指针，不存原文**：`content=` 只算 blake2b 指纹落 `content_hash`
   列，原文永不进库；回放时可重新投喂原文验真（`verify()`）。
   敏感字段（密钥/Cookie/完整私有文档）按键名脱敏成
   `{"redacted": true, "sha256": ...}`——能比对、不可阅读。
2. **留痕是旁路，不是主链路**：所有记录接口 best-effort。调用方
   （DagScheduler / BrowserSkill）的 tracer 出异常绝不上抛——
   不留痕不能挡住干活；但 tracer 自身的错误会如实抛出给直接调用者。
3. **线程本地连接 + 显式 close()**：`with sqlite3.connect()` 是事务
   上下文不是关闭，会泄漏连接（ResourceWarning）。连接登记在册，
   close() 一并收掉**所有线程**的连接；回归测试在
   `-W error::ResourceWarning` 下必须零泄漏。
4. **seq 按 trace 单调分配**：`BEGIN IMMEDIATE` 内 `MAX(seq)+1`，
   回放顺序即发生顺序；`after_seq` 游标支持增量拉取。
5. **detail 有界**：超限截断（留头部 + 全文哈希），防一条事件把库撑爆。
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sqlite3
import sys
import threading
import time
from hashlib import blake2b
from typing import Any, Iterable, Optional

DEFAULT_DB_PATH = os.path.join("data", "trace.db")

# 这些键名（大小写不敏感、子串匹配）出现时，值不落库，只留指纹
REDACT_KEYS = (
    "secret", "token", "cookie", "password", "passwd", "credential",
    "api_key", "apikey", "private_key", "authorization", "bearer",
    "access_key", "signature", "otp", "session",
)

MAX_DETAIL_BYTES = 8192      # detail 序列化后的体积上限
TRUNC_KEEP = 2000            # 截断时保留的头部字符数

# 标准事件类型（文档性质，不强制——自定义 kind 允许，但推荐沿用）
EVENT_KINDS = (
    "plan_built",           # 拆解完成（工序列表/依赖/<待补>标记）
    "task_registered",      # 任务登记（含依赖）
    "task_claimed",         # 派单：谁认领、是否原子锁成功
    "task_completed",       # 完成（结果只留键名+指纹）
    "task_failed",          # 失败
    "task_needs_human",     # 升级人工（不级联，与 failed 语义不同）
    "tool_call",            # 工具调用（浏览器/语音/文件/外部 API）
    "approval_requested",   # 审批发起
    "approval_resolved",    # 审批结果（批准/拒绝/超时）
    "quality_rejected",     # 质检打回（理由+轮次）
    "quality_passed",       # 质检通过
    "cost_snapshot",        # 成本快照
    "trace_finished",       # 轨迹收尾
)


class TraceError(Exception):
    """trace 层自身错误（库损坏/参数非法）。best-effort 语义不掩蔽真错误。"""


def new_trace_id() -> str:
    """生成 trace_id：`tr_` + 12 位十六进制（72 位熵，撞不动）。"""
    return "tr_" + secrets.token_hex(6)


def _canonical(obj: Any) -> str:
    """规范化 JSON：键排序、无空白、保留中文——用于哈希与比对。"""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), default=str)


def digest(data: bytes) -> str:
    """内容指纹：blake2b-128（与 assets.py 同口径）。"""
    return blake2b(data, digest_size=16).hexdigest()


def _digest_text(text: str) -> str:
    return digest(text.encode("utf-8"))


def _is_sensitive_key(key: str) -> bool:
    k = str(key).lower()
    return any(s in k for s in REDACT_KEYS)


def redact(obj: Any) -> Any:
    """递归脱敏：敏感键的值 → {"redacted": true, "sha256": ...}。

    只按**键名**判断（不猜值像不像密钥）——键名是调用方声明的语义，
    比启发式可靠，也不会把正常文本误杀。
    """
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if _is_sensitive_key(k):
                out[k] = {"redacted": True, "sha256": _digest_text(_canonical(v))}
            else:
                out[k] = redact(v)
        return out
    if isinstance(obj, (list, tuple)):
        return [redact(v) for v in obj]
    return obj


def _bound_detail(detail: Any) -> Any:
    """detail 有界化：超限截断（留头 + 全文指纹），返回可 JSON 化对象。"""
    if detail is None:
        return None
    try:
        red = redact(detail)
    except Exception:  # noqa: BLE001 —— 脱敏失败按文本处理，不丢事件
        red = str(detail)
    text = _canonical(red)
    if len(text.encode("utf-8")) <= MAX_DETAIL_BYTES:
        return red
    head = text[:TRUNC_KEEP]
    return {"truncated": True, "bytes": len(text.encode("utf-8")),
            "head": head, "sha256": _digest_text(text)}


def trace_id_of(payload: Any) -> Optional[str]:
    """从任务 payload 取 trace_id（缺省/空 → None）。"""
    if not isinstance(payload, dict):
        return None
    tid = payload.get("trace_id")
    return tid if isinstance(tid, str) and tid else None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS traces (
    trace_id   TEXT PRIMARY KEY,
    title      TEXT NOT NULL DEFAULT '',
    kind       TEXT NOT NULL DEFAULT 'pipeline',
    started_by TEXT NOT NULL DEFAULT '',
    started_at REAL NOT NULL,
    ended_at   REAL,
    status     TEXT NOT NULL DEFAULT 'open',
    meta       TEXT
);
CREATE TABLE IF NOT EXISTS trace_events (
    trace_id     TEXT NOT NULL,
    seq          INTEGER NOT NULL,
    ts           REAL NOT NULL,
    task_id      TEXT,
    kind         TEXT NOT NULL,
    actor        TEXT NOT NULL DEFAULT '',
    summary      TEXT NOT NULL DEFAULT '',
    detail       TEXT,
    content_hash TEXT,
    PRIMARY KEY (trace_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_trace_events_task ON trace_events(task_id);
CREATE INDEX IF NOT EXISTS idx_trace_events_kind ON trace_events(kind);
"""


class TraceStore:
    """轨迹存储：线程本地连接 + 显式 close()。

    跨实例安全：seq 在 BEGIN IMMEDIATE 内分配，多进程/多线程同时
    写同一 trace 也不会重号。
    """

    def __init__(self, db_path: str = None):
        self.db_path = db_path or os.environ.get("TRACE_DB_PATH") or DEFAULT_DB_PATH
        self._local = threading.local()
        self._all_conns: set = set()   # 全部线程的连接登记：close() 一并收掉
        self._lock = threading.Lock()
        self._closed = False

    # ---- 连接 ----
    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            return conn
        if self._closed:
            raise TraceError("TraceStore 已关闭")
        directory = os.path.dirname(os.path.abspath(self.db_path))
        os.makedirs(directory, exist_ok=True)
        last_err = None
        for _ in range(3):
            try:
                conn = sqlite3.connect(self.db_path, timeout=30,
                                       check_same_thread=False)
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA busy_timeout=30000;")
                conn.execute("PRAGMA journal_mode=WAL;")
                # 建连探针：确认真能拿写锁（防 Windows 多进程首开半残连接）
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("ROLLBACK")
                conn.executescript(_SCHEMA)
                conn.commit()
                with self._lock:
                    self._all_conns.add(conn)
                self._local.conn = conn
                return conn
            except sqlite3.Error as e:  # noqa: PERF203
                last_err = e
                try:
                    conn.close()  # type: ignore[possibly-undefined]
                except Exception:  # noqa: BLE001
                    pass
        raise TraceError(f"无法打开 trace 库 {self.db_path}: {last_err}")

    def close(self):
        """关闭**所有线程**的连接（幂等）。

        只关当前线程不够：thread-local 连接在线程结束后没人收，
        GC 才报 ResourceWarning。这里用连接登记表一并关掉
        （check_same_thread=False，跨线程 close 安全）。
        """
        with self._lock:
            conns = list(self._all_conns)
            self._all_conns.clear()
        for c in conns:
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass
        self._local.conn = None
        self._closed = True

    # ---- 写 ----
    def start_trace(self, title: str = "", kind: str = "pipeline",
                    started_by: str = "", trace_id: str = None,
                    meta: dict = None) -> str:
        """开一条轨迹，返回 trace_id（缺省自动生成）。"""
        tid = trace_id or new_trace_id()
        conn = self._conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "INSERT INTO traces(trace_id, title, kind, started_by, started_at, status, meta)"
                " VALUES (?,?,?,?,?, 'open', ?)"
                " ON CONFLICT(trace_id) DO NOTHING",
                (tid, title, kind, started_by, time.time(),
                 _canonical(meta) if meta is not None else None))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return tid

    def record(self, trace_id: str, kind: str, task_id: str = None,
               actor: str = "", summary: str = "", detail: Any = None,
               content: str = None, ts: float = None) -> int:
        """记一条事件，返回 seq。content 只留指纹；detail 脱敏+有界。

        trace_id 未见过时自动补建轨迹行（worker 只从 payload 拿到
        trace_id 也能直接记）。
        """
        if not trace_id or not isinstance(trace_id, str):
            raise TraceError("trace_id 不能为空")
        if not kind:
            raise TraceError("kind 不能为空")
        bounded = _bound_detail(detail)
        detail_json = _canonical(bounded) if bounded is not None else None
        chash = _digest_text(content) if content is not None else None
        now = ts if ts is not None else time.time()
        conn = self._conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            seq = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM trace_events WHERE trace_id=?",
                (trace_id,)).fetchone()[0]
            conn.execute(
                "INSERT INTO trace_events(trace_id, seq, ts, task_id, kind, actor,"
                " summary, detail, content_hash) VALUES (?,?,?,?,?,?,?,?,?)",
                (trace_id, seq, now, task_id, kind, actor, summary,
                 detail_json, chash))
            conn.execute(
                "INSERT INTO traces(trace_id, started_at, status) VALUES (?,?, 'open')"
                " ON CONFLICT(trace_id) DO NOTHING",
                (trace_id, now))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return seq

    def finish(self, trace_id: str, status: str = "completed",
               meta_updates: dict = None) -> bool:
        """收尾轨迹。返回是否**本次**生效——只有 open 态会被改写，
        重复 finish 返回 False 且不动 ended_at（首次收尾留证）；
        meta 合并不受此限（迟到的事件摘要仍可补记）。"""
        conn = self._conn()
        cur = conn.execute(
            "UPDATE traces SET ended_at=?, status=? WHERE trace_id=? AND status='open'",
            (time.time(), status, trace_id))
        conn.commit()
        changed = cur.rowcount > 0
        if meta_updates:
            row = conn.execute("SELECT meta FROM traces WHERE trace_id=?",
                               (trace_id,)).fetchone()
            merged = json.loads(row["meta"]) if row and row["meta"] else {}
            merged.update(meta_updates)
            conn.execute("UPDATE traces SET meta=? WHERE trace_id=?",
                         (_canonical(merged), trace_id))
            conn.commit()
        return changed

    # ---- 读 ----
    @staticmethod
    def _row_to_event(r: sqlite3.Row) -> dict:
        return {
            "trace_id": r["trace_id"], "seq": r["seq"], "ts": r["ts"],
            "task_id": r["task_id"], "kind": r["kind"], "actor": r["actor"],
            "summary": r["summary"],
            "detail": json.loads(r["detail"]) if r["detail"] else None,
            "content_hash": r["content_hash"],
        }

    def events(self, trace_id: str, after_seq: int = 0,
               limit: int = None) -> list:
        """按 seq 正序回放（after_seq 游标支持增量拉取）。"""
        sql = ("SELECT * FROM trace_events WHERE trace_id=? AND seq>?"
               " ORDER BY seq ASC")
        params: list = [trace_id, after_seq]
        if limit:
            sql += " LIMIT ?"
            params.append(int(limit))
        rows = self._conn().execute(sql, params).fetchall()
        return [self._row_to_event(r) for r in rows]

    def by_task(self, task_id: str) -> dict:
        """按任务反查轨迹并回放（取该任务最新事件所在 trace）。"""
        row = self._conn().execute(
            "SELECT trace_id, MAX(seq) AS last_seq FROM trace_events"
            " WHERE task_id=? GROUP BY trace_id ORDER BY last_seq DESC LIMIT 1",
            (task_id,)).fetchone()
        if not row:
            return {"trace_id": None, "task_id": task_id, "events": []}
        tid = row["trace_id"]
        meta = self.get(tid)
        return {"trace_id": tid, "task_id": task_id, "trace": meta,
                "events": self.events(tid)}

    def get(self, trace_id: str) -> Optional[dict]:
        """轨迹元信息 + 事件统计 + 类型直方图。"""
        conn = self._conn()
        row = conn.execute("SELECT * FROM traces WHERE trace_id=?",
                           (trace_id,)).fetchone()
        if not row:
            return None
        total = conn.execute(
            "SELECT COUNT(*) AS c, MIN(ts) AS first_ts, MAX(ts) AS last_ts"
            " FROM trace_events WHERE trace_id=?",
            (trace_id,)).fetchone()
        kinds = conn.execute(
            "SELECT kind, COUNT(*) AS c FROM trace_events WHERE trace_id=?"
            " GROUP BY kind ORDER BY c DESC", (trace_id,)).fetchall()
        return {
            "trace_id": trace_id, "title": row["title"], "kind": row["kind"],
            "started_by": row["started_by"], "started_at": row["started_at"],
            "ended_at": row["ended_at"], "status": row["status"],
            "meta": json.loads(row["meta"]) if row["meta"] else None,
            "event_count": total["c"] or 0,
            "first_ts": total["first_ts"], "last_ts": total["last_ts"],
            "kinds": {k["kind"]: k["c"] for k in kinds},
        }

    def list(self, limit: int = 50, status: str = None) -> list:
        """最近轨迹（倒序）。"""
        sql = "SELECT * FROM traces"
        params: list = []
        if status:
            sql += " WHERE status=?"
            params.append(status)
        sql += " ORDER BY started_at DESC LIMIT ?"
        params.append(int(limit))
        out = []
        for r in self._conn().execute(sql, params).fetchall():
            out.append({"trace_id": r["trace_id"], "title": r["title"],
                        "kind": r["kind"], "started_by": r["started_by"],
                        "started_at": r["started_at"], "ended_at": r["ended_at"],
                        "status": r["status"]})
        return out

    def verify(self, trace_id: str, seq: int, content: str) -> bool:
        """回放验真：重新投喂原文，比对当时留存的指纹。"""
        row = self._conn().execute(
            "SELECT content_hash FROM trace_events WHERE trace_id=? AND seq=?",
            (trace_id, seq)).fetchone()
        if not row or not row["content_hash"]:
            return False
        return row["content_hash"] == _digest_text(content)

    def stats(self) -> dict:
        """全局统计：轨迹数、事件数、类型分布、状态分布。"""
        conn = self._conn()
        traces = conn.execute("SELECT COUNT(*) AS c FROM traces").fetchone()["c"]
        events = conn.execute("SELECT COUNT(*) AS c FROM trace_events").fetchone()["c"]
        by_kind = dict(conn.execute(
            "SELECT kind, COUNT(*) FROM trace_events GROUP BY kind").fetchall())
        by_status = dict(conn.execute(
            "SELECT status, COUNT(*) FROM traces GROUP BY status").fetchall())
        return {"traces": traces, "events": events,
                "by_kind": by_kind, "by_status": by_status}


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------
def main(argv=None):
    p = argparse.ArgumentParser(description="trace 回放层 CLI")
    p.add_argument("--db-path", default=None)
    p.add_argument("--new", action="store_true", help="开一条新轨迹")
    p.add_argument("--title", default="")
    p.add_argument("--kind", default="pipeline")
    p.add_argument("--by", default="", help="发起人标识")
    p.add_argument("--record", action="store_true", help="记一条事件")
    p.add_argument("--trace", help="trace_id")
    p.add_argument("--task", help="task_id（--record 时关联；--show 时按任务反查）")
    p.add_argument("--event-kind", default="custom")
    p.add_argument("--actor", default="")
    p.add_argument("--summary", default="")
    p.add_argument("--detail", default=None, help="JSON 字符串")
    p.add_argument("--content", default=None, help="只留指纹，不落原文")
    p.add_argument("--show", action="store_true", help="回放一条轨迹")
    p.add_argument("--after-seq", type=int, default=0)
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--list", action="store_true", help="列最近轨迹")
    p.add_argument("--status", default=None)
    p.add_argument("--stats", action="store_true")
    p.add_argument("--verify", nargs=2, metavar=("TRACE_ID", "SEQ"),
                   help="验真：--verify tr_x 3 --content '原文'")
    p.add_argument("--finish", metavar="TRACE_ID", help="收尾轨迹")
    a = p.parse_args(argv)

    store = TraceStore(a.db_path)
    try:
        if a.new:
            tid = store.start_trace(title=a.title, kind=a.kind, started_by=a.by)
            print(json.dumps({"trace_id": tid}, ensure_ascii=False))
        elif a.record:
            detail = json.loads(a.detail) if a.detail else None
            seq = store.record(a.trace, a.event_kind, task_id=a.task,
                               actor=a.actor, summary=a.summary,
                               detail=detail, content=a.content)
            print(json.dumps({"trace_id": a.trace, "seq": seq}, ensure_ascii=False))
        elif a.finish:
            ok = store.finish(a.finish, status=a.status or "completed")
            print(json.dumps({"trace_id": a.finish, "finished": ok}, ensure_ascii=False))
        elif a.verify:
            ok = store.verify(a.verify[0], int(a.verify[1]), a.content or "")
            print(json.dumps({"verified": ok}, ensure_ascii=False))
        elif a.show:
            if a.task and not a.trace:
                print(json.dumps(store.by_task(a.task), ensure_ascii=False, indent=2))
            else:
                meta = store.get(a.trace)
                evs = store.events(a.trace, after_seq=a.after_seq, limit=a.limit)
                print(json.dumps({"trace": meta, "events": evs},
                                 ensure_ascii=False, indent=2))
        elif a.list:
            print(json.dumps(store.list(limit=a.limit or 50, status=a.status),
                             ensure_ascii=False, indent=2))
        elif a.stats:
            print(json.dumps(store.stats(), ensure_ascii=False, indent=2))
        else:
            p.print_help()
    finally:
        store.close()


if __name__ == "__main__":
    main()
