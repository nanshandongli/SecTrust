"""Trace 回放层回归测试（2026-10-25）。

锁定承诺：
1. seq 按 trace 单调分配，events() 正序回放即发生顺序；after_seq 游标增量拉取；
2. **哈希指针不存原文**：content 只留 blake2b 指纹，敏感键脱敏——
   直接读库文件断言敏感串不存在（最强的"没落库"证据）；verify() 可回放验真；
3. detail 有界：超限截断且留全文指纹；
4. 留痕旁路语义：record 对未知 trace_id 自动补建；错误参数显式抛错不兜底；
5. 跨实例、多线程写同一 trace 不重号；
6. by_task 反查回放；finish 幂等 + meta 合并；
7. 连接纪律：close() 幂等（-W error::ResourceWarning 下零泄漏）；
8. CLI 全流程（new/record/show/list/stats/verify/finish）。
"""

from __future__ import annotations

import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import trace as trace_mod                          # noqa: E402
from trace import (TraceError, TraceStore, digest,  # noqa: E402
                   new_trace_id, redact, trace_id_of)


class TraceStoreTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sectrust-trace-")
        self.db = os.path.join(self.tmp, "trace.db")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = TraceStore(db_path=self.db)
        self.addCleanup(self.store.close)

    # ---- 基础读写 ----
    def test_start_and_record_roundtrip(self):
        tid = self.store.start_trace(title="双十一营销", kind="pipeline",
                                     started_by="ops")
        self.assertTrue(tid.startswith("tr_"))
        s1 = self.store.record(tid, "plan_built", summary="拆成 7 个工序",
                               detail={"tasks": ["调研", "文案", "设计"]})
        s2 = self.store.record(tid, "task_claimed", task_id="t-1",
                               actor="worker-a", summary="认领调研")
        self.assertEqual((s1, s2), (1, 2))
        evs = self.store.events(tid)
        self.assertEqual([e["seq"] for e in evs], [1, 2])
        self.assertEqual(evs[0]["kind"], "plan_built")
        self.assertEqual(evs[0]["detail"], {"tasks": ["调研", "文案", "设计"]})
        self.assertEqual(evs[1]["actor"], "worker-a")

    def test_record_auto_creates_trace_row(self):
        """worker 只从 payload 拿到 trace_id 也能直接记。"""
        tid = new_trace_id()
        self.store.record(tid, "tool_call", summary="goto")
        meta = self.store.get(tid)
        self.assertIsNotNone(meta)
        self.assertEqual(meta["status"], "open")
        self.assertEqual(meta["event_count"], 1)

    def test_seq_monotonic_under_threads(self):
        tid = self.store.start_trace()
        results = []
        lock = threading.Lock()

        def worker(n):
            for i in range(20):
                seq = self.store.record(tid, "tool_call", actor=f"w{n}",
                                        summary=f"{n}-{i}")
                with lock:
                    results.append(seq)

        # 同 store 同线程池：thread-local 各自持有连接
        threads_list = [threading.Thread(target=worker, args=(n,))
                        for n in range(4)]
        for t in threads_list:
            t.start()
        for t in threads_list:
            t.join()
        self.assertEqual(len(results), 80)
        self.assertEqual(len(set(results)), 80, "seq 重号")
        self.assertEqual(sorted(results), list(range(1, 81)))
        self.assertEqual(len(self.store.events(tid)), 80)

    def test_cross_instance_no_seq_collision(self):
        tid = self.store.start_trace()
        self.store.record(tid, "tool_call", summary="a")
        other = TraceStore(db_path=self.db)
        self.addCleanup(other.close)
        seq = other.record(tid, "tool_call", summary="b")
        self.assertEqual(seq, 2)
        self.assertEqual(len(self.store.events(tid)), 2)

    # ---- 哈希指针与脱敏（核心纪律）----
    def test_content_only_hash_never_raw(self):
        tid = self.store.start_trace()
        secret_text = "super-secret-cookie-value-xyz"
        seq = self.store.record(tid, "tool_call", task_id="t-9",
                                summary="type_text", content=secret_text)
        ev = self.store.events(tid)[0]
        self.assertEqual(ev["content_hash"], digest(secret_text.encode("utf-8")))
        self.assertNotIn(secret_text, json.dumps(ev, ensure_ascii=False))
        # 最强证据：直接读库文件，敏感串一个字节都不在
        with open(self.db, "rb") as f:
            raw = f.read()
        self.assertNotIn(secret_text.encode("utf-8"), raw)
        # 验真：原文可后补比对
        self.assertTrue(self.store.verify(tid, seq, secret_text))
        self.assertFalse(self.store.verify(tid, seq, "wrong-text"))

    def test_redact_sensitive_keys(self):
        tid = self.store.start_trace()
        self.store.record(tid, "approval_resolved",
                          detail={"token": "abc123", "nested": {"api_key": "k-1",
                                  "note": "正常字段"}, "items": [{"password": "p"}]})
        ev = self.store.events(tid)[0]["detail"]
        self.assertEqual(ev["token"], {"redacted": True,
                                       "sha256": digest(b'"abc123"')})
        self.assertEqual(ev["nested"]["note"], "正常字段")
        self.assertTrue(ev["nested"]["api_key"]["redacted"])
        self.assertTrue(ev["items"][0]["password"]["redacted"])

    def test_redact_pure_function(self):
        out = redact({"Authorization": "Bearer x", "keep": 1, "arr": ["s"]})
        self.assertTrue(out["Authorization"]["redacted"])
        self.assertEqual(out["keep"], 1)
        self.assertEqual(out["arr"], ["s"])
        self.assertEqual(redact("plain"), "plain")

    def test_detail_truncated_with_full_hash(self):
        tid = self.store.start_trace()
        big = "x" * (trace_mod.MAX_DETAIL_BYTES + 5000)
        self.store.record(tid, "tool_call", detail={"blob": big})
        ev = self.store.events(tid)[0]["detail"]
        self.assertTrue(ev["truncated"])
        self.assertEqual(ev["bytes"], len(big.encode("utf-8")) + len('{"blob":""}'))
        self.assertIn("sha256", ev)
        self.assertLessEqual(len(ev["head"]), trace_mod.TRUNC_KEEP)

    # ---- 查询 ----
    def test_after_seq_cursor(self):
        tid = self.store.start_trace()
        for i in range(5):
            self.store.record(tid, "tool_call", summary=f"e{i}")
        page = self.store.events(tid, after_seq=2)
        self.assertEqual([e["seq"] for e in page], [3, 4, 5])
        self.assertEqual(len(self.store.events(tid, after_seq=4, limit=1)), 1)

    def test_by_task_replay(self):
        tid = self.store.start_trace()
        self.store.record(tid, "task_claimed", task_id="t-42", actor="w1")
        self.store.record(tid, "task_completed", task_id="t-42")
        res = self.store.by_task("t-42")
        self.assertEqual(res["trace_id"], tid)
        self.assertEqual(len(res["events"]), 2)
        self.assertEqual(res["trace"]["event_count"], 2)
        missing = self.store.by_task("nope")
        self.assertIsNone(missing["trace_id"])

    def test_get_histogram_and_list_filter(self):
        t1 = self.store.start_trace(title="a", kind="pipeline")
        t2 = self.store.start_trace(title="b", kind="batch")
        self.store.record(t1, "task_claimed")
        self.store.record(t1, "task_claimed")
        self.store.record(t1, "task_failed")
        meta = self.store.get(t1)
        self.assertEqual(meta["kinds"], {"task_claimed": 2, "task_failed": 1})
        self.assertEqual(meta["event_count"], 3)
        self.store.finish(t1, status="completed")
        listing = self.store.list(status="completed")
        self.assertEqual([x["trace_id"] for x in listing], [t1])
        self.assertIsNotNone(self.store.get(t2))
        self.assertIsNone(self.store.get("tr_missing"))

    def test_finish_idempotent_and_meta_merge(self):
        tid = self.store.start_trace(meta={"trace_only": 1})
        self.assertTrue(self.store.finish(tid, meta_updates={"cost": 0.5}))
        self.assertFalse(self.store.finish(tid))  # 幂等：第二次返回 False
        meta = self.store.get(tid)
        self.assertEqual(meta["status"], "completed")
        self.assertEqual(meta["meta"], {"trace_only": 1, "cost": 0.5})
        self.assertIsNotNone(meta["ended_at"])

    def test_stats(self):
        tid = self.store.start_trace()
        self.store.record(tid, "tool_call")
        st = self.store.stats()
        self.assertEqual(st["traces"], 1)
        self.assertEqual(st["events"], 1)
        self.assertEqual(st["by_kind"], {"tool_call": 1})

    # ---- 错误与关闭 ----
    def test_invalid_args_raise(self):
        with self.assertRaises(TraceError):
            self.store.record("", "tool_call")
        with self.assertRaises(TraceError):
            self.store.record("tr_x", "")

    def test_closed_store_rejects_and_close_idempotent(self):
        store = TraceStore(db_path=os.path.join(self.tmp, "closed.db"))
        store.start_trace()
        store.close()
        store.close()  # 幂等
        with self.assertRaises(TraceError):
            store.record("tr_x", "tool_call")

    # ---- 辅助 ----
    def test_trace_id_of(self):
        self.assertEqual(trace_id_of({"trace_id": "tr_a"}), "tr_a")
        self.assertIsNone(trace_id_of({"trace_id": ""}))
        self.assertIsNone(trace_id_of({}))
        self.assertIsNone(trace_id_of(None))
        self.assertIsNone(trace_id_of("tr_a"))

    def test_new_trace_id_unique(self):
        ids = {new_trace_id() for _ in range(200)}
        self.assertEqual(len(ids), 200)
        for i in ids:
            self.assertRegex(i, r"^tr_[0-9a-f]{12}$")

    def test_reopen_after_close_same_file(self):
        tid = self.store.start_trace()
        self.store.record(tid, "tool_call", summary="before-close")
        self.store.close()
        reopened = TraceStore(db_path=self.db)
        self.addCleanup(reopened.close)
        self.assertEqual(len(reopened.events(tid)), 1)


