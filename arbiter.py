"""
编排层 · 冲突仲裁（arbiter.py）—— 多 AI 产出的版本裁决。

用户描述里的这一环：

    如果不同 AI 产出的内容有冲突（比如调研数据和测算数据不一致），
    会有专门的协调 AI 核对数据源、给出权威版本，避免内容矛盾。

## 设计原则：默认不自动裁

自动仲裁是**看起来很智能、实际上很危险**的功能——两个 AI 各给一个数，
系统悄悄选一个，人类连发生过冲突都不知道。所以本模块的默认策略是
`human`：登记冲突、陈列各方主张与依据，**等人来裁**。

可选的自动策略（都要显式点名才启用）：
  - `recency`         取最新提交（隐含假设：后跑的知道更多）
  - `source_priority` 按来源权威序（外部权威源 > 内部估算，可配置）
  - `evidence`        有 accepted 证据的一方胜出（接 EvidenceGate 的记录）

人工裁定 `decide()` 会记录谁裁的、选哪版、理由——审计可回放，
和 ApprovalCenter"默认拒绝"是同一种保守哲学。
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from transport.broker import Broker  # 可选事件总线
except Exception:
    Broker = None

DEFAULT_DB = os.path.join("data", "arbiter.db")

# 自动策略白名单：不在这张表里的 strategy 一律按 human 处理（防静默乱裁）
AUTO_STRATEGIES = ("recency", "source_priority", "evidence")

# 默认来源权威序（越靠前越权威；可按项目覆盖）
DEFAULT_SOURCE_PRIORITY = ("official_api", "research", "analytics", "estimate")


class ArbiterError(ValueError):
    pass


class Arbiter:
    """冲突登记与裁决。所有写入审计可回放；默认人工裁定。"""

    def __init__(self, db_path: str = DEFAULT_DB, broker: Broker | None = None,
                 source_priority=None):
        self.db_path = db_path
        self.source_priority = tuple(source_priority or DEFAULT_SOURCE_PRIORITY)
        self.broker = broker if broker is not None else self._default_broker()
        d = os.path.dirname(db_path)
        if d:
            os.makedirs(d, exist_ok=True)
        self._ensure_schema()

    def _default_broker(self):
        if Broker is None or not os.environ.get("MQ_DB_PATH"):
            return None
        try:
            return Broker()
        except Exception:
            return None

    def _conn(self):
        c = sqlite3.connect(self.db_path, timeout=30)
        c.execute("PRAGMA journal_mode=WAL")
        return c

    def _ensure_schema(self):
        with self._conn() as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS positions (
                    position_id  TEXT PRIMARY KEY,
                    task_id      TEXT NOT NULL,
                    ckey         TEXT NOT NULL,     -- 冲突键（如 "月活用户数"）
                    value        TEXT NOT NULL,     -- JSON 序列化的主张值
                    source       TEXT NOT NULL,     -- 主张方（agent/任务/来源类型）
                    note         TEXT,             -- 依据说明
                    evidence_id  TEXT,             -- 可选：EvidenceGate 证据 id
                    submitted_at REAL NOT NULL,
                    superseded   INTEGER NOT NULL DEFAULT 0
                )
            """)
            c.execute("CREATE INDEX IF NOT EXISTS idx_pos_task "
                      "ON positions(task_id, ckey)")
            c.execute("""
                CREATE TABLE IF NOT EXISTS decisions (
                    decision_id  TEXT PRIMARY KEY,
                    task_id      TEXT NOT NULL,
                    ckey         TEXT NOT NULL,
                    chosen_value TEXT NOT NULL,
                    chosen_from  TEXT,             -- 胜出的 position_id 或 'custom'
                    strategy     TEXT NOT NULL,
                    decided_by   TEXT NOT NULL,    -- 'human:xxx' / 'auto:xxx'
                    reason       TEXT,
                    decided_at   REAL NOT NULL
                )
            """)
            c.execute("CREATE INDEX IF NOT EXISTS idx_dec_task "
                      "ON decisions(task_id, ckey)")

    # ------------------------------------------------------------------
    # 登记主张
    # ------------------------------------------------------------------
    def submit(self, task_id: str, key: str, value, source: str,
               note: str = "", evidence_id: str = "") -> str:
        """登记一个冲突键下的一方主张。同一 task_id+key 可多次 submit（多版本）。"""
        if not task_id or not key:
            raise ArbiterError("task_id 与 key 不能为空")
        pid = f"pos-{int(time.time() * 1000):x}-{os.urandom(3).hex()}"
        with self._conn() as c:
            c.execute(
                "INSERT INTO positions VALUES (?,?,?,?,?,?,?,?,?)",
                (pid, task_id, key,
                 json.dumps(value, ensure_ascii=False),
                 source, note, evidence_id, time.time(), 0))
        self._emit("position", {"task_id": task_id, "key": key,
                                "source": source, "position_id": pid})
        return pid

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def positions(self, task_id: str, key: str = None,
                  include_superseded: bool = False) -> list[dict]:
        q = ("SELECT position_id, task_id, ckey, value, source, note, "
             "evidence_id, submitted_at, superseded FROM positions "
             "WHERE task_id=?")
        params = [task_id]
        if key:
            q += " AND ckey=?"
            params.append(key)
        if not include_superseded:
            q += " AND superseded=0"
        q += " ORDER BY submitted_at ASC"
        with self._conn() as c:
            rows = c.execute(q, params).fetchall()
        return [{"position_id": r[0], "task_id": r[1], "key": r[2],
                 "value": json.loads(r[3]), "source": r[4], "note": r[5],
                 "evidence_id": r[6], "submitted_at": r[7],
                 "superseded": bool(r[8])} for r in rows]

    def conflicts(self, task_id: str = None) -> list[dict]:
        """未决冲突：同一 task_id+key 下有 ≥2 个未裁主张且无决定记录。"""
        q = ("SELECT task_id, ckey, COUNT(*) FROM positions "
             "WHERE superseded=0 GROUP BY task_id, ckey HAVING COUNT(*) >= 2")
        params = []
        if task_id:
            q = ("SELECT task_id, ckey, COUNT(*) FROM positions "
                 "WHERE superseded=0 AND task_id=? GROUP BY task_id, ckey "
                 "HAVING COUNT(*) >= 2")
            params.append(task_id)
        with self._conn() as c:
            pairs = c.execute(q, params).fetchall()
        out = []
        for tid, key, n in pairs:
            if self.decision(tid, key):
                continue  # 已裁
            out.append({"task_id": tid, "key": key, "positions": n,
                        "sources": [p["source"] for p in self.positions(tid, key)]})
        return out

    def decision(self, task_id: str, key: str) -> dict | None:
        with self._conn() as c:
            r = c.execute(
                "SELECT decision_id, chosen_value, chosen_from, strategy, "
                "decided_by, reason, decided_at FROM decisions "
                "WHERE task_id=? AND ckey=? ORDER BY decided_at DESC",
                (task_id, key)).fetchone()
        if not r:
            return None
        return {"decision_id": r[0], "value": json.loads(r[1]),
                "chosen_from": r[2], "strategy": r[3], "decided_by": r[4],
                "reason": r[5], "decided_at": r[6]}

    # ------------------------------------------------------------------
    # 裁决
    # ------------------------------------------------------------------
    def resolve(self, task_id: str, key: str,
                strategy: str = "human", **kw) -> dict:
        """按策略裁决。返回决定 + 各方主张快照。

        strategy=human（默认）：不自动裁，返回 pending_human 与全部主张。
        未知/不在白名单的策略一律降级为 human——宁可慢，不可乱。
        """
        poss = self.positions(task_id, key)
        if not poss:
            raise ArbiterError(f"无冲突主张: {task_id}/{key}")
        if len(poss) < 2:
            # 只有一方主张，不存在冲突，直接采纳并记录
            return self.decide(task_id, key, poss[0]["value"],
                               by=f"auto:{strategy}", reason="仅一方主张，无需仲裁",
                               chosen_from=poss[0]["position_id"],
                               strategy="single")

        if strategy not in AUTO_STRATEGIES:
            self._emit("pending", {"task_id": task_id, "key": key,
                                   "strategy": strategy})
            return {"status": "pending_human", "task_id": task_id, "key": key,
                    "strategy": "human",
                    "reason": "默认不自动裁决——请人工 decide()，"
                              "或显式指定策略: " + ", ".join(AUTO_STRATEGIES),
                    "positions": poss}

        if strategy == "recency":
            win = max(poss, key=lambda p: p["submitted_at"])
            why = "最新提交"
        elif strategy == "source_priority":
            def prio(p):
                try:
                    return self.source_priority.index(p["source"])
                except ValueError:
                    return len(self.source_priority)  # 未列名来源排最后
            win = min(poss, key=lambda p: (prio(p), -p["submitted_at"]))
            why = f"来源权威序（{win['source']}）"
        else:  # evidence
            with_ev = [p for p in poss if p["evidence_id"]]
            if not with_ev:
                return {"status": "pending_human", "task_id": task_id,
                        "key": key, "strategy": "evidence",
                        "reason": "没有任何一方带证据，无法按证据裁决",
                        "positions": poss}
            win = max(with_ev, key=lambda p: p["submitted_at"])
            why = "有 accepted 证据且最新"
        return self.decide(task_id, key, win["value"],
                           by=f"auto:{strategy}", reason=why,
                           chosen_from=win["position_id"], strategy=strategy)

    def decide(self, task_id: str, key: str, value, by: str = "human",
               reason: str = "", chosen_from: str = "custom",
               strategy: str = "human") -> dict:
        """人工（或系统显式）裁定。记录谁裁的、选哪版、为什么。"""
        if by.startswith("human") is False and by.startswith("auto") is False:
            by = f"human:{by}"
        did = f"dec-{int(time.time() * 1000):x}-{os.urandom(3).hex()}"
        with self._conn() as c:
            c.execute("INSERT INTO decisions VALUES (?,?,?,?,?,?,?,?,?)",
                      (did, task_id, key,
                       json.dumps(value, ensure_ascii=False),
                       chosen_from, strategy, by, reason, time.time()))
            # 落定后其余主张标记 superseded（审计留痕，不删）
            c.execute("UPDATE positions SET superseded=1 "
                      "WHERE task_id=? AND ckey=? AND superseded=0 "
                      "AND position_id<>?",
                      (task_id, key, chosen_from))
        self._emit("decided", {"task_id": task_id, "key": key,
                               "decided_by": by, "strategy": strategy})
        return {"status": "decided", "decision_id": did, "task_id": task_id,
                "key": key, "value": value, "decided_by": by,
                "reason": reason, "strategy": strategy}

    def report(self, task_id: str = None) -> dict:
        """冲突总览：未决 + 已决（供看板/人类终审）。"""
        return {"pending": self.conflicts(task_id),
                "decided": self._decided_list(task_id)}

    def _decided_list(self, task_id: str = None) -> list[dict]:
        q = ("SELECT task_id, ckey, chosen_value, strategy, decided_by, "
             "reason, decided_at FROM decisions")
        params = []
        if task_id:
            q += " WHERE task_id=?"
            params.append(task_id)
        q += " ORDER BY decided_at DESC LIMIT 200"
        with self._conn() as c:
            rows = c.execute(q, params).fetchall()
        return [{"task_id": r[0], "key": r[1], "value": json.loads(r[2]),
                 "strategy": r[3], "decided_by": r[4], "reason": r[5],
                 "decided_at": r[6]} for r in rows]

    def _emit(self, event_type: str, data: dict):
        if self.broker is None:
            return
        try:
            self.broker.publish_event("arbiter", event_type, data)
        except Exception:
            pass


if __name__ == "__main__":
    import tempfile

    db = os.path.join(tempfile.mkdtemp(prefix="arbiter_demo_"), "a.db")
    a = Arbiter(db)
    # 调研 AI 与测算 AI 对同一指标给出不同值
    a.submit("p1/assembly", "月活用户数", 1200, "research",
             note="来自第三方报告2026Q3")
    a.submit("p1/assembly", "月活用户数", 850, "roi",
             note="按投放转化率反推的估算")
    print("未决冲突:", a.conflicts("p1/assembly"))
    r = a.resolve("p1/assembly", "月活用户数")   # 默认 human
    print("默认裁决:", r["status"], "|", r["reason"][:30], "…")
    r = a.resolve("p1/assembly", "月活用户数", strategy="source_priority")
    print("按来源权威序:", r["value"], "|", r["reason"])
    print("裁定记录:", a.decision("p1/assembly", "月活用户数")["decided_by"])
    print("报告:", {k: len(v) for k, v in a.report("p1/assembly").items()})
