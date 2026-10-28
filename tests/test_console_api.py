"""
tests/test_console_api.py —— 协作审计台「工作台」后端 API 层测试

覆盖：
  1. 会话：登录/校验/过期/登出；角色与用户名校验；**诚实边界声明必须存在**。
  2. 审计名单：岗位 × 在岗状态推导；无会话时全是待命。
  3. 助理：内置专家 + 已装技能合成；缺技能要标未安装（不谎报就绪）。
  4. 新建任务：真写入 registry + 留痕；校验拒绝（空标题/坏角色/坏依赖）。
  5. 资料库索引：产物条目 + 素材统计；空目录不崩。
  6. 前端契约：console_web.py 必须真的接了全部新端点与面板（防"壳化"回归）。
"""

import json
import os
import tempfile
import unittest

import console_api as A


# --------------------------------------------------------------------------
# 1) 会话
# --------------------------------------------------------------------------
class TestSessionStore(unittest.TestCase):

    def _store(self):
        import tempfile
        return A.SessionStore(data_dir=tempfile.mkdtemp())

    def test_login_returns_jwt_and_notice(self):
        s = self._store()
        r = s.login("operator", "auditor")
        # 真实可校验 JWT：三段式（header.payload.signature）
        self.assertEqual(r["token"].count("."), 2)
        self.assertEqual(r["token_type"], "JWT")
        self.assertEqual(r["user"], "operator")
        self.assertEqual(r["role"], "auditor")
        # 诚实边界：必须明说无密码校验、且不构成对抗第三方的认证边界
        self.assertIn("无密码校验", r["notice"])
        self.assertIn("认证边界", r["notice"])

    def test_verify_roundtrip(self):
        s = self._store()
        tok = s.login("alice", "writer")["token"]
        sess = s.verify(tok)
        self.assertIsNotNone(sess)
        self.assertEqual(sess["user"], "alice")
        self.assertEqual(sess["role"], "writer")
        self.assertEqual(sess["scope"], "console:writer")

    def test_verify_rejects_bad_token(self):
        s = self._store()
        self.assertIsNone(s.verify("nope"))
        self.assertIsNone(s.verify(""))
        self.assertIsNone(s.verify("a.b.c"))  # 三段但签名错 / 非 JWT

    def test_verify_rejects_tampered_payload(self):
        s = self._store()
        tok = s.login("alice", "writer")["token"]
        h, p, sig = tok.split(".")
        import base64, json
        pad = "=" * (-len(p) % 4)
        payload = json.loads(base64.urlsafe_b64decode(p + pad))
        payload["sub"] = "mallory"  # 篡改主体
        np_ = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
        self.assertIsNone(s.verify(h + "." + np_ + "." + sig))

    def test_logout_invalidates(self):
        s = self._store()
        tok = s.login("bob", "designer")["token"]
        self.assertTrue(s.logout(tok))
        self.assertIsNone(s.verify(tok))

    def test_expired_session_rejected(self):
        s = self._store()
        s.ttl = -1  # 过期令牌：exp == now-1 < now，立即被拒
        tok = s.login("carol", "analyst")["token"]
        self.assertIsNone(s.verify(tok))

    def test_invalid_user_rejected(self):
        s = A.SessionStore()
        with self.assertRaises(ValueError):
            s.login("bad user!", "auditor")

    def test_invalid_role_rejected(self):
        s = A.SessionStore()
        with self.assertRaises(ValueError):
            s.login("ok", "no-such-role")

    def test_default_user_when_empty(self):
        s = A.SessionStore()
        self.assertEqual(s.login("")["user"], "operator")

    def test_list_users_tracks_on_duty(self):
        s = A.SessionStore()
        s.login("dave", "publisher")
        users = s.list_users()
        self.assertEqual(len(users), 1)
        self.assertEqual(users[0]["user"], "dave")
        self.assertEqual(users[0]["role"], "publisher")


