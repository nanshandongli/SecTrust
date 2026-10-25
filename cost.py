"""
cost.py —— 成本打点层（Cost Layer）

在 Trace 之上叠加**可归因的成本账本**：按任务、按 AI 角色、按工具
记录四层成本，任务结束时落一条快照，看板按天/模板/角色聚合，
直接回答"哪条流水线吞金、哪些任务反复打回、哪些工具调用异常"。

四层（用户 2026-10-25 规格）：
  1. 模型成本   —— 输入/输出 Token、模型调用次数（layer="model"）
  2. 工具成本   —— 浏览器/语音/文件/外部 API 调用次数（layer="tool"）
  3. 重试成本   —— 失败重试、质检打回、死信（layer="retry"）
  4. 基础设施   —— 沙箱秒数、Redis、存储、日志（layer="infra"）

设计纪律（改动不可破坏）：
1. **任务快照 schema 与用户给定一致**：cost_records 一行一个任务，
   字段 = trace_id/task_id/model_calls/input_tokens/output_tokens/
   tool_calls/retries/sandbox_seconds/cost_estimate（另加归因列
   role/template/tier/outcome 与诚实列 unpriced_calls，均为附加，不替换）。
2. **计价诚实**：内置价格表只是公开挂牌价的静态快照（2026-10），
   可用 COST_PRICE_TABLE（JSON）或 prices= 覆盖；**未知模型不计价**——
   cost 记 0、unpriced_calls +1，绝不编一个数字糊墙。Mock/Ollama
   本地调用显式按 0 计（真免费）。
3. **打点是旁路不是主链路**：各挂点（DagScheduler/QualityGate/
   BrowserSkill）的 cost 记录全部 best-effort，坏了绝不挡任务完成；
   但 CostStore 自身的错误如实抛给直接调用者（与 TraceStore 同语义）。
4. **快照幂等**：finalize_task 从 cost_events 重聚合后 upsert，
   重复调用结果一致（不会把一次完成记成两倍）；record_task 是
   调用方直接写快照（用户 schema 原样落库），两者都走 upsert。
5. **线程本地连接 + 显式 close()**：`with sqlite3.connect()` 是事务
   上下文不是关闭，会泄漏连接（ResourceWarning）。连接登记在册，
   close() 一并收掉**所有线程**的连接；回归测试在
   `-W error::ResourceWarning` 下必须零泄漏。
6. **写自愈（_write）**：Windows 多线程同时首开同一个新库时连接可能
   "用着用着变坏"（建连探针挡不住晚发的竞争窗口）。写操作撞
   OperationalError → 丢弃连接重建 → 重试一次；SQLite 单语句失败
   即无副作用，重跑不会重复记账（与 task_registry 同纪律）。
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import threading
import time
from typing import Any, Optional

from trace import redact  # 复用脱敏实现（敏感键只留指纹，一套逻辑）

DEFAULT_DB_PATH = os.path.join("data", "cost.db")

# 四层成本（细粒度事件的 layer 取值）
LAYERS = ("model", "tool", "retry", "infra")

MAX_DETAIL_BYTES = 4096      # detail 序列化后的体积上限（成本事件从简）
TRUNC_KEEP = 500             # 截断时保留的头部字符数

# ----------------------------------------------------------------------
# 价格表（USD / 1K tokens，公开挂牌价静态快照 2026-10）
#
# 只是**量级估算**：真实计费以各平台计费页为准（阶梯价/缓存命中/
# 批量折扣都不在此表内）。前缀匹配（长前缀优先），如 "gpt-4o-2024-08-06"
# 命中 "gpt-4o"。未知模型 → 不计价（unpriced），不猜。
# 覆盖方式：CostStore(prices={...}) 或环境变量 COST_PRICE_TABLE=JSON。
# ----------------------------------------------------------------------
PRICE_PER_1K = {
    # OpenAI（input, output）
    "gpt-4o-mini": (0.00015, 0.0006),
    "gpt-4o": (0.0025, 0.01),
    "gpt-4.1-mini": (0.0004, 0.0016),
    "gpt-4.1": (0.002, 0.008),
    "o4-mini": (0.0011, 0.0044),
    # Anthropic
    "claude-haiku-4": (0.001, 0.005),
    "claude-3-5-haiku": (0.0008, 0.004),
    "claude-sonnet-4": (0.003, 0.015),
    "claude-3-5-sonnet": (0.003, 0.015),
    "claude-opus-4": (0.015, 0.075),
    # DeepSeek
    "deepseek-chat": (0.00027, 0.0011),
    "deepseek-reasoner": (0.00055, 0.0022),
    # 阿里通义
    "qwen-max": (0.0016, 0.0064),
    "qwen-plus": (0.0004, 0.0012),
    "qwen-turbo": (0.0003, 0.0009),
    # 智谱 GLM
    "glm-4-plus": (0.0007, 0.0007),
    "glm-4": (0.0007, 0.0007),
    # 本地/免费（显式 0：真免费，不是"不知道"）
    "mock": (0.0, 0.0),
    "ollama": (0.0, 0.0),
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cost_events (
    event_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            REAL NOT NULL,
    trace_id      TEXT,
    task_id       TEXT,
    layer         TEXT NOT NULL,
    calls         INTEGER NOT NULL DEFAULT 0,
    input_tokens  INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    seconds       REAL NOT NULL DEFAULT 0,
    cost          REAL NOT NULL DEFAULT 0,
    priced        INTEGER NOT NULL DEFAULT 1,
    model         TEXT NOT NULL DEFAULT '',
    label         TEXT NOT NULL DEFAULT '',
    actor         TEXT NOT NULL DEFAULT '',
    detail        TEXT
);
CREATE INDEX IF NOT EXISTS idx_cost_events_task ON cost_events(task_id);
CREATE INDEX IF NOT EXISTS idx_cost_events_trace ON cost_events(trace_id);
CREATE INDEX IF NOT EXISTS idx_cost_events_layer ON cost_events(layer);

CREATE TABLE IF NOT EXISTS cost_records (
    task_id        TEXT PRIMARY KEY,
    trace_id       TEXT,
    role           TEXT NOT NULL DEFAULT '',
    template       TEXT NOT NULL DEFAULT '',
    tier           TEXT NOT NULL DEFAULT '',
    model_calls    INTEGER NOT NULL DEFAULT 0,
    input_tokens   INTEGER NOT NULL DEFAULT 0,
    output_tokens  INTEGER NOT NULL DEFAULT 0,
    tool_calls     INTEGER NOT NULL DEFAULT 0,
    retries        INTEGER NOT NULL DEFAULT 0,
    sandbox_seconds REAL NOT NULL DEFAULT 0,
    cost_estimate  REAL NOT NULL DEFAULT 0,
    unpriced_calls INTEGER NOT NULL DEFAULT 0,
    outcome        TEXT NOT NULL DEFAULT '',
    updated_at     REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cost_records_trace ON cost_records(trace_id);
"""


