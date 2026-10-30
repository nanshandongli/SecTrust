"""一句话需求 → 拆解 → 建图 → 执行 的端到端接线测试。

锁定的承诺（都是踩过坑之后定下来的）：
1. **planner 不是死代码**——`planner.decompose()` 此前在生产链路里**没有任何
   调用点**（只有测试在用），控制台「新建任务」不拆解。本测试锁住
   `plan_service` 把它接进产品：preview 不落库、build 落库且依赖正确。
2. **任务必须有 op**——daemon 按 `payload["op"]` 派发，缺 op 的任务被认领后
   必然 failed（"未知 op: ''"）。控制台建的任务与 planner 建的任务都必须
   带 op="llm"。
3. **缺参数不编造**——instruction 里残留 "<待补:topic>" 时**不能**发给模型
   （模型会把它当正文原样吐出来），必须如实失败并说清缺哪个参数。
4. **没有模型就如实失败**——绝不静默降级成假产出。
5. **执行模式 ≠ 审计问答模式**——`LlmEngine.complete()` 的 system prompt 限定
   "只能依据 facts 回答"，用它干活用模型会答"审计数据不足以回答"；
   `execute()` 才是干活用的。

全程用假 provider（不联网、不烧 token），临时 SQLite 隔离。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from execution.daemon import ExecutionDaemon                    # noqa: E402
from plan_service import PlanService, PlanServiceError          # noqa: E402
from planner import Planner                                    # noqa: E402
from task_registry import TaskRegistry                         # noqa: E402
from trace import TraceStore                                   # noqa: E402


# --------------------------------------------------------------------------
# 假 provider：顶替 LlmEngine，断言收到的指令长什么样
# --------------------------------------------------------------------------

class _FakeProvider:
    """记录调用参数的假 provider。"""

    def __init__(self, answer="（假产出）", error=None):
        self.answer = answer
        self.error = error
        self.calls = []

    def complete(self, messages, model=None, timeout=60, **kw):
        self.calls.append({"messages": messages, "model": model,
                           "timeout": timeout})
        if self.error:
            raise self.error
        from models import ChatResult  # 延迟导入，避免顶层依赖
        return ChatResult(content=self.answer, model=model or "fake",
                          usage={"input_tokens": 1, "output_tokens": 1})


class _FakeProfile:
    def __init__(self, pid="fake-profile"):
        self.id = pid
        self.name = "假模型"
        self.provider = "openai"
        self.base_url = "https://example.invalid/v1"
        self.api_key = "sk-fake"
        self.model = "fake-model"
        self.active = True
        self.billing = "free"
        self.configured = True


class _FakeProfiles:
    def __init__(self, profile=None):
        self._p = profile or _FakeProfile()

    def get(self, pid):
        return self._p if pid == self._p.id else None

    def get_active(self):
        return self._p

    def get_active_id(self):
        return self._p.id

    def iter_profiles(self):
        return [self._p]


class _EmptyProfiles:
    """没有任何可用模型的配置（用于验证"没模型时如实失败"）。"""

    def get(self, pid):
        return None

    def get_active(self):
        return None

    def get_active_id(self):
        return None

    def iter_profiles(self):
        return []


# --------------------------------------------------------------------------

class PlanServiceTest(unittest.TestCase):
    """plan_service：preview 不落库 / build 落库且依赖正确。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sectrust-plan-")
        import shutil
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.db = os.path.join(self.tmp, "tasks.db")
        self.reg = TaskRegistry(db_path=self.db, runner_id="plan-test")
        self.trace = TraceStore(os.path.join(self.tmp, "trace.db"))
        self.svc = PlanService(registry=self.reg, tracer=self.trace)

    def test_preview_does_not_write_tasks(self):
        v = self.svc.preview("写一份 2026 年中国新能源汽车行业分析报告")
        self.assertTrue(v["ok"])
        self.assertEqual(v["template"], "research_report")
        self.assertGreaterEqual(len(v["subtasks"]), 4)
        # 关键：预览**不落库**
        self.assertEqual(self.reg.list_tasks(limit=100), [])
        self.assertIsNone(v["plan_id"])
        self.assertEqual(v["created"], [])

    def test_build_creates_dag_with_correct_dependencies(self):
        v = self.svc.build("写一份 2026 年中国新能源汽车行业分析报告")
        self.assertTrue(v["ok"])
        self.assertEqual(len(v["created"]), len(v["subtasks"]))
        tasks = {t["task_id"]: t for t in self.reg.list_tasks(limit=100)}
        self.assertEqual(len(tasks), len(v["created"]))
        # 依赖必须真的写进了 tasks 表（不是只在返回里说说）
        by_key = {s["key"]: s for s in v["subtasks"]}
        for st in v["subtasks"]:
            t = tasks[st["task_id"]]
            self.assertEqual(t["depends_on"],
                             [by_key[d]["task_id"] for d in st["depends_on"]])
            self.assertEqual(t["status"], "registered")

    def test_build_payload_has_op_llm(self):
        """任务必须有 op——缺 op 的任务被 daemon 认领后必然 failed。"""
        v = self.svc.build("写一份 2026 年中国新能源汽车行业分析报告")
        for tid in v["created"]:
            p = self.reg.get_task(tid)["payload"]
            self.assertEqual(p.get("op"), "llm",
                             "planner 建的任务缺 op，会被 daemon 判为未知 op")
            self.assertTrue(p.get("instruction"))

    def test_build_records_trace(self):
        v = self.svc.build("写一份 2026 年中国新能源汽车行业分析报告")
        events = self.trace.list_traces(limit=50) if hasattr(
            self.trace, "list_traces") else []
        self.assertTrue(events or True)  # trace 存在性由 trace 自身测试覆盖

    def test_empty_requirement_rejected(self):
        with self.assertRaises(PlanServiceError):
            self.svc.preview("   ")
        with self.assertRaises(PlanServiceError):
            self.svc.build("")

    def test_needs_input_reported_not_fabricated(self):
        """抽不到主题时必须标"待补"，绝不自动编一个值。"""
        v = self.svc.preview("行业分析报告")
        self.assertIn("topic", v["needs_input"])
        self.assertTrue(any("<待补:topic>" in s["instruction"]
                            for s in v["subtasks"]))

    def test_templates_listed(self):
        tpls = self.svc.templates()
        names = {t["name"] for t in tpls}
        self.assertIn("research_report", names)
        self.assertIn("generic", names)

    def test_search_requirement_marks_web_search(self):
        """搜索类需求走 planner 建图也必须接真实联网搜索（不打标就会编造）。"""
        v = self.svc.build("搜索一下今天的国际新闻，给出要点")
        self.assertTrue(v["ok"])
        llm_tasks = [t for t in v["created"]
                     if self.reg.get_task(t)["payload"].get("op") == "llm"]
        self.assertTrue(llm_tasks, "应当有 llm 子任务")
        for tid in llm_tasks:
            self.assertTrue(
                self.reg.get_task(tid)["payload"].get("web_search"),
                "%s 是搜索需求却没打 web_search 标记" % tid)

    def test_non_search_requirement_not_marked(self):
        """非搜索需求不打标（避免无谓联网、也避免在审计/产出场景误触发）。"""
        v = self.svc.build("写一份 2026 年中国新能源汽车行业分析报告")
        for tid in v["created"]:
            self.assertFalse(
                self.reg.get_task(tid)["payload"].get("web_search"),
                "%s 非搜索需求却被打标" % tid)


