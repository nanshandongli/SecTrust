"""成本路由层测试（P1.5）——修「任何需求都拆 4-7 任务 DAG、简单任务付全价」。

纪律（改动时不可破坏）：
- 分档判据**可解释**（match_info.tier_reasons 非空且为人话）
- **不确定往 complex 倒**：判据不明（多叶子/无关键路径）时不猜，退回 complex
- simple 档 instruction 必须**自包含**（不引用不存在的上游）
- simple 档也保留质量门（不能因便宜就放行劣质产出）
- tier 可被调用方强制覆盖（运维手动纠偏）
- 成本估算随 Plan 落 payload（可观测、可归因）
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from planner import Planner, COMPLEXITY_TIERS, REWORK_ROUNDS
from orchestration import DagScheduler


class ComplexityRoutingTest(unittest.TestCase):
    def setUp(self):
        self.p = Planner()

    def test_simple_requirement_gets_single_task(self):
        """简单需求 -> 单任务，不经 DAG（成本纪律的核心）。"""
        for text in ("整理一下会议纪要", "写一份周报",
                     "帮我把这段话翻译成英文：今天天气很好"):
            plan = self.p.decompose(text)
            self.assertEqual(plan.tier, "simple", f"{text} 应为 simple")
            self.assertEqual(len(plan.subtasks), 1)
            self.assertEqual(plan.subtasks[0]["depends_on"], [])

    def test_explicit_multi_step_requirement_escalates(self):
        """明确列举多环节的需求必须升档——漏环节是质量事故，比多花钱严重。"""
        plan = self.p.decompose(
            "为新产品制定完整的上市营销计划，包括市场调研、策略制定、"
            "文案撰写、设计需求、投放测算与合规审核，主题：智能手表",
            params={"topic": "智能手表"})
        self.assertEqual(plan.tier, "complex")
        self.assertEqual(len(plan.subtasks), 7, "完整模板不应被裁")

    def test_mid_complexity_linearizes(self):
        """中等复杂度 -> 关键路径线性化（保留主干、去掉并行分支）。"""
        plan = self.p.decompose("为智能手表做上市营销计划，主题：智能手表",
                                params={"topic": "智能手表"})
        self.assertEqual(plan.tier, "medium")
        keys = [s["key"] for s in plan.subtasks]
        self.assertIn("assembly", keys, "交付环节必须在主干上")
        # 线性化：每个节点最多一个前驱，且首节点无前驱
        for s in plan.subtasks:
            self.assertLessEqual(len(s["depends_on"]), 1)
        self.assertEqual(plan.subtasks[0]["depends_on"], [])

    def test_fanout_intent_forces_at_least_medium(self):
        """多方案意图至少 medium（单任务无从择优）。"""
        plan = self.p.decompose("多个角度分析要不要进入东南亚市场，并给出三个方案择优")
        self.assertIn(plan.tier, ("medium", "complex"))
        self.assertTrue(any(s.get("fanout") for s in plan.subtasks),
                        "fanout 应被注入")

    def test_tier_reasons_are_explainable(self):
        """分档判据必须可解释（不许黑箱）。"""
        plan = self.p.decompose(
            "为新产品制定完整的上市营销计划，包括市场调研、策略制定、"
            "文案撰写、设计需求、投放测算与合规审核，主题：智能手表",
            params={"topic": "智能手表"})
        reasons = plan.match_info.get("tier_reasons")
        self.assertTrue(reasons, "必须有判据说明")
        self.assertTrue(all(isinstance(r, str) and r for r in reasons))

    def test_tier_can_be_overridden(self):
        """调用方可强制档位（运维手动纠偏自动判断）。"""
        plan = self.p.decompose(
            "为新产品制定完整的上市营销计划，包括市场调研、策略制定、"
            "文案撰写、设计需求、投放测算与合规审核，主题：智能手表",
            params={"topic": "智能手表"}, tier="simple")
        self.assertEqual(plan.tier, "simple")
        self.assertEqual(len(plan.subtasks), 1)

    def test_simple_instruction_is_self_contained(self):
        """simple 档不得引用不存在的上游（模板叶子常写"按审核意见修订"）。"""
        plan = self.p.decompose("写一份周报")
        ins = plan.subtasks[0]["instruction"]
        self.assertIn("无上游输入", ins)
        for bad in ("审核意见", "修订并交付", "引用上游"):
            self.assertNotIn(bad, ins, f"simple 指令不应引用上游: {bad}")

    def test_simple_keeps_quality_gate(self):
        """便宜不等于放行劣质产出：simple 档质量门必须还在。"""
        plan = self.p.decompose("写一份周报")
        self.assertTrue(plan.subtasks[0].get("quality"),
                        "simple 档也必须带质量判据")

    def test_cost_estimate_scales_with_tier(self):
        """成本估算随档位增长，且用相对倍数（不假造 token 数）。"""
        costs = {}
        for text in ("写一份周报",
                     "为智能手表做上市营销计划，主题：智能手表"):
            plan = self.p.decompose(text, params={"topic": "智能手表"})
            costs[plan.tier] = plan.cost
        self.assertLess(costs["simple"]["llm_calls_estimate"],
                        costs["medium"]["llm_calls_estimate"])
        self.assertEqual(costs["simple"]["rework_rounds"], REWORK_ROUNDS)
        self.assertIn("note", costs["simple"], "必须声明这是相对倍数非绝对 token")

    def test_cost_lands_in_payload(self):
        """成本元信息必须落进任务 payload（可观测、可归因）。"""
        plan = self.p.decompose("为智能手表做上市营销计划，主题：智能手表",
                                params={"topic": "智能手表"})
        sch = DagScheduler(db_path=os.path.join(tempfile.mkdtemp(), "r.db"))
        try:
            plan.build(sch, plan_id="cost1")
            t = sch.registry.get_task("cost1/research")
            meta = t["payload"]["plan"]
            self.assertEqual(meta["tier"], plan.tier)
            self.assertIn("cost", meta)
            self.assertIn("llm_calls_estimate", meta["cost"])
        finally:
            sch.close()

    def test_medium_graph_is_valid_dag(self):
        """裁剪后的图必须仍是合法 DAG（validate 不抛异常）。"""
        plan = self.p.decompose("为智能手表做上市营销计划，主题：智能手表",
                                params={"topic": "智能手表"})
        plan.validate()  # 不抛即通过
        self.assertEqual(plan.waves()[0], ["research"])

    def test_tier_values_in_whitelist(self):
        self.assertEqual(set(COMPLEXITY_TIERS), {"simple", "medium", "complex"})
        for text in ("写一份周报", "为智能手表做上市营销计划，主题：智能手表"):
            self.assertIn(self.p.decompose(text, params={"topic": "智能手表"}).tier,
                          COMPLEXITY_TIERS)


if __name__ == "__main__":
    unittest.main()
