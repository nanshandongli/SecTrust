"""
审批中心（ApprovalCenter）—— Human-in-the-loop 的核心。

任何「高权限 / 副作用 / 触碰外部资源」的动作（如 /scan repo、调用外部 API、
写文件）在执行前先进入审批中心挂起为 pending，由人工（或自动策略）批准/拒绝后才放行。
超时未决自动 expire，绝不默认放行——这是「保守默认拒绝」原则的落地。

审计链（2026-10-25 #91 审批链强化）：每次审批的**请求/决策/过期**都会在
Trace 轨迹里留一条事件（`approval_requested` / `approval_decided` /
`approval_expired`），detail 只存脱敏结构、原文只留 blake2b 指纹——
高危动作（`HIGH_RISK_ACTIONS`：外发/发布/购买/支付/删除/扫描）在事件里带
`risk=high` 标记。trace_id 解析：调用方显式传入 > 任务 payload 内嵌 >
无上下文则不记（不硬造孤儿轨迹）。轨迹写入全程 best-effort：
**审计留痕失败绝不挡审批主链路**。`audit(approval_id)` 返回一条审批的
完整证据链（审批行 + 轨迹事件）。

存储：与 task_registry 共用同一 SQLite 文件（同机共享），但独立维护 approvals 表，
      不污染最小契约。连接管理同样遵循：每线程独立连接、WAL、busy_timeout、
      CREATE TABLE IF NOT EXISTS（删库自动重建）。

用法（代码）：
  ac = ApprovalCenter(db_path="data/task_registry.db")
  aid = ac.request("scan_repo", "https://github.com/foo/bar", task_id="t1")
  # 阻塞等待真人/CLI 决策
  decision = ac.wait_for_decision(aid, timeout=120)
  if decision == "approved":
      ... 放行执行 ...
  chain = ac.audit(aid)        # 审计证据链（审批行 + 轨迹事件）

用法（CLI，真人审批入口）：
  python approval.py --list                      # 列出待审批
  python approval.py --approve <approval_id>      # 批准
  python approval.py --reject <approval_id>       # 拒绝
  python approval.py --audit <approval_id>        # 查看某审批的完整审计链
  python approval.py --sweep [--max-age 86400]    # 过期清理：挂起超时的标 expired
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import threading
import time
import uuid

from trace import TraceStore, trace_id_of


DEFAULT_DB_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "data", "task_registry.db"
)

# 连接打开重试次数：与 task_registry 同纪律。多进程/线程同时首次打开同一个
# 新库时，可能拿到"connect 成功但实际争锁失败"的连接，重开即可恢复。
_CONN_OPEN_RETRIES = 5

# 高危动作：审计事件带 risk=high（路线图口径：高风险 / 扫描 / 外发）
HIGH_RISK_ACTIONS = frozenset({
    "send", "publish", "post", "buy", "pay", "delete",
    "scan", "scan_repo",
})


def _risk_of(action: str) -> str:
    return "high" if action in HIGH_RISK_ACTIONS else "normal"


class ApprovalCenter:
    """审批中心：pending / approved / rejected / expired 四态。默认拒绝。

    tracer 参数：None（默认）= 自动装配 TraceStore（同一 SQLite 文件，
    懒连接）——每次请求/决策/过期都在轨迹里留审计事件；
    传入 TraceStore 实例 = 用调用方的；传 False = 完全关闭轨迹记录
    （纯审批表模式，供不想在同库建 trace 表的场景）。
    """

    def __init__(self, db_path=DEFAULT_DB_PATH, timeout=30, tracer=None):
        self.db_path = db_path
        os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)
        self._timeout = timeout
        self._local = threading.local()
        if tracer is False:
            self._trace_store = None
            self._owns_trace_store = False
        elif tracer is not None:
            self._trace_store = tracer          # 调用方的 store：生命周期归调用方
            self._owns_trace_store = False
        else:
            self._trace_store = TraceStore(db_path=self.db_path)
            self._owns_trace_store = True
        # 建表也走重试：多进程/线程首开竞争时，坏连接可能在建表这一步才暴露
        self._retry_write(self._ensure_schema)

    # ---- 连接管理（与 task_registry 同纪律：写探针 + 重试自愈）----
    def _open_conn(self):
        """建立一条"确认可写"的连接。

        同 task_registry 的纪律：新建连接后用 `BEGIN IMMEDIATE` + `ROLLBACK`
        做一次廉价写探针——它既确认连接真能拿写锁（挡住 Windows 上多进程首开
        的半残连接），也把并发首开串行化（每个连接拿到锁、释放后才轮到下一个，
        消除全新库上 `PRAGMA journal_mode=WAL` / CREATE TABLE 的
        `database is locked` 竞争）。探针窗口之外的瞬时锁冲突由 busy_timeout 兜底。
        """
        last_err = None
        for attempt in range(_CONN_OPEN_RETRIES):
            conn = sqlite3.connect(
                self.db_path, timeout=self._timeout, check_same_thread=False
            )
            try:
                conn.execute("PRAGMA busy_timeout=30000;")
                mode = conn.execute("PRAGMA journal_mode=WAL;").fetchone()[0]
                if str(mode).lower() != "wal":
                    raise sqlite3.OperationalError(f"journal_mode={mode!r}，期望 wal")
                conn.execute("BEGIN IMMEDIATE")   # 探针：拿一次写锁再释放
                conn.execute("ROLLBACK")
                return conn
            except sqlite3.Error as e:
                last_err = e
                try:
                    conn.close()
                except Exception:  # noqa: BLE001  关闭失败不影响重试
                    pass
                time.sleep(0.05 * (attempt + 1))
        raise RuntimeError(
            f"无法建立可写连接（重试 {_CONN_OPEN_RETRIES} 次后仍失败）: "
            f"{type(last_err).__name__}: {last_err}"
        )

    def _connect(self):
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._open_conn()
            self._local.conn = conn
        return conn

    def _drop_conn(self):
        """丢掉当前线程的连接（下次 _connect 重建）——用于连接损坏的恢复。"""
        conn = getattr(self._local, "conn", None)
        self._local.conn = None
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001  关闭失败不阻碍重建
                pass

    def _retry_write(self, fn):
        """执行一次写操作；失败则丢连接重开再试一次，仍失败才向上抛。

        fn(conn) 必须幂等（CREATE TABLE IF NOT EXISTS / 吞 duplicate column
        的 ALTER 均为幂等），因此"重试一次"不产生副作用。
        """
        try:
            with self._connect() as conn:
                return fn(conn)
        except sqlite3.Error:
            self._drop_conn()
            try:
                with self._connect() as conn:
                    return fn(conn)
            except sqlite3.Error:
                self._drop_conn()
                raise

    def _ensure_schema(self, conn):
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS approvals (
                approval_id TEXT PRIMARY KEY,
                task_id      TEXT,
                channel      TEXT,
                user_id      TEXT,
                action       TEXT NOT NULL,
                detail       TEXT,
                status       TEXT NOT NULL DEFAULT 'pending',
                created_at   REAL NOT NULL,
                decided_at   REAL,
                decided_by   TEXT
            )
            """
        )
        # 旧库迁移（2026-10-25 #91）：补 trace_id 列——审批行自描述审计链，
        # 任何进程（含 CLI）不必再解析任务 payload 就能找到轨迹。
        # 并发安全：两个连接可能同时看到"无此列"同时 ALTER，后一个撞
        # duplicate column——那正是"别人已经迁好了"，吞掉即可（标准
        # SQLite 在线迁移模式）。PRAGMA 与 ALTER 之间不持事务，窗口真实存在。
        cols = {r[1] for r in conn.execute("PRAGMA table_info(approvals)").fetchall()}
        if "trace_id" not in cols:
            try:
                conn.execute("ALTER TABLE approvals ADD COLUMN trace_id TEXT")
            except sqlite3.OperationalError as e:
                if "duplicate column" not in str(e):
                    raise
        conn.commit()

    def close(self):
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            finally:
                self._local.conn = None
        store = getattr(self, "_trace_store", None)
        if store is not None and getattr(self, "_owns_trace_store", False):
            try:
                store.close()
            except Exception:  # noqa: BLE001 —— 关连接的清理尽力而为
                pass

    # ---- 写：发起审批 ----
    def request(self, action: str, detail: str, task_id: str = "",
                channel: str = "", user_id: str = "",
                trace_id: str = None) -> str:
        """发起审批，返回 approval_id。

        trace_id 显式传入优先；否则从任务 payload 解析（approvals 与 tasks
        同库，任何进程含 CLI 都能自助解析）；都取不到则不记轨迹——
        不硬造孤儿轨迹。轨迹写入失败不影响审批本身（旁路纪律）。
        """
        aid = f"ap-{uuid.uuid4().hex[:10]}"
        now = time.time()
        tid = trace_id or self._resolve_trace_id(task_id)   # 解析一次，落进行内
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO approvals "
                "(approval_id, task_id, channel, user_id, action, detail, status, "
                " created_at, decided_at, decided_by, trace_id) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (aid, task_id, channel, user_id, action, detail,
                 "pending", now, None, None, tid),
            )
        self._trace_request(aid, action, detail, task_id, channel, user_id, tid)
        return aid

    # ---- 写：决策 ----
    def decide(self, approval_id: str, decision: str, by: str = "human") -> bool:
        """decision: 'approve' / 'reject'。返回是否成功（id 存在且当时为 pending）。"""
        decision = decision.lower()
        if decision not in ("approve", "reject"):
            raise ValueError("decision 仅支持 approve / reject")
        status = "approved" if decision == "approve" else "rejected"
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE approvals SET status=?, decided_at=?, decided_by=? "
                "WHERE approval_id=? AND status='pending'",
                (status, time.time(), by, approval_id),
            )
            ok = cur.rowcount == 1
        if ok:
            self._trace_transition(approval_id, "approval_decided", actor=by)
        return ok

    def _mark_expired(self, approval_id: str) -> bool:
        """标记过期（仅当时仍 pending 才生效）。返回是否本次标记成功。"""
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE approvals SET status='expired', decided_at=?, decided_by='system' "
                "WHERE approval_id=? AND status='pending'",
                (time.time(), approval_id),
            )
            ok = cur.rowcount == 1
        if ok:
            self._trace_transition(approval_id, "approval_expired", actor="system")
        return ok

    def sweep_expired(self, max_age: float = 86400) -> list:
        """过期清理：把挂起超过 max_age 秒的审批标记 expired。

        解决"没人 wait 的审批永远 pending"——wait_for_decision 只在有人
        等待时才标记过期，无人认领的审批会一直挂起。返回本次过期的
        approval_id 列表（每条补记 approval_expired 轨迹事件）。
        """
        cutoff = time.time() - float(max_age)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT approval_id FROM approvals "
                "WHERE status='pending' AND created_at<?",
                (cutoff,),
            ).fetchall()
        ids = [r[0] for r in rows]
        for aid in ids:
            self._mark_expired(aid)
        return ids

    # ---- 读：等待决策（供 runner 阻塞等待真人/CLI）----
    def wait_for_decision(self, approval_id: str, timeout: float = 120, poll: float = 1.0) -> str:
        """阻塞直到该审批被决定或超时。超时返回 'expired'（绝不返回 approved）。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            st = self.status(approval_id)
            if st in ("approved", "rejected", "expired"):
                if st == "expired":
                    self._mark_expired(approval_id)
                return st
            time.sleep(poll)
        self._mark_expired(approval_id)
        return "expired"

    def status(self, approval_id: str) -> str:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT status FROM approvals WHERE approval_id=?", (approval_id,)
            ).fetchone()
        return row[0] if row else "unknown"

    def is_approved(self, approval_id: str) -> bool:
        return self.status(approval_id) == "approved"

    # ---- 只读查询（task_center / CLI 用）----
    def list_approvals(self, status: str = None, limit: int = 100) -> list:
        q = ("SELECT approval_id, task_id, channel, user_id, action, detail, status, "
             "created_at, decided_at, decided_by, trace_id FROM approvals WHERE 1=1")
        params = []
        if status:
            q += " AND status=?"
            params.append(status)
        q += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(q, params).fetchall()
        return [
            {
                "approval_id": r[0], "task_id": r[1], "channel": r[2], "user_id": r[3],
                "action": r[4], "detail": r[5], "status": r[6],
                "created_at": r[7], "decided_at": r[8], "decided_by": r[9],
                "trace_id": r[10],
            }
            for r in rows
        ]

    # ---- 审计链：审批行 + 轨迹证据 ----
    def approvals_for_task(self, task_id: str, limit: int = 100) -> list:
        """按任务反查审批列表（倒序）。结案报告按任务聚合审批链用。"""
        if not task_id:
            return []
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT approval_id, task_id, channel, user_id, action, detail, status, "
                "created_at, decided_at, decided_by, trace_id FROM approvals "
                "WHERE task_id=? ORDER BY created_at DESC LIMIT ?",
                (task_id, int(limit)),
            ).fetchall()
        return [
            {
                "approval_id": r[0], "task_id": r[1], "channel": r[2], "user_id": r[3],
                "action": r[4], "detail": r[5], "status": r[6],
                "created_at": r[7], "decided_at": r[8], "decided_by": r[9],
                "trace_id": r[10],
            }
            for r in rows
        ]

    def _row(self, approval_id: str):
        """取单条审批行（dict；不存在返回 None）。"""
        with self._connect() as conn:
            r = conn.execute(
                "SELECT approval_id, task_id, channel, user_id, action, detail, status, "
                "created_at, decided_at, decided_by, trace_id FROM approvals "
                "WHERE approval_id=?",
                (approval_id,),
            ).fetchone()
        if r is None:
            return None
        return {
            "approval_id": r[0], "task_id": r[1], "channel": r[2], "user_id": r[3],
            "action": r[4], "detail": r[5], "status": r[6],
            "created_at": r[7], "decided_at": r[8], "decided_by": r[9],
            "trace_id": r[10],
        }

    def _resolve_trace_id(self, task_id: str):
        """从任务 payload 解析 trace_id（tasks 表不在/无 payload → None）。"""
        if not task_id:
            return None
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT payload FROM tasks WHERE task_id=?", (task_id,)
                ).fetchone()
        except sqlite3.Error:          # 纯审批库没有 tasks 表 → 无轨迹上下文
            return None
        if not row or not row[0]:
            return None
        try:
            return trace_id_of(json.loads(row[0]))
        except (json.JSONDecodeError, TypeError):
            return None

    def _trace_request(self, aid: str, action: str, detail: str, task_id: str,
                       channel: str, user_id: str, trace_id: str):
        """approval_requested 事件（best-effort，任何失败静默跳过）。
        trace_id 已是 request() 解析好的最终值（显式传入或任务 payload）。"""
        store = self._trace_store
        if store is None or not trace_id:
            return
        try:
            store.record(
                trace_id, "approval_requested", task_id=task_id or None,
                actor=user_id or channel or "",
                summary="请求审批 %s" % action,
                detail={"approval_id": aid, "action": action, "task_id": task_id,
                        "channel": channel, "user_id": user_id,
                        "risk": _risk_of(action)},
                content=detail,
            )
        except Exception:              # noqa: BLE001 —— 旁路纪律：留痕不挡审批
            pass

    def _trace_transition(self, approval_id: str, kind: str, actor: str):
        """approval_decided / approval_expired 事件（best-effort）。

        trace_id 取审批行内值（request 时落库）；旧库迁移前的审批没有该值，
        回退到任务 payload 解析。
        """
        store = self._trace_store
        if store is None:
            return
        try:
            row = self._row(approval_id)
            if row is None:
                return
            tid = row["trace_id"] or self._resolve_trace_id(row["task_id"])
            if not tid:
                return
            detail = {"approval_id": approval_id, "action": row["action"],
                      "task_id": row["task_id"], "status": row["status"],
                      "risk": _risk_of(row["action"])}
            if kind == "approval_decided":
                detail["decided_by"] = row["decided_by"]
                if row["decided_at"] and row["created_at"]:
                    detail["latency_s"] = round(row["decided_at"] - row["created_at"], 1)
            store.record(
                tid, kind, task_id=row["task_id"] or None,
                actor=actor or "",
                summary="%s %s（%s）" % (kind, row["action"], row["status"]),
                detail=detail,
                content=row["detail"],   # 原文只留指纹，证明"批的到底是什么"
            )
        except Exception:              # noqa: BLE001 —— 旁路纪律
            pass

    def audit(self, approval_id: str) -> dict:
        """一条审批的完整审计链：审批行 + 轨迹里的全部审批事件。

        无轨迹上下文时 trace_id=None、events=[]——如实标注"没挂上 trace"，
        而不是假装没有证据缺口。
        """
        out = {"approval": self._row(approval_id), "trace_id": None, "events": []}
        row = out["approval"]
        store = self._trace_store
        if row is None or store is None:
            return out
        try:
            tid = row["trace_id"] or self._resolve_trace_id(row["task_id"])
            if not tid:
                return out
            out["trace_id"] = tid
            out["events"] = [
                e for e in store.events(tid)
                if e["kind"].startswith("approval")
                and (e.get("detail") or {}).get("approval_id") == approval_id
            ]
        except Exception:              # noqa: BLE001 —— 审计读取失败不抛
            pass
        return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="审批中心 CLI（真人审批入口）")
    ap.add_argument("--db-path", default=DEFAULT_DB_PATH)
    ap.add_argument("--list", action="store_true", help="列出待审批(及最近)")
    ap.add_argument("--approve", metavar="APPROVAL_ID", help="批准指定审批")
    ap.add_argument("--reject", metavar="APPROVAL_ID", help="拒绝指定审批")
    ap.add_argument("--by", default="human", help="决策人标识")
    ap.add_argument("--audit", metavar="APPROVAL_ID",
                    help="查看某审批的完整审计链（审批行 + 轨迹事件）")
    ap.add_argument("--sweep", action="store_true",
                    help="过期清理：挂起超时的审批标记 expired")
    ap.add_argument("--max-age", type=float, default=86400,
                    help="--sweep 的挂起超时阈值（秒，默认 86400=1 天）")
    args = ap.parse_args(argv)

    ac = ApprovalCenter(db_path=args.db_path)
    try:
        if args.approve:
            ok = ac.decide(args.approve, "approve", by=args.by)
            print("批准" if ok else "失败(不存在或已决策):", args.approve)
        elif args.reject:
            ok = ac.decide(args.reject, "reject", by=args.by)
            print("拒绝" if ok else "失败(不存在或已决策):", args.reject)
        elif args.audit:
            chain = ac.audit(args.audit)
            row = chain["approval"]
            if row is None:
                print("未找到审批:", args.audit)
                return 1
            print("%s  [%s]  %s  %s" % (row["approval_id"], row["status"],
                                        row["action"], row["detail"]))
            print("  task=%s  by=%s  decided_at=%s"
                  % (row["task_id"] or "-", row["decided_by"] or "-",
                     row["decided_at"] or "-"))
            print("  trace_id=%s  审计事件 %d 条:"
                  % (chain["trace_id"] or "（无轨迹上下文）", len(chain["events"])))
            for e in chain["events"]:
                print("    #%s %s  %s  actor=%s"
                      % (e["seq"], e["kind"], e["summary"], e["actor"] or "-"))
            return 0
        elif args.sweep:
            ids = ac.sweep_expired(max_age=args.max_age)
            print("过期清理：%d 条挂起超 %gs 的审批已标记 expired" % (len(ids), args.max_age))
            for aid in ids:
                print("  ", aid)
        else:
            rows = ac.list_approvals(limit=50)
            for r in rows:
                print(f"{r['approval_id']}  [{r['status']}]  {r['action']}  {r['detail']}  "
                      f"(task={r['task_id']}, by={r['decided_by']})")
    finally:
        ac.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
