"""质量回流回路（QualityGate）测试：核验规则 + 打回 + 升级。"""

import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agentskills.evidence import Evidence, EvidenceGate
from agentskills.quality import QualityGate
from orchestration import DagScheduler

LONG_GOOD = ("抖音渠道文案三版齐全：主文案突出双十一美妆卖点，"
             "副文案强调限时礼盒，评论区话术自然不违规，整体超过一百个字符。")


class TestCheckOutput(unittest.TestCase):
    def setUp(self):
        self.qg = QualityGate()  # 无库：纯核验

    def test_min_chars(self):
        v = self.qg.check_output("太短", {"min_chars": 50})
        self.assertFalse(v.accepted)
        self.assertTrue(any("过短" in x for x in v.violations))

    def test_max_chars(self):
        v = self.qg.check_output("x" * 100, {"max_chars": 10})
        self.assertFalse(v.accepted)

    def test_required_missing(self):
        v = self.qg.check_output("正文内容足够长但没有关键词", {"min_chars": 5,
                                                       "required": ["摘要"]})
        self.assertFalse(v.accepted)
        self.assertEqual(v.checks["required_missing"], ["摘要"])

    def test_forbidden_hit(self):
        v = self.qg.check_output("这里是 TODO 待补充的内容", {"forbidden": ["TODO"]})
        self.assertFalse(v.accepted)
        self.assertEqual(v.checks["forbidden_hit"], ["TODO"])

    def test_unfilled_markers(self):
        v = self.qg.check_output("主题：<待补:topic> 的内容", {})
        self.assertFalse(v.accepted)
        self.assertEqual(v.checks["unfilled_markers"], ["topic"])

    def test_no_unfilled_can_be_disabled(self):
        v = self.qg.check_output("<待补:topic>", {"no_unfilled": False})
        self.assertTrue(v.accepted)

    def test_json_ok_and_fields(self):
        v = self.qg.check_output('{"a": 1, "b": 2}',
                                 {"format": "json", "json_fields": ["a"]})
        self.assertTrue(v.accepted)
        v = self.qg.check_output('{"a": 1}',
                                 {"format": "json", "json_fields": ["a", "b"]})
        self.assertFalse(v.accepted)
        self.assertEqual(v.checks["json_fields_missing"], ["b"])

    def test_invalid_json(self):
        v = self.qg.check_output("{不是json", {"format": "json"})
        self.assertFalse(v.accepted)
        self.assertFalse(v.checks["json_parsed"])

    def test_markdown_heading(self):
        self.assertTrue(self.qg.check_output("# 标题\n正文", {"format": "markdown"}).accepted)
        self.assertFalse(self.qg.check_output("没有标题的文本", {"format": "markdown"}).accepted)

    def test_references_key_or_name(self):
        v = self.qg.check_output("基于 p1/research 的结论……",
                                 {"references": ["p1/research"]},
                                 ref_names={"p1/research": "市场调研"})
        self.assertTrue(v.accepted)
        v = self.qg.check_output("自说自话",
                                 {"references": ["p1/research"]},
                                 ref_names={"p1/research": "市场调研"})
        self.assertFalse(v.accepted)
        self.assertEqual(v.checks["references_missing"], ["市场调研"])

    def test_all_good_passes(self):
        v = self.qg.check_output(LONG_GOOD,
                                 {"min_chars": 50, "required": ["抖音"],
                                  "forbidden": ["TODO"]})
        self.assertTrue(v.accepted)
        self.assertEqual(v.feedback, "")


