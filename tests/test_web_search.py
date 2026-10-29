"""
tests/test_web_search.py —— 真实 Web 搜索 + 搜索意图打标 + 控制台随启动拉起 daemon

覆盖：
  1. execution.web_search：recency 归一、SERP 抽取（去重）、诚实降级
     （空查询 / 网络不可达 / 反爬拦截 / 解析为空 → ok=False，绝不抛异常、绝不伪造）。
  2. console_chat.detect_search_intent：命中/排除（审计守卫）。
  3. console_api.create_task：搜索意图任务打 web_search=True，非搜索任务 False。
  4. console_web._ensure_daemon：已有存活 daemon → 跳过（防重复派发）；
     无存活 daemon → detached 拉起子进程。
"""

import os
import sys
import unittest
from unittest import mock
from urllib.error import URLError as urllib_error_URLError

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import console_api as A
import console_chat as C
from execution import web_search as WS
import console_web as W


class TestBuildSearchQuery(unittest.TestCase):
    def test_strips_task_tail(self):
        # 任务性尾缀（给模型的要求）不该进搜索词
        q = WS.build_search_query("搜索一下今天的AI方面的新闻，给出3条要点")
        self.assertNotIn("要点", q)
        self.assertIn("AI", q)

    def test_strips_leading_verbs(self):
        self.assertEqual(WS.build_search_query("搜索一下人工智能行业动态"),
                         "人工智能行业动态")
        self.assertEqual(WS.build_search_query("帮我查一下竞品最新进展"),
                         "竞品最新进展")

    def test_plain_query_unchanged(self):
        self.assertEqual(WS.build_search_query("OpenAI 最新动态"),
                         "OpenAI 最新动态")

    def test_empty_safe(self):
        self.assertEqual(WS.build_search_query(""), "")
        # 清洗后为空 → 原样返回，绝不凭空造词
        self.assertEqual(WS.build_search_query("搜索一下，给出要点"), "搜索一下")


class TestRelevanceFilter(unittest.TestCase):
    def test_terms_extraction(self):
        terms = WS._query_terms("AI新闻")
        self.assertIn("AI", terms)
        # 停用词（新闻/最新）不作匹配判据
        self.assertNotIn("新闻", terms)
        self.assertNotIn("最新", WS._query_terms("最新 人工智能 动态"))

    def test_filters_offtopic_results(self):
        # 跑题结果（不含任何查询词）必须被滤掉
        res = [
            {"title": "图像的大小与哪些因素有关", "url": "https://a/1", "snippet": "像素点灰度"},
            {"title": "OpenAI 发布新模型", "url": "https://a/2", "snippet": "AI research company"},
        ]
        kept = WS._filter_relevant(res, WS._query_terms("AI新闻"))
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["url"], "https://a/2")

    def test_empty_terms_keeps_all(self):
        res = [{"title": "x", "url": "https://a", "snippet": ""}]
        self.assertEqual(WS._filter_relevant(res, []), res)


class TestNormalizeQuery(unittest.TestCase):
    def test_today_news_normalized(self):
        # 「今天 + 新闻类词」必须归一为「最新」（代理坏垂直规避）
        self.assertEqual(WS._normalize_query("今天 AI 新闻"), "最新 AI 新闻")
        self.assertEqual(WS._normalize_query("今天 行业动态"), "最新 行业动态")

    def test_plain_query_unchanged(self):
        self.assertEqual(WS._normalize_query("人工智能 行业新闻"), "人工智能 行业新闻")
        self.assertEqual(WS._normalize_query("写一份报告"), "写一份报告")


class TestExtractBlocks(unittest.TestCase):
    FIXTURE = (
        '<li class="b_algo"><h2><a href="https://example.com/a">标题A</a></h2>'
        '<div><p class="b_lineclamp2">摘要A内容</p></div></li>'
        '<li class="b_algo"><h2><a href="https://example.com/b">标题B</a></h2>'
        '<p>摘要B</p></li>'
        # 重复 URL 必须去重
        '<li class="b_algo"><h2><a href="https://example.com/a">标题A重复</a></h2>'
        '<p>xx</p></li>'
        # 无 h2 标题块必须跳过
        '<li class="b_algo"><div>无标题</div></li>'
    )

    def test_extract_and_dedupe(self):
        res = WS._extract_blocks(self.FIXTURE)
        self.assertEqual(len(res), 2)  # 去重后只剩 A、B
        self.assertEqual(res[0]["title"], "标题A")
        self.assertEqual(res[0]["url"], "https://example.com/a")
        self.assertEqual(res[0]["snippet"], "摘要A内容")
        self.assertEqual(res[1]["url"], "https://example.com/b")

    def test_empty_html(self):
        self.assertEqual(WS._extract_blocks("<html>无结果</html>"), [])


class _FakeOpener:
    """模拟 urllib opener：open() 抛 URLError（网络/代理不可达）。"""
    def open(self, *a, **k):
        raise urllib_error_URLError("模拟不可达")


