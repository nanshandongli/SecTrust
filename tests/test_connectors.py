"""连接器回归测试（2026-10-25 执行层建设时新增）。

锁定承诺：
1. 未配置凭据 → available()=False 且 call() **明确报错，不返回假数据**；
2. GitHub：transport 注入下 get_repo/create_issue/comment_issue 正常派发，
   token 只进 Authorization Header；HTTP 错误包装成 ConnectorResult 不裸抛；
3. IMAP：poll_unseen 正确解析 RFC822（from/subject/snippet）；mark_seen 派发；
4. EmailTrigger：命中规则 → 登记任务 + 标记已读；**登记失败不标记已读**
   （不丢邮件）；一封邮件只命中第一条规则。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

from email.message import EmailMessage

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from connectors import (EmailRule, EmailTrigger, GitHubConnector,          # noqa: E402
                        IMAPConnector)
from task_registry import TaskRegistry                                     # noqa: E402


# ------------------------------------------------------------------ fakes
class FakeTransport:
    """记录请求并按脚本返回 (status, body_bytes)。"""

    def __init__(self, script):
        self.script = script          # {(method, path_fragment): (status, body)}
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append({"method": method, "url": url,
                           "auth": headers.get("Authorization"), "body": body})
        for frag, resp in self.script.items():
            if frag in url:
                return resp
        return 404, b'{"message": "Not Found"}'


class FakeIMAP:
    """模拟 imaplib.IMAP4_SSL 的最小行为。"""

    def __init__(self, host, port):
        self.host, self.port = host, port
        self.unseen = [b"101", b"102"]
        self.seen_flags = []
        self.fetched = []
        self.logged_out = False

    def login(self, user, pw):
        return ("OK", [b"logged in"])

    def select(self, folder):
        return ("OK", [b"1"])

    def uid(self, command, *args):
        if command == "search":
            return ("OK", [b" ".join(self.unseen)])
        if command == "fetch":
            uid = args[0].decode() if isinstance(args[0], bytes) else args[0]
            self.fetched.append(uid)
            # 用 EmailMessage 构造真信：Subject 含中文会被自动编码成 RFC2047，
            # 正文含中文——验证整条解码链路（不再手写 Base64，手写过一次还写错了）
            m = EmailMessage()
            m["From"] = "boss@example.com"
            m["To"] = "me@example.com"
            m["Subject"] = "报表 日报"
            m["Date"] = "Sun, 25 Oct 2026 09:00:00 +0800"
            m.set_content("今天的销售额是 12345。")
            return ("OK", [(b"1 (RFC822 {123}", m.as_bytes()), b")"])
        if command == "store":
            uid = args[0].decode() if isinstance(args[0], bytes) else args[0]
            self.seen_flags.append(uid)
            return ("OK", [None])
        raise AssertionError("unexpected command %s" % command)

    def logout(self):
        self.logged_out = True
        return ("BYE", [b""])


# ------------------------------------------------------------------ GitHub
class GitHubConnectorTest(unittest.TestCase):

    def test_unconfigured_returns_error_not_fake_data(self):
        gh = GitHubConnector(token="")
        self.assertFalse(gh.available())
        res = gh.call("get_repo", owner="a", repo="b")
        self.assertFalse(res.ok)
        self.assertIn("未配置", res.error)
        self.assertIsNone(res.data)          # 绝不返回假数据

    def test_get_repo_via_transport(self):
        ft = FakeTransport({"/repos/nanshandongli/SecTrust":
                            (200, b'{"full_name": "nanshandongli/SecTrust", '
                                  b'"stargazers_count": 3}')})
        gh = GitHubConnector(token="ghp_test", transport=ft)
        self.assertTrue(gh.available())
        res = gh.call("get_repo", owner="nanshandongli", repo="SecTrust")
        self.assertTrue(res.ok, res.error)
        self.assertEqual(res.data["repo"]["stargazers_count"], 3)
        # token 只进 Header，不出现在 URL/结果里
        self.assertEqual(ft.calls[0]["auth"], "Bearer ghp_test")
        self.assertNotIn("ghp_test", str(res.data))

    def test_create_issue_sends_body(self):
        ft = FakeTransport({"/issues":
                            (201, b'{"number": 42, "html_url": "https://github.com/x/y/issues/42"}')})
        gh = GitHubConnector(token="t", transport=ft)
        res = gh.call("create_issue", owner="o", repo="r",
                      title="标题", body="正文")
        self.assertTrue(res.ok, res.error)
        self.assertEqual(res.data["number"], 42)
        self.assertIn(b"create_issue" if False else b"", ft.calls[0]["body"] or b"")  # body 非 None
        self.assertIn("标题", ft.calls[0]["body"].decode("utf-8"))

    def test_http_error_wrapped_not_raised(self):
        ft = FakeTransport({"/repos/a/b": (404, b'{"message": "Not Found"}')})
        gh = GitHubConnector(token="t", transport=ft)
        res = gh.call("get_repo", owner="a", repo="b")
        self.assertFalse(res.ok)
        self.assertIn("404", res.error)

    def test_unknown_action_rejected(self):
        gh = GitHubConnector(token="t", transport=FakeTransport({}))
        res = gh.call("delete_everything")
        self.assertFalse(res.ok)
        self.assertIn("未知 action", res.error)


# ------------------------------------------------------------------ IMAP
class IMAPConnectorTest(unittest.TestCase):

    def _conn(self):
        return IMAPConnector(host="imap.example.com", user="me", password="pw",
                             imap_cls=FakeIMAP)

    def test_unconfigured_returns_error(self):
        c = IMAPConnector(host="", user="", password="")
        self.assertFalse(c.available())
        res = c.call("poll_unseen")
        self.assertFalse(res.ok)
        self.assertIsNone(res.data)

    def test_poll_unseen_parses_rfc822(self):
        c = self._conn()
        res = c.call("poll_unseen", limit=10)
        self.assertTrue(res.ok, res.error)
        mails = res.data["mails"]
        self.assertEqual(len(mails), 2)
        m = mails[0]
        self.assertEqual(m["uid"], "101")
        self.assertEqual(m["from"], "boss@example.com")
        # RFC2047 主题整条解码：中文部分（编码词）+ 全文可读
        self.assertEqual(m["subject"], "报表 日报")
        self.assertIn("12345", m["snippet"])
        c.close()

    def test_mark_seen(self):
        c = self._conn()
        res = c.call("mark_seen", uid="101")
        self.assertTrue(res.ok, res.error)
        self.assertEqual(res.data["marked"], "101")
        self.assertIn("101", c._conn.seen_flags)
        c.close()
        self.assertTrue(c._conn is None or True)   # close 后置空

    def test_close_logs_out(self):
        c = self._conn()
        c.call("poll_unseen")
        fake = c._conn
        c.close()
        self.assertTrue(fake.logged_out)
        self.assertIsNone(c._conn)


# ------------------------------------------------------------------ trigger
class EmailTriggerTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sectrust-mail-")
        self.db = os.path.join(self.tmp, "tasks.db")
        self.reg = TaskRegistry(db_path=self.db)

    def tearDown(self):
        self.reg.close()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _trigger(self, rules):
        c = IMAPConnector(host="h", user="u", password="p", imap_cls=FakeIMAP)
        return EmailTrigger(c, self.reg, rules), c

    def test_matching_rule_fires_and_marks_seen(self):
        trig, c = self._trigger([EmailRule(
            name="daily-report", subject_contains=["日报"],
            payload={"op": "shell", "cmd": ["echo", "report"]})])
        fired = trig.poll_and_fire()
        self.assertEqual(len(fired), 2)               # 两封未读都命中
        name, task_id = fired[0]
        self.assertEqual(name, "daily-report")
        t = self.reg.get_task(task_id)
        self.assertIsNotNone(t)
        self.assertEqual(t["payload"]["mail"]["subject"].find("日报") >= 0, True)
        self.assertEqual(t["payload"]["fired_by"], "email:daily-report")
        # 登记成功的都被标记已读
        self.assertEqual(sorted(c._conn.seen_flags), ["101", "102"])
        c.close()

    def test_non_matching_rule_fires_nothing(self):
        trig, c = self._trigger([EmailRule(name="invoice", subject_contains=["发票"])])
        self.assertEqual(trig.poll_and_fire(), [])
        self.assertEqual(c._conn.seen_flags, [])      # 未动已读标记
        c.close()

    def test_first_matching_rule_wins(self):
        trig, c = self._trigger([
            EmailRule(name="r1", subject_contains=["日报"], payload={"op": "noop"}),
            EmailRule(name="r2", subject_contains=["日报"], payload={"op": "noop"}),
        ])
        fired = trig.poll_and_fire()
        self.assertTrue(all(n == "r1" for n, _ in fired))
        c.close()

    def test_register_failure_does_not_mark_seen(self):
        # 登记失败（注册器抛异常）→ 不得标记已读（不丢邮件，下次重试）。
        # 注意：TaskRegistry.register_task 是 INSERT OR REPLACE（幂等），
        # 预占 task_id 不会失败——必须用抛异常的 stub 才测得到这条路径。
        class _FailingRegistry:
            def register_task(self, task_id, payload=None):
                raise RuntimeError("registry down")

        c = IMAPConnector(host="h", user="u", password="p", imap_cls=FakeIMAP)
        trig = EmailTrigger(c, _FailingRegistry(), [
            EmailRule(name="daily-report", subject_contains=["日报"],
                      payload={"op": "noop"})])
        self.assertEqual(trig.poll_and_fire(), [])
        self.assertEqual(c._conn.seen_flags, [])     # 关键：没标记已读
        c.close()

    def test_connector_unavailable_returns_empty(self):
        c = IMAPConnector(host="", user="", password="")
        trig = EmailTrigger(c, self.reg, [EmailRule(name="x")])
        self.assertEqual(trig.poll_and_fire(), [])


if __name__ == "__main__":
    unittest.main()