class CostError(Exception):
    """cost 层自身错误（库损坏/参数非法）。挂点处 best-effort 不掩蔽真错误。"""


def _canonical(obj: Any) -> str:
    """规范化 JSON：键排序、无空白、保留中文。"""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), default=str)


def _bounded(detail: Any) -> Any:
    """detail 脱敏 + 有界化（超限截断留头 + 全文长度）。"""
    if detail is None:
        return None
    try:
        red = redact(detail)
    except Exception:  # noqa: BLE001 —— 脱敏失败按文本处理，不丢事件
        red = str(detail)
    text = _canonical(red)
    if len(text.encode("utf-8")) <= MAX_DETAIL_BYTES:
        return red
    return {"truncated": True, "bytes": len(text.encode("utf-8")),
            "head": text[:TRUNC_KEEP]}


def usage_tokens(usage: Any) -> tuple:
    """从模型返回的 usage 提取 (input_tokens, output_tokens)。

    兼容两种键风格：OpenAI/Ollama 的 prompt_tokens/completion_tokens
    与 Anthropic 的 input_tokens/output_tokens。缺 key（如 Mock）
    → (0, 0)：调用发生过，只是没用量数据，不猜。
    """
    if not isinstance(usage, dict):
        return 0, 0
    inp = usage.get("prompt_tokens")
    if inp is None:
        inp = usage.get("input_tokens")
    out = usage.get("completion_tokens")
    if out is None:
        out = usage.get("output_tokens")
    try:
        return int(inp or 0), int(out or 0)
    except (TypeError, ValueError):
        return 0, 0


