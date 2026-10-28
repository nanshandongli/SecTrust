"""tests/test_assistant_channels.py —— 助理远程通道 + 上下文管理。

覆盖：
  - 目录完整性：只含真实可接通的 webhook 通道；字段规范。
  - 配置持久化、掩码回传保留原值、公开视图不含明文 webhook。
  - 未配置/未知通道的 send() 如实失败（诚实边界）。
  - 真实 HTTP 发送：对本地回环桩服务器 POST，成功与失败两条路。
  - 偏好（context_memory）默认开启、只能改白名单键。
"""

import base64
import hashlib
import hmac
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import assistant_channels as ac  # noqa: E402


class TestCatalog(unittest.TestCase):
    def test_catalog_only_real_channels(self):
        for ch in ac.CHANNELS:
            self.assertTrue(ch["id"] and ch["name"] and ch["desc"])
            self.assertTrue(any(f["key"] == "webhook" for f in ch["fields"]))
        # 诚实边界：没有 SDK 级接入的通道不允许出现在目录里
        ids = {c["id"] for c in ac.CHANNELS}
        self.assertNotIn("wecom_assistant", ids)  # 微信助理需要官方平台，未接入
        self.assertNotIn("qq_bot", ids)

    def test_mask_webhook_hides_key_keeps_tail(self):
        url = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abcd1234wxyz"
        masked = ac.mask_webhook(url)
        self.assertNotIn("abcd1234wxyz", masked)
        self.assertIn("****wxyz", masked)
        self.assertIn("key=", masked)

    def test_mask_webhook_path_type_hides_feishu_uuid(self):
        # 飞书凭据在路径末段（无 ?key=），旧实现会完整明文泄露给未鉴权的 GET
        url = "https://open.feishu.cn/open-apis/bot/v2/hook/07380eb4-c34b-46de-a360-8b74b9194e16"
        masked = ac.mask_webhook(url)
        self.assertNotIn("07380eb4-c34b-46de-a360-8b74b9194e16", masked)
        self.assertIn("****4e16", masked)
        self.assertIn("https://open.feishu.cn/open-apis/bot/v2/hook/", masked)

    def test_mask_webhook_plain_url_unchanged(self):
        self.assertEqual(ac.mask_webhook("https://gw.example.com/hook"),
                         "https://gw.example.com/hook")


class TestStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.st = ac.AssistantSettings(data_dir=self.tmp)

    def test_defaults(self):
        self.assertTrue(self.st.prefs()["context_memory"])
        self.assertFalse(self.st.to_public()["channels"][0]["configured"])

    def test_configure_and_public_hides_secret(self):
        self.st.configure("wecom_bot",
                          webhook="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abcd1234wxyz",
                          enabled=True)
        pub = self.st.to_public()
        c = next(x for x in pub["channels"] if x["id"] == "wecom_bot")
        self.assertTrue(c["configured"] and c["enabled"])
        self.assertNotIn("abcd1234wxyz", json.dumps(pub, ensure_ascii=False))
        # 磁盘有明文（本机 data/ 已 gitignore），公开视图绝不回传
        self.assertIn("abcd1234wxyz", open(
            os.path.join(self.tmp, "assistant_channels.json"), encoding="utf-8").read())

    def test_mask_roundtrip_keeps_original(self):
        url = "https://oapi.dingtalk.com/robot/send?access_token=tok123456789"
        self.st.configure("dingtalk_bot", webhook=url, secret="SECabc")
        masked = ac.mask_webhook(url)
        self.st.configure("dingtalk_bot", webhook=masked, secret="****",
                          enabled=True)
        self.assertEqual(self.st.get_config("dingtalk_bot")["webhook"], url)
        self.assertEqual(self.st.get_config("dingtalk_bot")["secret"], "SECabc")

    def test_public_never_leaks_feishu_full_url(self):
        # 路径型 webhook 绝不以明文出现在公开视图（GET /api/assistant/settings 不鉴权）
        url = "https://open.feishu.cn/open-apis/bot/v2/hook/07380eb4-c34b-46de-a360-8b74b9194e16"
        self.st.configure("feishu_bot", webhook=url, enabled=True)
        pub = json.dumps(self.st.to_public(), ensure_ascii=False)
        self.assertNotIn(url, pub)
        self.assertNotIn("07380eb4-c34b-46de-a360-8b74b9194e16", pub)
        # 磁盘保留明文（data/ 已 gitignore），掩码视图只给末 4 位
        c = next(x for x in self.st.to_public()["channels"] if x["id"] == "feishu_bot")
        self.assertIn("****4e16", c["webhook_masked"])

    def test_enable_requires_webhook(self):
        with self.assertRaises(ValueError):
            self.st.set_enabled("feishu_bot", True)

    def test_unknown_channel_rejected(self):
        with self.assertRaises(ValueError):
            self.st.configure("qq_bot", webhook="https://x.example.com")

    def test_webhook_must_be_http(self):
        with self.assertRaises(ValueError):
            self.st.configure("generic_webhook", webhook="ftp://x")

    def test_prefs_whitelist(self):
        with self.assertRaises(ValueError):
            self.st.set_pref("evil_key", True)
        with self.assertRaises(ValueError):
            self.st.set_pref("context_memory", "yes")
        self.st.set_pref("context_memory", False)
        self.assertFalse(self.st.prefs()["context_memory"])
        # 重载后仍生效
        self.assertFalse(ac.AssistantSettings(data_dir=self.tmp).prefs()["context_memory"])


