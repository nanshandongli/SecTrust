"""tests/test_console_connectors_ui.py —— 技能连接器画廊（WorkBuddy 式）测试。

覆盖：
  - SkillHub 搜索解析：兼容线上 {code,data:{skills:[...]}} 结构（含 category）。
  - skillhub_featured：真实调 API 的封装；不可达时如实 ok=False，不编造。
  - 自定义连接器：登记/校验/去重/移除/持久化（data/custom_connectors.json）。
  - _skills() 扫描自定义连接器目录（source=custom + origin）。
  - 写操作鉴权：install / custom / custom/delete 无登录 → 401。
  - 前端骨架：卡片网格、搜索框、自定义连接器弹窗、绿色对勾。
"""

import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import console_api  # noqa: E402
import console_web  # noqa: E402


class _FakeResp(object):
    def __init__(self, payload):
        self._buf = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._buf

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestSkillhubParsing(unittest.TestCase):
    def test_search_parses_skills_key(self):
        """线上返回结构是 data.skills（旧解析只认 list/items，会漏）。"""
        payload = {"code": 0, "data": {"skills": [
            {"slug": "sec-a", "name": "安全技能A",
             "description": "d" * 300, "author": "bob", "category": "security"},
            "bad-item",
        ]}}
        with mock.patch.object(console_api.urllib.request, "urlopen",
                               return_value=_FakeResp(payload)):
            r = console_api._skillhub_search("安全")
        self.assertTrue(r["ok"])
        self.assertEqual(len(r["results"]), 1)
        item = r["results"][0]
        self.assertEqual(item["slug"], "sec-a")
        self.assertEqual(item["category"], "security")
        self.assertLessEqual(len(item["desc"]), 200)

    def test_featured_honest_failure(self):
        with mock.patch.object(console_api.urllib.request, "urlopen",
                               side_effect=urllib.error.URLError("boom")):
            r = console_api.skillhub_featured()
        self.assertFalse(r["ok"])
        self.assertEqual(r["results"], [])
        self.assertIn("不可达", r["error"])

    def test_featured_passes_through(self):
        payload = {"data": {"skills": [{"slug": "x", "name": "X"}]}}
        with mock.patch.object(console_api.urllib.request, "urlopen",
                               return_value=_FakeResp(payload)):
            r = console_api.skillhub_featured()
        self.assertTrue(r["ok"])
        self.assertEqual(r["results"][0]["slug"], "x")


class TestCustomConnectors(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="conn_test_")
        self.skill_dir = os.path.join(self.root, "myskills")
        os.makedirs(self.skill_dir)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_add_requires_name_and_valid_dir(self):
        self.assertFalse(console_api.add_custom_connector(
            self.root, {"name": "", "path": self.skill_dir})["ok"])
        self.assertFalse(console_api.add_custom_connector(
            self.root, {"name": "x", "path": ""})["ok"])
        r = console_api.add_custom_connector(
            self.root, {"name": "x", "path": os.path.join(self.root, "nope")})
        self.assertFalse(r["ok"])
        self.assertIn("不存在", r["error"])

    def test_add_duplicate_rejected(self):
        self.assertTrue(console_api.add_custom_connector(
            self.root, {"name": "a", "path": self.skill_dir})["ok"])
        r = console_api.add_custom_connector(
            self.root, {"name": "a", "path": self.skill_dir})
        self.assertFalse(r["ok"])
        self.assertIn("已存在", r["error"])

    def test_roundtrip_and_remove(self):
        console_api.add_custom_connector(
            self.root, {"name": "a", "desc": "我的源", "path": self.skill_dir})
        store = console_api.list_custom_connectors(self.root)
        self.assertEqual(len(store), 1)
        self.assertEqual(store[0]["type"], "dir")
        self.assertTrue(console_api.remove_custom_connector(self.root, "a")["ok"])
        self.assertEqual(console_api.list_custom_connectors(self.root), [])
        self.assertFalse(console_api.remove_custom_connector(self.root, "a")["ok"])

    def test_connector_status_includes_custom(self):
        console_api.add_custom_connector(
            self.root, {"name": "a", "path": self.skill_dir})
        st = console_api.connector_status(
            os.path.join(self.root, "skills"), project_root=self.root)
        self.assertEqual(st["custom_connectors"][0]["name"], "a")


class TestSkillsScanCustom(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="conn_scan_")
        self.skill_dir = os.path.join(self.root, "myskills", "my-skill")
        os.makedirs(self.skill_dir)
        with open(os.path.join(self.skill_dir, "SKILL.md"), "w",
                  encoding="utf-8") as f:
            f.write("# 我的自定义技能\n正文")
        console_api.add_custom_connector(
            self.root, {"name": "我的源", "path": os.path.join(self.root, "myskills")})

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_custom_skills_scanned(self):
        h = console_web.Handler.__new__(console_web.Handler)
        h.skills_dir = os.path.join(self.root, "skills")
        os.makedirs(h.skills_dir, exist_ok=True)
        h.project_root = self.root
        skills = h._skills()
        custom = [s for s in skills if s.get("source") == "custom"]
        self.assertEqual(len(custom), 1)
        self.assertEqual(custom[0]["name"], "my-skill")
        self.assertEqual(custom[0]["origin"], "我的源")
        self.assertIn("我的自定义技能", custom[0]["description"])


class TestConnectorAuthAndFrontend(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), console_web.Handler)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

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

    def test_writes_require_login(self):
        for path, payload in (("/api/connectors/skillhub/install", {"slug": "x"}),
                              ("/api/connectors/custom", {"name": "x", "path": "/tmp"}),
                              ("/api/connectors/custom/delete", {"name": "x"})):
            status, data = self._post(path, payload)
            self.assertEqual(status, 401, "%s 未鉴权" % path)
            self.assertIn("登录", data.get("error", ""))

    def test_featured_endpoint_shape(self):
        url = "http://127.0.0.1:%d/api/connectors/skillhub/featured" % self.port
        with urllib.request.urlopen(url, timeout=20) as r:
            data = json.loads(r.read().decode("utf-8"))
        # 无论联网与否，结构必须是 ok+results；不可达时不伪造列表
        self.assertIn("ok", data)
        self.assertIn("results", data)

    def test_frontend_scaffold(self):
        html = console_web.APP_HTML
        for frag in ('id="connBuiltIn"', 'id="connInstalled"', 'id="connCustom"',
                     'id="connFeatured"', 'id="connResults"', 'id="connQ"',
                     'id="connModal"', 'id="ccName"', 'id="ccPath"',
                     "openConnCustom", "saveConnCustom", "removeConnCustom",
                     "connCard", "conn-check", "conn-plus",
                     "/api/connectors/skillhub/featured"):
            self.assertIn(frag, html, "缺少连接器画廊元素：%s" % frag)


if __name__ == "__main__":
    unittest.main()
