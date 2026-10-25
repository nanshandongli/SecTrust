"""cost.py 接线测试：四挂点（DagScheduler/QualityGate/BrowserSkill/模型调用）
+ 两入口（MCP 五工具 / REST 端点）+ 旁路纪律（账本坏了不挡主链路）。"""

import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from orchestration import DagScheduler  # noqa: E402
from cost import CostStore  # noqa: E402
from trace import TraceStore  # noqa: E402
from execution.browser import BrowserSkill, BrowserError  # noqa: E402

import mcp_server as mcp_mod  # noqa: E402
from api.app import RegistryAPI  # noqa: E402
from agentskills.quality import QualityGate  # noqa: E402


def _payload(task="p1/copy", **kw):
    p = {"name": "文案", "skill": "copywriter",
         "plan": {"template": "campaign", "tier": "complex"},
         "trace_id": "tr_w"}
    p.update(kw)
    return p


class SchedulerCostWiringTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sectrust-cost-wire-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.reg_db = os.path.join(self.tmp, "reg.db")
        self.cost = CostStore(db_path=os.path.join(self.tmp, "cost.db"))
        self.tracer = TraceStore(db_path=os.path.join(self.tmp, "trace.db"))
        self.addCleanup(self.cost.close)
        self.addCleanup(self.tracer.close)
        self.s = DagScheduler(db_path=self.reg_db, tracer=self.tracer,
                              cost=self.cost, events=False)
        self.addCleanup(self.s.close)

    def test_complete_finalizes_cost_with_attribution(self):
        self.s.add_task("p1/copy", payload=_payload())
        self.s.claim_ready(runner_id="w1")
        self.cost.record_model_call(task_id="p1/copy", trace_id="tr_w",
                                    model="gpt-4o", input_tokens=8400,
                                    output_tokens=2100)
        ok = self.s.complete("p1/copy", result={"output": "文案正文"})
        self.assertTrue(ok)
        rec = self.cost.task_cost("p1/copy")
        self.assertIsNotNone(rec["record"])
        self.assertEqual(rec["record"]["role"], "copywriter")
        self.assertEqual(rec["record"]["template"], "campaign")
        self.assertEqual(rec["record"]["tier"], "complex")
        self.assertEqual(rec["record"]["outcome"], "completed")
        self.assertEqual(rec["record"]["trace_id"], "tr_w")
        self.assertEqual(rec["record"]["model_calls"], 1)
        self.assertAlmostEqual(rec["record"]["cost_estimate"], 0.042, places=9)
        # 轨迹里应有成本快照事件
        evs = self.tracer.events("tr_w")
        kinds = [e["kind"] for e in evs]
        self.assertIn("cost_snapshot", kinds)
        snap_ev = [e for e in evs if e["kind"] == "cost_snapshot"][0]
        self.assertEqual(snap_ev["detail"]["model_calls"], 1)
        self.assertAlmostEqual(snap_ev["detail"]["cost_estimate"], 0.042,
                               places=9)

    def test_fail_records_retry_and_finalizes(self):
        self.s.add_task("p1/copy", payload=_payload())
        self.s.claim_ready(runner_id="w1")
        self.cost.record_tool_call(task_id="p1/copy", trace_id="tr_w",
                                   tool="browser:goto")
        blocked = self.s.fail("p1/copy", error="模型超时")
        self.assertEqual(blocked, [])
        rec = self.cost.task_cost("p1/copy")
        self.assertEqual(rec["record"]["outcome"], "failed")
        self.assertEqual(rec["record"]["tool_calls"], 1)
        # 失败的一次尝试记一笔重试成本
        retries = [e for e in rec["events"] if e["layer"] == "retry"]
        self.assertEqual(len(retries), 1)
        self.assertIn("模型超时", retries[0]["label"])

    def test_broken_cost_does_not_block_complete(self):
        """账本炸了，任务照常完成（旁路纪律）。"""

        class _BrokenCost:
            def finalize_task(self, *a, **kw):
                raise RuntimeError("cost db gone")

            def record_retry(self, *a, **kw):
                raise RuntimeError("cost db gone")

        s = DagScheduler(db_path=os.path.join(self.tmp, "broken.db"),
                         tracer=self.tracer, cost=_BrokenCost(), events=False)
        self.addCleanup(s.close)
        s.add_task("p1/x", payload=_payload())
        s.claim_ready(runner_id="w1")
        self.assertTrue(s.complete("p1/x", result={"ok": True}))
        s.add_task("p1/y", payload=_payload(task="p1/y"))
        s.claim_ready(runner_id="w1")
        self.assertEqual(s.fail("p1/y", error="boom"), [])

    def test_no_cost_injected_still_works(self):
        s = DagScheduler(db_path=os.path.join(self.tmp, "plain.db"),
                         events=False)
        self.addCleanup(s.close)
        s.add_task("p1/z", payload=_payload())
        s.claim_ready(runner_id="w1")
        self.assertTrue(s.complete("p1/z", result={"ok": True}))

    def test_complete_is_idempotent_for_cost(self):
        """complete 幂等（第二次返回 False）→ 成本快照不会被记两遍。"""
        self.s.add_task("p1/copy", payload=_payload())
        self.s.claim_ready(runner_id="w1")
        self.cost.record_model_call(task_id="p1/copy", trace_id="tr_w",
                                    model="gpt-4o", input_tokens=100,
                                    output_tokens=50)
        self.assertTrue(self.s.complete("p1/copy", result={"o": 1}))
        self.assertFalse(self.s.complete("p1/copy", result={"o": 2}))
        rec = self.cost.task_cost("p1/copy")
        self.assertEqual(rec["record"]["model_calls"], 1)
        self.assertEqual(len([e for e in self.tracer.events("tr_w")
                              if e["kind"] == "cost_snapshot"]), 1)