def _match_price(model: str, prices: dict) -> Optional[tuple]:
    """长前缀优先匹配价格；命中返回 (in, out)，未命中 None。"""
    if not model:
        return None
    best = None
    for prefix, pair in prices.items():
        if model.startswith(prefix):
            if best is None or len(prefix) > len(best[0]):
                best = (prefix, pair)
    return best[1] if best else None


def estimate_cost(model: str, input_tokens: int, output_tokens: int,
                  prices: dict = None) -> tuple:
    """估算单次模型调用成本。返回 (cost, priced)。

    priced=False 表示"有调用但无法计价"（未知模型）——调用方应把
    它计入 unpriced_calls，而不是把 cost 当 0 元。
    """
    table = prices if prices is not None else PRICE_PER_1K
    pair = _match_price(model, table)
    if pair is None:
        return 0.0, False
    pin, pout = pair
    return (int(input_tokens or 0) / 1000.0 * pin
            + int(output_tokens or 0) / 1000.0 * pout), True


class CostStore:
    """成本账本：线程本地连接 + 显式 close()（与 TraceStore 同纪律）。"""

    def __init__(self, db_path: str = None, prices: dict = None):
        self.db_path = db_path or os.environ.get("COST_DB_PATH") or DEFAULT_DB_PATH
        if prices is not None:
            self.prices = dict(prices)
        else:
            env = os.environ.get("COST_PRICE_TABLE")
            if env:
                try:
                    self.prices = {k: tuple(v) for k, v in json.loads(env).items()}
                except Exception as e:  # noqa: BLE001 —— 环境变量写错不静默
                    raise CostError(f"COST_PRICE_TABLE 不是合法 JSON: {e}")
            else:
                self.prices = dict(PRICE_PER_1K)
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
            raise CostError("CostStore 已关闭")
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
        raise CostError(f"无法打开 cost 库 {self.db_path}: {last_err}")

    def close(self):
        """关闭**所有线程**的连接（幂等）。理由同 TraceStore.close。"""
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

    # ------------------------------------------------------------------
    # 写自愈：_write（Windows 半残连接对策，与 task_registry 同纪律）
    # ------------------------------------------------------------------
    def _write(self, fn):
        """带自愈的写：sqlite3.OperationalError → 丢弃连接重建 → 重试一次。

        Windows 上多线程同时首开同一个新库时，连接可能"用着用着变坏"
        （建连探针挡不住这种晚发的竞争窗口）。SQLite 单语句执行失败
        即无副作用（语句原子），重建后重跑同一语句不会重复记账——
        所以这里不需要调用方保证幂等，只需要写是单语句的。
        """
        try:
            return fn(self._conn())
        except sqlite3.OperationalError:
            # 丢弃当前线程连接，强制下次 _conn() 重建
            conn = getattr(self._local, "conn", None)
            if conn is not None:
                with self._lock:
                    self._all_conns.discard(conn)
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass
            self._local.conn = None
            return fn(self._conn())

    # ------------------------------------------------------------------
    # 写：四层细粒度事件
    # ------------------------------------------------------------------
    def _record_event(self, layer: str, task_id: str = None,
                      trace_id: str = None, calls: int = 0,
                      input_tokens: int = 0, output_tokens: int = 0,
                      seconds: float = 0.0, cost: float = 0.0,
                      priced: int = 1, model: str = "", label: str = "",
                      actor: str = "", detail: Any = None) -> int:
        if layer not in LAYERS:
            raise CostError(f"未知成本层: {layer}（可选 {LAYERS}）")
        bounded = _bounded(detail)
        detail_json = _canonical(bounded) if bounded is not None else None
        row = (time.time(), trace_id, task_id, layer, int(calls or 0),
               int(input_tokens or 0), int(output_tokens or 0),
               float(seconds or 0.0), float(cost or 0.0), int(priced),
               model or "", (label or "")[:300], actor or "", detail_json)

        def _do(conn):
            cur = conn.execute(
                "INSERT INTO cost_events(ts, trace_id, task_id, layer, calls,"
                " input_tokens, output_tokens, seconds, cost, priced, model,"
                " label, actor, detail) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                row)
            conn.commit()
            return int(cur.lastrowid)

        return self._write(_do)

    def record_model_call(self, task_id: str = None, trace_id: str = None,
                          model: str = "", input_tokens: int = 0,
                          output_tokens: int = 0, rounds: int = 0,
                          actor: str = "", detail: Any = None) -> int:
        """模型成本：一次模型调用。tokens 缺（Mock）时记 0 但 calls 记 1。"""
        cost, priced = estimate_cost(model, input_tokens, output_tokens,
                                     self.prices)
        d = dict(detail) if isinstance(detail, dict) else {}
        if rounds:
            d["rounds"] = int(rounds)
        if not priced:
            d["unpriced_model"] = model or "(未署名)"
        return self._record_event(
            "model", task_id=task_id, trace_id=trace_id, calls=1,
            input_tokens=input_tokens, output_tokens=output_tokens,
            cost=cost, priced=1 if priced else 0, model=model or "",
            actor=actor, detail=d or None)

    def record_tool_call(self, task_id: str = None, trace_id: str = None,
                         tool: str = "", actor: str = "",
                         detail: Any = None) -> int:
        """工具成本：一次工具调用（浏览器/语音/文件/外部 API）。"""
        return self._record_event(
            "tool", task_id=task_id, trace_id=trace_id, calls=1,
            label=tool, actor=actor, detail=detail)

    def record_retry(self, task_id: str = None, trace_id: str = None,
                     reason: str = "", actor: str = "") -> int:
        """重试成本：一次失败重试/质检打回/死信。"""
        return self._record_event(
            "retry", task_id=task_id, trace_id=trace_id, calls=1,
            label=str(reason or "")[:300], actor=actor)

    def record_infra(self, task_id: str = None, trace_id: str = None,
                     kind: str = "sandbox", seconds: float = 0.0,
                     cost: float = 0.0, actor: str = "",
                     detail: Any = None) -> int:
        """基础设施成本：沙箱秒数/Redis/存储/日志。

        cost 一般留 0——本地资源没有诚实单价；调用方确知单价
        （如云 Redis 按小时）时才显式传入。
        """
        return self._record_event(
            "infra", task_id=task_id, trace_id=trace_id,
            seconds=seconds, cost=cost, label=kind, actor=actor,
            detail=detail)

    # ------------------------------------------------------------------
    # 写：任务级快照（用户 schema）
    # ------------------------------------------------------------------
    _SNAPSHOT_FIELDS = ("trace_id", "role", "template", "tier",
                        "model_calls", "input_tokens", "output_tokens",
                        "tool_calls", "retries", "sandbox_seconds",
                        "cost_estimate", "unpriced_calls", "outcome")

    def record_task(self, snapshot: dict) -> str:
        """直接写一条任务成本快照（调用方在任务结束时握有全部数字）。

        snapshot 至少含 task_id；其余字段缺省不动（upsert 语义）。
        这就是用户给定 schema 的落库入口。
        """
        if not isinstance(snapshot, dict) or not snapshot.get("task_id"):
            raise CostError("record_task 需要 snapshot['task_id']")
        # 下面这行的字段清单常量是个 21 字符的普通类属性名，高熵兜底探测器会把它
        # 当"无前缀凭证"（熵 4.07 刚过线），属代码标识符误报，行内抑制
        vals = {k: snapshot[k] for k in self._SNAPSHOT_FIELDS  # scanner:allow
                if k in snapshot and snapshot[k] is not None}
        cols = ["task_id"] + list(vals.keys()) + ["updated_at"]
        row = [snapshot["task_id"]] + [vals[k] for k in vals] + [time.time()]
        placeholders = ",".join("?" for _ in cols)
        updates = ",".join(f"{c}=excluded.{c}" for c in cols if c != "task_id")
        sql = (f"INSERT INTO cost_records({','.join(cols)})"
               f" VALUES ({placeholders})"
               f" ON CONFLICT(task_id) DO UPDATE SET {updates}")

        def _do(conn):
            conn.execute(sql, row)
            conn.commit()
            return snapshot["task_id"]

        return self._write(_do)

    def finalize_task(self, task_id: str, trace_id: str = None,
                      role: str = "", template: str = "", tier: str = "",
                      outcome: str = "") -> dict:
        """任务结束：从 cost_events 重聚合出快照并 upsert（幂等）。

        重复调用结果一致——聚合自事件，不叠加旧值。归因列
        （role/template/tier/outcome）显式传入才更新，避免把
        "未标注"覆盖成已知信息。
        """
        conn = self._conn()
        row = conn.execute(
            "SELECT"
            " COALESCE(SUM(CASE WHEN layer='model' THEN calls ELSE 0 END),0) AS mc,"
            " COALESCE(SUM(CASE WHEN layer='model' THEN input_tokens ELSE 0 END),0) AS it,"
            " COALESCE(SUM(CASE WHEN layer='model' THEN output_tokens ELSE 0 END),0) AS ot,"
            " COALESCE(SUM(CASE WHEN layer='tool' THEN calls ELSE 0 END),0) AS tc,"
            " COALESCE(SUM(CASE WHEN layer='retry' THEN calls ELSE 0 END),0) AS rt,"
            " COALESCE(SUM(seconds),0) AS sec,"
            " COALESCE(SUM(cost),0) AS cost,"
            " COALESCE(SUM(CASE WHEN priced=0 THEN calls ELSE 0 END),0) AS up"
            " FROM cost_events WHERE task_id=?",
            (task_id,)).fetchone()
        snap = {
            "task_id": task_id,
            "trace_id": trace_id,
            "model_calls": int(row["mc"]), "input_tokens": int(row["it"]),
            "output_tokens": int(row["ot"]), "tool_calls": int(row["tc"]),
            "retries": int(row["rt"]), "sandbox_seconds": round(float(row["sec"]), 3),
            "cost_estimate": round(float(row["cost"]), 6),
            "unpriced_calls": int(row["up"]),
        }
        if role:
            snap["role"] = role
        if template:
            snap["template"] = template
        if tier:
            snap["tier"] = tier
        if outcome:
            snap["outcome"] = outcome
        self.record_task(snap)
        return snap

    # ------------------------------------------------------------------
    # 读
    # ------------------------------------------------------------------
    def task_cost(self, task_id: str) -> Optional[dict]:
        """任务的成本视图：快照（可能还没收尾 → None）+ 细粒度事件。

        未收尾但有事件时附带 "aggregate"（当场聚合、不落库）——
        任务跑到一半也能问"目前花了多少"。完全没有事件才返回 None。
        """
        conn = self._conn()
        row = conn.execute("SELECT * FROM cost_records WHERE task_id=?",
                           (task_id,)).fetchone()
        events = conn.execute(
            "SELECT trace_id, layer, calls, input_tokens, output_tokens,"
            " seconds, cost, priced, model, label, actor, ts, detail"
            " FROM cost_events WHERE task_id=? ORDER BY event_id ASC",
            (task_id,)).fetchall()
        if not row and not events:
            return None
        out = {"task_id": task_id, "record": dict(row) if row else None,
               "events": [dict(e) for e in events]}
        if not row and events:
            agg = conn.execute(
                "SELECT COALESCE(SUM(calls),0) AS calls,"
                " COALESCE(SUM(input_tokens),0) AS it,"
                " COALESCE(SUM(output_tokens),0) AS ot,"
                " COALESCE(SUM(seconds),0) AS sec,"
                " COALESCE(SUM(cost),0) AS cost"
                " FROM cost_events WHERE task_id=?", (task_id,)).fetchone()
            out["aggregate"] = {"model_calls": int(agg["calls"]),
                                "input_tokens": int(agg["it"]),
                                "output_tokens": int(agg["ot"]),
                                "sandbox_seconds": round(float(agg["sec"]), 3),
                                "cost_estimate": round(float(agg["cost"]), 6),
                                "note": "尚未 finalize，以上为实时聚合（未落库）"}
        return out

    def trace_cost(self, trace_id: str) -> Optional[dict]:
        """按轨迹聚合（一条流水线花了多少）。"""
        conn = self._conn()
        row = conn.execute(
            "SELECT"
            " COALESCE(SUM(calls),0) AS calls,"
            " COALESCE(SUM(input_tokens),0) AS it,"
            " COALESCE(SUM(output_tokens),0) AS ot,"
            " COALESCE(SUM(seconds),0) AS sec,"
            " COALESCE(SUM(cost),0) AS cost"
            " FROM cost_events WHERE trace_id=?",
            (trace_id,)).fetchone()
        n = conn.execute("SELECT COUNT(*) AS c FROM cost_events WHERE trace_id=?",
                         (trace_id,)).fetchone()["c"]
        if not n:
            return None
        return {"trace_id": trace_id, "events": int(n),
                "calls": int(row["calls"]), "input_tokens": int(row["it"]),
                "output_tokens": int(row["ot"]),
                "sandbox_seconds": round(float(row["sec"]), 3),
                "cost_estimate": round(float(row["cost"]), 6)}

    # ------------------------------------------------------------------
    # 看板：按天 / 按模板 / 按角色 + 异常榜
    # ------------------------------------------------------------------
    def dashboard(self, days: int = 30, top: int = 5) -> dict:
        """聚合看板。回答：哪条流水线吞金、哪些任务反复打回、哪些工具异常。"""
        conn = self._conn()
        since = time.time() - max(1, int(days)) * 86400

        def _group(sql, key):
            return [{"key": r[0] if r[0] else "(未标注)",
                     "tasks": r[1], "model_calls": r[2],
                     "tokens": r[3] + r[4], "tool_calls": r[5],
                     "retries": r[6], "cost_estimate": round(r[7], 6)}
                    for r in conn.execute(sql, (since,)).fetchall()]

        by_day = _group(
            "SELECT date(updated_at,'unixepoch') AS k, COUNT(*),"
            " SUM(model_calls), SUM(input_tokens), SUM(output_tokens),"
            " SUM(tool_calls), SUM(retries), SUM(cost_estimate)"
            " FROM cost_records WHERE updated_at>=?"
            " GROUP BY k ORDER BY k ASC", "day")
        by_template = _group(
            "SELECT template AS k, COUNT(*),"
            " SUM(model_calls), SUM(input_tokens), SUM(output_tokens),"
            " SUM(tool_calls), SUM(retries), SUM(cost_estimate)"
            " FROM cost_records WHERE updated_at>=?"
            " GROUP BY k ORDER BY 8 DESC", "template")
        by_role = _group(
            "SELECT role AS k, COUNT(*),"
            " SUM(model_calls), SUM(input_tokens), SUM(output_tokens),"
            " SUM(tool_calls), SUM(retries), SUM(cost_estimate)"
            " FROM cost_records WHERE updated_at>=?"
            " GROUP BY k ORDER BY 8 DESC", "role")
        totals = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(model_calls),0),"
            " COALESCE(SUM(input_tokens+output_tokens),0),"
            " COALESCE(SUM(tool_calls),0), COALESCE(SUM(retries),0),"
            " COALESCE(SUM(cost_estimate),0), COALESCE(SUM(unpriced_calls),0)"
            " FROM cost_records WHERE updated_at>=?", (since,)).fetchone()
        outliers = {
            "top_cost": [dict(r) for r in conn.execute(
                "SELECT task_id, role, template, cost_estimate, retries,"
                " tool_calls FROM cost_records WHERE updated_at>=?"
                " ORDER BY cost_estimate DESC LIMIT ?", (since, top)).fetchall()],
            "top_retries": [dict(r) for r in conn.execute(
                "SELECT task_id, role, template, retries, cost_estimate"
                " FROM cost_records WHERE updated_at>=?"
                " ORDER BY retries DESC, cost_estimate DESC LIMIT ?",
                (since, top)).fetchall()],
            "top_tool_calls": [dict(r) for r in conn.execute(
                "SELECT task_id, role, template, tool_calls, cost_estimate"
                " FROM cost_records WHERE updated_at>=?"
                " ORDER BY tool_calls DESC, cost_estimate DESC LIMIT ?",
                (since, top)).fetchall()],
        }
        return {
            "days": int(days), "by_day": by_day, "by_template": by_template,
            "by_role": by_role, "outliers": outliers,
            "totals": {"tasks": totals[0], "model_calls": totals[1],
                       "tokens": totals[2], "tool_calls": totals[3],
                       "retries": totals[4], "cost_estimate": round(totals[5], 6),
                       "unpriced_calls": totals[6]},
            "price_note": "cost_estimate 为公开挂牌价静态快照的量级估算"
                          "（2026-10），非账单；unpriced_calls>0 表示有调用"
                          "无法计价（未知模型）",
        }

    def stats(self) -> dict:
        """全局统计：事件数/层分布/快照数/可计价性。"""
        conn = self._conn()
        events = conn.execute("SELECT COUNT(*) AS c FROM cost_events").fetchone()["c"]
        by_layer = dict(conn.execute(
            "SELECT layer, COUNT(*) FROM cost_events GROUP BY layer").fetchall())
        records = conn.execute("SELECT COUNT(*) AS c FROM cost_records").fetchone()["c"]
        unpriced = conn.execute(
            "SELECT COUNT(*) AS c FROM cost_events WHERE priced=0").fetchone()["c"]
        return {"events": events, "by_layer": by_layer, "records": records,
                "unpriced_events": unpriced}


