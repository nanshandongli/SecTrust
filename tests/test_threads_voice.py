"""会话线程 + 语音输出回归测试（2026-10-25 消息框架）。

锁定承诺：
1. 线程存储：create/ensure/append/history(limit+before 游标)/list(预览)/
   search/rename/delete(级联)；**往不存在的线程 append 明确报错**（不自动兜）；
2. 角色与内容校验；meta JSON 往返（含中文）；跨实例持久化；
3. 语音：合成落盘（原子写）、只回 URL 时透出不下载、合成失败返回 ok=False
   不抛栈、Mock 假音频明示 fake=True；
4. deliver 诚实分级：有 send_audio/send_file 的渠道直接发语音；
   纯文本渠道 → 文本兜底 + 文件路径，**不谎称已发语音**；
5. 连接纪律：close() 幂等（-W error::ResourceWarning 下零泄漏）。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import threads                                    # noqa: E402
import voice as voice_mod                         # noqa: E402
from channels import OutboundMessage              # noqa: E402
from models.multimodal import MediaResult         # noqa: E402
from threads import ThreadError, ThreadStore      # noqa: E402
from voice import VoiceSpeaker                    # noqa: E402


# ================= 会话线程 =================

class ThreadStoreTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sectrust-threads-")
        self.db = os.path.join(self.tmp, "threads.db")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = ThreadStore(db_path=self.db)
        self.addCleanup(self.store.close)

    def test_create_append_history_roundtrip(self):
        t = self.store.create_thread(title="双十一复盘", channel="wecom")
        self.store.append(t["thread_id"], "user", "第一句话")
        self.store.append(t["thread_id"], "assistant", "第一段回复")
        hist = self.store.history(t["thread_id"])
        self.assertEqual([m["role"] for m in hist], ["user", "assistant"])
        self.assertEqual([m["content"] for m in hist],
                         ["第一句话", "第一段回复"])
        self.assertTrue(all(m["created_at"] <= hist[i + 1]["created_at"]
                            for i, m in enumerate(hist[:-1])))

    def test_history_limit_and_before_cursor(self):
        t = self.store.create_thread()
        for i in range(5):
            self.store.append(t["thread_id"], "user", "m%d" % i)
            time.sleep(0.001)
        # 聊天语义：limit 取"最近 N 条"（正序呈现），不是最早 N 条
        page_new = self.store.history(t["thread_id"], limit=2)
        self.assertEqual([m["content"] for m in page_new], ["m3", "m4"])
        # before 游标：接着上一页最早一条往前翻
        page_old = self.store.history(t["thread_id"], limit=2,
                                      before=page_new[0]["created_at"])
        self.assertEqual([m["content"] for m in page_old], ["m1", "m2"])

    def test_list_threads_order_and_preview(self):
        a = self.store.create_thread(title="A")
        b = self.store.create_thread(title="B")
        self.store.append(a["thread_id"], "user", "A 的最后一句")
        time.sleep(0.001)
        self.store.append(b["thread_id"], "user", "B 的最后一句")
        lst = self.store.list_threads()
        self.assertEqual([t["thread_id"] for t in lst][:2],
                         [b["thread_id"], a["thread_id"]])   # 最近活跃在前
        self.assertEqual(lst[0]["preview"], "B 的最后一句")

    def test_search_keyword_and_escaping(self):
        t = self.store.create_thread()
        self.store.append(t["thread_id"], "user", "包含 100% 折扣 的信息")
        self.store.append(t["thread_id"], "assistant", "普通回复")
        self.assertEqual(len(self.store.search("折扣")), 1)
        self.assertEqual(len(self.store.search("100%")), 1)   # % 不当通配符
        self.assertEqual(len(self.store.search("不存在的词")), 0)
        self.assertEqual(self.store.search("  "), [])

    def test_delete_cascades_messages(self):
        t = self.store.create_thread()
        self.store.append(t["thread_id"], "user", "将被删除")
        self.assertTrue(self.store.delete_thread(t["thread_id"]))
        self.assertIsNone(self.store.get_thread(t["thread_id"]))
        self.assertEqual(self.store.history(t["thread_id"]), [])
        self.assertFalse(self.store.delete_thread(t["thread_id"]))  # 幂等

    def test_rename(self):
        t = self.store.create_thread(title="旧标题")
        self.assertTrue(self.store.rename_thread(t["thread_id"], "新标题"))
        self.assertEqual(self.store.get_thread(t["thread_id"])["title"], "新标题")
        self.assertFalse(self.store.rename_thread("th-nope", "x"))

    def test_append_to_missing_thread_raises(self):
        with self.assertRaises(ThreadError):
            self.store.append("th-nope", "user", " orphan ")

    def test_invalid_role_and_empty_content(self):
        t = self.store.create_thread()
        with self.assertRaises(ThreadError):
            self.store.append(t["thread_id"], "robot", "hi")
        with self.assertRaises(ThreadError):
            self.store.append(t["thread_id"], "user", "   ")

    def test_ensure_thread_returns_existing(self):
        t = self.store.create_thread(title="已有")
        self.store.append(t["thread_id"], "user", "一句话")
        got = self.store.ensure_thread(t["thread_id"])
        self.assertEqual(got["thread_id"], t["thread_id"])
        self.assertEqual(len(self.store.history(got["thread_id"])), 1)

    def test_meta_json_roundtrip_chinese(self):
        t = self.store.create_thread()
        meta = {"渠道": "企业微信", "tokens": 42}
        m = self.store.append(t["thread_id"], "assistant", "回复", meta=meta)
        self.assertEqual(m["meta"], meta)
        back = self.store.history(t["thread_id"])[0]
        self.assertEqual(back["meta"], meta)

    def test_persist_across_instances(self):
        t = self.store.create_thread(title="跨实例")
        self.store.append(t["thread_id"], "user", "持久化验证")
        s2 = ThreadStore(db_path=self.db)
        self.addCleanup(s2.close)
        self.assertEqual(len(s2.history(t["thread_id"])), 1)
        self.assertEqual(s2.stats()["threads"], 1)

    def test_close_idempotent(self):
        self.store.close()
        self.store.close()                       # 幂等不炸

    def test_cli_flow(self):
        rc = threads.main(["--db", self.db, "--new", "--title", "CLI线程"])
        self.assertEqual(rc, 0)
        t = self.store.list_threads()[0]
        rc = threads.main(["--db", self.db, "--thread", t["thread_id"],
                           "--append", "CLI 消息"])
        self.assertEqual(rc, 0)
        rc = threads.main(["--db", self.db, "--thread", t["thread_id"]])
        self.assertEqual(rc, 0)
        rc = threads.main(["--db", self.db, "--search", "CLI"])
        self.assertEqual(rc, 0)
        rc = threads.main(["--db", self.db, "--list"])
        self.assertEqual(rc, 0)


# ================= 语音输出 =================

class _FakeMedia:
    def __init__(self, result=None, exc=None):
        self.calls = []
        self._result = result
        self._exc = exc

    def generate(self, kind, prompt, **opts):
        self.calls.append((kind, prompt, opts))
        if self._exc:
            raise self._exc
        return self._result


class _FakeChannel:
    def __init__(self, audio=False, file_only=False, audio_exc=None):
        self.name = "fake"
        self.sent = []
        self._audio, self._file_only = audio, file_only
        self._audio_exc = audio_exc
        if audio:
            self.send_audio = self._send_audio
        if file_only:
            self.send_file = self._send_file

    def _send_audio(self, user_id, path, text=""):
        if self._audio_exc:
            raise self._audio_exc
        self.sent.append(("audio", user_id, path))

    def _send_file(self, user_id, path):
        self.sent.append(("file", user_id, path))

    def send(self, msg: OutboundMessage):
        self.sent.append(("text", msg.user_id, msg.text))


class VoiceSpeakerTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sectrust-voice-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _speaker(self, media):
        return VoiceSpeaker(media=media, out_dir=self.tmp)

    def test_speak_writes_fake_audio_and_flags_fake(self):
        media = _FakeMedia(MediaResult(
            kind="audio", provider="mock", model="mock-media",
            mime="audio/mpeg", data=b"ID3\x03\x00\x00\x00FAKE",
            meta={"fake": True}))
        res = self._speaker(media).speak("大家好")
        self.assertTrue(res["ok"])
        self.assertTrue(res["fake"])                # 假音频必须明示
        with open(res["path"], "rb") as f:
            self.assertEqual(f.read(), b"ID3\x03\x00\x00\x00FAKE")
        self.assertEqual(media.calls[0][0], "audio")

    def test_speak_url_only_passthrough(self):
        media = _FakeMedia(MediaResult(
            kind="audio", provider="x", mime="audio/mpeg",
            url="https://cdn.example/a.mp3"))
        res = self._speaker(media).speak("hi")
        self.assertTrue(res["ok"])
        self.assertEqual(res["url"], "https://cdn.example/a.mp3")
        self.assertNotIn("path", res)               # 不擅自下载

    def test_speak_failure_returns_error(self):
        media = _FakeMedia(exc=RuntimeError("网络超时"))
        res = self._speaker(media).speak("hi")
        self.assertFalse(res["ok"])
        self.assertIn("网络超时", res["error"])

    def test_speak_empty_text(self):
        res = self._speaker(_FakeMedia()).speak("   ")
        self.assertFalse(res["ok"])

    def test_deliver_prefers_send_audio(self):
        media = _FakeMedia(MediaResult(
            kind="audio", provider="mock", mime="audio/mpeg",
            data=b"ABC", meta={"fake": True}))
        chan = _FakeChannel(audio=True)
        res = self._speaker(media).deliver(chan, "u1", "语音内容")
        self.assertEqual(res["delivered"], "audio")
        self.assertEqual(chan.sent[0][0], "audio")
        self.assertEqual(chan.sent[0][1], "u1")

    def test_deliver_send_file_channel(self):
        media = _FakeMedia(MediaResult(
            kind="audio", provider="mock", mime="audio/mpeg", data=b"ABC"))
        chan = _FakeChannel(file_only=True)
        res = self._speaker(media).deliver(chan, "u1", "x")
        self.assertEqual(res["delivered"], "audio")
        self.assertEqual(chan.sent[0][0], "file")

    def test_deliver_text_fallback_honest(self):
        media = _FakeMedia(MediaResult(
            kind="audio", provider="mock", mime="audio/mpeg", data=b"ABC"))
        chan = _FakeChannel()
        res = self._speaker(media).deliver(chan, "u1", "正文")
        self.assertEqual(res["delivered"], "text-fallback")
        kind, uid, text = chan.sent[0]
        self.assertEqual(kind, "text")
        self.assertIn("正文", text)
        self.assertIn(res["path"], text)            # 文件路径随文本给出
        self.assertIn("note", res)                  # 降级原因如实记录

    def test_deliver_audio_failure_falls_back(self):
        media = _FakeMedia(MediaResult(
            kind="audio", provider="mock", mime="audio/mpeg", data=b"ABC"))
        chan = _FakeChannel(audio=True, audio_exc=OSError("渠道挂了"))
        res = self._speaker(media).deliver(chan, "u1", "x")
        self.assertEqual(res["delivered"], "text-fallback")
        self.assertIn("渠道挂了", res["deliver_error"])
        self.assertEqual(chan.sent[0][0], "text")

    def test_default_media_router_builds(self):
        router = voice_mod._default_media()
        res = router.generate("audio", "测试路由", model=None)
        self.assertEqual(res.kind, "audio")         # 无 key → Mock 兜底
        self.assertTrue(res.meta.get("fake"))

    def test_cli_speak(self):
        media = _FakeMedia(MediaResult(
            kind="audio", provider="mock", mime="audio/mpeg",
            data=b"ABC", meta={"fake": True}))
        with mock.patch.object(voice_mod, "_default_media", return_value=media):
            rc = voice_mod.main(["--text", "CLI 语音", "--out", self.tmp])
        self.assertEqual(rc, 0)
        files = os.listdir(self.tmp)
        self.assertEqual(len(files), 1)


if __name__ == "__main__":
    unittest.main()