class QualityCostWiringTest(unittest.TestCase):

    def test_send_back_records_retry(self):
        tmp = tempfile.mkdtemp(prefix="sectrust-cost-qg-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        cost = CostStore(db_path=os.path.join(tmp, "cost.db"))
        self.addCleanup(cost.close)
        s = DagScheduler(db_path=os.path.join(tmp, "dag.db"), events=False)
        self.addCleanup(s.close)
        s.add_task("p1/copy", payload=_payload(
            quality={"min_chars": 50, "required": ["抖音"],
                     "forbidden": ["TODO"], "no_unfilled": True}))
        qg = QualityGate(db_path=os.path.join(tmp, "q.db"), max_rework=2,
                         cost=cost)
        s.claim_ready(runner_id="w1")
        r = qg.review(s, "p1/copy", "TODO 抖音文案")   # 必被打回
        self.assertFalse(r["accepted"])
        self.assertTrue(r["sent_back"])
        rec = cost.task_cost("p1/copy")
        retries = [e for e in rec["events"] if e["layer"] == "retry"]
        self.assertEqual(len(retries), 1)
        self.assertIn("quality_rejected", retries[0]["label"])
        self.assertEqual(retries[0]["trace_id"], "tr_w")

    def test_broken_cost_does_not_block_send_back(self):
        class _BrokenCost:
            def record_retry(self, *a, **kw):
                raise RuntimeError("cost db gone")

        tmp = tempfile.mkdtemp(prefix="sectrust-cost-qg2-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        s = DagScheduler(db_path=os.path.join(tmp, "dag.db"), events=False)
        self.addCleanup(s.close)
        s.add_task("p1/copy", payload=_payload(quality={"min_chars": 50}))
        qg = QualityGate(db_path=os.path.join(tmp, "q.db"), cost=_BrokenCost())
        s.claim_ready(runner_id="w1")
        r = qg.review(s, "p1/copy", "短")
        self.assertTrue(r["sent_back"])   # 打回照常生效


class _FakeProc:
    def __init__(self):
        self.returncode = None

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = 0

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.returncode = 0


class _CdpFakeWS:
    def __init__(self, handler=None):
        import queue
        self.sent = []
        self.handler = handler or (lambda msg: {})
        self.closed = False
        self._incoming = queue.Queue()

    def send_text(self, text):
        msg = json.loads(text)
        self.sent.append(msg)
        if "id" in msg:
            result = self.handler(msg)
            if result is not None:
                self._incoming.put(json.dumps(
                    {"id": msg["id"], "result": result}))

    def recv_text(self):
        while not self.closed:
            try:
                item = self._incoming.get(timeout=0.2)
            except Exception:  # noqa: BLE001
                continue
            if item is None:
                break
            return item
        raise OSError("FakeWS closed")

    def close(self):
        self.closed = True
        self._incoming.put(None)


def _fake_http():
    def _get(url, timeout=5.0):
        if url.endswith("/json/version"):
            return {"Browser": "Edge/Test", "webSocketDebuggerUrl": "ws://x"}
        if url.endswith("/json/list"):
            return [{"type": "page", "id": "P1",
                     "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/P1"}]
        raise OSError("unexpected url %s" % url)
    return _get


class BrowserCostWiringTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sectrust-cost-browser-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cost = CostStore(db_path=os.path.join(self.tmp, "cost.db"))
        self.addCleanup(self.cost.close)

    def _skill(self, handler, **kw):
        sk = BrowserSkill(edge_path="/fake/msedge.exe", timeout=0.4,
                          launcher=lambda *a: _FakeProc(),
                          ws_factory=lambda url: _CdpFakeWS(handler),
                          http_get=_fake_http(),
                          cost=self.cost, task_id="t-1", trace_id="tr_b",
                          **kw)
        self.addCleanup(sk.close)
        return sk

    def test_goto_records_tool_cost(self):
        sk = self._skill(lambda msg: {})
        self.assertEqual(sk.start(), "cdp")
        sk.goto("https://example.com")
        rec = self.cost.task_cost("t-1")
        tools = [e for e in rec["events"] if e["layer"] == "tool"]
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0]["label"], "goto")
        self.assertEqual(tools[0]["trace_id"], "tr_b")
        snap = self.cost.finalize_task("t-1")
        self.assertEqual(snap["tool_calls"], 1)

    def test_failed_action_also_costs(self):
        sk = self._skill(lambda msg: {"result": {"value": None}})
        sk.start()
        with self.assertRaises(BrowserError):
            sk.click("#nope")
        rec = self.cost.task_cost("t-1")
        tools = [e for e in rec["events"] if e["layer"] == "tool"]
        self.assertEqual(len(tools), 1)   # 失败的动作也是花掉的钱
        self.assertEqual(tools[0]["label"], "click")

    def test_no_cost_injected_no_error(self):
        sk = BrowserSkill(edge_path="/fake/msedge.exe", timeout=0.4,
                          launcher=lambda *a: _FakeProc(),
                          ws_factory=lambda url: _CdpFakeWS(lambda m: {}),
                          http_get=_fake_http())
        self.addCleanup(sk.close)
        sk.start()
        sk.goto("https://example.com")   # 不炸即过


