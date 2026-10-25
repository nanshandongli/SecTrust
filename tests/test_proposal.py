"""主动提议循环回归测试（2026-10-25 执行层建设时新增）。

锁定承诺：
1. 五条规则各自触发条件正确（stalled/unclaimed/backlog/failed/needs_human汇总），
   未达条件不提案；
2. 两道去重闸：冷却表去重 + 审批中心未决去重；冷却跨实例持久化；
3. scan() 是纯读（不写审批、不碰冷却）；submit() 才写审批中心；
4. 提案**不自动执行**：只以 action="proposal" 进审批中心等人裁；
5. 规则可独立禁用；未知规则名/负阈值明确报错不静默；
6. LLM 摘要可注入、异常降级、缺 key（路由落 mock）不摘要——诚实降级；
7. 连接纪律：close() 幂等；注入的 registry/approvals 不被引擎关闭。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import proposal                                    # noqa: E402
from approval import ApprovalCenter                 # noqa: E402
from proposal import DEFAULT_RULES, Proposal, ProposalEngine, ProposalError  # noqa: E402
from task_registry import TaskRegistry              # noqa: E402


def _probe() -> Proposal:
    return Proposal(rule="stalled_task", target="t-probe", severity="high",
                    summary="探针提案", detail="规则详情", suggestion="人工查看")


class _Base(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sectrust-prop-")
        self.db = os.path.join(self.tmp, "proposals.db")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.reg = TaskRegistry(db_path=os.path.join(self.tmp, "task_registry.db"))
        self.approvals = ApprovalCenter(db_path=os.path.join(self.tmp, "task_registry.db"))
        self.addCleanup(self.reg.close)
        self.addCleanup(self.approvals.close)

    def _engine(self, **kw):
        eng = ProposalEngine(db_path=self.db, registry=self.reg,
                             approvals=self.approvals, **kw)
        self.addCleanup(eng.close)
        return eng

    def _age(self, task_id, field, seconds):
        """直接把任务的 created_at/updated_at 改旧（模拟时间流逝，不 sleep）。"""
        with self.reg._connect() as conn:
            conn.execute("UPDATE tasks SET %s=? WHERE task_id=?" % field,
                         (time.time() - seconds, task_id))

    def _age_approval(self, approval_id, seconds):
        with self.approvals._connect() as conn:
            conn.execute("UPDATE approvals SET created_at=? WHERE approval_id=?",
                         (time.time() - seconds, approval_id))


class RuleTest(_Base):
    """五条规则各自的触发条件。"""

    def test_stalled_fires_only_when_running_stale(self):
        self.reg.register_task("t-run-new")
        self.reg.claim_task("t-run-new")
        self.reg.register_task("t-run-old")
        self.reg.claim_task("t-run-old")
        self._age("t-run-old", "updated_at", 3600)
        eng = self._engine(rules={"stalled_task": 1800})
        got = [p for p in eng.scan(now=time.time()) if p.rule == "stalled_task"]
        self.assertEqual([p.target for p in got], ["t-run-old"])
        self.assertEqual(got[0].severity, "high")

    def test_unclaimed_includes_quality_feedback(self):
        self.reg.register_task("t-stall", payload={"op": "shell",
                                                   "last_feedback": "缺结论章节"})
        self._age("t-stall", "created_at", 3600)
        eng = self._engine(rules={"unclaimed_task": 900})
        got = [p for p in eng.scan() if p.rule == "unclaimed_task"]
        self.assertEqual([p.target for p in got], ["t-stall"])
        self.assertIn("缺结论章节", got[0].detail)      # 打回反馈要呈现给人

    def test_approval_backlog_fires_on_stale_pending(self):
        aid = self.approvals.request("send", "给客户发周报", task_id="t-1")
        eng = self._engine(rules={"approval_backlog": 1800})
        fresh = [p for p in eng.scan() if p.rule == "approval_backlog"]
        self.assertEqual(fresh, [])                    # 刚发起不催
        self._age_approval(aid, 3600)
        got = [p for p in eng.scan() if p.rule == "approval_backlog"]
        self.assertEqual([p.target for p in got], [aid])
        self.assertIn("send", got[0].detail)

    def test_failed_rule_immediate_with_error(self):
        self.reg.register_task("t-fail", payload={"op": "shell"})
        self.reg.claim_task("t-fail")
        with self.reg._connect() as conn:
            conn.execute(
                "UPDATE tasks SET status='failed', payload=? WHERE task_id=?",
                (json.dumps({"op": "shell", "error": "exit 127"}, ensure_ascii=False),
                 "t-fail"))
        eng = self._engine(rules={"failed_task": 0})
        got = [p for p in eng.scan() if p.rule == "failed_task"]
        self.assertEqual([p.target for p in got], ["t-fail"])
        self.assertIn("exit 127", got[0].detail)

    def test_needs_human_backlog_summary_threshold(self):
        for i in range(2):
            tid = "t-nh-%d" % i
            self.reg.register_task(tid)
            with self.reg._connect() as conn:
                conn.execute("UPDATE tasks SET status='needs_human' WHERE task_id=?",
                             (tid,))
        eng = self._engine(rules={"needs_human_backlog": 3})
        self.assertEqual([p for p in eng.scan() if p.rule == "needs_human_backlog"], [])
        self.reg.register_task("t-nh-2")
        with self.reg._connect() as conn:
            conn.execute("UPDATE tasks SET status='needs_human' WHERE task_id='t-nh-2'")
        got = [p for p in eng.scan() if p.rule == "needs_human_backlog"]
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].target, "summary")     # 汇总提案，不逐条轰炸
        self.assertEqual(got[0].meta["count"], 3)

    def test_default_rules_cover_all_five(self):
        self.assertEqual(set(DEFAULT_RULES), set(proposal._RULES))
        eng = self._engine()
        self.assertEqual({r["rule"] for r in eng.list_rules()}, set(DEFAULT_RULES))


class DedupTest(_Base):
    """两道去重闸。"""

    def _stalled_task(self, tid="t-run-old"):
        self.reg.register_task(tid)
        self.reg.claim_task(tid)
        self._age(tid, "updated_at", 3600)

    def test_cooldown_suppresses_repeat(self):
        """冷却只由 submit 落下；scan 纯读。人类裁完后仍受冷却保护。"""
        self._stalled_task()
        eng = self._engine(rules={"stalled_task": 1800}, cooldown=1800)
        props = eng.scan()
        self.assertEqual([p.target for p in props], ["t-run-old"])
        eng.submit(props)                              # 落冷却
        # 直接拒绝刚提交的那条，排除"未决去重"干扰，单独验证冷却闸
        pend = [a for a in self.approvals.list_approvals(status="pending", limit=50)]
        self.assertEqual(len(pend), 1)
        self.approvals.decide(pend[0]["approval_id"], "reject")
        self.assertEqual(eng.scan(), [])               # 冷却期内不重复
        # 冷却过期后重新提案（now 推后 2 小时）
        later = time.time() + 7200
        self.assertEqual([p.target for p in eng.scan(now=later)], ["t-run-old"])

    def test_cooldown_persists_across_instances(self):
        self._stalled_task()
        e1 = self._engine(rules={"stalled_task": 1800}, cooldown=3600)
        props = e1.scan()
        self.assertEqual([p.target for p in props], ["t-run-old"])
        e1.submit(props)                               # 落冷却记录
        e2 = ProposalEngine(db_path=self.db, registry=self.reg,
                            approvals=self.approvals,
                            rules={"stalled_task": 1800}, cooldown=3600)
        self.addCleanup(e2.close)
        self.assertEqual(e2.scan(), [])                # 新实例仍认冷却

    def test_pending_proposal_dedup(self):
        self._stalled_task()
        eng = self._engine(rules={"stalled_task": 1800}, cooldown=0)
        # 审批中心已有同 key 的 pending 提案 → 不重复弹窗
        self.approvals.request("proposal", "旧的同目标提案",
                               task_id="stalled_task:t-run-old")
        self.assertEqual(eng.scan(), [])

    def test_scan_is_pure_read(self):
        self._stalled_task()
        eng = self._engine(rules={"stalled_task": 1800}, cooldown=1800)
        before_appr = len(self.approvals.list_approvals(limit=500))
        eng.scan()
        eng.scan()
        self.assertEqual(len(self.approvals.list_approvals(limit=500)),
                         before_appr)                 # 扫描不写审批
        with eng._conn() as conn:
            n = conn.execute("SELECT COUNT(*) FROM cooldowns").fetchone()[0]
        self.assertEqual(n, 0)                         # 扫描也不碰冷却

    def test_pending_dedup_only_for_proposals(self):
        """同 task_id 的非提案审批（如 send）不构成提案去重。"""
        self._stalled_task()
        self.approvals.request("send", "发周报", task_id="stalled_task:t-run-old")
        eng = self._engine(rules={"stalled_task": 1800})
        self.assertEqual([p.target for p in eng.scan()], ["t-run-old"])


def _probe2() -> Proposal:
    return _probe()


class SubmitTest(_Base):
    """提交路径：写审批 + 落冷却 + 人类可读渲染。"""

    def test_submit_writes_approval_and_cooldown(self):
        self.reg.register_task("t-run-old")
        self.reg.claim_task("t-run-old")
        self._age("t-run-old", "updated_at", 3600)
        eng = self._engine(rules={"stalled_task": 1800})
        out = eng.run_once()
        self.assertEqual(len(out), 1)
        p, aid = out[0]
        self.assertEqual(p.key, "stalled_task:t-run-old")
        rec = [a for a in self.approvals.list_approvals(limit=50)
               if a["approval_id"] == aid][0]
        self.assertEqual(rec["action"], "proposal")    # 进审批中心，等人裁
        self.assertEqual(rec["task_id"], p.key)
        self.assertIn("不自动执行", rec["detail"])     # 红线写给人看
        with eng._conn() as conn:
            row = conn.execute("SELECT last_at FROM cooldowns WHERE rule=? AND target=?",
                               ("stalled_task", "t-run-old")).fetchone()
        self.assertIsNotNone(row)

    def test_run_once_then_decision_flow(self):
        """人类批准/拒绝只影响该审批本身；引擎不会替人执行任何任务。"""
        self.reg.register_task("t-fail", payload={"op": "noop"})
        self.reg.claim_task("t-fail")
        with self.reg._connect() as conn:
            conn.execute("UPDATE tasks SET status='failed' WHERE task_id='t-fail'")
        eng = self._engine(rules={"failed_task": 0})
        out = eng.run_once()
        self.assertEqual(len(out), 1)
        aid = out[0][1]
        self.assertTrue(self.approvals.decide(aid, "approve", by="human"))
        self.assertEqual(self.approvals.status(aid), "approved")
        # 批准后任务状态不变（模块不越权执行）
        t = self.reg.get_task("t-fail")
        self.assertEqual(t["status"], "failed")

    def test_submit_empty_is_noop(self):
        eng = self._engine()
        self.assertEqual(eng.submit([]), [])


class ConfigTest(_Base):
    """规则配置与错误路径。"""

    def test_rule_disable(self):
        self.reg.register_task("t-run-old")
        self.reg.claim_task("t-run-old")
        self._age("t-run-old", "updated_at", 3600)
        eng = self._engine(rules={"stalled_task": None})
        self.assertNotIn("stalled_task", eng.rules)
        self.assertEqual([p for p in eng.scan() if p.rule == "stalled_task"], [])

    def test_unknown_rule_name_rejected(self):
        with self.assertRaises(ProposalError):
            ProposalEngine(db_path=self.db, registry=self.reg,
                           approvals=self.approvals, rules={"stalled": 60})

    def test_negative_threshold_rejected(self):
        with self.assertRaises(ProposalError):
            ProposalEngine(db_path=self.db, registry=self.reg,
                           approvals=self.approvals, rules={"stalled_task": -1})

    def test_injected_deps_not_closed_by_engine(self):
        eng = self._engine()
        eng.close()
        eng.close()                                   # 幂等
        # 注入的 registry 仍可用（引擎不越权关闭别人的连接）
        self.reg.register_task("after-close")
        self.assertIsNotNone(self.reg.get_task("after-close"))


class SummarizerTest(_Base):
    """LLM 摘要：注入 / 降级 / 诚实边界。"""

    def test_injected_summarizer_replaces_detail(self):
        self.reg.register_task("t-run-old")
        self.reg.claim_task("t-run-old")
        self._age("t-run-old", "updated_at", 3600)
        calls = []

        def fake(p):
            calls.append(p)
            return "LLM 润色后的提醒"

        eng = self._engine(rules={"stalled_task": 1800}, summarizer=fake)
        got = eng.scan()
        self.assertEqual(len(calls), 1)
        self.assertEqual(got[0].detail, "LLM 润色后的提醒")
        self.assertEqual(got[0].summary, "任务 t-run-old 已 1 小时 0 分钟 无更新，疑似卡死")

    def test_summarizer_exception_degrades_to_rule_text(self):
        self.reg.register_task("t-run-old")
        self.reg.claim_task("t-run-old")
        self._age("t-run-old", "updated_at", 3600)

        def boom(p):
            raise RuntimeError("模型超时")

        eng = self._engine(rules={"stalled_task": 1800}, summarizer=boom)
        got = eng.scan()
        self.assertEqual(len(got), 1)
        self.assertIn("running", got[0].detail)        # 降级回规则文本
        self.assertIn("不自动执行", ProposalEngine._render(got[0]))

    def test_auto_summarizer_without_keys_returns_none(self):
        """缺 key（路由落 mock）时：不摘要、不调外部 API。"""
        env = {k: "" for k in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY",
                               "OPENAI_BASE_URL", "OLLAMA_BASE_URL")}
        env["DEFAULT_MODEL"] = "mock"
        with mock.patch.dict(os.environ, env):
            fn = proposal._auto_summarizer()
        if fn is None:
            self.skipTest("当前环境无法构造模型路由（依赖缺失）")
        self.assertIsNone(fn(_probe()))                # mock 路由 → None，不摘要


class CliTest(_Base):
    """CLI：dry-run 不写审批；--once 才提交。"""

    def test_dry_run_writes_nothing(self):
        self.reg.register_task("t-run-old")
        self.reg.claim_task("t-run-old")
        self._age("t-run-old", "updated_at", 3600)
        rc = proposal.main(["--db", self.db, "--dry-run"])
        self.assertEqual(rc, 0)
        self.assertEqual([a for a in self.approvals.list_approvals(limit=50)
                          if a["action"] == "proposal"], [])

    def test_once_submits(self):
        self.reg.register_task("t-run-old")
        self.reg.claim_task("t-run-old")
        self._age("t-run-old", "updated_at", 3600)
        rc = proposal.main(["--db", self.db, "--once"])
        self.assertEqual(rc, 0)
        props = [a for a in self.approvals.list_approvals(limit=50)
                 if a["action"] == "proposal"]
        self.assertEqual(len(props), 1)
        self.assertEqual(props[0]["task_id"], "stalled_task:t-run-old")

    def test_list_rules(self):
        rc = proposal.main(["--db", self.db, "--list-rules"])
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
