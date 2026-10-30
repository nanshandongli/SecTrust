"""Planner（规则模板版拆解器）测试。"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from planner import (Plan, PlanError, Planner, TEMPLATES, extract_slots,
                     _fmt)
from orchestration import DagError, DagScheduler


class TestExtractSlots(unittest.TestCase):
    def test_topic_from_quotes(self):
        s = extract_slots("策划一场「双十一美妆营销」活动，预算500万")
        self.assertEqual(s["topic"], "双十一美妆营销")
        self.assertEqual(s["budget_cny"], 5000000.0)

    def test_topic_from_campaign(self):
        s = extract_slots("做一场年货节促销")
        self.assertEqual(s["topic"], "年货节促销")

    def test_topic_from_guanyu(self):
        s = extract_slots("关于私域流量的运营方案")
        self.assertEqual(s["topic"], "私域流量的运营方案")

    def test_budget_units(self):
        self.assertEqual(extract_slots("预算3万元")["budget_cny"], 30000.0)
        self.assertEqual(extract_slots("预算5k")["budget_cny"], 5000.0)
        self.assertEqual(extract_slots("预算2.5万")["budget_cny"], 25000.0)

    def test_channels_and_audience(self):
        s = extract_slots("主投抖音、小红书和B站，面向Z世代与宝妈")
        self.assertEqual(s["channels"], ["抖音", "小红书", "B站"])
        self.assertEqual(s["audience"], ["Z世代", "宝妈"])

    def test_deadline_formats(self):
        self.assertEqual(extract_slots("10月23日前交")["deadline"],
                         f"{__import__('time').localtime().tm_year}-10-23")
        self.assertEqual(extract_slots("2026-12-01 截止")["deadline"], "2026-12-01")

    def test_empty_text(self):
        self.assertEqual(extract_slots(""), {})

    def test_missing_slots_absent_not_fabricated(self):
        s = extract_slots("帮我整理一下团建方案")
        self.assertNotIn("budget_cny", s)
        self.assertNotIn("channels", s)


class TestFmt(unittest.TestCase):
    def test_fill_and_mark(self):
        out = _fmt("为{topic}做{channels}投放，预算{budget_cny}",
                   {"topic": "新品", "channels": ["抖音"], "budget_cny": 100})
        self.assertEqual(out, "为新品做抖音投放，预算100")

    def test_missing_slot_marked(self):
        out = _fmt("主题：{topic}，渠道：{channels}", {"topic": "X"})
        self.assertIn("<待补:channels>", out)


class TestTemplateMatching(unittest.TestCase):
    def setUp(self):
        self.p = Planner()

    def test_marketing(self):
        plan = self.p.decompose("做一场双十一美妆营销方案，主投抖音")
        self.assertEqual(plan.template, "content_marketing")
        self.assertIn("双十一", plan.match_info["hits"])

    def test_software_dev(self):
        plan = self.p.decompose("开发一个用户登录功能，含前后端接口")
        self.assertEqual(plan.template, "software_dev")

    def test_research_report(self):
        plan = self.p.decompose("写一份新能源汽车行业分析报告")
        self.assertEqual(plan.template, "research_report")

    def test_customer_service(self):
        plan = self.p.decompose("设计一套客服咨询投诉处理流程")
        self.assertEqual(plan.template, "customer_service")

    def test_generic_fallback(self):
        plan = self.p.decompose("帮我整理一下团建方案")
        self.assertEqual(plan.template, "generic")
        self.assertEqual(plan.match_info["hits"], [])

    def test_every_template_has_waves(self):
        for t in TEMPLATES:
            plan = Plan("需求", t["name"], t["title"], {}, [
                {"key": s["key"], "name": s["name"], "instruction": s["instruction"],
                 "skill": s.get("skill", ""), "model": s.get("model", ""),
                 "depends_on": list(s.get("depends_on", [])),
                 "quality": dict(s.get("quality", {}))}
                for s in t["stages"]])
            plan.validate()
            self.assertTrue(plan.waves(), f"{t['name']} 应有波次")


class TestPlan(unittest.TestCase):
    def test_marketing_waves(self):
        # tier="complex"：显式要完整模板（成本路由默认会按复杂度裁剪，
        # 见 tests/test_cost_routing.py；这里锁的是"完整模板路径仍可用"）
        plan = Planner().decompose("做一场双十一营销活动", tier="complex")
        self.assertEqual(plan.waves(),
                         [["research"], ["strategy"],
                          ["copy", "design_brief", "roi"],
                          ["compliance"], ["assembly"]])

    def test_validate_unknown_dep(self):
        with self.assertRaises(PlanError):
            Plan("r", "t", "t", {}, [{"key": "a", "depends_on": ["ghost"]}]).validate()

    def test_validate_cycle(self):
        with self.assertRaises(PlanError):
            Plan("r", "t", "t", {}, [
                {"key": "a", "depends_on": ["b"]},
                {"key": "b", "depends_on": ["a"]}]).validate()

    def test_serialization_roundtrip(self):
        plan = Planner().decompose("做一场618营销活动，预算100万")
        plan2 = Plan.from_json(plan.to_json())
        self.assertEqual(plan.template, plan2.template)
        self.assertEqual(plan.slots, plan2.slots)
        self.assertEqual(len(plan.subtasks), len(plan2.subtasks))

    def test_params_override_slots(self):
        # tier="complex"：roi 环节只在完整模板里（medium 线性化会裁掉它）
        plan = Planner().decompose("做一场营销活动",
                                   params={"topic": "年会", "budget_cny": 800},
                                   tier="complex")
        instr = next(s["instruction"] for s in plan.subtasks
                     if s["key"] == "roi")
        self.assertIn("800", instr)

    def test_empty_requirement_raises(self):
        with self.assertRaises(PlanError):
            Planner().decompose("   ")

    def test_needs_input_marked_in_payload(self):
        # 主题**抽不到**时，instruction 里必须留 "<待补:topic>" 占位，
        # 交人补——绝不自动编一个值填进去（planner 的核心纪律）。
        # 用「行业分析报告」：命中 research_report 模板，但没有任何
        # 可抽取主题的句式（不能写"写一份X"，那个现在能抽到了）。
        plan = Planner().decompose("行业分析报告")
        self.assertTrue(any("<待补:topic>" in s["instruction"]
                            for s in plan.subtasks))

    def test_topic_from_write_pattern(self):
        # 「写一份X报告」是最常见句式，之前抽不到 topic，
        # 会导致 instruction 里留占位符、被模型原样吐出来。
        s = extract_slots("写一份2026年中国新能源汽车行业分析报告")
        self.assertEqual(s["topic"], "2026年中国新能源汽车行业分析报告")
        plan = Planner().decompose("写一份2026年中国新能源汽车行业分析报告")
        self.assertFalse(any("<待补:topic>" in st["instruction"]
                             for st in plan.subtasks))


class TestBuildWithScheduler(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="planner_test_")
        self.s = DagScheduler(os.path.join(self.tmp, "dag.db"))

    def tearDown(self):
        self.s.close()

    def test_build_creates_prefixed_dag(self):
        plan = Planner().decompose("做一场双十一营销活动，预算500万", tier="complex")
        ids = plan.build(self.s, plan_id="p1")
        self.assertEqual(ids[0], "p1/research")
        self.assertEqual(len(ids), 7)
        # 上游先于下游（拓扑序）
        self.assertLess(ids.index("p1/strategy"), ids.index("p1/copy"))
        t = self.s.registry.get_task("p1/copy")
        self.assertEqual(t["depends_on"], ["p1/strategy"])
        payload = t["payload"]
        self.assertEqual(payload["skill"], "copywriter")
        self.assertEqual(payload["quality"].get("required", []), [])  # copy 无 required
        self.assertEqual(payload["plan"]["template"], "content_marketing")

    def test_build_scheduler_waves_match_plan(self):
        plan = Planner().decompose("开发一个登录功能", tier="complex")
        plan.build(self.s, plan_id="p1")
        # 波次含 write_code（代码落盘）：前端/后端产出先落成文件，再进测试
        self.assertEqual(self.s.waves(),
                         [["p1/requirements"], ["p1/architecture"],
                          ["p1/backend", "p1/frontend"], ["p1/write_code"],
                          ["p1/testing"], ["p1/docs"]])

    def test_build_twice_rejected(self):
        plan = Planner().decompose("做一场营销活动")
        plan.build(self.s, plan_id="p1")
        with self.assertRaises(DagError):
            plan.build(self.s, plan_id="p1")

    def test_full_flow_claim_and_ready(self):
        plan = Planner().decompose("写一份行业分析报告")
        plan.build(self.s, plan_id="p1")
        self.assertEqual([t["task_id"] for t in self.s.ready_tasks()],
                         ["p1/data"])
        t = self.s.claim_ready(runner_id="w1")
        self.assertEqual(t["task_id"], "p1/data")
        self.assertIn("数据", t["payload"]["instruction"])
        self.s.complete("p1/data", result={"rows": 5})
        self.assertEqual([x["task_id"] for x in self.s.ready_tasks()],
                         ["p1/analysis"])


class TestWebSearchMarking(unittest.TestCase):
    """planner 建图路径也要透传 web_search 标记（与 create_task 单任务路径一致）。

    否则走 planner 的搜索类需求（"今天重要国际新闻"等）会漏标，退化成模型
    凭记忆编造——这正是线上一次用户投诉的根因。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="planner_ws_")
        self.s = DagScheduler(os.path.join(self.tmp, "dag.db"))

    def tearDown(self):
        self.s.close()

    def test_web_search_true_marks_only_llm_subtasks(self):
        plan = Planner().decompose("写一份行业分析报告")
        ids = plan.build(self.s, plan_id="p1", web_search=True)
        for tid in ids:
            p = self.s.registry.get_task(tid)["payload"]
            if p["op"] == "llm":
                self.assertTrue(p.get("web_search"), "%s 应打标" % tid)
            else:
                self.assertFalse(p.get("web_search"), "%s 非 llm 不应打标" % tid)

    def test_web_search_false_leaves_unmarked(self):
        plan = Planner().decompose("写一份行业分析报告")
        ids = plan.build(self.s, plan_id="p2", web_search=False)
        for tid in ids:
            self.assertFalse(
                self.s.registry.get_task(tid)["payload"].get("web_search"),
                "%s 不应被打标" % tid)

    def test_payload_still_has_op(self):
        # 回归：加 web_search 字段不能把 op 弄丢（白名单透传纪律）
        plan = Planner().decompose("写一份行业分析报告")
        ids = plan.build(self.s, plan_id="p3", web_search=True)
        for tid in ids:
            p = self.s.registry.get_task(tid)["payload"]
            self.assertEqual(p["op"], "llm")


if __name__ == "__main__":
    unittest.main(verbosity=2)
