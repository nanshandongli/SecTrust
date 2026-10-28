"""tests/test_notify.py —— 事件推送引擎（notify.py）。

覆盖：
  - 事件目录完整性 + 订阅默认值（默认全开，用户可显式关）。
  - 订阅持久化 / 未知通道与未知事件被忽略。
  - 推送成功、失败、失败不阻塞（notify 永不抛异常）。
  - 去重窗口：同 payload 不重复推。
  - 审批事件回调（requested/decided/expired）→ 正确的 event 类型。
  - 任务状态映射：只有终态才推。
  - 入站指令解析：认得出 / 认不出（不猜不执行）。
  - 入站令牌校验（拒绝空令牌 / 错误令牌）。
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import assistant_channels as ac  # noqa: E402
import notify as nt  # noqa: E402


class _FakeSettings:
    """替身：只记录 send() 调用，不发真实 HTTP。"""

    def __init__(self, ok=True, error=None):
        self.calls = []
        self.ok = ok
        self.error = error
        self._data_dir = "data"

    def get_config(self, cid):
        return {"webhook": "https://example.invalid/hook", "enabled": True,
                "secret": ""}

    def send(self, cid, title, text, webhook="", secret=""):
        self.calls.append({"channel": cid, "title": title, "text": text})
        if self.ok:
            return {"ok": True, "detail": {"code": 0}}
        return {"ok": False, "error": self.error or "boom"}


def _engine(settings=None, **kw):
    d = tempfile.mkdtemp(prefix="notify_test_")
    settings = settings or _FakeSettings()
    eng = nt.NotifyEngine(settings,
                          path=os.path.join(d, "subs.json"),
                          log_path=os.path.join(d, "log.jsonl"),
                          **kw)
    return eng, settings, d


class TestEventCatalog(unittest.TestCase):
    def test_catalog_has_core_events(self):
        ids = {e["id"] for e in nt.event_catalog()}
        for must in ("approval.pending", "approval.decided",
                     "security.high_risk", "task.completed", "task.failed"):
            self.assertIn(must, ids)

    def test_catalog_entries_have_labels(self):
        for e in nt.event_catalog():
            self.assertTrue(e["label"])
            self.assertTrue(e["desc"])
            self.assertIn(e["severity"], ("info", "warn", "high", "error"))

    def test_event_order_matches_catalog(self):
        self.assertEqual(nt.EVENT_ORDER, [e["id"] for e in nt.event_catalog()])


class TestSubscriptions(unittest.TestCase):
    def test_default_subscribes_all_events(self):
        eng, _s, _d = _engine()
        subs = eng.subscriptions()
        self.assertEqual(set(subs.keys()), set(ac.CHANNEL_MAP.keys()))
        for evs in subs.values():
            self.assertEqual(set(evs), set(nt.EVENT_TYPES.keys()))

    def test_set_subscriptions_ignores_unknown(self):
        eng, _s, d = _engine()
        out = eng.set_subscriptions({"feishu_bot": ["approval.decided", "nope"],
                                     "not_a_channel": ["approval.decided"]})
        self.assertEqual(out, {"feishu_bot": ["approval.decided"]})
        # 持久化
        eng2, _s2, _d2 = _engine()
        eng2._subs = out
        eng2._save()
        self.assertTrue(os.path.exists(os.path.join(d, "subs.json")))

    def test_set_subscription_validates_channel(self):
        eng, _s, _d = _engine()
        with self.assertRaises(ValueError):
            eng.set_subscription("nope", ["approval.decided"])

    def test_reload_preserves_subscriptions(self):
        eng, _s, d = _engine()
        eng.set_subscriptions({"feishu_bot": ["task.completed"]})
        eng2 = nt.NotifyEngine(_FakeSettings(),
                                path=os.path.join(d, "subs.json"),
                                log_path=os.path.join(d, "log.jsonl"))
        self.assertEqual(eng2.subscriptions(), {"feishu_bot": ["task.completed"]})


class TestNotify(unittest.TestCase):
    def test_pushes_to_subscribed_enabled_channels(self):
        eng, settings, _d = _engine()
        eng.set_subscriptions({"feishu_bot": ["approval.decided"]})
        res = eng.notify("approval.decided", "标题", "正文")
        self.assertTrue(res["ok"])
        self.assertEqual(len(settings.calls), 1)
        self.assertEqual(settings.calls[0]["channel"], "feishu_bot")

    def test_unsubscribed_event_not_pushed(self):
        eng, settings, _d = _engine()
        eng.set_subscriptions({"feishu_bot": ["task.completed"]})
        res = eng.notify("approval.decided", "t", "b")
        self.assertTrue(res["ok"])
        self.assertEqual(res["skipped"], ["no_target"])
        self.assertEqual(settings.calls, [])

    def test_failure_does_not_raise_and_is_reported(self):
        eng, settings, _d = _engine(_FakeSettings(ok=False, error="平台拒绝"))
        eng.set_subscriptions({"feishu_bot": ["approval.decided"]})
        res = eng.notify("approval.decided", "t", "b")
        self.assertFalse(res["ok"])
        self.assertFalse(res["delivered"][0]["ok"])
        self.assertIn("平台拒绝", res["delivered"][0]["error"])

    def test_send_exception_is_swallowed(self):
        class Boom(_FakeSettings):
            def send(self, *a, **k):
                raise RuntimeError("net down")
        eng, _s, _d = _engine(Boom())
        eng.set_subscriptions({"feishu_bot": ["approval.decided"]})
        res = eng.notify("approval.decided", "t", "b")   # 不抛
        self.assertFalse(res["ok"])
        self.assertIn("推送异常", res["delivered"][0]["error"])

    def test_unknown_event_rejected(self):
        eng, _s, _d = _engine()
        res = eng.notify("no.such.event", "t", "b")
        self.assertFalse(res["ok"])
        self.assertIn("unknown_event", res["skipped"])

    def test_dedupe_window(self):
        eng, settings, _d = _engine()
        eng.set_subscriptions({"feishu_bot": ["approval.decided"]})
        eng.notify("approval.decided", "t", "b", dedupe_key="K")
        res = eng.notify("approval.decided", "t", "b", dedupe_key="K")
        self.assertEqual(res["skipped"], ["duplicate"])
        self.assertEqual(len(settings.calls), 1)

    def test_dedupe_key_is_stable_across_processes(self):
        """去重键不能用内置 hash()（每进程随机盐）。"""
        k1 = nt._digest_key("e", "t", "b", "")
        k2 = nt._digest_key("e", "t", "b", "")
        self.assertEqual(k1, k2)

    def test_disabled_channel_not_targeted(self):
        class Off(_FakeSettings):
            def get_config(self, cid):
                return {"webhook": "https://x.invalid", "enabled": False}
        eng, settings, _d = _engine(Off())
        eng.set_subscriptions({"feishu_bot": ["approval.decided"]})
        res = eng.notify("approval.decided", "t", "b")
        self.assertEqual(res["skipped"], ["no_target"])
        self.assertEqual(settings.calls, [])

    def test_log_records_delivery(self):
        eng, _s, d = _engine()
        eng.set_subscriptions({"feishu_bot": ["approval.decided"]})
        eng.notify("approval.decided", "标题", "正文")
        rec = eng.recent(5)
        self.assertEqual(len(rec), 1)
        self.assertEqual(rec[0]["channel"], "feishu_bot")
        self.assertTrue(rec[0]["ok"])


class TestApprovalCallback(unittest.TestCase):
    def test_requested_maps_to_pending(self):
        eng, settings, _d = _engine()
        eng.set_subscriptions({"feishu_bot": ["approval.pending"]})
        eng.on_approval_event("requested", {"approval_id": "ap-1",
                                             "action": "publish",
                                             "detail": "上线"})
        self.assertEqual(len(settings.calls), 1)
        self.assertIn("ap-1", settings.calls[0]["text"])

    def test_decided_maps_to_decided(self):
        eng, settings, _d = _engine()
        eng.set_subscriptions({"feishu_bot": ["approval.decided"]})
        eng.on_approval_event("decided", {"approval_id": "ap-2",
                                          "decision": "approve", "by": "u1"})
        self.assertIn("已批准", settings.calls[0]["title"])

    def test_callback_never_raises(self):
        eng, _s, _d = _engine()
        eng.on_approval_event("requested", {"approval_id": "ap-3"})
        eng.on_approval_event("decided", {"approval_id": "ap-3"})
        eng.on_approval_event("expired", {"approval_id": "ap-3"})

    def test_expired_maps_to_decided_event(self):
        eng, settings, _d = _engine()
        eng.set_subscriptions({"feishu_bot": ["approval.decided"]})
        eng.on_approval_event("expired", {"approval_id": "ap-4"})
        self.assertEqual(len(settings.calls), 1)


class TestTaskEvent(unittest.TestCase):
    def test_terminal_states_map(self):
        s = _FakeSettings()
        d = tempfile.mkdtemp()
        eng = nt.NotifyEngine(s, path=os.path.join(d, "s.json"),
                              log_path=os.path.join(d, "l.jsonl"))
        eng.set_subscriptions({"feishu_bot": ["task.completed", "task.failed"]})
        nt.push_task_event("done", "t1", engine=eng)
        nt.push_task_event("failed", "t2", engine=eng)
        self.assertEqual(len(s.calls), 2)
        titles = " ".join(c["title"] for c in s.calls)
        self.assertIn("t1", titles)
        self.assertIn("t2", titles)

    def test_event_type_mapping(self):
        self.assertEqual(nt.task_event_type("done"), "task.completed")
        self.assertEqual(nt.task_event_type("completed"), "task.completed")
        self.assertEqual(nt.task_event_type("failed"), "task.failed")
        self.assertEqual(nt.task_event_type("needs_human"), "task.failed")
        self.assertEqual(nt.task_event_type("running"), "")
        self.assertEqual(nt.task_event_type("registered"), "")
        self.assertEqual(nt.task_event_type(""), "")

    def test_non_terminal_not_pushed(self):
        s = _FakeSettings()
        d = tempfile.mkdtemp()
        eng = nt.NotifyEngine(s, path=os.path.join(d, "s.json"),
                              log_path=os.path.join(d, "l.jsonl"))
        eng.set_subscriptions({"feishu_bot": ["task.completed", "task.failed"]})
        res = nt.push_task_event("running", "t1", engine=eng)
        self.assertEqual(res["skipped"], ["not_terminal"])
        self.assertEqual(s.calls, [])


class TestInboundCommand(unittest.TestCase):
    def test_parse_approve(self):
        cmd = nt.parse_command("/approve ap-abc")
        self.assertEqual(cmd, {"command": "approve", "arg": "ap-abc"})

    def test_parse_without_slash(self):
        cmd = nt.parse_command("reject ap-xyz")
        self.assertEqual(cmd["command"], "reject")
        self.assertEqual(cmd["arg"], "ap-xyz")

    def test_parse_pending(self):
        cmd = nt.parse_command("/pending")
        self.assertEqual(cmd["command"], "pending")
        self.assertEqual(cmd["arg"], "")

    def test_unrecognized_returns_none(self):
        self.assertIsNone(nt.parse_command("今天天气不错"))
        self.assertIsNone(nt.parse_command(""))
        self.assertIsNone(nt.parse_command("/unknowncmd"))

    def test_help_text_mentions_commands(self):
        h = nt.help_text()
        for c in ("approve", "reject", "pending"):
            self.assertIn(c, h)


class TestInboundToken(unittest.TestCase):
    def test_token_stable_and_verifies(self):
        d = tempfile.mkdtemp()
        t1 = nt.ensure_inbound_token(d)
        t2 = nt.ensure_inbound_token(d)
        self.assertEqual(t1, t2)
        self.assertTrue(nt.check_inbound_token(d, t1))

    def test_wrong_or_empty_token_rejected(self):
        d = tempfile.mkdtemp()
        nt.ensure_inbound_token(d)
        self.assertFalse(nt.check_inbound_token(d, ""))
        self.assertFalse(nt.check_inbound_token(d, "wrong"))


if __name__ == "__main__":
    unittest.main()
