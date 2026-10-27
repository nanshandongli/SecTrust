"""
tests/test_console_chat.py —— 协作审计台「对话」后端与前端契约测试

覆盖：
  1. 规则引擎：审计类问题必须命中规则（engine=rule），答案含真实数据、不含幻觉。
  2. 总结规则优先级：「总结当前审计状态」必须走总结规则，不能被单主题规则截胡。
  3. 诚实降级：LLM 不可用时 engine=unavailable，且答案明说"不可用"，
     绝不能返回伪装成模型输出的内容（这是本模块最关键的不变量）。
  4. engine 字段恒存在：前端靠它标注答案来源。
  5. 前端契约：console_web.py 必须真的接了 /api/chat（避免"壳没接线"）。
"""

import json
import os
import re
import unittest

import console_chat as C


def _ctx():
    return {
        "overview": {"counts": {"tasks_total": 5, "running": 2, "pending": 1,
                                "traces": 3},
                     "cost_total": 1.23},
        "tasks": [
            {"task_id": "t_run", "status": "running", "progress": 50,
             "payload": {"role": "designer"}},
            {"task_id": "t_done", "status": "completed", "progress": 100,
             "payload": {"role": "writer"}},
            {"task_id": "t_fail", "status": "needs_human", "progress": 30,
             "payload": {"role": "qa"}},
        ],
        "approvals": [
            {"approval_id": "ap_pending", "status": "pending", "action": "publish",
             "detail": "发布到 CDN", "task_id": "t_run"},
            {"approval_id": "ap_ok", "status": "approved", "action": "delete",
             "detail": "删除临时文件", "decided_by": "ops-1"},
        ],
        "cost": {
            "days": 30,
            "totals": {"cost_estimate": 1.23, "model_calls": 5, "tool_calls": 4},
            "by_template": [{"key": "promo", "cost_estimate": 1.0, "tasks": 2,
                             "model_calls": 4, "role": "designer"}],
            "by_role": [{"key": "designer", "cost_estimate": 1.0, "tasks": 2}],
            "outliers": {"top_cost": [{"task_id": "t_run", "cost_estimate": 0.9,
                                       "template": "promo", "retries": 2,
                                       "tool_calls": 3}]},
            "price_note": "未知模型一律标记未计价",
        },
        "security": {
            "sandbox_backend": "local-enforced",
            "sandbox_containment": "limited",
            "self_scan_real_leaks": 0,
            "self_scan_hits": 22,
            "sandbox_note": "auto() 按 docker→e2b→local-enforced→local 选择",
        },
    }


class _NoLlm:
    """强制 LLM 不可用，用于验证诚实降级路径（独立于 LlmEngine 的桩）。"""

    def __init__(self):
        pass

    def status(self):
        return {"rule_engine": True, "active_id": None, "profiles": [],
                "note": "规则引擎始终可用；LLM 不可用时不会伪造模型回答。"}

    def complete(self, question, context, history=None, model=None):
        raise RuntimeError("no_llm_available")


def _svc(llm=None):
    return C.ChatService(lambda: _ctx(), llm=llm or _NoLlm())


class TestRuleEngine(unittest.TestCase):
    """规则引擎：审计类问题零幻觉直答。"""

    def setUp(self):
        self.svc = _svc()

    def test_task_question_hits_rule(self):
        r = self.svc.ask("今天有哪些任务在跑")
        self.assertEqual(r["engine"], "rule")
        self.assertIsNone(r["model"])
        self.assertIn("t_run", r["answer"])
        self.assertIn("运行中", r["answer"])

    def test_approval_question_hits_rule(self):
        r = self.svc.ask("素材发布审批通过了吗")
        self.assertEqual(r["engine"], "rule")
        self.assertIn("ap_pending", r["answer"])
        self.assertIn("待审批", r["answer"])

    def test_cost_question_hits_rule(self):
        r = self.svc.ask("这个月成本花了多少")
        self.assertEqual(r["engine"], "rule")
        self.assertIn("1.23", r["answer"])
        # 计价口径必须出现在答案里（诚实边界：不能虚构金额）
        self.assertIn("未计价", r["answer"])

    def test_secret_question_hits_rule(self):
        r = self.svc.ask("代码里有没有泄露真实密钥")
        self.assertEqual(r["engine"], "rule")
        self.assertIn("0", r["answer"])
        self.assertIn("22", r["answer"])

    def test_summary_question_hits_summary_rule(self):
        """「总结」必须走总结规则，不能被单主题规则截胡（关键词优先级问题）。"""
        r = self.svc.ask("帮我总结一下当前审计状态")
        self.assertEqual(r["engine"], "rule")
        ans = r["answer"]
        # 总结答案应同时覆盖多个维度
        self.assertIn("任务", ans)
        self.assertIn("成本", ans)
        self.assertIn("安全", ans)
        self.assertIn("轨迹", ans)
        self.assertIn("t_run", ans)      # 含运行中任务
        self.assertIn("ap_pending", ans)  # 含待审批数

    def test_summary_precedence_over_topic(self):
        """含「成本」但主诉是总结时，应由总结规则先命中。"""
        r = self.svc.ask("总结一下成本和任务整体情况")
        self.assertEqual(r["engine"], "rule")
        self.assertIn("当前审计态总结", r["answer"])

    def test_all_answers_have_basis_and_engine(self):
        for q in ("任务", "审批", "成本", "密钥", "轨迹", "总结"):
            r = self.svc.ask(q)
            self.assertIn("engine", r)
            self.assertTrue(r.get("basis"), "规则答案必须给出依据面板：%s" % q)
            self.assertIn("next_steps", r)