class TestWebSearchHonestDegradation(unittest.TestCase):
    def test_empty_query(self):
        r = WS.web_search("   ")
        self.assertFalse(r["ok"])
        self.assertIsNotNone(r["error"])
        self.assertEqual(r["results"], [])

    def test_network_unreachable(self):
        # 网络/代理不可达 → 诚实降级，绝不抛异常、绝不伪造结果
        with mock.patch.object(WS, "_build_opener", return_value=(_FakeOpener(), "http://p")):
            r = WS.web_search("人工智能 行业新闻")
        self.assertFalse(r["ok"])
        self.assertIn("不可达", r["error"])
        self.assertEqual(r["results"], [])
        self.assertEqual(r["engine"], "bing")

    def test_unexpected_exception_does_not_crash(self):
        def boom(*a, **k):
            raise RuntimeError("boom")
        with mock.patch.object(WS, "_build_opener", side_effect=boom):
            r = WS.web_search("x")
        self.assertFalse(r["ok"])
        self.assertIn("异常", r["error"])
        self.assertEqual(r["results"], [])


class TestDetectSearchIntent(unittest.TestCase):
    def test_search_keywords(self):
        for q in ["搜索一下今天的AI新闻", "查一下竞品动态", "最新大模型进展",
                  "行业新闻汇总", "search for python news"]:
            self.assertTrue(C.detect_search_intent(q), q)

    def test_non_search(self):
        for q in ["写一份产品方案", "生成主图", "帮我润色这段文字", "总结会议纪要"]:
            self.assertFalse(C.detect_search_intent(q), q)

    def test_audit_guard_excluded(self):
        # 审计语境不该当搜索任务
        self.assertFalse(C.detect_search_intent("查一下密钥泄露情况"))
        self.assertFalse(C.detect_search_intent("搜索审批记录"))


class _FakeRegistry:
    def __init__(self):
        self.tasks = {}

    def list_tasks(self, limit=1000):
        return list(self.tasks.values())

    def register_task(self, task_id, payload=None, depends_on=None):
        self.tasks[task_id] = {"task_id": task_id, "status": "registered",
                                "payload": payload or {},
                                "depends_on": list(depends_on or [])}
        return task_id


class _FakeTracer:
    def start_trace(self, *a, **k):
        return "trace-x"

    def record(self, *a, **k):
        pass

    def finish(self, *a, **k):
        pass


class TestCreateTaskWebSearchMarking(unittest.TestCase):
    def setUp(self):
        self.reg = _FakeRegistry()
        self.tr = _FakeTracer()

    def _payload(self, title):
        r = A.create_task(self.reg, self.tr, title, "analyst", operator="u")
        self.assertTrue(r["ok"])
        return r["payload"]

    def test_search_intent_marked_true(self):
        p = self._payload("搜索一下今天的AI新闻")
        self.assertTrue(p.get("web_search"))

    def test_non_search_marked_false(self):
        p = self._payload("写一份竞品分析报告")
        self.assertFalse(p.get("web_search"))

    def test_audit_query_not_marked_search(self):
        # 审计查询不标 web_search（交给规则引擎）
        p = self._payload("查一下有没有密钥泄露")
        self.assertFalse(p.get("web_search"))


class TestEnsureDaemon(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "data")

    def test_skips_when_alive_daemon_present(self):
        # 已有非 stale 的 daemon（排除 console_web 自身）→ 跳过拉起
        fake_runners = [
            {"runner_id": "console_web", "status": "alive"},
            {"runner_id": "exec-abc", "status": "alive"},
        ]
        with mock.patch.object(W, "TaskRegistry") as TR, \
                mock.patch.object(W.subprocess, "Popen") as Popen:
            TR.return_value.list_runners.return_value = fake_runners
            TR.return_value.close.return_value = None
            spawned = W._ensure_daemon("data/task_registry.db", self.tmp)
        self.assertFalse(spawned)
        Popen.assert_not_called()

    def test_spawns_when_no_alive_daemon(self):
        # 只有 stale 的 runner / 仅 console_web → 应 detached 拉起
        fake_runners = [
            {"runner_id": "console_web", "status": "alive"},
            {"runner_id": "exec-old", "status": "stale"},
        ]
        with mock.patch.object(W, "TaskRegistry") as TR, \
                mock.patch.object(W.subprocess, "Popen") as Popen:
            TR.return_value.list_runners.return_value = fake_runners
            TR.return_value.close.return_value = None
            spawned = W._ensure_daemon("data/task_registry.db", self.tmp)
        self.assertTrue(spawned)
        Popen.assert_called_once()
        args = Popen.call_args.kwargs["args"]
        self.assertEqual(args[1], "-m")
        self.assertEqual(args[2], "execution.daemon")
        # 脱离进程组：Windows creationflags 或 POSIX start_new_session
        k = Popen.call_args.kwargs
        self.assertTrue(k.get("start_new_session") or k.get("creationflags"))

    def test_spawns_when_no_runners(self):
        with mock.patch.object(W, "TaskRegistry") as TR, \
                mock.patch.object(W.subprocess, "Popen") as Popen:
            TR.return_value.list_runners.return_value = []
            TR.return_value.close.return_value = None
            spawned = W._ensure_daemon("data/task_registry.db", self.tmp)
        self.assertTrue(spawned)
        Popen.assert_called_once()


if __name__ == "__main__":
    unittest.main()