class TestReviewLoop(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="qg_test_")
        self.s = DagScheduler(os.path.join(self.tmp, "dag.db"))
        self.qg = QualityGate(db_path=os.path.join(self.tmp, "q.db"),
                              max_rework=2)
        self.s.add_task("p1/copy", payload={
            "name": "文案撰写",
            "quality": {"min_chars": 50, "required": ["抖音"],
                        "forbidden": ["TODO"], "no_unfilled": True}})

    def tearDown(self):
        self.s.close()

    def _claim(self):
        return self.s.claim_ready(runner_id="w1")

    def test_no_such_task(self):
        r = self.qg.review(self.s, "ghost", "x")
        self.assertEqual(r["error"], "no_such_task")

    def test_not_running_rejected(self):
        r = self.qg.review(self.s, "p1/copy", "内容")  # 未认领 = registered
        self.assertEqual(r["error"], "not_running")

    def test_pass_completes_with_result(self):
        self._claim()
        r = self.qg.review(self.s, "p1/copy", LONG_GOOD)
        self.assertTrue(r["accepted"])
        self.assertEqual(r["status"], "completed")
        t = self.s.registry.get_task("p1/copy")
        self.assertEqual(t["status"], "completed")
        self.assertEqual(t["payload"]["result"]["output"], LONG_GOOD)

    def test_fail_sends_back_with_feedback(self):
        self._claim()
        r = self.qg.review(self.s, "p1/copy", "TODO 抖音文案")
        self.assertFalse(r["accepted"])
        self.assertTrue(r["sent_back"])
        self.assertEqual(r["status"], "registered")
        self.assertEqual(r["rework_count"], 1)
        self.assertIn("TODO", r["feedback"])
        t = self.s.registry.get_task("p1/copy")
        self.assertEqual(t["payload"]["rework_count"], 1)
        self.assertIn("last_feedback", t["payload"])
        # 打回后重新就绪，可再认领
        self.assertEqual([x["task_id"] for x in self.s.ready_tasks()], ["p1/copy"])
        self.assertIsNotNone(self._claim())

    def test_rework_then_pass(self):
        self._claim()
        self.qg.review(self.s, "p1/copy", "TODO")
        self._claim()
        r = self.qg.review(self.s, "p1/copy", LONG_GOOD)
        self.assertTrue(r["accepted"])
        self.assertEqual(r["rework_count"], 0)  # 通过不计打回

    def test_escalation_after_max_rework(self):
        for i in range(3):
            self._claim()
            r = self.qg.review(self.s, "p1/copy", "TODO 抖音")
        self.assertTrue(r["escalated"])
        self.assertEqual(r["status"], "needs_human")
        self.assertEqual(r["rework_count"], 3)
        t = self.s.registry.get_task("p1/copy")
        self.assertEqual(t["status"], "needs_human")
        self.assertIn("escalated_reason", t["payload"])
        self.assertEqual(self.qg.escalated_tasks(), ["p1/copy"])
        # needs_human 不会再被认领（不在 ready 集）
        self.assertEqual(self.s.ready_tasks(), [])

    def test_rules_default_from_payload(self):
        """不显式传 rules 时，用 planner 写进 payload 的 quality。"""
        self._claim()
        r = self.qg.review(self.s, "p1/copy", "没有必含词的足够长内容……" * 3)
        self.assertFalse(r["accepted"])
        self.assertIn("抖音", str(r["verdict"]["violations"]))

    def test_history(self):
        self._claim()
        self.qg.review(self.s, "p1/copy", "TODO")
        h = self.qg.history("p1/copy")
        self.assertEqual(len(h), 1)
        self.assertFalse(h[0]["accepted"])

    def test_evidence_gate_composition(self):
        """质量过 + 证据不过 => 仍然打回（双门）。"""
        eg = EvidenceGate()  # 只核验形态
        qg = QualityGate(db_path=os.path.join(self.tmp, "q2.db"),
                         max_rework=2, evidence_gate=eg)
        self._claim()
        bad_ev = Evidence(command="python -m unittest discover -s tests",
                          exit_code=1, stdout="FAILED (failures=1)",
                          claim="测试全过", ran_at=time.time())
        r = qg.review(self.s, "p1/copy", LONG_GOOD, evidence=bad_ev)
        self.assertFalse(r["accepted"])
        self.assertIn("evidence_gate", r["verdict"]["checks"])
        self.assertEqual(r["status"], "registered")

    def test_evidence_gate_composition_pass(self):
        eg = EvidenceGate()
        qg = QualityGate(db_path=os.path.join(self.tmp, "q3.db"),
                         max_rework=2, evidence_gate=eg)
        self._claim()
        ok_ev = Evidence(command="python -m unittest discover -s tests",
                         exit_code=0, stdout="Ran 200 tests\nOK",
                         claim="测试全过", ran_at=time.time())
        r = qg.review(self.s, "p1/copy", LONG_GOOD, evidence=ok_ev)
        self.assertTrue(r["accepted"])
        self.assertEqual(r["status"], "completed")


class TestPlannerQualityIntegration(unittest.TestCase):
    """planner 的 quality 字段 -> QualityGate 自动读取，端到端。"""

    def test_plan_output_gate(self):
        from planner import Planner

        tmp = tempfile.mkdtemp(prefix="qg_int_")
        s = DagScheduler(os.path.join(tmp, "dag.db"))
        qg = QualityGate(db_path=os.path.join(tmp, "q.db"), max_rework=2)
        plan = Planner().decompose("做一场双十一营销活动，预算500万")
        plan.build(s, plan_id="p1")
        s.claim_ready(runner_id="w1")
        # 调研产出留了占位符 -> 应被打回
        r = qg.review(s, "p1/research",
                      "# 调研简报\n数据来源：<待补:topic> 第三方报告。"
                      "内容足够长以满足最小字符限制，这里再补充一些文字确保超过三百字符。"
                      "近三年赛道数据、竞品活动数据与目标用户偏好均已涵盖，"
                      "数据来源字段也已标注，唯一问题是主题槽位未填。")
        self.assertFalse(r["accepted"])
        self.assertIn("unfilled", str(r["verdict"]["checks"]) or "")
        self.assertEqual(r["verdict"]["checks"]["unfilled_markers"], ["topic"])
        self.assertTrue(any("占位符" in v for v in r["verdict"]["violations"]))
        s.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