class TestHonestDegradation(unittest.TestCase):
    """LLM 不可用时必须诚实说明，绝不伪造模型回答。"""

    def test_unavailable_is_explicit(self):
        svc = _svc()
        r = svc.ask("帮我写一首关于审计的诗")
        self.assertEqual(r["engine"], "unavailable")
        self.assertIsNone(r["model"])
        self.assertIn("不可用", r["answer"])
        # 答案里应给出可操作的下一步
        self.assertTrue(r["next_steps"])

    def test_unavailable_never_claims_model(self):
        svc = _svc()
        r = svc.ask("随便问点什么")
        self.assertNotIn("engine", "llm")
        self.assertNotIn("模型回答", r["answer"].replace("不会伪造模型回答", ""))

    def test_status_reports_availability(self):
        s = _svc().status()
        self.assertTrue(s["rule_engine"])
        self.assertIn("不会伪造", s["note"])

    def test_empty_question(self):
        r = _svc().ask("   ")
        self.assertEqual(r["engine"], "rule")
        self.assertIn("请输入", r["answer"])


class TestContextBuilding(unittest.TestCase):
    """喂给 LLM 的事实上下文必须包含关键数字。"""

    def test_context_contains_facts(self):
        ctx = C.build_context(_ctx()["overview"], _ctx()["tasks"],
                              _ctx()["approvals"], _ctx()["cost"],
                              _ctx()["security"])
        self.assertIn("<facts>", C.SYSTEM_PREAMBLE + ctx + "</facts>")
        self.assertIn("t_run", ctx)
        self.assertIn("ap_pending", ctx)
        self.assertIn("1.23", ctx)
        self.assertIn("未计价", ctx)

    def test_context_tolerates_empty_input(self):
        ctx = C.build_context({}, [], [], {}, {})
        self.assertIsInstance(ctx, str)
        self.assertTrue(ctx.strip())


class TestFrontendContract(unittest.TestCase):
    """前端契约：确保界面真的接了后端，不是空壳。"""

    @classmethod
    def setUpClass(cls):
        here = os.path.dirname(os.path.abspath(__file__))
        cls.src = open(os.path.join(here, "..", "console_web.py"),
                        encoding="utf-8").read()

    def test_chat_endpoint_wired(self):
        self.assertIn("/api/chat", self.src)
        self.assertIn("do_POST", self.src)
        self.assertIn("_chat_service", self.src)

    def test_chat_status_endpoint(self):
        self.assertIn("/api/chat/status", self.src)

    def test_frontend_displays_engine_source(self):
        """每次回答必须显示引擎来源（rule / llm / unavailable）——诚实边界要求。"""
        self.assertIn("引擎：", self.src)
        self.assertIn("chatEngine", self.src)

    def test_global_search_wired(self):
        """顶部搜索框必须真接了逻辑（此前是死控件）。"""
        self.assertIn("initSearch", self.src)
        self.assertIn("doSearch", self.src)
        self.assertIn("searchPop", self.src)

    def test_panels_have_detail_views(self):
        for fn_name in ("showTaskCost", "setTraceFilter", "setApprTab"):
            self.assertIn(fn_name, self.src)

    def test_no_external_cdn_dependency(self):
        """单文件 SPA 不许引外部 CDN（离线可用的前提）。"""
        self.assertNotIn("http://cdn", self.src)
        self.assertNotIn("https://cdn", self.src)
        self.assertNotIn("unpkg.com", self.src)

    def test_html_has_expected_panels(self):
        for vid in ("view-overview", "view-chat", "view-tasks", "view-trace",
                    "view-cost", "view-approvals", "view-security",
                    "view-skills", "view-settings"):
            self.assertIn('id="%s"' % vid, self.src)

    def test_no_hardcoded_secret(self):
        """新增代码不得硬编码密钥。"""
        self.assertNotRegex(self.src, r"(?i)api_key\s*=\s*['\"][A-Za-z0-9]{16,}")


if __name__ == "__main__":
    unittest.main()
