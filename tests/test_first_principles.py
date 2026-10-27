"""
test_first_principles.py —— 「第一性原理 vs 标新立异」多视角分析器

覆盖：
  1. 单元测试 FirstPrinciplesAnalyzer：
     - 正常多视角生成 + 判别（第一性原理/标新立异/混合分类 + 本质度评分 + verdict）
     - 无可用算力模型 → 抛 RuntimeError("no_llm_available")（诚实边界）
     - 判别 JSON 解析失败 → 全部标记 unknown，不崩
     - 视角生成阶段调用失败 → 诚实返回 engine=unavailable
  2. 契约测试：console_web.py 接好 /api/first-principles 路由；mobile_console.html 有入口与接口调用
  3. 端到端：起服务，无可用模型时 POST 返回 200 + engine=unavailable（绝不伪造）
"""
import json
import os
import threading
import unittest
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer

import first_principles
import console_web


# --------------------------------------------------------------------------
# Fake provider（复用与真实 provider 完全一致的 complete 签名）
# --------------------------------------------------------------------------
class _FakeRes:
    def __init__(self, content):
        self.content = content
        self.model = "fake-model"
        self.usage = {"prompt_tokens": 1, "completion_tokens": 1}


class _FakeProvider:
    """视角生成返回固定内容；判别请求（user 以 [ 开头）返回注入的 JSON 数组。"""

    def __init__(self, disc_json):
        self.disc_json = disc_json

    def complete(self, messages, model=None, timeout=60, **opts):
        user = messages[-1].content if messages else ""
        if user.strip().startswith("["):
            return _FakeRes(self.disc_json)
        return _FakeRes("视角内容：" + user[:30])


class _BoomProvider:
    """视角生成阶段直接抛错，验证诚实降级。"""

    def complete(self, messages, model=None, timeout=60, **opts):
        raise RuntimeError("simulated network failure")


# --------------------------------------------------------------------------
# Profiles stub
# --------------------------------------------------------------------------
class _Unconfigured:
    configured = False


class _NoActiveProfiles:
    def get_active(self):
        return _Unconfigured()

    def get(self, mid):
        return None


DISC = json.dumps([
    {"id": "first_principles", "classification": "first_principles",
     "essence_score": 9, "reasoning": "回到底层事实重构"},
    {"id": "analogy_break", "classification": "mixed",
     "essence_score": 6, "reasoning": "部分推翻假设，部分仍惯例"},
    {"id": "novelty_skin", "classification": "novelty_seeking",
     "essence_score": 2, "reasoning": "只是换皮"},
], ensure_ascii=False)


class TestFirstPrinciplesAnalyzer(unittest.TestCase):

    def setUp(self):
        self._orig = first_principles._build_provider

    def tearDown(self):
        first_principles._build_provider = self._orig

    def _analyzer(self, provider):
        first_principles._build_provider = lambda p: provider
        # 真实 provider 需要一个"已配置"的 profile；这里用 stub 让 get_active 返回 configured
        class _Cfg:
            configured = True
            provider = "openai"
            model = "fake"
        class _Profiles:
            def get_active(self):
                return _Cfg()
            def get(self, mid):
                return _Cfg()
        return first_principles.FirstPrinciplesAnalyzer(_Profiles())

    def test_normal_analysis(self):
        a = self._analyzer(_FakeProvider(DISC))
        out = a.analyze("为什么审批流程这么慢")
        self.assertEqual(out["engine"], "llm")
        self.assertEqual(len(out["perspectives"]), 3)
        by_id = {p["id"]: p for p in out["perspectives"]}
        self.assertEqual(by_id["first_principles"]["classification"], "first_principles")
        self.assertEqual(by_id["novelty_skin"]["classification"], "novelty_seeking")
        self.assertEqual(by_id["first_principles"]["essence_score"], 9)
        # verdict：第一性原理里本质度最高的应为 best
        self.assertEqual(out["verdict"]["best_id"], "first_principles")
        self.assertIn("第一性原理", out["verdict"]["summary"])

    def test_no_llm_available_raises(self):
        a = first_principles.FirstPrinciplesAnalyzer(_NoActiveProfiles())
        with self.assertRaises(RuntimeError) as ctx:
            a.analyze("主题")
        self.assertIn("no_llm_available", str(ctx.exception))

    def test_bad_disc_json_marks_unknown(self):
        # 判别返回非 JSON → 全部 unknown，但不崩，视角内容仍在
        a = self._analyzer(_FakeProvider("这不是 json 啊"))
        out = a.analyze("主题")
        self.assertEqual(out["engine"], "llm")
        self.assertEqual(len(out["perspectives"]), 3)
        for p in out["perspectives"]:
            self.assertEqual(p["classification"], "unknown")

    def test_generation_failure_returns_unavailable(self):
        first_principles._build_provider = lambda p: _BoomProvider()
        class _Cfg:
            configured = True
            provider = "openai"
            model = "fake"
        class _Profiles:
            def get_active(self):
                return _Cfg()
            def get(self, mid):
                return _Cfg()
        a = first_principles.FirstPrinciplesAnalyzer(_Profiles())
        out = a.analyze("主题")
        self.assertEqual(out["engine"], "unavailable")
        self.assertIn("视角生成", out["error"])

    def test_empty_topic_unavailable(self):
        a = self._analyzer(_FakeProvider(DISC))
        out = a.analyze("   ")
        self.assertEqual(out["engine"], "unavailable")


# --------------------------------------------------------------------------
# 契约（防壳化）
# --------------------------------------------------------------------------
class TestFirstPrinciplesWiring(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "..", "console_web.py"), encoding="utf-8") as f:
            cls.web = f.read()
        with open(os.path.join(here, "..", "mobile_console.html"), encoding="utf-8") as f:
            cls.mobile = f.read()

    def test_route_wired(self):
        self.assertIn('"/api/first-principles"', self.web)
        self.assertIn("FirstPrinciplesAnalyzer", self.web)

    def test_mobile_entry_present(self):
        # 分段切换按钮 + 结果容器 + 接口调用都要真实接上
        self.assertIn('data-mode="fp"', self.mobile)
        self.assertIn('id="fp-result"', self.mobile)
        self.assertIn('"/api/first-principles"', self.mobile)
        self.assertIn("runFirstPrinciples", self.mobile)


# --------------------------------------------------------------------------
# 端到端：无可用模型时诚实返回 unavailable（不伪造）
# --------------------------------------------------------------------------
class TestFirstPrinciplesEndpoint(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        console_web.Handler.model_profiles = _NoActiveProfiles()
        console_web.Handler.sessions = None
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), console_web.Handler)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        console_web.Handler.model_profiles = None

    def _post(self, path, payload):
        req = urllib.request.Request(
            "http://127.0.0.1:%d%s" % (self.port, path),
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8"))

    def test_unavailable_when_no_model(self):
        status, data = self._post("/api/first-principles", {"topic": "测试主题"})
        self.assertEqual(status, 200)
        self.assertEqual(data["engine"], "unavailable")
        self.assertEqual(data["error"], "no_llm_available")
        self.assertEqual(data["perspectives"], [])


if __name__ == "__main__":
    unittest.main()
