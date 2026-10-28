"""tests/test_console_model_catalog.py —— 「添加模型」弹窗的后端端点 + 前端结构测试。

覆盖：
  - GET /api/chat/providers 返回完整目录（分组、条目、提示），无需登录（只读目录）。
  - 目录条目不含任何密钥字段。
  - 前端 APP_HTML 内含弹窗骨架与关键钩子（避免"改了后端忘了改前端"）。
"""

import json
import os
import sys
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import console_web  # noqa: E402
import model_providers as mp  # noqa: E402


class TestProvidersEndpoint(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        console_web.Handler.model_profiles = None
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), console_web.Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def _get(self, path):
        url = "http://127.0.0.1:%d%s" % (self.port, path)
        with urllib.request.urlopen(url, timeout=10) as r:
            return r.status, json.loads(r.read().decode("utf-8"))

    def test_providers_endpoint_ok(self):
        status, data = self._get("/api/chat/providers")
        self.assertEqual(status, 200)
        self.assertEqual(data["groups"], mp.GROUP_ORDER)
        self.assertEqual(len(data["providers"]), len(mp.PROVIDERS))
        self.assertTrue(data["notice"])

    def test_providers_endpoint_no_secret_fields(self):
        _, data = self._get("/api/chat/providers")
        for item in data["providers"]:
            self.assertNotIn("api_key", item)
            self.assertNotIn("key", item)

    def test_provider_entries_carry_real_base_url(self):
        _, data = self._get("/api/chat/providers")
        for item in data["providers"]:
            self.assertTrue(item["base_url"].startswith("http"),
                            "%s 的 base_url 不合法" % item["id"])
            self.assertIn("billing_label", item)
            self.assertIn("protocol", item)

    def test_providers_endpoint_exposes_manual_options(self):
        """手动填写模式所需：计费方式选项 + 协议选项（前端不硬编码枚举）。"""
        _, data = self._get("/api/chat/providers")
        billing = data["billing_options"]
        self.assertEqual([b["value"] for b in billing],
                         [mp.BILLING_FREE, mp.BILLING_SUBSCRIPTION,
                          mp.BILLING_PAYG, mp.BILLING_LOCAL])
        for b in billing:
            self.assertTrue(b["label"])
            self.assertTrue(b["hint"], "%s 缺少计费说明" % b["value"])
        protos = data["protocol_options"]
        self.assertEqual([p["value"] for p in protos], list(mp.PROTOCOLS))
        for p in protos:
            self.assertTrue(p["label"])
            self.assertTrue(p["hint"])


class TestFrontendScaffold(unittest.TestCase):
    """前端结构断言 —— 只验骨架，避免"页面看起来有了但钩子没接上"。"""

    @classmethod
    def setUpClass(cls):
        cls.html = console_web.APP_HTML

    def test_modal_markup_present(self):
        for frag in ('id="modelModal"', 'id="mmPick"', 'id="mmSearch"',
                     'id="mmList"', 'id="mmBase"', 'id="mmKey"', 'id="mmModel"',
                     'id="mmSave"', 'id="mmCancel"'):
            self.assertIn(frag, self.html, "缺少弹窗元素：%s" % frag)

    def test_manual_mode_markup_present(self):
        """手动填写（key + base_url + 计费方式）所需的弹窗元素。"""
        for frag in ('id="mmTabCatalog"', 'id="mmTabManual"', 'id="mmCatalogPane"',
                     'id="mmManualPane"', 'id="mmProto"', 'id="mmBilling"',
                     'id="mmBillingHint"', 'id="mmProtoHint"'):
            self.assertIn(frag, self.html, "缺少手动填写元素：%s" % frag)

    def test_manual_mode_hooks_present(self):
        for frag in ("setModelMode", "fillBillingSelect", "syncBillingHint",
                     "syncProtoHint", "billing_options", "protocol_options"):
            self.assertIn(frag, self.html, "缺少手动填写钩子：%s" % frag)

    def test_hooks_present(self):
        for frag in ("openModelModal", "renderProvList", "selectProvider",
                     "saveModel", "closeModelModal", "/api/chat/providers"):
            self.assertIn(frag, self.html, "缺少前端钩子：%s" % frag)

    def test_settings_has_model_section(self):
        self.assertIn('id="btnAddModel"', self.html)
        self.assertIn('id="modelList"', self.html)

    def test_no_stale_inline_config_panel(self):
        """旧的行内配置面板已被弹窗取代，不应残留（避免两套入口并存）。"""
        self.assertNotIn('id="modelCfg"', self.html)
        self.assertNotIn('id="cfgSave"', self.html)


if __name__ == "__main__":
    unittest.main()