# --------------------------------------------------------------------------
# 2) 审计名单
# --------------------------------------------------------------------------
class TestAuditRoster(unittest.TestCase):

    def test_all_roles_listed(self):
        d = A.audit_roster(A.ROLES, [], [])
        self.assertEqual(d["counts"]["roles"], len(A.ROLES))
        self.assertEqual(d["counts"]["on_duty"], 0)
        self.assertEqual(d["counts"]["standby"], len(A.ROLES))

    def test_on_duty_derived_from_sessions(self):
        sessions = [{"user": "a", "role": "auditor"},
                    {"user": "b", "role": "auditor"}]
        d = A.audit_roster(A.ROLES, sessions, [])
        auditor = [e for e in d["entries"] if e["id"] == "auditor"][0]
        self.assertEqual(auditor["on_duty"], 2)
        self.assertEqual(auditor["status"], "on_duty")
        self.assertEqual(sorted(auditor["on_duty_users"]), ["a", "b"])

    def test_installed_skills_matched_against_installed_set(self):
        skills = [{"slug": "writing-plans", "name": "writing-plans"}]
        d = A.audit_roster(A.ROLES, [], skills)
        writer = [e for e in d["entries"] if e["id"] == "writer"][0]
        self.assertIn("writing-plans", writer["skills_installed"])
        # 未安装的不能算已装
        designer = [e for e in d["entries"] if e["id"] == "designer"][0]
        self.assertEqual(designer["skills_installed"], [])


# --------------------------------------------------------------------------
# 3) 助理
# --------------------------------------------------------------------------
class TestAssistants(unittest.TestCase):

    def test_builtin_experts_present(self):
        d = A.assistants([], [])
        experts = [a for a in d["assistants"] if a["kind"] == "内置专家"]
        self.assertEqual(len(experts), len(A.BUILTIN_EXPERTS))

    def test_expert_with_missing_skill_marked_not_installed(self):
        d = A.assistants([], [])  # 什么都没装
        exp = [a for a in d["assistants"] if a["id"] == "exp-auditor"][0]
        self.assertFalse(exp["installed"], "缺技能不能谎报就绪")

    def test_expert_installed_when_skills_present(self):
        skills = [{"slug": "systematic-debugging", "name": "systematic-debugging",
                   "description": "d"}]
        d = A.assistants(skills, [])
        exp = [a for a in d["assistants"] if a["id"] == "exp-auditor"][0]
        self.assertTrue(exp["installed"])

    def test_installed_skill_becomes_assistant(self):
        skills = [{"slug": "foo", "name": "foo", "description": "x",
                   "source": "skillhub"}]
        d = A.assistants(skills, [])
        self.assertTrue(any(a["id"] == "skill-foo" for a in d["assistants"]))
        self.assertEqual(d["counts"]["skills"], 1)


# --------------------------------------------------------------------------
# 4) 新建任务（写操作）
# --------------------------------------------------------------------------
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
    def __init__(self):
        self.traces = []
        self.events = []

    def start_trace(self, title=None, kind=None, started_by=None):
        tid = "tr_%d" % len(self.traces)
        self.traces.append({"title": title, "kind": kind, "started_by": started_by})
        return tid

    def record(self, tid, kind, **kw):
        self.events.append((tid, kind, kw))

    def finish(self, tid, status=None):
        pass


