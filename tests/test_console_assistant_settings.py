"""tests/test_console_assistant_settings.py —— 助理设置端点 + 前端结构。

覆盖：
  - GET /api/assistant/settings 返回目录 + 偏好（无需登录，只读）。
  - 写端点（config/toggle/test/prefs）无登录一律 401。
  - 前端 APP_HTML 含远程通道卡片骨架、上下文管理开关、弹窗钩子。
"""

import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import console_web  # noqa: E402
import assistant_channels as ac  # noqa: E402


class TestAssistantEndpoints(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        console_web.Handler.assistant_settings = ac.AssistantSettings(data_dir=cls.tmp)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), console_web.Handler)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        console_web.Handler.assistant_settings = None

    def _req(self, path, payload=None):
        data = json.dumps(payload or {}).encode() if payload is not None else None
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path),
                                     data=data,
                                     headers={"Content-Type": "application/json"},
                                     method="POST" if data else "GET")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode() or "{}")

    def test_get_settings_public(self):
        status, data = self._req("/api/assistant/settings")
        self.assertEqual(status, 200)
        self.assertEqual(len(data["channels"]), len(ac.CHANNELS))
        self.assertTrue(data["prefs"]["context_memory"])
        for c in data["channels"]:
            self.assertNotIn("webhook", c)  # 只给 webhook_masked
            self.assertNotIn("secret", c)

    def test_write_requires_login(self):
        for path in ("/api/assistant/channels/config",
                     "/api/assistant/channels/toggle",
                     "/api/assistant/channels/test",
                     "/api/assistant/prefs"):
            status, data = self._req(path, {"id": "wecom_bot", "webhook": "https://x.e.com"})
            self.assertEqual(status, 401, path)
            self.assertIn("登录", data.get("error", ""))


class TestFrontendScaffold(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = console_web.APP_HTML

    def test_channel_section_present(self):
        for frag in ("远程通道", 'id="chanGrid"', "上下文管理", 'id="ctxMem"'):
            self.assertIn(frag, self.html, "缺少助理设置元素：%s" % frag)

    def test_channel_modal_present(self):
        for frag in ('id="chanModal"', 'id="chanBody"', 'id="chanTest"',
                     'id="chanSave"', "openChan", "saveChan", "testChan", "toggleCtx"):
            self.assertIn(frag, self.html)

    def test_api_hooks(self):
        for frag in ("/api/assistant/settings", "/api/assistant/channels/config",
                     "/api/assistant/channels/test", "/api/assistant/prefs"):
            self.assertIn(frag, self.html)


if __name__ == "__main__":
    unittest.main()