class TraceCliTest(unittest.TestCase):

    def _run_cli(self, *argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            trace_mod.main(list(argv))
        text = buf.getvalue().strip()
        return json.loads(text) if text else None

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sectrust-trace-cli-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.db = os.path.join(self.tmp, "trace.db")
        self.base = ["--db-path", self.db]

    def test_full_cli_flow(self):
        created = self._run_cli(*self.base, "--new", "--title", "营销流水线",
                                "--kind", "pipeline", "--by", "ops")
        tid = created["trace_id"]
        rec = self._run_cli(*self.base, "--record", "--trace", tid,
                            "--event-kind", "task_claimed", "--task", "t-1",
                            "--actor", "worker-a", "--summary", "认领",
                            "--detail", '{"selector": "#go"}',
                            "--content", "敏感原文不进库")
        self.assertEqual(rec["seq"], 1)
        shown = self._run_cli(*self.base, "--show", "--trace", tid)
        self.assertEqual(shown["trace"]["title"], "营销流水线")
        self.assertEqual(len(shown["events"]), 1)
        self.assertEqual(shown["events"][0]["detail"], {"selector": "#go"})
        by_task = self._run_cli(*self.base, "--show", "--task", "t-1")
        self.assertEqual(by_task["trace_id"], tid)
        listed = self._run_cli(*self.base, "--list")
        self.assertEqual(listed[0]["trace_id"], tid)
        stats = self._run_cli(*self.base, "--stats")
        self.assertEqual(stats["events"], 1)
        verified = self._run_cli(*self.base, "--verify", tid, "1",
                                 "--content", "敏感原文不进库")
        self.assertTrue(verified["verified"])
        bad = self._run_cli(*self.base, "--verify", tid, "1", "--content", "错")
        self.assertFalse(bad["verified"])
        fin = self._run_cli(*self.base, "--finish", tid)
        self.assertTrue(fin["finished"])
        # CLI 跑完连接必须已关（ResourceWarning 会炸回归）
        with open(self.db, "rb") as f:
            raw = f.read()
        self.assertNotIn("敏感原文不进库".encode("utf-8"), raw)


if __name__ == "__main__":
    unittest.main()