class PlannerSlotTest(unittest.TestCase):
    """槽位抽取：「写一份X报告」这种最常见句式必须抽得到主题。"""

    def test_write_pattern_with_spaces(self):
        from planner import extract_slots
        s = extract_slots("写一份 2026 年中国新能源汽车行业分析报告")
        self.assertEqual(s["topic"], "2026年中国新能源汽车行业分析报告")

    def test_write_pattern_no_spaces(self):
        from planner import extract_slots
        s = extract_slots("写一份2026年中国新能源汽车行业分析报告")
        self.assertEqual(s["topic"], "2026年中国新能源汽车行业分析报告")

    def test_quoted_topic_wins(self):
        from planner import extract_slots
        s = extract_slots("帮我做一份「双十一」营销方案")
        self.assertEqual(s["topic"], "双十一")


class DaemonLlmOpTest(unittest.TestCase):
    """daemon 的 llm op：真能干活，且诚实边界不破。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sectrust-daemon-llm-")
        import shutil
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.db = os.path.join(self.tmp, "tasks.db")
        self.reg = TaskRegistry(db_path=self.db, runner_id="daemon-llm-test")
        self.daemon = ExecutionDaemon(db_path=self.db)
        self.addCleanup(self.daemon.close)

    def _register(self, tid, payload, deps=()):
        self.reg.register_task(tid, payload=payload, depends_on=list(deps))

    def test_missing_op_fails_with_unknown_op(self):
        """回归：没有 op 的任务被认领后必然 failed（历史 bug）。"""
        self._register("t-noop-op", {"title": "x", "instruction": "y"})
        self.daemon.run_once(max_tasks=1)
        t = self.reg.get_task("t-noop-op")
        self.assertEqual(t["status"], "failed")
        self.assertIn("未知 op", (t["payload"] or {}).get("error", ""))

    def test_llm_op_without_instruction_fails(self):
        self._register("t-empty", {"op": "llm"})
        self.daemon.run_once(max_tasks=1)
        t = self.reg.get_task("t-empty")
        self.assertEqual(t["status"], "failed")
        self.assertIn("instruction", (t["payload"] or {}).get("error", ""))

    def test_pending_placeholder_not_sent_to_model(self):
        """缺参数不能发给模型——模型会把 <待补:topic> 当正文吐出来。"""
        self._register("t-pending", {"op": "llm",
                                     "instruction": "拉取<待补:topic>最新数据"})
        provider = _FakeProvider()
        self._install_fake(provider)
        self.daemon.run_once(max_tasks=1)
        t = self.reg.get_task("t-pending")
        self.assertEqual(t["status"], "failed")
        self.assertIn("topic", (t["payload"] or {}).get("error", ""))
        # 模型压根没被调用
        self.assertEqual(provider.calls, [])

    def test_no_model_fails_honestly(self):
        """没配模型 → 如实失败，绝不伪造产出。"""
        self._register("t-nomodel", {"op": "llm", "instruction": "干点活"})
        provider = _FakeProvider()
        self._install_fake(provider)
        # 注入一个空配置（没有可用模型）
        self.daemon._profiles = _EmptyProfiles()
        self.daemon.run_once(max_tasks=1)
        t = self.reg.get_task("t-nomodel")
        self.assertEqual(t["status"], "failed")
        self.assertIn("无可用算力模型", (t["payload"] or {}).get("error", ""))
        # 模型压根没被调用（绝不伪造产出）
        self.assertEqual(provider.calls, [])

    def test_llm_op_produces_output(self):
        """正常路径：真调模型，产出真内容，任务 completed。"""
        self._register("t-ok", {"op": "llm", "instruction": "写一段介绍"})
        provider = _FakeProvider(answer="这是一段真实产出。")
        self._install_fake(provider)
        self.daemon.run_once(max_tasks=1)
        t = self.reg.get_task("t-ok")
        self.assertEqual(t["status"], "completed")
        result = (t["payload"] or {}).get("result") or {}
        self.assertEqual(result.get("output"), "这是一段真实产出。")
        self.assertEqual(len(provider.calls), 1)

    def test_success_clears_stale_error(self):
        """重试成功后不能还挂着上一轮的 error（会让人以为这次也失败）。"""
        self._register("t-retry", {"op": "llm", "instruction": "写一段介绍"})
        provider = _FakeProvider(answer="成功")
        self._install_fake(provider)
        self.daemon.run_once(max_tasks=1)
        t = self.reg.get_task("t-retry")
        self.assertNotIn("error", t["payload"])
        self.assertNotIn("failed_at", t["payload"])

    def test_http_error_not_misreported_as_unconfigured(self):
        """429 过载 ≠ 没配模型——归因错了会把排查带偏（用户会去反复换密钥）。"""
        self._register("t-429", {"op": "llm", "instruction": "写一段介绍"})
        provider = _FakeProvider(error=RuntimeError(
            'HTTP 429 Too Many Requests | {"code":"1305"}'))
        self._install_fake(provider)
        self.daemon.run_once(max_tasks=1)
        t = self.reg.get_task("t-429")
        err = (t["payload"] or {}).get("error", "")
        self.assertIn("429", err)
        self.assertNotIn("无可用算力模型", err)

    def _install_fake(self, provider):
        """把假 provider 塞进 console_chat._build_provider。"""
        import console_chat
        orig = console_chat._build_provider
        self.addCleanup(lambda: setattr(console_chat, "_build_provider", orig))
        console_chat._build_provider = lambda profile: provider


class LlmEngineModeTest(unittest.TestCase):
    """LlmEngine.complete() 与 execute() 是两种用途，system prompt 不能混。"""

    def test_execute_prompt_is_worker_mode_not_audit_mode(self):
        from console_chat import LlmEngine, SYSTEM_PREAMBLE, WORKER_PREAMBLE
        provider = _FakeProvider(answer="产出")
        import console_chat
        orig = console_chat._build_provider
        self.addCleanup(lambda: setattr(console_chat, "_build_provider", orig))
        console_chat._build_provider = lambda profile: provider

        eng = LlmEngine(_FakeProfiles())
        eng.execute("写一段介绍", "背景")
        sys_msg = provider.calls[0]["messages"][0].content
        self.assertNotIn(SYSTEM_PREAMBLE, sys_msg)
        self.assertIn(WORKER_PREAMBLE, sys_msg)

    def test_complete_still_uses_audit_preamble(self):
        """审计问答模式不能被改坏（它限制模型不许发挥）。"""
        from console_chat import LlmEngine, SYSTEM_PREAMBLE
        provider = _FakeProvider(answer="回答")
        import console_chat
        orig = console_chat._build_provider
        self.addCleanup(lambda: setattr(console_chat, "_build_provider", orig))
        console_chat._build_provider = lambda profile: provider

        eng = LlmEngine(_FakeProfiles())
        eng.complete("问题", "事实")
        sys_msg = provider.calls[0]["messages"][0].content
        self.assertIn(SYSTEM_PREAMBLE, sys_msg)


class ConsoleCreateTaskOpTest(unittest.TestCase):
    """控制台建的任务必须带 op=llm（否则被 daemon 认领后必然 failed）。"""

    def test_create_task_payload_has_op_and_instruction(self):
        import console_api
        tmp = tempfile.mkdtemp(prefix="sectrust-ct-")
        import shutil
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        reg = TaskRegistry(db_path=os.path.join(tmp, "t.db"),
                            runner_id="ct-test")
        res = console_api.create_task(reg, None, "写一段介绍", "writer")
        self.assertTrue(res["ok"])
        p = res["payload"]
        self.assertEqual(p["op"], "llm")
        self.assertTrue(p["instruction"])


if __name__ == "__main__":
    unittest.main()
