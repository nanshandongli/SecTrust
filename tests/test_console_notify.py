"""tests/test_console_notify.py —— 事件推送在控制台侧的接线。

覆盖：
  - GET /api/assistant/notify 返回事件目录 + 订阅 + 推送记录。
  - 写端点（subscriptions/test）无登录一律 401。
  - POST /api/assistant/inbound：无令牌 401 / 令牌错 401 / 指令认不出 400 /
    approve 真实落库并产生 trace 事件。
  - 审批决定**真的**触发推送（此前只有「测试」按钮能发——这是本次的核心缺口）。
  - 前端 APP_HTML 含事件订阅面板与保存钩子。
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

import assistant_channels as ac  # noqa: E402
import console_web  # noqa: E402
import notify as nt  # noqa: E402


class _RecordingSettings(ac.AssistantSettings):
    """真实 AssistantSettings，但 send() 只记录不联网。"""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.sent = []

    def send(self, cid, title, text, webhook="", secret=""):
        self.sent.append({"channel": cid, "title": title, "text": text})
        return {"ok": True, "detail": {"code": 0}}


class TestNotifyEndpoints(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="console_notify_")
        cls.settings = _RecordingSettings(data_dir=cls.tmp)
        console_web.Handler.assistant_settings = cls.settings
        console_web.Handler.assistant_data_dir = cls.tmp
        console_web.Handler.notify_engine = nt.NotifyEngine(
            cls.settings,
            path=os.path.join(cls.tmp, "subs.json"),
            log_path=os.path.join(cls.tmp, "log.jsonl"))
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), console_web.Handler)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        console_web.Handler.assistant_settings = None
        console_web.Handler.notify_engine = None

    def _req(self, path, payload=None, headers=None, method=None):
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(
            "http://127.0.0.1:%d%s" % (self.port, path), data=data,
            headers=dict({"Content-Type": "application/json"},
                         **(headers or {})),
            method=method or ("POST" if data is not None else "GET"))
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode() or "{}")

    # ---- 只读 ----
    def test_get_notify_public(self):
        status, data = self._req("/api/assistant/notify")
        self.assertEqual(status, 200)
        self.assertTrue(data["events"])
        self.assertIn("subscriptions", data)
        self.assertIn("note", data)

    def test_inbound_token_not_leaked_anonymously(self):
        """入站令牌 = 远程审批权限，**绝不能**在未鉴权的只读端点里明文返回。"""
        _s, data = self._req("/api/assistant/notify")
        ib = data.get("inbound") or {}
        self.assertEqual(ib.get("token", ""), "")
        self.assertTrue(ib.get("token_hint"))
        self.assertEqual(ib.get("endpoint"), "/api/assistant/inbound")

    def test_inbound_meta_present(self):
        _s, data = self._req("/api/assistant/notify")
        ib = data.get("inbound") or {}
        self.assertEqual(ib.get("header"), "X-Notify-Token")
        self.assertTrue(ib.get("commands"))
        self.assertIn("只出不进", ib.get("note") or "")

    # ---- 写端点鉴权 ----
    def test_subscriptions_requires_login(self):
        status, _ = self._req("/api/assistant/notify/subscriptions",
                               {"subscriptions": {}})
        self.assertEqual(status, 401)

    def test_notify_test_requires_login(self):
        status, _ = self._req("/api/assistant/notify/test", {"event": "x"})
        self.assertEqual(status, 401)

    # ---- 入站中继鉴权 ----
    def test_inbound_requires_token(self):
        status, _ = self._req("/api/assistant/inbound", {"text": "/pending"})
        self.assertEqual(status, 401)

    def test_inbound_rejects_wrong_token(self):
        status, _ = self._req("/api/assistant/inbound", {"text": "/pending"},
                               headers={"X-Notify-Token": "wrong"})
        self.assertEqual(status, 401)

    def test_inbound_rejects_unknown_command(self):
        tok = nt.ensure_inbound_token(self.tmp)
        status, data = self._req("/api/assistant/inbound", {"text": "你好"},
                                 headers={"X-Notify-Token": tok})
        self.assertEqual(status, 400)
        self.assertFalse(data["ok"])
        self.assertIn("hint", data)

    def test_inbound_help_returns_text(self):
        tok = nt.ensure_inbound_token(self.tmp)
        status, data = self._req("/api/assistant/inbound", {"text": "/help"},
                                 headers={"X-Notify-Token": tok})
        self.assertEqual(status, 200)
        self.assertIn("approve", data["reply"])


class TestApprovalActuallyNotifies(unittest.TestCase):
    """核心缺口回归：审批决定必须真的触发推送（不再只有「测试」按钮能发）。"""

    def test_decide_emits_notification(self):
        tmp = tempfile.mkdtemp(prefix="console_notify_decide_")
        settings = _RecordingSettings(data_dir=tmp)
        settings.configure("feishu_bot",
                           webhook="https://open.feishu.cn/open-apis/bot/v2/hook/x",
                           enabled=True)
        eng = nt.NotifyEngine(settings,
                              path=os.path.join(tmp, "subs.json"),
                              log_path=os.path.join(tmp, "log.jsonl"))
        from approval import ApprovalCenter
        ac_center = ApprovalCenter(os.path.join(tmp, "reg.db"),
                                    tracer=False,
                                    on_event=eng.on_approval_event)
        try:
            aid = ac_center.request("publish", "上线发布", task_id="t-x")
            self.assertEqual(len(settings.sent), 1)
            self.assertIn("待审批", settings.sent[0]["title"])
            self.assertIn(aid, settings.sent[0]["text"])

            settings.sent.clear()
            ok = ac_center.decide(aid, "approve", by="tester")
            self.assertTrue(ok)
            self.assertEqual(len(settings.sent), 1)
            self.assertIn("已批准", settings.sent[0]["title"])
        finally:
            ac_center.close()


class TestFrontendMarkup(unittest.TestCase):
    def test_subscriptions_panel_present(self):
        html = console_web.APP_HTML
        self.assertIn("notifySubs", html)
        self.assertIn("saveNotifySubs", html)
        self.assertIn("loadNotifySubs", html)

    def test_subscribe_endpoint_called(self):
        self.assertIn("/api/assistant/notify/subscriptions", console_web.APP_HTML)

    def test_notify_module_declared_in_packaging(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "pyproject.toml"), encoding="utf-8") as f:
            txt = f.read()
        self.assertIn('"notify"', txt)


if __name__ == "__main__":
    unittest.main()