class TestCreateTask(unittest.TestCase):

    def setUp(self):
        self.reg = _FakeRegistry()
        self.tr = _FakeTracer()

    def test_creates_task_with_payload(self):
        r = A.create_task(self.reg, self.tr, "生成主图", "designer",
                          template="promo", priority="high", operator="alice")
        self.assertTrue(r["ok"])
        self.assertIn(r["task_id"], self.reg.tasks)
        t = self.reg.tasks[r["task_id"]]
        self.assertEqual(t["payload"]["title"], "生成主图")
        self.assertEqual(t["payload"]["role"], "designer")
        self.assertEqual(t["payload"]["created_by"], "alice")
        self.assertEqual(t["status"], "registered")

    def test_records_trace_event(self):
        r = A.create_task(self.reg, self.tr, "t", "writer", operator="bob")
        self.assertIsNotNone(r["trace_id"])
        self.assertEqual(len(self.tr.events), 1)
        tid, kind, kw = self.tr.events[0]
        self.assertEqual(kind, "task_created")
        self.assertEqual(kw["actor"], "bob")
        self.assertEqual(kw["task_id"], r["task_id"])

    def test_empty_title_rejected(self):
        r = A.create_task(self.reg, self.tr, "   ", "designer")
        self.assertFalse(r["ok"])
        self.assertIn("标题", r["error"])
        self.assertEqual(len(self.reg.tasks), 0)

    def test_too_long_title_rejected(self):
        r = A.create_task(self.reg, self.tr, "x" * 121, "designer")
        self.assertFalse(r["ok"])

    def test_unknown_role_rejected(self):
        r = A.create_task(self.reg, self.tr, "t", "not-a-role")
        self.assertFalse(r["ok"])
        self.assertIn("角色", r["error"])

    def test_missing_dependency_rejected(self):
        r = A.create_task(self.reg, self.tr, "t", "designer",
                          depends_on=["task-nope"])
        self.assertFalse(r["ok"])
        self.assertIn("依赖任务不存在", r["error"])
        self.assertEqual(len(self.reg.tasks), 0, "被拒时不应留下半成品任务")

    def test_valid_dependency_accepted(self):
        self.reg.register_task("task-up", {"title": "up"})
        r = A.create_task(self.reg, self.tr, "t", "designer",
                          depends_on=["task-up"])
        self.assertTrue(r["ok"])
        self.assertEqual(self.reg.tasks[r["task_id"]]["depends_on"], ["task-up"])

    def test_tracer_failure_does_not_break_creation(self):
        class BrokenTracer:
            def start_trace(self, **kw):
                raise RuntimeError("boom")
        r = A.create_task(self.reg, BrokenTracer(), "t", "designer")
        self.assertTrue(r["ok"], "轨迹失败不应回滚任务创建")
        self.assertIsNone(r["trace_id"])


# --------------------------------------------------------------------------
# 5) 资料库
# --------------------------------------------------------------------------
class TestLibraryIndex(unittest.TestCase):

    def test_indexes_files(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "docs"), exist_ok=True)
            with open(os.path.join(d, "docs", "a.md"), "w", encoding="utf-8") as f:
                f.write("# hello")
            idx = A.library_index(d)
            names = [e["name"] for e in idx["entries"]]
            self.assertIn("a.md", names)
            e = [x for x in idx["entries"] if x["name"] == "a.md"][0]
            self.assertEqual(e["kind"], "文档")
            self.assertTrue(e["path"].startswith("docs/"))

    def test_empty_root_does_not_crash(self):
        with tempfile.TemporaryDirectory() as d:
            idx = A.library_index(d)
            self.assertEqual(idx["entries"], [])
            self.assertIn("counts", idx)

    def test_non_document_files_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "docs"), exist_ok=True)
            with open(os.path.join(d, "docs", "x.bin"), "wb") as f:
                f.write(b"\x00")
            idx = A.library_index(d)
            self.assertEqual([e for e in idx["entries"] if e["name"] == "x.bin"], [])


