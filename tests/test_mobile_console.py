"""
test_mobile_console.py —— 手机远程控制台后端验证

覆盖：
  1. GET /mobile 真正返回移动端页面（200 + 含登录表单）。
  2. POST /api/approvals/decide 需要登录（401）。
  3. /api/approvals/decide 决策成功路径（登录后 approve → 状态变更）。
  4. 非法 decision（400）与未知审批（409）的边界。
"""
import json
import os
import threading
import unittest
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer

import console_web
from approval import ApprovalCenter
import console_api


class TestMobileConsole(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")
        os.makedirs(cls.tmp, exist_ok=True)
        cls.ap_db = os.path.join(cls.tmp, "_mobile_test_approval.db")
        if os.path.exists(cls.ap_db):
            os.remove(cls.ap_db)
        console_web.Handler.approval = ApprovalCenter(cls.ap_db, tracer=None)
        console_web.Handler.sessions = console_api.SessionStore()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), console_web.Handler)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        console_web.Handler.approval = None
        console_web.Handler.sessions = None
        try:
            os.remove(cls.ap_db)
        except OSError:
            pass

    def _get(self, path):
        url = "http://127.0.0.1:%d%s" % (self.port, path)
        with urllib.request.urlopen(url, timeout=10) as r:
            return r.status, r.read().decode("utf-8")

    def _post(self, path, payload, token=None):
        req = urllib.request.Request(
            "http://127.0.0.1:%d%s" % (self.port, path),
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        if token:
            req.add_header("X-Session-Token", token)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8"))

    def _login(self):
        status, data = self._post("/api/login", {"user": "mobile-tester", "role": "auditor"})
        self.assertEqual(status, 200)
        self.assertIn("token", data)
        return data["token"]

    def test_mobile_page_served(self):
        status, body = self._get("/mobile")
        self.assertEqual(status, 200)
        self.assertIn("协作审计台", body)
        self.assertIn('id="login"', body)
        self.assertIn('id="view"', body)

    def test_decide_requires_login(self):
        # 未登录直接决策 → 401
        status, data = self._post("/api/approvals/decide",
                                 {"approval_id": "x", "decision": "approve"})
        self.assertEqual(status, 401)
        self.assertIn("登录", data.get("error", ""))

    def test_decide_bad_decision(self):
        token = self._login()
        status, data = self._post("/api/approvals/decide",
                                 {"approval_id": "x", "decision": "maybe"}, token=token)
        self.assertEqual(status, 400)

    def test_decide_unknown_approval(self):
        token = self._login()
        status, data = self._post("/api/approvals/decide",
                                 {"approval_id": "no-such", "decision": "approve"}, token=token)
        self.assertEqual(status, 409)

    def test_decide_success(self):
        token = self._login()
        aid = console_web.Handler.approval.request(
            "mobile_test_rule", "冒烟测试：导出日志", task_id="mobile-task")
        self.assertIsNotNone(aid)
        self.assertEqual(console_web.Handler.approval.status(aid), "pending")
        status, data = self._post("/api/approvals/decide",
                                 {"approval_id": aid, "decision": "approve"}, token=token)
        self.assertEqual(status, 200)
        self.assertTrue(data.get("ok"))
        self.assertEqual(console_web.Handler.approval.status(aid), "approved")


if __name__ == "__main__":
    unittest.main()
