"""
质量回流回路（Quality Gate）—— 产出校验 → 不合格打回重做。

## 它补什么缺口

evidence.py 的 EvidenceGate 解决"**声明完成前必须有可核验证据**"（命令实跑类）。
但它天然管不了另一类失败：命令全绿、证据齐全，**产出物本身却不过关**——
文案里留着 TODO、报告缺摘要、JSON 缺必填字段、该引用上游的地方没引用。

用户描述的多 AI 协同流程里，这是关键一环：

    每个环节完成后会有对应校验 AI 做质量检查，
    有问题直接打回重做，不用等到最终汇总才发现问题。

本模块把这句话做成**状态机回路**：

    running --review(通过)--> completed
           --review(不合格)--> registered（带回 feedback，rework_count+1）
           --重试超限--------> needs_human（升级人工，绝不静默放行）

## 规则（全部可判定，与 planner 的 quality 字段同一格式）

| 规则 | 判据 |
|------|------|
| min_chars / max_chars | 产出长度边界（去空白后） |
| required | 必须包含的全部关键词（缺一个即不合格） |
| forbidden | 不得包含的词（TODO / 待补充 / lorem …） |
| no_unfilled | 不得残留 `<待补:key>` 占位符（planner 槽位没填不许交货） |
| format=json | 必须能 json.loads；json_fields 给出必填键 |
| format=markdown | 至少含一个标题行（# …），防止把纯文本当 Markdown 交货 |
| references | 必须提及每个上游任务（key 或其名称）——强制"站在上游产出上干活" |

## 与 EvidenceGate 的关系（组合而非替代）

    EvidenceGate 问："你凭什么说做完了？"   → 命令、退出码、输出、新鲜度
    QualityGate  问："你做出来的东西合格吗？" → 内容、结构、引用、长度

`review(evidence=...)` 可同时挂两道门：任一门不过即打回。
两道门都过才 completed——这就是"校验 AI"在系统里的位置。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from typing import List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# EvidenceGate 复用（可选组合）：真跑证据 + 质量门双保险
try:
    from agentskills.evidence import Evidence, EvidenceGate
except Exception:  # 单独运行 / 相对导入失败时降级
    from evidence import Evidence, EvidenceGate  # type: ignore

UNFILLED_RE = re.compile(r"<待补:(\w+)>")
_MD_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+\S", re.M)


def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8", "replace")).hexdigest()


def _head(s: str, n: int = 300) -> str:
    return s if len(s) <= n else f"{s[:n]}\n...[truncated {len(s) - n} chars]"


@dataclass
class QualityVerdict:
    """一次产出核验的结果。feedback 是打回时交给 worker 的修订指引。"""
    accepted: bool
    violations: List[str] = field(default_factory=list)
    checks: dict = field(default_factory=dict)
    feedback: str = ""
    rework_count: int = 0

    def to_dict(self) -> dict:
        return {"accepted": self.accepted, "violations": self.violations,
                "checks": self.checks, "feedback": self.feedback,
                "rework_count": self.rework_count}


class QualityGate:
    """质量门禁 + 打回回路。

    用法（worker 侧）：
        qg = QualityGate(db_path="data/quality.db")
        res = qg.review(scheduler, "p1/copy", my_output)   # 规则从 payload.quality 自动取
        if res["accepted"]:
            ...  # 任务已 completed
        else:
            ...  # 已按 res["feedback"] 打回，修订后重新认领再交

    用法（主控/人类侧）：
        qg.history("p1/copy")          # 这个任务被打回过几次、为什么
        qg.escalated_tasks()           # 哪些任务已升级待人工
    """

    def __init__(self, db_path: Optional[str] = None, max_rework: int = 3,
                 evidence_gate: Optional[EvidenceGate] = None):
        self.db_path = db_path
        self.max_rework = max_rework
        self.evidence_gate = evidence_gate  # 可选：与 EvidenceGate 组合双门
        if db_path:
            self._ensure_schema()

    # ---------------- 持久化（审计可回放） ----------------
    def _conn(self):
        if not self.db_path:
            return None
        c = sqlite3.connect(self.db_path, timeout=30)
        c.execute("PRAGMA journal_mode=WAL")
        return c

    def _ensure_schema(self):
        c = self._conn()
        if not c:
            return
        try:
            with c:
                c.execute("""
                    CREATE TABLE IF NOT EXISTS quality_reviews (
                        review_id    TEXT PRIMARY KEY,
                        task_id      TEXT NOT NULL,
                        accepted     INTEGER NOT NULL,
                        violations   TEXT,
                        feedback     TEXT,
                        rework_count INTEGER NOT NULL DEFAULT 0,
                        output_sha256 TEXT,
                        output_head  TEXT,
                        created_at   REAL
                )
            """)
                c.execute("CREATE INDEX IF NOT EXISTS idx_qr_task "
                          "ON quality_reviews(task_id)")
        finally:
            # `with c` 是事务上下文，不关连接——显式 close，否则每次
            # 实例化泄漏一个 sqlite3.Connection。
            c.close()

    def _record(self, task_id: str, v: QualityVerdict, output: str) -> str:
        c = self._conn()
        if not c:
            return ""
        rid = _sha256(f"{task_id}|{time.time()}|{_sha256(output)[:16]}")[:32]
        with c:
            c.execute(
                "INSERT OR REPLACE INTO quality_reviews VALUES (?,?,?,?,?,?,?,?,?)",
                (rid, task_id, 1 if v.accepted else 0,
                 json.dumps(v.violations, ensure_ascii=False),
                 v.feedback, v.rework_count, _sha256(output)[:32],
                 _head(output), time.time()))
        c.close()
        return rid

    def history(self, task_id: str, limit: int = 20) -> List[dict]:
        c = self._conn()
        if not c:
            return []
        rows = c.execute(
            "SELECT review_id, accepted, violations, feedback, rework_count, "
            "output_sha256, created_at FROM quality_reviews WHERE task_id=? "
            "ORDER BY created_at DESC LIMIT ?", (task_id, limit)).fetchall()
        c.close()
        return [{"review_id": r[0], "accepted": bool(r[1]),
                 "violations": json.loads(r[2] or "[]"), "feedback": r[3],
                 "rework_count": r[4], "output_sha256": r[5],
                 "created_at": r[6]} for r in rows]

    def escalated_tasks(self) -> List[str]:
        """已升级 needs_human 的任务（按最后评审时间倒序去重）。"""
        c = self._conn()
        if not c:
            return []
        rows = c.execute(
            "SELECT task_id, MAX(created_at) FROM quality_reviews "
            "WHERE rework_count > ? GROUP BY task_id ORDER BY 2 DESC",
            (self.max_rework,)).fetchall()
        c.close()
        return [r[0] for r in rows]

    # ---------------- 纯核验（不碰状态，可单测） ----------------
    def check_output(self, output: str, rules: dict,
                     ref_names: dict = None) -> QualityVerdict:
        """按 rules 核验产出物。ref_names: {上游key: 显示名}，供 references 用。"""
        rules = rules or {}
        violations: List[str] = []
        checks: dict = {}
        text = output or ""

        # 1) 长度
        n = len(text.strip())
        mn, mx = rules.get("min_chars"), rules.get("max_chars")
        checks["length"] = n
        if mn and n < mn:
            violations.append(f"产出过短：{n} 字符 < 下限 {mn}")
        if mx and n > mx:
            violations.append(f"产出过长：{n} 字符 > 上限 {mx}")

        # 2) 必含关键词
        req = rules.get("required") or []
        missing = [k for k in req if k not in text]
        checks["required_missing"] = missing
        for k in missing:
            violations.append(f"缺少必含内容: {k!r}")

        # 3) 禁用词
        forb = rules.get("forbidden") or []
        hit = [w for w in forb if w in text]
        checks["forbidden_hit"] = hit
        for w in hit:
            violations.append(f"命中禁用词: {w!r}（未完成的占位/敷衍内容不许交货）")

        # 4) 未填槽位占位符
        if rules.get("no_unfilled", True):
            marks = sorted(set(UNFILLED_RE.findall(text)))
            checks["unfilled_markers"] = marks
            if marks:
                violations.append(
                    f"残留未填槽位占位符: {marks}——先向上游/人类补齐再交货")

        # 5) 格式
        fmt = (rules.get("format") or "").lower()
        checks["format"] = fmt or None
        if fmt == "json":
            try:
                obj = json.loads(text)
                checks["json_parsed"] = True
                fields = rules.get("json_fields") or []
                if isinstance(obj, dict):
                    lack = [f for f in fields if f not in obj]
                    checks["json_fields_missing"] = lack
                    for f in lack:
                        violations.append(f"JSON 缺必填字段: {f!r}")
                elif fields:
                    violations.append("JSON 顶层不是对象，无法校验必填字段")
            except (json.JSONDecodeError, ValueError) as e:
                checks["json_parsed"] = False
                violations.append(f"不是合法 JSON: {e}")
        elif fmt == "markdown":
            ok = bool(_MD_HEADING_RE.search(text))
            checks["markdown_heading"] = ok
            if not ok:
                violations.append("Markdown 产出没有任何标题行（# …）")

        # 6) 上游引用：必须提及每个上游任务（key 或显示名命中其一）
        refs = rules.get("references") or []
        if refs:
            names = ref_names or {}
            missed = []
            for r in refs:
                label = names.get(r, r)
                if r not in text and label not in text:
                    missed.append(label)
            checks["references_missing"] = missed
            for label in missed:
                violations.append(
                    f"未引用上游产出: {label}——本环节的结论必须建立在它之上")

        accepted = not violations
        feedback = ""
        if not accepted:
            feedback = ("质量门未通过，请修订后重新提交：\n- "
                        + "\n- ".join(violations))
        return QualityVerdict(accepted=accepted, violations=violations,
                              checks=checks, feedback=feedback)

    # ---------------- 回路：review / send_back / escalate ----------------
    def _upstream_names(self, scheduler, task) -> dict:
        """取上游任务的显示名 {task_id: payload.name}，供 references 匹配。"""
        names = {}
        try:
            for d in task.get("depends_on", []):
                up = scheduler.registry.get_task(d)
                if up and isinstance(up.get("payload"), dict):
                    names[d] = up["payload"].get("name") or d
                else:
                    names[d] = d
        except Exception:
            pass
        return names

    def review(self, scheduler, task_id: str, output: str,
               rules: dict = None, evidence: Optional[Evidence] = None) -> dict:
        """核验产出并驱动状态机。

        规则来源优先级：显式 rules > payload.quality（planner 写入的）。
        通过 → scheduler.complete(task_id, result=output)；不通过 → send_back
        （或重试超限 → escalate）。返回完整 verdict（含新状态）。
        """
        task = scheduler.registry.get_task(task_id)
        if task is None:
            return {"accepted": False, "error": "no_such_task",
                    "reason": f"任务不存在: {task_id}"}
        if task["status"] != "running":
            return {"accepted": False, "error": "not_running",
                    "reason": f"任务状态为 {task['status']}，只有 running 可送审"}
        payload = task["payload"] if isinstance(task["payload"], dict) else {}
        eff_rules = rules if rules is not None else payload.get("quality", {})

        v = self.check_output(output, eff_rules,
                              ref_names=self._upstream_names(scheduler, task))

        # 可选组合：EvidenceGate 证据门也挂上（双门齐过才放行）
        if evidence is not None and v.accepted and self.evidence_gate is not None:
            ev_v = self.evidence_gate.verify(evidence)
            checks_ev = ev_v.to_dict()
            if not ev_v.accepted:
                v.accepted = False
                v.violations.extend(ev_v.reasons)
                v.feedback = ("证据门未通过：\n- "
                              + "\n- ".join(ev_v.reasons))
            v.checks["evidence_gate"] = checks_ev

        rework = int(payload.get("rework_count", 0)) + (0 if v.accepted else 1)
        v.rework_count = rework
        self._record(task_id, v, output or "")

        if v.accepted:
            scheduler.complete(task_id, result={"output": output})
            return {"accepted": True, "completed": True, "task_id": task_id,
                    "status": "completed", "rework_count": 0,
                    "verdict": v.to_dict()}

        if rework > self.max_rework:
            return self._escalate(scheduler, task_id, v, output)

        return self._send_back(scheduler, task_id, v, output)

    def _send_back(self, scheduler, task_id: str, v: QualityVerdict,
                   output: str) -> dict:
        """CAS running→registered，feedback + rework_count 写进 payload。

        任务重新就绪（依赖没动，上游仍 completed），原 worker 或别的 worker
        认领后按 feedback 修订——这就是"打回重做"的回路。
        """
        now = time.time()
        with scheduler.registry._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT status, payload FROM tasks WHERE task_id=?",
                    (task_id,)).fetchone()
                if not row or row[0] != "running":
                    conn.execute("COMMIT")
                    return {"accepted": False, "error": "not_running",
                            "reason": "状态已变化，打回未生效"}
                payload = json.loads(row[1]) if row[1] else {}
                if not isinstance(payload, dict):
                    payload = {}
                payload["rework_count"] = v.rework_count
                payload["last_feedback"] = v.feedback
                payload["last_output_head"] = _head(output or "", 200)
                cur = conn.execute(
                    "UPDATE tasks SET status=?, payload=?, updated_at=? "
                    "WHERE task_id=? AND status=?",
                    ("registered", json.dumps(payload, ensure_ascii=False),
                     now, task_id, "running"))
                ok = cur.rowcount == 1
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        if ok:
            self._emit(scheduler, "rework",
                       {"task_id": task_id, "rework_count": v.rework_count,
                        "violations": v.violations})
        return {"accepted": False, "completed": False, "sent_back": ok,
                "task_id": task_id, "status": "registered" if ok else "running",
                "rework_count": v.rework_count, "feedback": v.feedback,
                "verdict": v.to_dict(),
                "hint": f"按 feedback 修订后重新认领（还可重试 "
                        f"{max(0, self.max_rework - v.rework_count + 1)} 次）"}

    def _escalate(self, scheduler, task_id: str, v: QualityVerdict,
                  output: str) -> dict:
        """重试超限 → needs_human（不静默放行、不假装完成）。

        刻意不调 scheduler.fail()：那是"上游失败级联阻塞下游"的语义；
        质量超限是"这台机器搞不定了，要人"——下游是否继续由人决定，
        所以只冻结本任务，不动全图。
        """
        now = time.time()
        with scheduler.registry._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT payload FROM tasks WHERE task_id=?",
                    (task_id,)).fetchone()
                payload = json.loads(row[0]) if row and row[0] else {}
                if not isinstance(payload, dict):
                    payload = {}
                payload["rework_count"] = v.rework_count
                payload["escalated_reason"] = (
                    f"质量重试 {v.rework_count} 次超限（max_rework="
                    f"{self.max_rework}），最后违规: {v.violations}")
                payload["last_feedback"] = v.feedback
                conn.execute(
                    "UPDATE tasks SET status=?, payload=?, updated_at=? "
                    "WHERE task_id=? AND status=?",
                    ("needs_human", json.dumps(payload, ensure_ascii=False),
                     now, task_id, "running"))
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        self._emit(scheduler, "escalated",
                   {"task_id": task_id, "rework_count": v.rework_count,
                    "violations": v.violations})
        return {"accepted": False, "completed": False, "escalated": True,
                "task_id": task_id, "status": "needs_human",
                "rework_count": v.rework_count, "feedback": v.feedback,
                "verdict": v.to_dict(),
                "reason": "质量重试超限，已升级人工处理"}

    def _emit(self, scheduler, event_type: str, data: dict):
        """经 scheduler 的 Broker 发观战事件（可选，失败静默）。"""
        try:
            if getattr(scheduler, "broker", None) is not None:
                scheduler.broker.publish_event("quality", event_type, data)
        except Exception:
            pass


if __name__ == "__main__":
    # 演示：一份不合格产出被打回，修订后通过
    import tempfile

    from orchestration import DagScheduler

    db = os.path.join(tempfile.mkdtemp(prefix="qg_demo_"), "dag.db")
    qdb = os.path.join(tempfile.mkdtemp(prefix="qg_demo_"), "quality.db")
    s = DagScheduler(db)
    qg = QualityGate(db_path=qdb, max_rework=2)

    s.add_task("p1/copy", payload={
        "name": "文案撰写",
        "quality": {"min_chars": 50, "required": ["抖音"], "forbidden": ["TODO"],
                    "no_unfilled": True},
    })
    s.claim_ready(runner_id="worker-copy")

    bad = "抖音文案初稿 TODO 待补：{topic}"
    r1 = qg.review(s, "p1/copy", bad)
    print("第1次送审:", r1["status"], "| violations:", r1["verdict"]["violations"])

    s.claim_ready(runner_id="worker-copy")
    good = "抖音渠道文案（3版）：\n# 主文案\n……完整内容超过五十字符……"
    r2 = qg.review(s, "p1/copy", good)
    print("第2次送审:", r2["status"], "| completed:", r2.get("completed"))
    print("历史:", len(qg.history("p1/copy")), "条")
    s.close()