class TestSend(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.st = ac.AssistantSettings(data_dir=self.tmp)
        self.received = []

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                TestSend.last_body = json.loads(body)
                out = json.dumps({"errcode": 0}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_send_unconfigured_honest_failure(self):
        res = self.st.send("wecom_bot", "t", "x")
        self.assertFalse(res["ok"])
        self.assertIn("未配置", res["error"])

    def test_send_unknown_channel(self):
        res = self.st.send("nope", "t", "x")
        self.assertFalse(res["ok"])

    def test_send_generic_webhook_real_post(self):
        TestSend.last_body = None
        res = self.st.send("generic_webhook", "标题", "内容",
                           webhook="http://127.0.0.1:%d/hook" % self.port)
        self.assertTrue(res["ok"], res)
        self.assertEqual(TestSend.last_body["event"], "sectrust.notify")
        self.assertEqual(TestSend.last_body["title"], "标题")

    def test_send_feishu_body_shape(self):
        TestSend.last_body = None
        res = self.st.send("feishu_bot", "标题", "内容",
                           webhook="http://127.0.0.1:%d/hook" % self.port)
        self.assertTrue(res["ok"], res)
        self.assertEqual(TestSend.last_body["msg_type"], "text")
        self.assertEqual(TestSend.last_body["content"]["text"], "标题\n内容")
        self.assertNotIn("sign", TestSend.last_body)

    def test_send_feishu_with_secret_serializable_and_correct(self):
        """回归：base64 结果必须 decode 成 str，否则 json.dumps 抛 TypeError。

        同时校验签名算法本身：HMAC key = "timestamp\\nsecret"，空消息，SHA256 → base64。
        """
        TestSend.last_body = None
        res = self.st.send("feishu_bot", "标题", "内容",
                           webhook="http://127.0.0.1:%d/hook" % self.port,
                           secret="SECtest")
        self.assertTrue(res["ok"], res)   # 未 decode 时这里会是 TypeError 而非 ok:False
        body = TestSend.last_body
        self.assertIsInstance(body["sign"], str)
        self.assertIsInstance(body["timestamp"], str)
        expect = base64.b64encode(hmac.new(
            ("%s\n%s" % (body["timestamp"], "SECtest")).encode(),
            b"", hashlib.sha256).digest()).decode("utf-8")
        self.assertEqual(body["sign"], expect)

    def test_send_platform_error_surfaced(self):
        class ErrH(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                out = json.dumps({"errcode": 93000, "errmsg": "invalid webhook"}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def log_message(self, *a):
                pass

        err_srv = ThreadingHTTPServer(("127.0.0.1", 0), ErrH)
        err_port = err_srv.server_address[1]
        threading.Thread(target=err_srv.serve_forever, daemon=True).start()
        try:
            res = self.st.send("generic_webhook", "t", "x",
                               webhook="http://127.0.0.1:%d/h" % err_port)
            self.assertFalse(res["ok"])
            self.assertIn("93000", res["error"])
        finally:
            err_srv.shutdown()
            err_srv.server_close()


if __name__ == "__main__":
    unittest.main()
