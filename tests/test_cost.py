"""cost.py 存储层测试：计价诚实性 / 聚合精确 / 幂等 / 看板 / 线程安全 / CLI。"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from cost import (CostError, CostStore, estimate_cost, tracked_complete,  # noqa: E402
                  usage_tokens)


def _priced_call(store, task="t/a", trace_id="tr_1", model="gpt-4o",
                 it=1000, ot=500, **kw):
    return store.record_model_call(task_id=task, trace_id=trace_id, model=model,
                                   input_tokens=it, output_tokens=ot, **kw)


class CostStoreBasicsTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sectrust-cost-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.db = os.path.join(self.tmp, "cost.db")
        self.store = CostStore(db_path=self.db)
        self.addCleanup(self.store.close)

    # ---------- 计价 ----------
    def test_known_model_priced_exactly(self):
        # gpt-4o: 1000/1000*0.0025 + 500/1000*0.01 = 0.0025 + 0.005 = 0.0075
        _priced_call(self.store, it=1000, ot=500)
        rec = self.store.task_cost("t/a")
        self.assertEqual(len(rec["events"]), 1)
        self.assertAlmostEqual(rec["events"][0]["cost"], 0.0075, places=9)
        snap = self.store.finalize_task("t/a")
        self.assertAlmostEqual(snap["cost_estimate"], 0.0075, places=9)
        self.assertEqual(snap["unpriced_calls"], 0)

    def test_prefix_match_longest_wins(self):
        # "gpt-4o-2024-08-06" 应命中 gpt-4o（而不是 gpt-4o-mini 之类更短前缀）
        c1, p1 = estimate_cost("gpt-4o-2024-08-06", 1000, 0)
        c2, _ = estimate_cost("gpt-4o", 1000, 0)
        self.assertTrue(p1)
        self.assertAlmostEqual(c1, c2, places=12)

    def test_unknown_model_never_fabricates_price(self):
        _priced_call(self.store, model="some-unknown-model-9000", it=100, ot=50)
        rec = self.store.task_cost("t/a")   # 未 finalize：只有事件 + 实时聚合
        self.assertIsNone(rec["record"])
        self.assertEqual(rec["events"][0]["cost"], 0.0)
        self.assertEqual(rec["events"][0]["priced"], 0)
        self.assertEqual(rec["aggregate"]["cost_estimate"], 0.0)
        snap = self.store.finalize_task("t/a")
        self.assertEqual(snap["cost_estimate"], 0.0)
        self.assertEqual(snap["unpriced_calls"], 1)   # 诚实标记：有调用但没价
        self.assertEqual(snap["model_calls"], 1)      # 调用数照记

    def test_mock_and_ollama_explicitly_free(self):
        for m in ("mock", "ollama", "ollama:llama3"):
            c, priced = estimate_cost(m, 9999, 9999)
            self.assertTrue(priced, m)   # 是"知道它免费"，不是"不知道"
            self.assertEqual(c, 0.0, m)

    def test_empty_model_unpriced(self):
        c, priced = estimate_cost("", 100, 100)
        self.assertFalse(priced)
        self.assertEqual(c, 0.0)

    def test_price_table_override_by_param(self):
        s = CostStore(db_path=os.path.join(self.tmp, "override.db"),
                      prices={"my-model": (1.0, 2.0)})
        self.addCleanup(s.close)
        c, priced = estimate_cost("my-model-x", 1000, 1000, prices=s.prices)
        self.assertTrue(priced)
        self.assertAlmostEqual(c, 3.0, places=9)   # 1.0 + 2.0

    def test_price_table_env_override(self):
        os.environ["COST_PRICE_TABLE"] = json.dumps({"env-model": [0.5, 0.25]})
        self.addCleanup(os.environ.pop, "COST_PRICE_TABLE", None)
        s = CostStore(db_path=os.path.join(self.tmp, "env.db"))
        self.addCleanup(s.close)
        c, priced = estimate_cost("env-model", 2000, 1000, prices=s.prices)
        self.assertTrue(priced)
        self.assertAlmostEqual(c, 1.25, places=9)  # 1.0 + 0.25

    def test_bad_env_price_table_raises_not_silent(self):
        os.environ["COST_PRICE_TABLE"] = "{not json"
        self.addCleanup(os.environ.pop, "COST_PRICE_TABLE", None)
        with self.assertRaises(CostError):
            CostStore(db_path=os.path.join(self.tmp, "bad.db"))

    def test_usage_tokens_both_key_styles(self):
        self.assertEqual(usage_tokens({"prompt_tokens": 10, "completion_tokens": 5}),
                         (10, 5))                       # OpenAI / Ollama
        self.assertEqual(usage_tokens({"input_tokens": 7, "output_tokens": 3}),
                         (7, 3))                        # Anthropic
        self.assertEqual(usage_tokens({"mock": True}), (0, 0))   # Mock 无用量
        self.assertEqual(usage_tokens(None), (0, 0))
        self.assertEqual(usage_tokens({"prompt_tokens": "x"}), (0, 0))  # 脏数据

    # ---------- 四层记录 ----------
    def test_four_layers_recorded(self):
        self.store.record_model_call(task_id="t/a", model="gpt-4o",
                                     input_tokens=10, output_tokens=5)
        self.store.record_tool_call(task_id="t/a", tool="browser:goto")
        self.store.record_retry(task_id="t/a", reason="quality_rejected")
        self.store.record_infra(task_id="t/a", kind="sandbox", seconds=12.5)
        st = self.store.stats()
        self.assertEqual(st["by_layer"],
                         {"model": 1, "tool": 1, "retry": 1, "infra": 1})

    def test_unknown_layer_rejected(self):
        with self.assertRaises(CostError):
            self.store._record_event("bogus", task_id="t/a")

    def test_model_call_without_tokens_still_counts(self):
        # Mock 场景：调用发生过，只是没用量数据
        self.store.record_model_call(task_id="t/a", model="mock")
        snap = self.store.finalize_task("t/a")
        self.assertEqual(snap["model_calls"], 1)
        self.assertEqual(snap["input_tokens"], 0)
        self.assertEqual(snap["output_tokens"], 0)

    # ---------- 快照 schema（用户给定） ----------
    def test_record_task_user_schema_roundtrip(self):
        snap = {
            "trace_id": "tr_u", "task_id": "t/u", "model_calls": 12,
            "input_tokens": 8400, "output_tokens": 2100, "tool_calls": 7,
            "retries": 2, "sandbox_seconds": 45, "cost_estimate": 0.0123,
        }
        self.store.record_task(snap)
        rec = self.store.task_cost("t/u")
        for k, v in snap.items():
            self.assertEqual(rec["record"][k], v, k)

    def test_record_task_upsert_merges(self):
        self.store.record_task({"task_id": "t/u", "model_calls": 3,
                                "input_tokens": 100})
        self.store.record_task({"task_id": "t/u", "tool_calls": 2})
        rec = self.store.task_cost("t/u")
        self.assertEqual(rec["record"]["model_calls"], 3)    # 旧值不被抹掉
        self.assertEqual(rec["record"]["tool_calls"], 2)

    def test_record_task_requires_task_id(self):
        with self.assertRaises(CostError):
            self.store.record_task({"model_calls": 1})

    def test_finalize_idempotent(self):
        _priced_call(self.store, task="t/a", it=100, ot=50)
        self.store.record_tool_call(task_id="t/a")
        s1 = self.store.finalize_task("t/a")
        s2 = self.store.finalize_task("t/a")
        s3 = self.store.finalize_task("t/a")
        self.assertEqual(s1, s2)
        self.assertEqual(s2, s3)   # 重复收尾不会把一次完成记成两倍

    def test_finalize_attribution_only_updates_when_given(self):
        _priced_call(self.store, task="t/a")
        self.store.finalize_task("t/a", role="copywriter", template="campaign",
                                 tier="complex", outcome="completed")
        # 第二次不传归因：不把已知信息覆盖成空
        self.store.finalize_task("t/a")
        rec = self.store.task_cost("t/a")
        self.assertEqual(rec["record"]["role"], "copywriter")
        self.assertEqual(rec["record"]["template"], "campaign")
        self.assertEqual(rec["record"]["tier"], "complex")

    def test_finalize_zero_event_task_writes_zero_snapshot(self):
        snap = self.store.finalize_task("t/none")
        self.assertEqual(snap["model_calls"], 0)
        self.assertEqual(snap["cost_estimate"], 0.0)

    # ---------- 读 ----------
    def test_task_cost_unknown_returns_none(self):
        self.assertIsNone(self.store.task_cost("no-such"))

    def test_trace_cost_aggregates(self):
        _priced_call(self.store, task="t/a", trace_id="tr_1", it=100, ot=50)
        self.store.record_tool_call(task_id="t/b", trace_id="tr_1")
        agg = self.store.trace_cost("tr_1")
        self.assertEqual(agg["events"], 2)
        self.assertEqual(agg["input_tokens"], 100)
        self.assertIsNone(self.store.trace_cost("tr_missing"))

    def test_detail_bounded_and_redacted(self):
        # 重试原因里混进敏感键：值只留指纹
        self.store.record_retry(task_id="t/a",
                                reason="x" * 100)
        self.store.record_tool_call(
            task_id="t/a", tool="api",
            detail={"api_key": "sk-super-secret", "note": "ok"})
        rec = self.store.task_cost("t/a")
        tool_ev = [e for e in rec["events"] if e["layer"] == "tool"][0]
        d = json.loads(tool_ev["detail"])
        self.assertEqual(d["api_key"]["redacted"], True)
        self.assertIn("sha256", d["api_key"])
        self.assertEqual(d["note"], "ok")
        with open(self.db, "rb") as f:
            raw = f.read()
        self.assertNotIn(b"sk-super-secret", raw)

    def test_oversized_detail_truncated(self):
        self.store.record_tool_call(task_id="t/a", tool="big",
                                    detail={"blob": "y" * 20000})
        rec = self.store.task_cost("t/a")
        stored = rec["events"][0]["detail"]
        d = json.loads(stored)
        self.assertTrue(d["truncated"])
        self.assertEqual(d["bytes"], 20011)      # 记的是原文大小（诚实）
        self.assertLessEqual(len(stored), 4096)  # 落库的必须真的有界

    # ---------- 看板 ----------
    def _seed_two_tasks(self):
        _priced_call(self.store, task="t/a", trace_id="tr_1", model="gpt-4o",
                     it=8400, ot=2100)
        self.store.record_tool_call(task_id="t/a", trace_id="tr_1",
                                    tool="browser:goto")
        self.store.record_model_call(task_id="t/b", trace_id="tr_1",
                                    model="claude-sonnet-4",
                                    input_tokens=1000, output_tokens=500)
        self.store.record_retry(task_id="t/b", trace_id="tr_1", reason="打回")
        self.store.record_retry(task_id="t/b", trace_id="tr_1", reason="打回")
        self.store.record_infra(task_id="t/b", trace_id="tr_1", seconds=45)
        self.store.finalize_task("t/a", role="copywriter",
                                 template="campaign", tier="complex",
                                 outcome="completed")
        self.store.finalize_task("t/b", role="reviewer",
                                 template="campaign", tier="complex",
                                 outcome="failed")

    def test_dashboard_groups_and_outliers(self):
        self._seed_two_tasks()
        d = self.store.dashboard(days=30)
        self.assertEqual(d["totals"]["tasks"], 2)
        self.assertEqual(d["totals"]["model_calls"], 2)
        self.assertEqual(d["totals"]["tokens"], 12000)
        self.assertEqual(d["totals"]["tool_calls"], 1)
        self.assertEqual(d["totals"]["retries"], 2)
        # 0.042 + 0.0105
        self.assertAlmostEqual(d["totals"]["cost_estimate"], 0.0525, places=9)
        self.assertEqual(len(d["by_template"]), 1)
        self.assertEqual(d["by_template"][0]["key"], "campaign")
        roles = {r["key"]: r for r in d["by_role"]}
        self.assertEqual(set(roles), {"copywriter", "reviewer"})
        self.assertEqual(roles["copywriter"]["tokens"], 10500)
        self.assertEqual(d["outliers"]["top_cost"][0]["task_id"], "t/a")
        self.assertEqual(d["outliers"]["top_retries"][0]["task_id"], "t/b")
        self.assertEqual(d["outliers"]["top_tool_calls"][0]["task_id"], "t/a")
        self.assertIn("price_note", d)

    def test_dashboard_days_window_excludes_old(self):
        self._seed_two_tasks()
        # 把快照时间改到 60 天前
        conn = self.store._conn()
        conn.execute("UPDATE cost_records SET updated_at=?",
                     (__import__("time").time() - 60 * 86400,))
        conn.commit()
        d = self.store.dashboard(days=30)
        self.assertEqual(d["totals"]["tasks"], 0)
        d60 = self.store.dashboard(days=90)
        self.assertEqual(d60["totals"]["tasks"], 2)

    def test_dashboard_unattributed_grouped(self):
        _priced_call(self.store, task="t/x")
        self.store.finalize_task("t/x")
        d = self.store.dashboard(days=30)
        self.assertEqual(d["by_template"][0]["key"], "(未标注)")
        self.assertEqual(d["by_role"][0]["key"], "(未标注)")

    # ---------- 连接纪律 ----------
    def test_close_idempotent_and_after_close_raises(self):
        s = CostStore(db_path=os.path.join(self.tmp, "close.db"))
        s.record_tool_call(task_id="t/a", tool="x")
        s.close()
        s.close()   # 幂等，不炸
        with self.assertRaises(CostError):
            s._record_event("tool", task_id="t/a")

    def test_threaded_recording_no_lost_events(self):
        n_threads, per = 8, 20
        errs = []

        def work(i):
            try:
                for j in range(per):
                    self.store.record_tool_call(task_id=f"t/{i}-{j}",
                                                tool="browser:goto")
            except Exception as e:  # noqa: BLE001
                errs.append(e)

        ts = [threading.Thread(target=work, args=(i,)) for i in range(n_threads)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(errs, [])
        st = self.store.stats()
        self.assertEqual(st["by_layer"]["tool"], n_threads * per)

    def test_cross_thread_close_collects_all_conns(self):
        """线程结束后连接也要被 close() 收掉（ResourceWarning 归零）。"""
        done = threading.Event()

        def work():
            self.store.record_tool_call(task_id="t/cross", tool="x")
            done.set()

        t = threading.Thread(target=work)
        t.start()
        done.wait(5)
        t.join()
        # 工作线程的 thread-local 连接没人关——close() 必须一并收掉
        with warnings_zero():
            self.store.close()
        self.assertRaises(CostError, self.store._record_event, "tool",
                          task_id="t/cross")


class warnings_zero:
    """把 ResourceWarning 升级为错误的上下文（与回归同口径）。"""

    def __enter__(self):
        import warnings
        self._ctx = warnings.catch_warnings()
        self._ctx.__enter__()
        warnings.simplefilter("error", ResourceWarning)
        return self

    def __exit__(self, *exc):
        return self._ctx.__exit__(*exc)


class TrackedCompleteTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sectrust-cost-track-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = CostStore(db_path=os.path.join(self.tmp, "cost.db"))
        self.addCleanup(self.store.close)

    class _FakeProvider:
        name = "fake"

        def __init__(self, usage, model="gpt-4o"):
            self.usage = usage
            self.model = model
            self.calls = 0

        def complete(self, messages, model=None, **opts):
            self.calls += 1
            from cost import usage_tokens as _ut  # noqa: F401
            return type("R", (), {"content": "ok", "model": self.model,
                                  "usage": self.usage})()

    def test_tracked_complete_records_usage(self):
        p = self._FakeProvider({"prompt_tokens": 8400, "completion_tokens": 2100})
        r = tracked_complete(self.store, p, [], task_id="t/a", trace_id="tr_1")
        self.assertEqual(r.content, "ok")
        snap = self.store.finalize_task("t/a")
        self.assertEqual(snap["model_calls"], 1)
        self.assertEqual(snap["input_tokens"], 8400)
        self.assertEqual(snap["output_tokens"], 2100)
        self.assertAlmostEqual(snap["cost_estimate"], 0.042, places=9)

    def test_tracked_complete_passthrough_when_cost_none(self):
        p = self._FakeProvider({"prompt_tokens": 1})
        r = tracked_complete(None, p, [], task_id="t/a")
        self.assertEqual(p.calls, 1)
        self.assertEqual(r.content, "ok")

    def test_tracked_complete_survives_broken_cost(self):
        """账本坏了不能把模型调用拖死（旁路纪律）。"""
        class _BrokenCost:
            def record_model_call(self, **kw):
                raise RuntimeError("db gone")

        p = self._FakeProvider({"prompt_tokens": 1})
        r = tracked_complete(_BrokenCost(), p, [], task_id="t/a")
        self.assertEqual(r.content, "ok")


class CostCliTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sectrust-cost-cli-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.db = os.path.join(self.tmp, "cost.db")

    def _run(self, *argv):
        env = dict(os.environ, COST_DB_PATH=self.db)
        return subprocess.run(
            [sys.executable, os.path.join(ROOT, "cost.py"), *argv],
            capture_output=True, text=True, env=env, timeout=60)

    def test_cli_full_flow(self):
        r = self._run("--model-call", "--task", "t/a", "--model", "gpt-4o",
                      "--in-tokens", "8400", "--out-tokens", "2100")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["layer"], "model")
        self._run("--tool-call", "--task", "t/a", "--tool", "browser:goto")
        self._run("--retry", "--task", "t/a", "--reason", "打回")
        self._run("--infra", "--task", "t/a", "--seconds", "45")
        r = self._run("--finalize", "--task", "t/a", "--role", "copywriter",
                      "--template", "campaign")
        snap = json.loads(r.stdout)
        self.assertEqual(snap["model_calls"], 1)
        self.assertEqual(snap["tool_calls"], 1)
        self.assertEqual(snap["retries"], 1)
        self.assertEqual(snap["sandbox_seconds"], 45.0)
        r = self._run("--show", "--task", "t/a")
        shown = json.loads(r.stdout)
        self.assertEqual(shown["task_id"], "t/a")
        self.assertIsNotNone(shown["record"])
        r = self._run("--dashboard", "--days", "30")
        self.assertEqual(json.loads(r.stdout)["totals"]["tasks"], 1)
        r = self._run("--stats")
        self.assertEqual(json.loads(r.stdout)["events"], 4)
        # 显式快照路径
        r = self._run("--snapshot", json.dumps(
            {"task_id": "t/u", "model_calls": 12, "input_tokens": 8400,
             "output_tokens": 2100, "tool_calls": 7, "retries": 2,
             "sandbox_seconds": 45, "cost_estimate": 0.0123,
             "trace_id": "tr_u"}))
        self.assertTrue(json.loads(r.stdout)["written"])


if __name__ == "__main__":
    unittest.main()
