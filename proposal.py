"""
proposal.py —— 主动提议循环（chief of staff；Grok Bot "主动提议"的本地最小版）。

职责：周期性扫描任务表 + 审批中心，用**规则**发现"值得人类注意的情况"，
生成提案写入审批中心等人工裁决定去留——**绝不自动执行**。
这是与"自动流水线"的红线区别：AI 可以提醒，动作的放行永远在人。

内置规则（每条独立阈值 + 独立开关 + 冷却去重）：
  - stalled_task       running 但 updated_at 超阈值（默认 30min）无更新 → 疑似卡死
  - unclaimed_task     registered 超阈值（默认 15min）无人认领 → 建议启动/检查
  - approval_backlog   pending 审批等待超阈值（默认 30min）→ 提醒人工处理
  - failed_task        failed 且超阈值（默认 0=立即）→ 失败待人工决策
  - needs_human_backlog needs_human 积压达阈值条数（默认 3）→ 汇总提醒

去重两道闸：
  1. 冷却表（cooldowns）：同 (规则, 目标) 冷却期内不重复提案（默认 30min）；
  2. 未决去重：审批中心已有同 key 的 pending 提案则不重复弹窗。

可选 LLM 摘要：
  - summarizer 可注入 callable(Proposal) -> str（测试替身 / 自定义）；
  - summarizer="auto" 时按环境变量构造真实模型路由，**路由落到 Mock（即缺 key）
    时返回 None 不摘要**——诚实降级到规则文本，绝不假装智能；
  - 摘要调用异常同样降级（规则文本已足够人类决策）。

纪律（与项目既有模块一致）：
  - SQLite 连接 thread-local 缓存 + 显式 close()（`with sqlite3.connect()`
    是事务上下文不是关闭，会泄漏连接）；
  - 注入的 registry / approvals 不负责关闭（_own_registry 模式，同 scheduler）；
  - 阈值语义逐规则不同（秒 or 条数），在 _RULES 文档字符串里写明；
  - 未知规则名直接 ValueError——拼错名字不许静默失效。

运行：
    python proposal.py --dry-run     # 只扫描打印，不写审批
    python proposal.py --once        # 扫描并提交提案（等人工裁）
    python proposal.py --list-rules  # 查看规则与阈值
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass, field

DEFAULT_DB = os.path.join("data", "proposals.db")

# 规则名 -> 默认阈值。语义逐规则不同，见各规则函数文档字符串：
#   秒类：stalled_task / unclaimed_task / approval_backlog / failed_task
#   条数类：needs_human_backlog
DEFAULT_RULES = {
    "stalled_task": 30 * 60,
    "unclaimed_task": 15 * 60,
    "approval_backlog": 30 * 60,
    "failed_task": 0,                # 0 = failed 立即提示（无时间阈值）
    "needs_human_backlog": 3,        # 3 条 needs_human 才汇总提醒
}

DEFAULT_COOLDOWN = 30 * 60           # 同 (规则, 目标) 冷却期（秒）


class ProposalError(ValueError):
    """提案引擎配置错误（如未知规则名）。"""


@dataclass
class Proposal:
    """一条待人工裁决的提议。detail 是给人看的；summary 是一句话结论。"""

    rule: str
    target: str                        # 任务 id / 审批 id / "summary"
    severity: str                      # low | medium | high
    summary: str
    detail: str
    suggestion: str
    meta: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        """审批中心里的唯一键（rule:target），用于未决去重。"""
        return "%s:%s" % (self.rule, self.target)

    def to_dict(self) -> dict:
        return {
            "rule": self.rule, "target": self.target, "severity": self.severity,
            "summary": self.summary, "detail": self.detail,
            "suggestion": self.suggestion, "meta": self.meta,
        }


# ---------------- 工具 ----------------

def _fmt_ts(ts) -> str:
    if not ts:
        return "未知时间"
    return time.strftime("%m-%d %H:%M", time.localtime(ts))


def _fmt_age(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return "%d 秒" % seconds
    if seconds < 3600:
        return "%d 分钟" % (seconds // 60)
    return "%d 小时 %d 分钟" % (seconds // 3600, (seconds % 3600) // 60)


def _task_label(t: dict) -> str:
    """任务的人类可读标签：优先 instruction/title/op，退回 task_id。"""
    payload = t.get("payload") if isinstance(t.get("payload"), dict) else {}
    for k in ("instruction", "title", "op", "name"):
        v = payload.get(k)
        if v:
            return str(v)
    return t.get("task_id", "")


def _payload_of(t: dict) -> dict:
    p = t.get("payload")
    return p if isinstance(p, dict) else {}


# ---------------- 规则（纯函数：tasks, pending_approvals, now, threshold） ----------------

def _rule_stalled(tasks, pending, now, threshold):
    """阈值=秒。running 且 (now-updated_at) 超阈值 → 疑似卡死。"""
    out = []
    for t in tasks:
        if t.get("status") != "running":
            continue
        ref = t.get("updated_at") or t.get("created_at") or now
        age = now - ref
        if age < threshold:
            continue
        out.append(Proposal(
            rule="stalled_task", target=t["task_id"], severity="high",
            summary="任务 %s 已 %s 无更新，疑似卡死" % (t["task_id"], _fmt_age(age)),
            detail=("任务 %s（%s）状态 running，但自 %s 起已 %s 没有心跳/进度更新。"
                    "可能是执行器已退出或命令永久阻塞。"
                    % (t["task_id"], _task_label(t), _fmt_ts(ref), _fmt_age(age))),
            suggestion="人工确认执行器存活；若已卡死，终止该任务或转 needs_human 保留现场。",
            meta={"age": age, "label": _task_label(t)},
        ))
    return out


def _rule_unclaimed(tasks, pending, now, threshold):
    """阈值=秒。registered 且 (now-created_at) 超阈值 → 无人认领。

    质量门打回的任务会带 payload.last_feedback，一并呈现给人。
    """
    out = []
    for t in tasks:
        if t.get("status") != "registered":
            continue
        created = t.get("created_at") or now
        age = now - created
        if age < threshold:
            continue
        payload = _payload_of(t)
        feedback = payload.get("last_feedback")
        extra = ""
        if feedback:
            extra = "\n质量门打回反馈：%s" % feedback
        out.append(Proposal(
            rule="unclaimed_task", target=t["task_id"], severity="low",
            summary="任务 %s 已登记 %s 无人认领" % (t["task_id"], _fmt_age(age)),
            detail=("任务 %s（%s）自 %s 登记后 %s 无人认领，可能执行器未跟上或依赖阻塞。%s"
                    % (t["task_id"], _task_label(t), _fmt_ts(created), _fmt_age(age), extra)),
            suggestion="检查执行 daemon 是否存活、依赖是否已就绪；确认无人可做则取消或改派。",
            meta={"age": age, "label": _task_label(t),
                  "has_feedback": bool(feedback)},
        ))
    return out


def _rule_approval_backlog(tasks, pending, now, threshold):
    """阈值=秒。pending 审批等待超阈值 → 提醒人工处理。"""
    out = []
    for a in pending:
        created = a.get("created_at") or now
        age = now - created
        if age < threshold:
            continue
        out.append(Proposal(
            rule="approval_backlog", target=a["approval_id"], severity="medium",
            summary="审批 %s 已等待 %s" % (a["approval_id"], _fmt_age(age)),
            detail=("审批 %s（action=%s）自 %s 起等待 %s：%s"
                    % (a["approval_id"], a.get("action", "?"), _fmt_ts(created),
                       _fmt_age(age), (a.get("detail") or "")[:200])),
            suggestion="尽快批准或拒绝；超时的审批会被标记 expired（默认拒绝，不自动放行）。",
            meta={"age": age, "action": a.get("action", "")},
        ))
    return out


def _rule_failed(tasks, pending, now, threshold):
    """阈值=秒（默认 0=立即）。failed → 待人工决策（重试/放弃/转 needs_human）。"""
    out = []
    for t in tasks:
        if t.get("status") != "failed":
            continue
        ref = t.get("updated_at") or t.get("created_at") or now
        age = now - ref
        if age < threshold:
            continue
        payload = _payload_of(t)
        error = payload.get("error") or payload.get("last_feedback") or "未记录原因"
        out.append(Proposal(
            rule="failed_task", target=t["task_id"], severity="high",
            summary="任务 %s 失败待处理" % t["task_id"],
            detail=("任务 %s（%s）失败于 %s：%s"
                    % (t["task_id"], _task_label(t), _fmt_ts(ref), str(error)[:300])),
            suggestion="人工决策：修复后重新登记、取消该任务，或转 needs_human 深入排查。",
            meta={"age": age, "error": str(error)[:300]},
        ))
    return out


def _rule_needs_human_backlog(tasks, pending, now, threshold):
    """阈值=条数。needs_human 积压达阈值 → 汇总提案（target=summary）。

    语义与其他规则不同（计数而非计时），docstring 与 DEFAULT_RULES 注释均已写明。
    """
    stuck = [t for t in tasks if t.get("status") == "needs_human"]
    if len(stuck) < threshold:
        return []
    listing = "; ".join("%s（%s）" % (t["task_id"], _task_label(t)[:40])
                        for t in stuck[:5])
    if len(stuck) > 5:
        listing += "; …等共 %d 条" % len(stuck)
    return [Proposal(
        rule="needs_human_backlog", target="summary", severity="medium",
        summary="%d 个任务挂起等人工" % len(stuck),
        detail=("当前有 %d 个任务处于 needs_human（不级联、保留现场）：%s"
                % (len(stuck), listing)),
        suggestion="逐个处理挂起任务；若集中出现同类问题，考虑修规则/技能而非逐条兜底。",
        meta={"count": len(stuck)},
    )]


_RULES = {
    "stalled_task": _rule_stalled,
    "unclaimed_task": _rule_unclaimed,
    "approval_backlog": _rule_approval_backlog,
    "failed_task": _rule_failed,
    "needs_human_backlog": _rule_needs_human_backlog,
}


# ---------------- 可选 LLM 摘要 ----------------

_SUMMARY_SYSTEM = (
    "你是协同系统的chief of staff。把下面这条系统规则提案改写成一段"
    "给人类负责人看的简短中文提醒（3-5 句）：说清现状、影响、建议动作。"
    "不要编造提案里没有的事实；语气平实，不用感叹号。"
)


def _auto_summarizer(model=None):
    """按环境变量构造"真模型才摘要"的摘要器；缺 key/不可用返回 None。

    路由落到 Mock provider（即没有任何真实 key）时返回 None——
    诚实降级到规则文本，绝不假装智能。
    """
    try:
        from config import load_config
        from models import ModelRouter
        from models.openai_like import OpenAIModelProvider
        from models.anthropic import AnthropicProvider
        from models.ollama import OllamaProvider
        md = load_config().model
        router = ModelRouter(
            [OpenAIModelProvider(md.openai_api_key, md.openai_base_url),
             AnthropicProvider(md.anthropic_api_key),
             OllamaProvider(md.ollama_base_url)],
            default_model=md.default_model,
        )
    except Exception:
        return None
    chosen = model or md.default_model

    def _summarize(p: Proposal):
        provider = router.route(chosen)
        if getattr(provider, "name", "") == "mock":
            return None
        res = provider.complete(
            [{"role": "system", "content": _SUMMARY_SYSTEM},
             {"role": "user", "content": json.dumps(p.to_dict(), ensure_ascii=False)}],
            model=chosen,
        )
        return res.content

    return _summarize


# ---------------- 引擎 ----------------

class ProposalEngine:
    """主动提议引擎：scan() 纯读；submit() 才写审批中心（人类裁决定去留）。"""

    def __init__(self, db_path: str = None, registry=None, approvals=None,
                 rules: dict = None, cooldown: float = DEFAULT_COOLDOWN,
                 summarizer=None, model: str = None,
                 task_limit: int = 1000, approval_limit: int = 200):
        self.db_path = db_path or os.environ.get("SECTRUST_PROPOSALS_DB", DEFAULT_DB)
        os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)

        # 规则合并：值 >=0 覆盖阈值；None = 禁用；未知名直接报错（不许静默失效）
        self.rules = dict(DEFAULT_RULES)
        for name, val in (rules or {}).items():
            if name not in _RULES:
                raise ProposalError("未知规则名: %r（可用: %s）"
                                    % (name, ", ".join(sorted(_RULES))))
            if val is None:
                self.rules.pop(name, None)
            else:
                v = float(val)
                if v < 0:
                    raise ProposalError("规则 %s 阈值不能为负: %r" % (name, val))
                self.rules[name] = v

        self.cooldown = float(cooldown)
        self.task_limit = task_limit
        self.approval_limit = approval_limit

        if summarizer == "auto":
            self.summarizer = _auto_summarizer(model=model)
        else:
            self.summarizer = summarizer

        # registry / approvals 可注入（测试替身）；自建的才负责关闭
        from task_registry import TaskRegistry
        self._own_registry = registry is None
        self.registry = registry or TaskRegistry(db_path=_sibling_registry(self.db_path))
        if approvals is None:
            from approval import ApprovalCenter
            approvals = ApprovalCenter(db_path=_sibling_registry(self.db_path))
            self._own_approvals = True
        else:
            self._own_approvals = False
        self.approvals = approvals

        self._local = threading.local()
        self._closed = False
        self._ensure_schema()

    # ---- 连接：thread-local + 显式 close（项目纪律）----
    def _conn(self):
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.db_path, timeout=30)
            c.execute("PRAGMA journal_mode=WAL")
            self._local.conn = c
        return c

    def _ensure_schema(self):
        with self._conn() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS cooldowns ("
                " rule TEXT NOT NULL,"
                " target TEXT NOT NULL,"
                " last_at REAL NOT NULL,"
                " PRIMARY KEY (rule, target))"
            )

    def close(self):
        if getattr(self, "_closed", False):
            return
        self._closed = True
        c = getattr(self._local, "conn", None)
        if c is not None:
            try:
                c.close()
            finally:
                self._local.conn = None
        if self._own_registry:
            try:
                self.registry.close()
            except Exception:
                pass
        if self._own_approvals:
            try:
                self.approvals.close()
            except Exception:
                pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    # ---- 冷却读写 ----
    def _cooldown_ok(self, rule: str, target: str, now: float) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT last_at FROM cooldowns WHERE rule=? AND target=?",
                (rule, target)).fetchone()
        if row is None:
            return True
        return (now - row[0]) >= self.cooldown

    def _touch(self, rule: str, target: str, now: float):
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO cooldowns (rule, target, last_at)"
                " VALUES (?,?,?)", (rule, target, now))

    def _pending_exists(self, proposal: Proposal, pending: list) -> bool:
        """审批中心已有同 key 的 pending 提案 → 不重复弹窗。"""
        return any(a.get("action") == "proposal" and a.get("task_id") == proposal.key
                   for a in pending)

    # ---- 扫描（纯读，不写任何东西）----
    def scan(self, now: float = None) -> list:
        """按启用规则生成提案列表。已过两道去重闸；不写审批、不碰冷却。"""
        now = time.time() if now is None else now
        tasks = self.registry.list_tasks(limit=self.task_limit)
        pending = self.approvals.list_approvals(status="pending",
                                                limit=self.approval_limit)
        raw = []
        for name, threshold in self.rules.items():
            raw.extend(_RULES[name](tasks, pending, now, threshold))
        fresh = [p for p in raw if self._cooldown_ok(p.rule, p.target, now)]
        fresh = [p for p in fresh if not self._pending_exists(p, pending)]
        if self.summarizer:
            for p in fresh:
                try:
                    text = self.summarizer(p)
                except Exception:
                    text = None      # 摘要失败降级规则文本，绝不阻断提议
                if text and text.strip():
                    p.detail = text.strip()
        return fresh

    # ---- 提交（写审批中心 + 冷却落库；去留由人裁）----
    def submit(self, proposals: list, now: float = None) -> list:
        """把提案写入审批中心。返回 [(Proposal, approval_id)]。"""
        now = time.time() if now is None else now
        out = []
        for p in proposals:
            aid = self.approvals.request("proposal", self._render(p),
                                         task_id=p.key)
            self._touch(p.rule, p.target, now)
            out.append((p, aid))
        return out

    @staticmethod
    def _render(p: Proposal) -> str:
        return ("[主动提议|%s] %s\n详情：%s\n建议：%s\n"
                "（规则 %s；批准=采纳建议，拒绝=忽略。本提案不自动执行任何动作。）"
                % (p.severity, p.summary, p.detail, p.suggestion, p.rule))

    def run_once(self, now: float = None) -> list:
        """扫描 + 提交一轮。返回 [(Proposal, approval_id)]。"""
        return self.submit(self.scan(now), now=now)

    # ---- 只读：当前启用规则 ----
    def list_rules(self) -> list:
        unit = {"needs_human_backlog": "条"}
        return [{"rule": n, "threshold": t,
                 "unit": unit.get(n, "秒"), "enabled": True}
                for n, t in self.rules.items()]


def _sibling_registry(db_path: str) -> str:
    """proposals.db 旁的 task_registry.db（同目录约定，approvals 也落那里）。"""
    d = os.path.dirname(os.path.abspath(db_path))
    for cand in ("task_registry.db", "tasks.db"):
        p = os.path.join(d, cand)
        if os.path.exists(p):
            return p
    return os.path.join(d, "task_registry.db")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="主动提议循环 CLI（提案等人工裁，不自动执行）")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--once", action="store_true", help="扫描并提交提案")
    ap.add_argument("--dry-run", action="store_true", help="只扫描打印，不写审批")
    ap.add_argument("--list-rules", action="store_true", help="查看规则与阈值")
    ap.add_argument("--cooldown", type=float, default=DEFAULT_COOLDOWN)
    args = ap.parse_args(argv)

    engine = ProposalEngine(db_path=args.db, cooldown=args.cooldown)
    try:
        if args.list_rules:
            for r in engine.list_rules():
                print("%-22s 阈值=%.0f%s" % (r["rule"], r["threshold"], r["unit"]))
            return 0
        proposals = engine.scan()
        if args.dry_run:
            for p in proposals:
                print("[%s] %s\n  规则=%s 目标=%s\n  建议=%s\n"
                      % (p.severity, p.summary, p.rule, p.target, p.suggestion))
            print("（dry-run：%d 条提案未提交）" % len(proposals))
            return 0
        if args.once:
            out = engine.submit(proposals)
            for p, aid in out:
                print("已提交提案 %s -> 审批 %s（等人工裁）" % (p.key, aid))
            print("共提交 %d 条（去留由人决定，本模块不自动执行）" % len(out))
            return 0
        ap.print_help()
        return 0
    finally:
        engine.close()


if __name__ == "__main__":
    raise SystemExit(main())