# --------------------------------------------------------------------------
# 6) 前端契约（防壳化回归）
# --------------------------------------------------------------------------
class TestFrontendWiring(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        here = os.path.dirname(os.path.abspath(__file__))
        cls.src = open(os.path.join(here, "..", "console_web.py"),
                        encoding="utf-8").read()

    def test_login_page_exists(self):
        self.assertIn('id="loginWrap"', self.src)
        self.assertIn('id="loginUser"', self.src)
        self.assertIn('id="loginRole"', self.src)
        self.assertIn('id="loginBtn"', self.src)

    def test_login_endpoints_wired(self):
        self.assertIn("/api/login", self.src)
        self.assertIn("/api/logout", self.src)
        self.assertIn("/api/session", self.src)

    def test_all_new_panels_have_containers(self):
        for vid in ("view-workspace", "view-newtask", "view-roster",
                    "view-assistants", "view-connectors", "view-schedule",
                    "view-library"):
            self.assertIn('id="%s"' % vid, self.src)

    def test_all_new_endpoints_wired(self):
        for ep in ("/api/roster", "/api/assistants", "/api/connectors",
                   "/api/schedule", "/api/library", "/api/tasks/new",
                   "/api/connectors/skillhub/search",
                   "/api/connectors/skillhub/install"):
            self.assertIn(ep, self.src)

    def test_nav_has_four_groups(self):
        for g in ("工作台", "审计", "资源", "系统"):
            self.assertIn("group:'%s'" % g, self.src)

    def test_new_task_form_fields(self):
        for fid in ("ntTitle", "ntRole", "ntTemplate", "ntPriority",
                    "ntDeps", "ntNote"):
            self.assertIn('id="%s"' % fid, self.src)

    def test_logout_button_bound(self):
        self.assertIn("doLogout", self.src)
        self.assertIn("logoutBtn", self.src)

    def test_session_restored_on_start(self):
        self.assertIn("restoreSession", self.src)
        self.assertIn("wbSession", self.src)

    def test_roles_injected_to_frontend(self):
        self.assertIn("__ROLES__", self.src)

    def test_no_external_cdn(self):
        for bad in ("http://cdn", "https://cdn", "unpkg.com"):
            self.assertNotIn(bad, self.src)

    def test_no_hardcoded_secret(self):
        self.assertNotRegex(self.src, r"(?i)api_key\s*=\s*['\"][A-Za-z0-9]{16,}")
        self.assertNotRegex(self.src, r"(?i)password\s*=\s*['\"][A-Za-z0-9]{8,}")

    def test_mobile_route_wired(self):
        # 手机远程控制台：/mobile 路由必须真实接上
        self.assertIn('"/mobile"', self.src)
        self.assertIn("_serve_mobile", self.src)
        # 审批决策写接口（移动端「通过/拒绝」依赖它）
        self.assertIn('"/api/approvals/decide"', self.src)

    def test_approval_action_buttons_wired(self):
        # 审批中心必须提供「通过/驳回」可视化按钮，真正调用决策端点
        # （之前只靠审批中心 API，UI 入口缺一步）
        self.assertIn("function decideApproval", self.src)
        self.assertIn('decideApproval(', self.src)
        self.assertIn('"/api/approvals/decide"', self.src)
        # 通过/驳回按钮的样式类（视觉可区分）
        self.assertIn('class="btn ok"', self.src)
        self.assertIn('class="btn bad"', self.src)
        # 审批表新增「操作」列，且只有 pending 行可操作
        self.assertIn("操作", self.src)
        self.assertIn("x.status==='pending'", self.src)
        # 轻量 toast 反馈（不依赖外部库）
        self.assertIn("function toast", self.src)


class TestMobileConsolePage(unittest.TestCase):
    """移动端页面（独立 SPA）的契约：结构与接口接线不能壳化。"""

    @classmethod
    def setUpClass(cls):
        here = os.path.dirname(os.path.abspath(__file__))
        cls.path = os.path.join(here, "..", "mobile_console.html")
        with open(cls.path, encoding="utf-8") as f:
            cls.html = f.read()

    def test_login_form_present(self):
        for frag in ('id="login"', 'id="login-user"', 'id="login-role"', 'id="login-btn"'):
            self.assertIn(frag, self.html)

    def test_five_tabs_present(self):
        for label in ("概览", "对话", "任务", "审批", "助理"):
            self.assertIn(label, self.html)

    def test_view_containers_present(self):
        for frag in ('id="view"', 'id="chat-log"', 'id="task-list"',
                     'id="ap-list"', 'id="as-content"'):
            self.assertIn(frag, self.html)

    def test_api_paths_wired(self):
        for ep in ("/api/login", "/api/overview", "/api/chat", "/api/chat/models",
                   "/api/tasks/new", "/api/approvals/decide",
                   "/api/assistant/settings", "/api/assistant/channels/toggle",
                   "/api/assistant/prefs"):
            self.assertIn(ep, self.html)

    def test_no_external_cdn(self):
        for bad in ("http://cdn", "https://cdn", "unpkg.com", "cdn.jsdelivr"):
            self.assertNotIn(bad, self.html)


if __name__ == "__main__":
    unittest.main()