class TrackedCompleteWiringTest(unittest.TestCase):
    """模型调用包一层：tracked_complete 的接线面（cost.py 提供）。"""

    def test_router_style_call_recorded(self):
        from cost import tracked_complete
        tmp = tempfile.mkdtemp(prefix="sectrust-cost-model-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        cost = CostStore(db_path=os.path.join(tmp, "cost.db"))
        self.addCleanup(cost.close)

        class _Router:
            def complete(self, messages, model=None, **opts):
                return type("R", (), {
                    "content": "回复", "model": "deepseek-chat",
                    "usage": {"prompt_tokens": 5000,
                              "completion_tokens": 1000}})()

        r = tracked_complete(cost, _Router(), [], model="deepseek-chat",
                             task_id="t/m", trace_id="tr_m", actor="runner")
        self.assertEqual(r.content, "回复")
        snap = cost.finalize_task("t/m")
        self.assertEqual(snap["model_calls"], 1)
        self.assertEqual(snap["input_tokens"], 5000)
        # deepseek-chat: 5*0.00027 + 1*0.0011 = 0.00135 + 0.0011 = 0.00245
        self.assertAlmostEqual(snap["cost_estimate"], 0.00245, places=9)


class CostEntrypointsTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sectrust-cost-entry-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cost_db = os.path.join(self.tmp, "cost.db")
        self._old = os.environ.get("COST_DB_PATH")
        os.environ["COST_DB_PATH"] = self.cost_db

        def restore():
            if self._old is None:
                os.environ.pop("COST_DB_PATH", None)
            else:
                os.environ["COST_DB_PATH"] = self._old
        self.addCleanup(restore)

    def test_mcp_five_tools_roundtrip(self):
        names = [t["name"] for t in mcp_mod._TOOLS]
        for n in ("cost_record", "cost_task", "cost_finalize", "cost_show",
                  "cost_dashboard"):
            self.assertIn(n, names)
            self.assertIn(n, mcp_mod._TOOL_ANNOTATIONS)
        d = mcp_mod._dispatch
        self.assertEqual(json.loads(d("cost_record", {
            "layer": "model", "task_id": "t/a", "model": "gpt-4o",
            "input_tokens": 100, "output_tokens": 50}))["layer"], "model")
        bad = json.loads(d("cost_record", {"layer": "bogus"}))
        self.assertEqual(bad["error"], "cost_rejected")
        snap = json.loads(d("cost_finalize", {
            "task_id": "t/a", "role": "copywriter", "template": "campaign"}))
        self.assertEqual(snap["model_calls"], 1)
        self.assertAlmostEqual(snap["cost_estimate"], 0.00075, places=9)
        written = json.loads(d("cost_task", {
            "task_id": "t/u", "model_calls": 12, "input_tokens": 8400,
            "output_tokens": 2100, "tool_calls": 7, "retries": 2,
            "sandbox_seconds": 45, "cost_estimate": 0.0123,
            "trace_id": "tr_u"}))
        self.assertTrue(written["written"])
        shown = json.loads(d("cost_show", {"task_id": "t/u"}))
        self.assertEqual(shown["record"]["model_calls"], 12)
        missing = json.loads(d("cost_show", {"task_id": "no-such"}))
        self.assertEqual(missing["error"], "cost_rejected")
        dash = json.loads(d("cost_dashboard", {"days": 30}))
        self.assertEqual(dash["totals"]["tasks"], 2)   # t/a + t/u

    def test_rest_endpoints(self):
        api = RegistryAPI(db_path=os.path.join(self.tmp, "tasks.db"))
        store = CostStore(db_path=self.cost_db)
        store.record_model_call(task_id="t/r", trace_id="tr_r",
                                model="gpt-4o", input_tokens=100,
                                output_tokens=50)
        store.finalize_task("t/r", role="editor", template="docs")
        store.close()
        code, dash = api._cost_dashboard([], {}, {})
        self.assertEqual(code, 200)
        self.assertEqual(dash["totals"]["tasks"], 1)
        code, rec = api._cost_show(["t/r"], {}, {})
        self.assertEqual(code, 200)
        self.assertEqual(rec["record"]["role"], "editor")
        code, err = api._cost_show(["no-such"], {}, {})
        self.assertEqual(code, 404)


if __name__ == "__main__":
    unittest.main()
