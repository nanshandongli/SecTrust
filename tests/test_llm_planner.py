"""LLM 版拆解器测试（tests/test_llm_planner.py）。

覆盖：正常 LLM 输出（含围栏/杂音）、结构校验失败回退、chat 异常回退、
槽位合并优先级、quality 键白名单、与 DagScheduler 的 build 对接。
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from planner import LLMPlanner, LLMPlannerError, PlanError, Planner
from orchestration import DagScheduler


def _fake_chat(payload, record=None):
    """返回一个 chat 可调用：把 payload 序列化后交回（模拟 LLM）。"""
    def chat(messages):
        if record is not None:
            record.append(messages)
        return payload if isinstance(payload, str) else json.dumps(payload)
    return chat


_GOOD = {
    "title": "登录功能研发",
    "slots": {"topic": "用户登录功能"},
    "subtasks": [
        {"key": "requirements", "name": "需求梳理",
         "instruction": "明确{topic}的用户故事与验收标准",
         "skill": "requirements-analyst", "model": "",
         "depends_on": [],
         "quality": {"min_chars": 200, "required": ["验收"],
                     "no_unfilled": True, "format": "markdown"}},
        {"key": "backend", "name": "后端实现",
         "instruction": "实现{topic}接口与数据层",
         "skill": "backend-dev",
         "depends_on": ["requirements"],
         "quality": {"min_chars": 100, "required": ["接口"]}},
        {"key": "frontend", "name": "前端实现",
         "instruction": "实现{topic}页面与状态管理",
         "skill": "frontend-dev",
         "depends_on": ["requirements"],
         "quality": {"min_chars": 100, "required": ["组件"]}},
        {"key": "testing", "name": "测试",
         "instruction": "为{topic}编写并执行测试",
         "skill": "qa", "depends_on": ["backend", "frontend"],
         "quality": {"min_chars": 100, "required": ["测试"]}},
    ],
}


class TestLLMPlanner(unittest.TestCase):

    def test_llm_plan_used_directly(self):
        p = LLMPlanner(chat=_fake_chat(_GOOD)).decompose("开发一个用户登录功能")
        self.assertEqual(p.template, "llm")
        self.assertEqual(p.title, "登录功能研发")
        self.assertEqual(len(p.subtasks), 4)
        self.assertIsNone(p.fallback_reason)
        self.assertEqual(p.waves(), [["requirements"], ["backend", "frontend"],
                                     ["testing"]])
        # 槽位已填进指令
        self.assertIn("用户登录功能", p.subtasks[0]["instruction"])
        self.assertNotIn("<待补", p.subtasks[0]["instruction"])

    def test_prompt_contains_requirement_and_rules(self):
        rec = []
        LLMPlanner(chat=_fake_chat(_GOOD, rec)).decompose("开发登录功能")
        msgs = rec[0]
        self.assertEqual(msgs[0]["role"], "system")
        self.assertIn("min_chars", msgs[0]["content"])  # quality 键白名单在 prompt 里
        self.assertIn("开发登录功能", msgs[1]["content"])

    def test_tolerates_code_fence_and_prose(self):
        raw = "好的，拆解如下：\n```json\n" + json.dumps(_GOOD, ensure_ascii=False) + \
              "\n```\n以上。"
        p = LLMPlanner(chat=_fake_chat(raw)).decompose("开发一个用户登录功能")
        self.assertEqual(p.template, "llm")
        self.assertEqual(len(p.subtasks), 4)

    def test_invalid_json_falls_back_to_rule(self):
        p = LLMPlanner(chat=_fake_chat("这不是JSON")).decompose("做一场双十一营销")
        self.assertEqual(p.template, "content_marketing")
        self.assertEqual(p.match_info["engine"], "rule-fallback")
        self.assertIn("JSON", p.fallback_reason)

    def test_cycle_falls_back(self):
        bad = json.loads(json.dumps(_GOOD))
        bad["subtasks"][0]["depends_on"] = ["testing"]  # requirements <- testing 成环
        p = LLMPlanner(chat=_fake_chat(bad)).decompose("开发一个用户登录功能")
        self.assertEqual(p.match_info["engine"], "rule-fallback")
        self.assertIn("环", p.fallback_reason)

    def test_unknown_dependency_falls_back(self):
        bad = json.loads(json.dumps(_GOOD))
        bad["subtasks"][1]["depends_on"] = ["nonexistent"]
        p = LLMPlanner(chat=_fake_chat(bad)).decompose("开发一个用户登录功能")
        self.assertEqual(p.match_info["engine"], "rule-fallback")
        self.assertIn("未知依赖", p.fallback_reason)

    def test_duplicate_key_falls_back(self):
        bad = json.loads(json.dumps(_GOOD))
        bad["subtasks"][1]["key"] = "requirements"
        p = LLMPlanner(chat=_fake_chat(bad)).decompose("开发一个用户登录功能")
        self.assertEqual(p.match_info["engine"], "rule-fallback")

    def test_too_many_tasks_falls_back(self):
        bad = {"title": "x", "subtasks": [
            {"key": f"t{i}", "name": "n", "instruction": "i"} for i in range(30)]}
        p = LLMPlanner(chat=_fake_chat(bad)).decompose("开发一个用户登录功能")
        self.assertEqual(p.match_info["engine"], "rule-fallback")
        self.assertIn("超限", p.fallback_reason)

    def test_chat_exception_falls_back(self):
        def boom(messages):
            raise RuntimeError("网络断了")
        p = LLMPlanner(chat=boom).decompose("开发一个用户登录功能")
        self.assertEqual(p.match_info["engine"], "rule-fallback")
        self.assertIn("网络断了", p.fallback_reason)

    def test_no_chat_configured_falls_back(self):
        p = LLMPlanner(chat=None).decompose("写一份2026年行业分析报告")
        self.assertEqual(p.template, "research_report")
        self.assertIn("未配置", p.fallback_reason)

    def test_empty_requirement_rejected(self):
        with self.assertRaises(PlanError):
            LLMPlanner(chat=_fake_chat(_GOOD)).decompose("   ")

    def test_slot_priority_params_over_llm_over_regex(self):
        d = json.loads(json.dumps(_GOOD))
        d["slots"] = {"topic": "LLM主题", "budget_cny": 100}
        p = LLMPlanner(chat=_fake_chat(d)).decompose(
            "开发一个用户登录功能，预算500万", params={"topic": "人工指定主题"})
        # 合并顺序：正则抽取 < LLM 给出 < params（人类补全最高）
        self.assertEqual(p.slots["topic"], "人工指定主题")
        self.assertEqual(p.slots["budget_cny"], 100)  # LLM 槽位优先于正则
        self.assertIn("人工指定主题", p.subtasks[0]["instruction"])

    def test_missing_slot_marked_not_invented(self):
        d = json.loads(json.dumps(_GOOD))
        d["subtasks"][1]["instruction"] = "按{budget_cny}元预算实现接口"
        d["slots"] = {"topic": "登录"}
        p = LLMPlanner(chat=_fake_chat(d)).decompose("开发一个用户登录功能")
        self.assertIn("<待补:budget_cny>", p.subtasks[1]["instruction"])

    def test_quality_keys_whitelisted(self):
        d = json.loads(json.dumps(_GOOD))
        d["subtasks"][0]["quality"] = {"min_chars": "300", "required": "数据",
                                       "evil_key": "drop me", "no_unfilled": 1}
        p = LLMPlanner(chat=_fake_chat(d)).decompose("开发一个用户登录功能")
        q = p.subtasks[0]["quality"]
        self.assertEqual(q["min_chars"], 300)          # 强制 int
        self.assertEqual(q["required"], ["数据"])       # 强制 list[str]
        self.assertTrue(q["no_unfilled"])               # 强制 bool
        self.assertNotIn("evil_key", q)                 # 未知键丢弃

    def test_build_with_dag_scheduler(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = DagScheduler(os.path.join(tmp, "dag.db"))
            try:
                p = LLMPlanner(chat=_fake_chat(_GOOD)).decompose("开发一个用户登录功能")
                ids = p.build(s, plan_id="p1")
                self.assertEqual(len(ids), 4)
                self.assertEqual(s.waves(), [["p1/requirements"],
                                             ["p1/backend", "p1/frontend"],
                                             ["p1/testing"]])
                t = s.claim_ready(runner_id="w1")
                self.assertEqual(t["task_id"], "p1/requirements")
                self.assertTrue(s.complete("p1/requirements"))
                ready = {x["task_id"] for x in s.ready_tasks()}
                self.assertEqual(ready, {"p1/backend", "p1/frontend"})
            finally:
                s.close()

    def test_chatresult_adapter(self):
        class R:
            content = json.dumps(_GOOD)
        p = LLMPlanner(chat=lambda m: R()).decompose("开发一个用户登录功能")
        self.assertEqual(p.template, "llm")

    def test_non_dict_top_falls_back(self):
        p = LLMPlanner(chat=_fake_chat("[1,2,3]")).decompose("开发一个用户登录功能")
        self.assertEqual(p.match_info["engine"], "rule-fallback")

    def test_subtask_not_dict_falls_back(self):
        d = json.loads(json.dumps(_GOOD))
        d["subtasks"][0] = "我不是对象"
        p = LLMPlanner(chat=_fake_chat(d)).decompose("开发一个用户登录功能")
        self.assertEqual(p.match_info["engine"], "rule-fallback")


if __name__ == "__main__":
    unittest.main()
