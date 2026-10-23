"""投票/辩论协同模式：同一任务多方案并行的登记与择优。

与 Arbiter 的分工（两者都默认不自动生效，但裁的东西不同）：
- **Arbiter** 裁"同一事实的不同主张"（如两个 agent 算出不同的月活数）→ 冲突裁决
- **Ballot** 排"同一任务的多个完整方案"（如三个 agent 各写一份营销方案）→ 择优推荐

设计纪律（改动时不可破坏）：
1. **默认不自动生效**：`rank()` 只产出推荐，生效必须走 `confirm()`。
   与 Arbiter 的"默认人工裁"同源——择优错误的代价由人类承担，不由系统猜。
2. **没有判据就不排序**：未配 quality rules 时 `score=None`，`rank()` 直接
   `needs_human`。没有验收标准却要"择优"，那是拍脑袋，不是工程。
3. **策略白名单**：不在 `BALLOT_STRATEGIES` 里的 strategy 一律按 human 处理。
4. **判据复用不重造**：打分直接调用 `agentskills.quality` 的同一套规则核验
   （planner 写入什么规则，这里就按什么规则打分——谁定义谁执行）。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

try:  # Broker 可选：没有配 MQ_DB_PATH 时静默不建事件总线
    from transport.queue import Broker
except Exception:  # noqa: BLE001
    Broker = None

DEFAULT_DB = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "data", "ballot.db"
)

# 策略白名单：不在这张表里的一律按 human（防静默乱裁，与 arbiter.AUTO_STRATEGIES 同思路）
BALLOT_STRATEGIES = ("quality_rules", "human")

# 每条质量违规扣的分（可解释：扣分完全来自 violations，无隐藏权重）
VIOLATION_PENALTY = 10

# top1 与 top2 分差小于该值时视为"分不出高下"→ 转人工
TIE_THRESHOLD = 5.0


class BallotError(ValueError):
    pass


class Ballot:
    """投票组登记与择优。所有写入审计可回放；推荐默认不自动生效。"""

    def __init__(self, db_path: str = DEFAULT_DB, broker: Optional[Any] = None,
                 max_proposals: int = 8):
        self.db_path = db_path
        self.max_proposals = max_proposals
        self.broker = broker if broker is not None else self._default_broker()
        self._local = threading.local()
        d = os.path.dirname(db_path)
        if d:
            os.makedirs(d, exist_ok=True)
        self._ensure_schema()
        # 复用质量判据：内存库实例，零文件副作用（check_output 是纯函数式方法）
        from agentskills.quality import QualityGate
        self._gate = QualityGate(db_path=":memory:")

    def close(self):
        """关闭本线程的连接（`with self._conn()` 是事务上下文，不关连接）。"""
        c = getattr(self._local, "conn", None)
        if c is not None:
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass
            self._local.conn = None

    def _default_broker(self):
        if Broker is None or not os.environ.get("MQ_DB_PATH"):
            return None
        try:
            return Broker()
        except Exception:  # noqa: BLE001
            return None

    def _conn(self):
        # thread-local 缓存：`with conn` 只提供事务语义（commit/rollback），
        # 不关闭连接——不缓存的话每次调用泄漏一个 sqlite3.Connection。
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.db_path, timeout=30)
            c.execute("PRAGMA journal_mode=WAL")
            self._local.conn = c
        return c

    def _ensure_schema(self):
        with self._conn() as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS ballots (
                grp           TEXT PRIMARY KEY,
                title         TEXT NOT NULL DEFAULT '',
                strategy      TEXT NOT NULL DEFAULT 'quality_rules',
                rules         TEXT,               -- JSON：质量判据（planner 写入的那份）
                task_ids      TEXT NOT NULL,      -- JSON：参与投票的 task_id 列表
                status        TEXT NOT NULL DEFAULT 'open',  -- open/ranked/confirmed
                recommended   TEXT,               -- rank 的推荐（**只是推荐，未生效**）
                winner_task   TEXT,               -- 生效的方案 task_id（confirm 后才有）
                    confidence    REAL,
                    needs_human   INTEGER NOT NULL DEFAULT 0,
                    reason        TEXT,
                    opened_at     REAL NOT NULL
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS proposals (
                    proposal_id   TEXT PRIMARY KEY,
                    grp           TEXT NOT NULL,
                    task_id       TEXT NOT NULL,
                    output        TEXT NOT NULL,
                    source        TEXT NOT NULL DEFAULT '',
                    violations    TEXT,               -- JSON：质量违规列表
                    score         REAL,               -- NULL = 无判据，不参与排序
                    submitted_at  REAL NOT NULL,
                    UNIQUE(grp, task_id)
                )
            """)
            c.execute("CREATE INDEX IF NOT EXISTS idx_prop_grp "
                      "ON proposals(grp)")
            # 旧库迁移：recommended 列是后加的（推荐与生效分离）
            cols = {r[1] for r in c.execute("PRAGMA table_info(ballots)").fetchall()}
            if "recommended" not in cols:
                c.execute("ALTER TABLE ballots ADD COLUMN recommended TEXT")

    # ------------------------------------------------------------------
    # 开票 / 提交
    # ------------------------------------------------------------------
    def open_ballot(self, grp: str, task_ids: List[str], rules: dict = None,
                    title: str = "", strategy: str = "quality_rules") -> dict:
        """登记一个投票组。重复开票被拒（幂等保护）。"""
        if not grp or not grp.strip():
            raise BallotError("投票组名不能为空")
        task_ids = list(task_ids or [])
        if len(task_ids) < 2:
            raise BallotError(
                f"投票至少需要 2 个并行方案，当前 {len(task_ids)} 个——"
                "单方案不存在'择优'，请直接走普通任务")
        if len(task_ids) > self.max_proposals:
            raise BallotError(
                f"方案数 {len(task_ids)} 超过上限 {self.max_proposals}——"
                "投票是降幻觉手段，不是规模化手段，成本随方案数线性涨")
        if strategy not in BALLOT_STRATEGIES:
            # 未知策略不猜，按 human 处理（与 arbiter 一致）
            strategy = "human"
        with self._conn() as c:
            exists = c.execute("SELECT 1 FROM ballots WHERE grp=?",
                               (grp,)).fetchone()
            if exists:
                raise BallotError(f"投票组已存在: {grp}（重复开票被拒）")
            c.execute(
                "INSERT INTO ballots(grp, title, strategy, rules, task_ids, "
                "opened_at) VALUES (?,?,?,?,?,?)",
                (grp, title, strategy, json.dumps(rules or {}, ensure_ascii=False),
                 json.dumps(task_ids, ensure_ascii=False), time.time()),
            )
        self._emit("ballot.opened", {"grp": grp, "n": len(task_ids),
                                     "strategy": strategy})
        return self.detail(grp)

    def submit_proposal(self, grp: str, task_id: str, output: str,
                        source: str = "") -> dict:
        """提交一个方案并立即按 rules 打分。

        同一 task_id 重复提交 → 覆盖（允许修订后重投，最后一次为准）。
        未登记的 task_id → 拒（防止往投票组里塞无关产出）。
        """
        b = self.detail(grp)
        if b is None:
            raise BallotError(f"投票组不存在: {grp}")
        if task_id not in b["task_ids"]:
            raise BallotError(
                f"task_id {task_id!r} 不属于投票组 {grp!r}——"
                f"该组只接受 {b['task_ids']}")
        rules = b["rules"] or {}
        if not rules or b["strategy"] == "human":
            # 没有判据：不打分（score=NULL）。没有验收标准却打分是制造虚假精确
            violations, score = [], None
        else:
            v = self._gate.check_output(output, rules)
            violations = list(v.violations)
            score = max(0.0, 100.0 - VIOLATION_PENALTY * len(violations))
        pid = uuid.uuid4().hex[:12]
        with self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO proposals(proposal_id, grp, task_id, "
                "output, source, violations, score, submitted_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (pid, grp, task_id, output or "", source,
                 json.dumps(violations, ensure_ascii=False), score, time.time()),
            )
            c.execute("UPDATE ballots SET status='open' WHERE grp=?", (grp,))
        self._emit("ballot.proposed", {"grp": grp, "task_id": task_id,
                                       "score": score})
        return {"grp": grp, "task_id": task_id, "score": score,
                "violations": violations, "submitted": True}

    # ------------------------------------------------------------------
    # 排序 / 生效
    # ------------------------------------------------------------------
    def rank(self, grp: str) -> dict:
        """对所有方案打分排序，给出推荐。**只推荐，不生效。**

        三种情况直接转人工（needs_human=True）：
        ① 方案未交齐；② 没有质量判据（score 全为 NULL）；
        ③ 前两名分差小于 TIE_THRESHOLD（分不出高下）。
        """
        b = self.detail(grp)
        if b is None:
            raise BallotError(f"投票组不存在: {grp}")
        with self._conn() as c:
            rows = c.execute(
                "SELECT task_id, source, output, violations, score "
                "FROM proposals WHERE grp=? ORDER BY submitted_at",
                (grp,)).fetchall()
        got = {r[0] for r in rows}
        missing = [t for t in b["task_ids"] if t not in got]
        if missing:
            return self._finish(grp, None, None, True,
                                f"方案未交齐，缺 {missing}——等 gather 汇合后再评")

        if b["strategy"] != "quality_rules" or any(r[4] is None for r in rows):
            return self._finish(grp, None, None, True,
                                "没有质量判据或策略为 human——系统不排序，请人工择优")

        ranked = sorted(
            [{"task_id": r[0], "source": r[1], "score": r[4],
              "violations": json.loads(r[3]) if r[3] else [],
              "output_preview": (r[2] or "")[:200]} for r in rows],
            key=lambda x: (-x["score"], x["task_id"]),
        )
        top1, top2 = ranked[0], (ranked[1] if len(ranked) > 1 else None)
        gap = (top1["score"] - top2["score"]) if top2 else 100.0
        if gap < TIE_THRESHOLD:
            return self._finish(
                grp, None, None, True,
                f"前两名分差 {gap:.1f} < {TIE_THRESHOLD}（{top1['task_id']} "
                f"{top1['score']:.0f} vs {top2['task_id']} {top2['score']:.0f}）"
                "——分不出高下，请人工择优")
        conf = round(min(1.0, gap / 100.0), 4)
        reason = (f"{top1['task_id']} 得分 {top1['score']:.0f}，"
                  f"领先次优 {gap:.1f} 分；判据：质量规则"
                  f"（每条违规 -{VIOLATION_PENALTY}）")
        return self._finish(grp, top1["task_id"], conf, False, reason,
                            ranked=ranked)

    def confirm(self, grp: str, task_id: str, by: str = "human",
                reason: str = "") -> dict:
        """人工确认生效。**这是 rank 之后唯一能让方案胜出的路径。**"""
        b = self.detail(grp)
        if b is None:
            raise BallotError(f"投票组不存在: {grp}")
        if task_id not in b["task_ids"]:
            raise BallotError(f"task_id {task_id!r} 不属于投票组 {grp!r}")
        with self._conn() as c:
            has = c.execute("SELECT 1 FROM proposals WHERE grp=? AND task_id=?",
                            (grp, task_id)).fetchone()
        if not has:
            raise BallotError(f"方案未提交，无法确认生效: {task_id}")
        with self._conn() as c:
            c.execute(
                "UPDATE ballots SET status='confirmed', winner_task=?, "
                "needs_human=0, reason=? WHERE grp=?",
                (task_id, reason or f"由 {by} 确认", grp))
        self._emit("ballot.confirmed", {"grp": grp, "task_id": task_id,
                                        "by": by})
        return self.detail(grp)

    # ------------------------------------------------------------------
    # 视图
    # ------------------------------------------------------------------
    def ballots(self, grp: str = None) -> List[dict]:
        # 注意：前 8 列必须与 detail() 的 SELECT 列序一致（_row_to_ballot 依赖它）
        cols = ("grp, title, strategy, status, winner_task, confidence, "
                "needs_human, opened_at, recommended")
        with self._conn() as c:
            if grp:
                rows = c.execute(
                    f"SELECT {cols} FROM ballots WHERE grp=? "
                    "ORDER BY opened_at DESC", (grp,)).fetchall()
            else:
                rows = c.execute(
                    f"SELECT {cols} FROM ballots "
                    "ORDER BY opened_at DESC").fetchall()
        out = []
        for r in rows:
            d = self._row_to_ballot(r)
            d["recommended"] = r[8]
            out.append(d)
        return out

    def detail(self, grp: str) -> Optional[dict]:
        # 注意：前 8 列必须与 ballots() 的 SELECT 列序一致（_row_to_ballot 依赖它）
        with self._conn() as c:
            row = c.execute(
                "SELECT grp, title, strategy, status, winner_task, "
                "confidence, needs_human, opened_at, recommended, rules, "
                "task_ids, reason FROM ballots WHERE grp=?", (grp,)).fetchone()
            props = c.execute(
                "SELECT task_id, source, score, submitted_at FROM proposals "
                "WHERE grp=? ORDER BY score DESC, submitted_at", (grp,)).fetchall()
        if not row:
            return None
        d = self._row_to_ballot(row)
        d["recommended"] = row[8]
        d["rules"] = json.loads(row[9]) if row[9] else {}
        d["task_ids"] = json.loads(row[10]) if row[10] else []
        d["reason"] = row[11]
        d["proposals"] = [
            {"task_id": p[0], "source": p[1], "score": p[2],
             "submitted_at": p[3]} for p in props]
        d["submitted_count"] = len(props)
        return d

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _finish(self, grp: str, recommended: Optional[str], conf: Optional[float],
                needs_human: bool, reason: str, ranked: list = None) -> dict:
        """落一次评选结果。

        **关键边界**：这里只写 `recommended`（推荐），**绝不写 `winner_task`**——
        胜出只有 confirm() 一条路。把推荐写进 winner 会让调用方误以为已生效，
        那是"系统替人拍板"的滑坡。
        """
        with self._conn() as c:
            c.execute(
                "UPDATE ballots SET status=?, recommended=?, confidence=?, "
                "needs_human=?, reason=? WHERE grp=?",
                ("ranked" if not needs_human else "open", recommended, conf,
                 1 if needs_human else 0, reason, grp))
        if not needs_human:
            self._emit("ballot.recommended", {"grp": grp, "recommended": recommended,
                                              "confidence": conf})
        else:
            self._emit("ballot.needs_human", {"grp": grp, "reason": reason})
        d = self.detail(grp)
        if ranked is not None:
            d["ranked"] = ranked
        return d

    @staticmethod
    def _row_to_ballot(r) -> dict:
        return {"grp": r[0], "title": r[1], "strategy": r[2],
                "status": r[3], "winner_task": r[4], "confidence": r[5],
                "needs_human": bool(r[6]), "opened_at": r[7]}

    def _emit(self, event_type: str, data: dict):
        if self.broker is None:
            return
        try:
            self.broker.publish(event_type, data)
        except Exception:  # noqa: BLE001
            pass  # 事件只观战，不进关键路径