# ----------------------------------------------------------------------
# 模型调用包一层：tracked_complete（鸭子类型，不 import models）
# ----------------------------------------------------------------------
def tracked_complete(cost, provider, messages, model: str = None,
                     task_id: str = None, trace_id: str = None,
                     actor: str = "", **opts):
    """包一层 provider.complete()：返回原结果，顺手按 usage 记账。

    cost 为 None 时完全透传（不打点不挡调用）；打点本身失败也静默
    （旁路纪律）——模型调用永远不该被账本拖死。
    """
    result = provider.complete(messages, model=model, **opts)
    if cost is None:
        return result
    try:
        usage = getattr(result, "usage", None) or {}
        it, ot = usage_tokens(usage)
        cost.record_model_call(
            task_id=task_id, trace_id=trace_id,
            model=getattr(result, "model", None) or model or "",
            input_tokens=it, output_tokens=ot, actor=actor)
    except Exception:  # noqa: BLE001 —— 打点是旁路
        pass
    return result


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------
def main(argv=None):
    p = argparse.ArgumentParser(description="cost 成本打点层 CLI")
    p.add_argument("--db-path", default=None)
    p.add_argument("--model-call", action="store_true", help="记一次模型调用")
    p.add_argument("--tool-call", action="store_true", help="记一次工具调用")
    p.add_argument("--retry", action="store_true", help="记一次重试/打回")
    p.add_argument("--infra", action="store_true", help="记一段基础设施")
    p.add_argument("--task", help="task_id")
    p.add_argument("--trace", help="trace_id")
    p.add_argument("--model", default="", help="模型名（--model-call）")
    p.add_argument("--tool", default="", help="工具名（--tool-call）")
    p.add_argument("--reason", default="", help="重试原因（--retry）")
    p.add_argument("--kind", default="sandbox", help="设施类型（--infra）")
    p.add_argument("--in-tokens", type=int, default=0)
    p.add_argument("--out-tokens", type=int, default=0)
    p.add_argument("--seconds", type=float, default=0.0)
    p.add_argument("--actor", default="")
    p.add_argument("--snapshot", default=None,
                   help="--task-snapshot：用户 schema 的 JSON 字符串")
    p.add_argument("--finalize", action="store_true",
                   help="从事件聚合任务快照（幂等）")
    p.add_argument("--role", default="")
    p.add_argument("--template", default="")
    p.add_argument("--tier", default="")
    p.add_argument("--outcome", default="")
    p.add_argument("--show", action="store_true", help="看一个任务的成本")
    p.add_argument("--trace-cost", help="按轨迹聚合")
    p.add_argument("--dashboard", action="store_true")
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--stats", action="store_true")
    a = p.parse_args(argv)

    store = CostStore(a.db_path)
    try:
        if a.model_call:
            eid = store.record_model_call(
                task_id=a.task, trace_id=a.trace, model=a.model,
                input_tokens=a.in_tokens, output_tokens=a.out_tokens,
                actor=a.actor)
            print(json.dumps({"event_id": eid, "layer": "model"},
                             ensure_ascii=False))
        elif a.tool_call:
            eid = store.record_tool_call(task_id=a.task, trace_id=a.trace,
                                         tool=a.tool, actor=a.actor)
            print(json.dumps({"event_id": eid, "layer": "tool"},
                             ensure_ascii=False))
        elif a.retry:
            eid = store.record_retry(task_id=a.task, trace_id=a.trace,
                                     reason=a.reason, actor=a.actor)
            print(json.dumps({"event_id": eid, "layer": "retry"},
                             ensure_ascii=False))
        elif a.infra:
            eid = store.record_infra(task_id=a.task, trace_id=a.trace,
                                     kind=a.kind, seconds=a.seconds,
                                     actor=a.actor)
            print(json.dumps({"event_id": eid, "layer": "infra"},
                             ensure_ascii=False))
        elif a.snapshot:
            tid = store.record_task(json.loads(a.snapshot))
            print(json.dumps({"task_id": tid, "written": True},
                             ensure_ascii=False))
        elif a.finalize:
            snap = store.finalize_task(a.task, trace_id=a.trace, role=a.role,
                                       template=a.template, tier=a.tier,
                                       outcome=a.outcome)
            print(json.dumps(snap, ensure_ascii=False, indent=2))
        elif a.show:
            rec = store.task_cost(a.task)
            print(json.dumps(rec if rec else {"task_id": a.task, "found": False},
                             ensure_ascii=False, indent=2))
        elif a.trace_cost:
            print(json.dumps(store.trace_cost(a.trace_cost),
                             ensure_ascii=False, indent=2))
        elif a.dashboard:
            print(json.dumps(store.dashboard(days=a.days),
                             ensure_ascii=False, indent=2))
        elif a.stats:
            print(json.dumps(store.stats(), ensure_ascii=False, indent=2))
        else:
            p.print_help()
    finally:
        store.close()


if __name__ == "__main__":
    main()
