"""image 算子回归测试（2026-10-30 给平台补文生图能力时新增）。

锁定的承诺：
1. 意图识别：detect_image_intent / Planner._is_image_request 识别画图请求，
   不把普通文本任务误判成画图；
2. 规划器：画图请求强制单任务 op=image（不进 LLM 文本流水线、不开投票），
   指令保持原话；
3. 模型配置：ModelProfile 支持 kind=chat/image 且落盘往返一致；
   get_image() 优先 kind=image，回退 active（Ollama 不参与）；
4. daemon image op：**诚实边界**——没配绘图模型如实 failed（绝不 Mock 假图）；
   端点 400 带状态码如实报错；loopback 假端点全链路成功（b64 落盘
   data/images + result.images 含 /api/image/ 直链）。
   loopback 假端点仅用于单测（不冒充真实跨网调用）。
"""

from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from console_chat import detect_image_intent, detect_search_intent        # noqa: E402
from execution.daemon import ExecutionDaemon                              # noqa: E402
from model_profiles import ModelProfileStore                              # noqa: E402
from planner import Planner                                               # noqa: E402


# 最小 PNG 头（落盘测试不校验图像内容，只验证字节写进文件）
_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


class _TmpEnv:
    """临时目录托管，tearDown 自动清理。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sectrust-image-test-")
        self.db = os.path.join(self.tmp, "tasks.db")
        # 测试期间清掉代理环境变量：loopback 假端点必须直连
        # （urllib 会吃 http_proxy，把 127.0.0.1 的请求转给代理就死了）。
        self._saved_proxy = {k: os.environ.get(k) for k in
                            ("http_proxy", "https_proxy", "HTTP_PROXY",
                             "HTTPS_PROXY", "all_proxy", "ALL_PROXY")}
        for k in self._saved_proxy:
            os.environ.pop(k, None)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)
        for k, v in self._saved_proxy.items():
            if v is not None:
                os.environ[k] = v


# ---------------------------------------------------------------- 意图识别
class ImageIntentTest(unittest.TestCase):

    def test_positive_phrases(self):
        for q in ["画一张山水画", "帮我画个头像", "生成一张图：日落海滩",
                  "画一张介绍量子计算的海报", "给这篇文章配个插图", "出一张图"]:
            self.assertTrue(detect_image_intent(q), q)

    def test_negative_phrases(self):
        # 关键负例：此前"重大国际新闻"被当画图/文本任务硬塞 LLM 的教训
        for q in ["搜索重大国际新闻", "重大国际新闻 今日", "写一首关于秋天的诗",
                  "总结这份报告", "检索一下最新的人工智能论文"]:
            self.assertFalse(detect_image_intent(q), q)

    def test_search_intent_not_clobbered(self):
        # 画图意图与搜索意图互不干扰（create_task 画图时关掉 web_search）
        self.assertTrue(detect_search_intent("搜索重大国际新闻"))
        self.assertFalse(detect_search_intent("画一张山水画"))

    def test_planner_agrees_with_console(self):
        # 两处关键词同源语义：常见样例两边判定一致（改此需同步改彼）
        p = Planner()
        for q in ["画一张山水画", "生成一张日落海滩的图", "写一份周报"]:
            self.assertEqual(p._is_image_request(q), detect_image_intent(q), q)


# ---------------------------------------------------------------- 规划器
class PlannerImageTest(unittest.TestCase):

    def test_image_request_single_leaf(self):
        plan = Planner().decompose("画一张赛博朋克风格的城市夜景")
        subs = plan.subtasks
        self.assertEqual(len(subs), 1, "画图请求应强制单任务，不拆多工序")
        leaf = subs[0]
        self.assertEqual(leaf["op"], "image")
        self.assertEqual(leaf["instruction"], "画一张赛博朋克风格的城市夜景")
        self.assertEqual(leaf.get("quality") or {}, {})

    def test_text_request_stays_llm(self):
        plan = Planner().decompose("写一份项目周报")
        self.assertNotIn("image", [s.get("op") for s in plan.subtasks])


# ---------------------------------------------------------------- 模型配置
class ProfileKindTest(unittest.TestCase):

    def _store(self):
        return ModelProfileStore(data_dir=self.tmp)

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sectrust-prof-test-")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_kind_roundtrip(self):
        st = self._store()
        st.upsert({"id": "img-1", "provider": "openai", "name": "画图模型",
                   "base_url": "http://127.0.0.1:1/v1", "api_key": "sk-test",
                   "model": "img-v1", "kind": "image"})
        # 重新从磁盘读：kind 必须持久化（不能只在内存里）
        st2 = ModelProfileStore(data_dir=self.tmp)
        pub = st2.to_public_list() if hasattr(st2, "to_public_list") else None
        raw = json.load(open(os.path.join(self.tmp, "model_profiles.json"),
                             encoding="utf-8"))
        kinds = {p["id"]: p.get("kind") for p in raw.get("profiles", [])}
        self.assertEqual(kinds.get("img-1"), "image")

    def test_get_image_prefers_image_kind(self):
        st = self._store()
        st.upsert({"id": "chat-1", "provider": "openai", "name": "对话模型",
                   "base_url": "http://127.0.0.1:1/v1", "api_key": "sk-a",
                   "model": "chat-v1", "active": True, "kind": "chat"})
        st.upsert({"id": "img-1", "provider": "openai", "name": "画图模型",
                   "base_url": "http://127.0.0.1:2/v1", "api_key": "sk-b",
                   "model": "img-v1", "kind": "image"})
        img = st.get_image()
        self.assertIsNotNone(img)
        self.assertEqual(img.id, "img-1")

    def test_get_image_falls_back_to_active(self):
        # 镜像控制台真实流程：upsert 建档 + set_active 设默认
        # （空 store 会自动播种 ollama-local active=True，upsert 不清别人
        #   的 active——所以"设为默认"必须走 set_active，与控制台一致）
        st = self._store()
        st.upsert({"id": "chat-1", "provider": "openai", "name": "对话模型",
                   "base_url": "http://127.0.0.1:1/v1", "api_key": "sk-a",
                   "model": "chat-v1", "kind": "chat"})
        st.set_active("chat-1")
        img = st.get_image()
        self.assertIsNotNone(img)
        self.assertEqual(img.id, "chat-1")

    def test_get_image_skips_ollama_fallback(self):
        # active 是本地 Ollama 时不回退（没绘图端点，调不通，不静默伪造）
        st = self._store()
        st.set_active("ollama-local")
        self.assertIsNone(st.get_image())

    def test_get_image_none_when_unconfigured(self):
        st = self._store()
        st.upsert({"id": "chat-1", "provider": "openai", "name": "对话模型",
                   "base_url": "http://127.0.0.1:1/v1", "api_key": "",
                   "model": "chat-v1", "kind": "chat"})
        st.set_active("chat-1")
        self.assertIsNone(st.get_image(), "无 key 的 profile 不算已配置")


# ---------------------------------------------------------------- daemon
class _FakeImageEndpoint:
    """loopback 假 OpenAI 兼容图像端点（单测专用，标 mock_fixture 语义）。

    mode="ok"   → 返回 1 张 b64 图
    mode="400"  → 返回 400 + 错误体（模拟网关不支持文生图）
    """

    def __init__(self, mode="ok"):
        self.mode = mode
        self.received = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
                outer.received.append(body)
                if outer.mode == "400":
                    payload = {"error": {"message": "images not supported"}}
                    data = json.dumps(payload).encode("utf-8")
                    self.send_response(400)
                else:
                    data = json.dumps({"data": [
                        {"b64_json": base64.b64encode(_PNG_BYTES).decode()}]}).encode()
                    self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):   # 静默
                pass

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.srv.server_address[1]
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self):
        return "http://127.0.0.1:%d/v1" % self.port

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


class DaemonImageTest(_TmpEnv, unittest.TestCase):

    def _daemon(self, **kw):
        kw.setdefault("workspace_root", os.path.join(self.tmp, "workspaces"))
        kw.setdefault("approval_wait", 1.0)
        d = ExecutionDaemon(db_path=self.db, **kw)
        self.addCleanup(d.close)
        return d

    def _profiles_with_image(self, base_url):
        st = ModelProfileStore(data_dir=self.tmp)
        st.upsert({"id": "img-1", "provider": "openai", "name": "画图模型",
                   "base_url": base_url, "api_key": "sk-loopback",
                   "model": "img-v1", "kind": "image"})
        return st

    def test_no_profile_fails_honestly(self):
        d = self._daemon(profiles=ModelProfileStore(data_dir=self.tmp))
        d.sched.add_task("t-img0", payload={"op": "image",
                                            "instruction": "画一张测试图"})
        out = d.run_once()
        self.assertEqual(out, [("t-img0", "failed")])
        t = d.reg.get_task("t-img0")
        self.assertEqual(t["status"], "failed")
        self.assertIn("未配置可用于绘图的模型", t["payload"].get("error", ""))
        # 绝不伪造：失败任务不得留下任何图像产物
        self.assertFalse(os.path.isdir(os.path.join(self.tmp, "images")))

    def test_success_saves_b64_and_result(self):
        ep = _FakeImageEndpoint(mode="ok")
        self.addCleanup(ep.close)
        d = self._daemon(profiles=self._profiles_with_image(ep.base_url))
        d.sched.add_task("t-img1", payload={"op": "image",
                                            "instruction": "画一张测试图"})
        out = d.run_once()
        self.assertEqual(out, [("t-img1", "done")])
        t = d.reg.get_task("t-img1")
        res = t["payload"]["result"]
        self.assertEqual(res["op"], "image")
        self.assertEqual(res["count"], 1)
        self.assertEqual(len(res["images"]), 1)
        img = res["images"][0]
        self.assertTrue(img["local"])
        self.assertEqual(img["url"], "/api/image/" + img["file"])
        # 落盘文件真实存在且字节与假端点返回一致（daemon 目录=data/ 的父目录）
        fp = os.path.join(self.tmp, "images", img["file"])
        self.assertTrue(os.path.isfile(fp))
        with open(fp, "rb") as fh:
            self.assertEqual(fh.read(), _PNG_BYTES)
        # 请求体：prompt 原样、b64_json 格式、n=1
        self.assertEqual(ep.received[0]["prompt"], "画一张测试图")
        self.assertEqual(ep.received[0]["response_format"], "b64_json")

    def test_endpoint_400_reports_status(self):
        ep = _FakeImageEndpoint(mode="400")
        self.addCleanup(ep.close)
        d = self._daemon(profiles=self._profiles_with_image(ep.base_url))
        d.sched.add_task("t-img2", payload={"op": "image",
                                            "instruction": "画一张测试图"})
        d.run_once()
        t = d.reg.get_task("t-img2")
        self.assertEqual(t["status"], "failed")
        err = t["payload"].get("error", "")
        self.assertIn("HTTP 400", err)
        self.assertIn("images not supported", err)

    def test_endpoint_down_fails_without_trace(self):
        # 指向不存在的端口：连接失败必须如实报错，不假装成功
        st = ModelProfileStore(data_dir=self.tmp)
        st.upsert({"id": "img-x", "provider": "openai", "name": "画图模型",
                   "base_url": "http://127.0.0.1:9/v1", "api_key": "sk-x",
                   "model": "img-v1", "kind": "image"})
        d = self._daemon(profiles=st)
        d.sched.add_task("t-img3", payload={"op": "image",
                                            "instruction": "画一张测试图"})
        d.run_once()
        t = d.reg.get_task("t-img3")
        self.assertEqual(t["status"], "failed")
        self.assertTrue(t["payload"].get("error", ""))
        self.assertFalse(os.path.isdir(os.path.join(self.tmp, "images")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
