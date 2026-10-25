"""Trace 接线回归测试（2026-10-25）：四个挂点 + 两个入口。

锁定承诺：
1. **planner 传播**：build(trace_id=...) 把 trace_id 写进每个任务 payload
   （普通叶 / fanout 分支 / 汇合点全覆盖）；scheduler 带 tracer 时记 plan_built；
2. **DagScheduler 旁路**：登记/认领/完成/失败自动留痕（含 blocked_downstream），
   tracer 故障**绝不挡调度主链路**；无 tracer 时行为与从前完全一致；
3. **浏览器留痕**：goto/click/type/screenshot/wait_for 记 tool_call（带耗时），
   失败动作也留痕（带 error）后再抛；**type_text 的明文只留指纹不落库**；
4. **入口**：MCP trace_start/record/show/list 与 REST GET /traces、/traces/{id}
   （含 404）走通；tool 注解数量随 _TOOLS 自动校验。
"""

from __future__ import annotations

import base64
import json
import os
import queue
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import mcp_server as mcp_mod                                    # noqa: E402
from api.app import RegistryAPI                                 # noqa: E402
from execution.browser import BrowserError, BrowserSkill        # noqa: E402
from orchestration import DagScheduler                          # noqa: E402
from planner import Plan                                        # noqa: E402
from trace import TraceStore                                    # noqa: E402


def _read_raw(db_path: str) -> bytes:
    with open(db_path, "rb") as f:
        return f.read()


# ================= planner 传播 =================

class PlannerTraceWiringTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sectrust-tw-plan-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.reg_db = os.path.join(self.tmp, "tasks.db")
        self.trace_db = os.path.join(self.tmp, "trace.db")
        self.store = TraceStore(db_path=self.trace_db)
        self.addCleanup(self.store.close)
        self.sched = DagScheduler(db_path=self.reg_db, tracer=self.store)
        self.addCleanup(self.sched.close)

    def _plan(self, subtasks):
        return Plan(requirement="双十一营销", template="ecommerce",
                    title="营销全案", slots={"topic": "新品"}, subtasks=subtasks)

    def test_linear_plan_stamps_trace_id(self):
        tid = self.store.start_trace(title="营销全案")
        plan = self._plan([
            {"key": "research", "name": "调研", "instruction": "调研{topic}",
             "depends_on": []},
            {"key": "copy", "name": "文案", "instruction": "写文案",
             "depends_on": ["research"]},
        ])
        created = plan.build(self.sched, plan_id="p1", trace_id=tid)
        self.assertEqual(len(created), 2)
        for task_id in created:
            payload = self.sched.registry.get_task(task_id)["payload"]
            self.assertEqual(payload["trace_id"], tid)
        # plan_built 事件
        evs = [e for e in self.store.events(tid) if e["kind"] == "plan_built"]
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0]["detail"]["tasks"], created)
        self.assertEqual(evs[0]["detail"]["plan_id"], "p1")

    def test_fanout_plan_stamps_all_nodes(self):
        tid = self.store.start_trace()
        plan = self._plan([
            {"key": "deliver", "name": "交付", "instruction": "出方案",
             "depends_on": [], "fanout": 3},
        ])
        created = plan.build(self.sched, plan_id="p2", trace_id=tid)
        # 3 分支 + 1 汇合
        self.assertEqual(len(created), 4)
        for task_id in created:
            payload = self.sched.registry.get_task(task_id)["payload"]
            self.assertEqual(payload["trace_id"], tid)
        branches = [t for t in created if "#" in t]
        gather = [t for t in created if t.endswith("__ballot")]
        self.assertEqual(len(branches), 3)
        self.assertEqual(len(gather), 1)

    def test_no_trace_id_no_key(self):
        plan = self._plan([
            {"key": "a", "name": "一步", "instruction": "做", "depends_on": []},
        ])
        created = plan.build(self.sched, plan_id="p3")
        payload = self.sched.registry.get_task(created[0])["payload"]
        self.assertNotIn("trace_id", payload)

    def test_scheduler_without_tracer_still_builds(self):
        plain = DagScheduler(db_path=os.path.join(self.tmp, "plain.db"))
        self.addCleanup(plain.close)
        plan = self._plan([
            {"key": "a", "name": "一步", "instruction": "做", "depends_on": []},
        ])
        created = plan.build(plain, plan_id="p4", trace_id="tr_whatever")
        self.assertEqual(len(created), 1)


# ================= DagScheduler 旁路 =================

class DagTracerWiringTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sectrust-tw-dag-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.reg_db = os.path.join(self.tmp, "tasks.db")
        self.trace_db = os.path.join(self.tmp, "trace.db")
        self.store = TraceStore(db_path=self.trace_db)
        self.addCleanup(self.store.close)
        self.tid = self.store.start_trace(title="dag 链路")
        self.sched = DagScheduler(db_path=self.reg_db, tracer=self.store)
        self.addCleanup(self.sched.close)

    def _add(self, task_id, deps=()):
        self.sched.add_task(task_id, payload={"trace_id": self.tid},
                            depends_on=list(deps))

    def test_full_lifecycle_events(self):
        self._add("a")
        self._add("b", ["a"])
        self.sched.claim_ready(runner_id="w1")
        self.sched.complete("a", result={"summary": "调研完成", "body": "x" * 50})
        self.sched.claim_ready(task_id="b", runner_id="w2")
        kinds = [e["kind"] for e in self.store.events(self.tid)]
        self.assertEqual(kinds, ["task_registered", "task_registered",
                                 "task_claimed", "task_completed",
                                 "task_claimed"])
        claimed = self.store.events(self.tid)[2]
        self.assertEqual(claimed["actor"], "w1")
        self.assertEqual(claimed["task_id"], "a")
        done = self.store.events(self.tid)[3]
        self.assertEqual(done["detail"]["result_keys"], ["body", "summary"])
        self.assertIsNotNone(done["content_hash"])
        # 结果原文不进库
        self.assertNotIn("调研完成".encode("utf-8"), _read_raw(self.trace_db))

    def test_fail_records_blocked_downstream(self):
        self._add("c")
        self._add("d", ["c"])
        self._add("e", ["d"])
        self.sched.claim_ready(task_id="c", runner_id="w1")
        blocked = self.sched.fail("c", error="模型超时")
        self.assertEqual(blocked, ["d", "e"])
        fail_ev = [e for e in self.store.events(self.tid)
                   if e["kind"] == "task_failed"]
        self.assertEqual(len(fail_ev), 1)
        self.assertEqual(fail_ev[0]["detail"]["error"], "模型超时")
        self.assertEqual(fail_ev[0]["detail"]["blocked_downstream"], ["d", "e"])

    def test_broken_tracer_never_blocks_scheduling(self):
        class _BrokenTracer:
            def record(self, *a, **kw):
                raise RuntimeError("trace 库炸了")

        sched = DagScheduler(db_path=os.path.join(self.tmp, "broken.db"),
                             tracer=_BrokenTracer())
        self.addCleanup(sched.close)
        sched.add_task("x", payload={"trace_id": "tr_x"})
        t = sched.claim_ready(runner_id="w1")
        self.assertIsNotNone(t)
        self.assertTrue(sched.complete("x"))
        self.assertEqual(sched.stats()["by_status"]["completed"], 1)

    def test_no_tracer_backwards_compatible(self):
        sched = DagScheduler(db_path=os.path.join(self.tmp, "plain.db"))
        self.addCleanup(sched.close)
        sched.add_task("x")
        t = sched.claim_ready(runner_id="w1")
        self.assertEqual(t["task_id"], "x")
        self.assertTrue(sched.complete("x"))

    def test_task_without_trace_id_skipped(self):
        """没标 trace_id 的任务不留痕（不猜、不硬造轨迹）。"""
        self.sched.add_task("solo")
        self.sched.claim_ready(task_id="solo", runner_id="w1")
        self.assertEqual(self.store.events(self.tid), [])


# ================= 浏览器留痕 =================

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
    """带 reader 兼容队列的假 WS（与 test_browser 同思路，自包含）。"""

    def __init__(self, handler=None):
        self.sent = []
        self.handler = handler or (lambda msg: {})
        self.closed = False
        self._incoming: "queue.Queue" = queue.Queue()

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
            except queue.Empty:
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


class BrowserTraceWiringTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sectrust-tw-browser-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.trace_db = os.path.join(self.tmp, "trace.db")
        self.store = TraceStore(db_path=self.trace_db)
        self.addCleanup(self.store.close)
        self.tid = self.store.start_trace(title="浏览器任务")
        self.out = os.path.join(self.tmp, "shot.png")

    def _skill(self, handler, **kw):
        def launcher(edge, port, headless, profile):
            return _FakeProc()
        sk = BrowserSkill(edge_path="/fake/msedge.exe", timeout=0.4,
                          launcher=launcher,
                          ws_factory=lambda url: _CdpFakeWS(handler),
                          http_get=_fake_http(),
                          tracer=self.store, task_id="t-1",
                          trace_id=self.tid, **kw)
        self.addCleanup(sk.close)
        return sk

    def test_goto_click_type_screenshot_wait_for_all_traced(self):
        def handler(msg):
            m = msg.get("method")
            if m == "Runtime.evaluate":
                return {"result": {"value": {"x": 10.0, "y": 20.0}}}
            if m == "Page.captureScreenshot":
                return {"data": base64.b64encode(b"\x89PNG-fake").decode()}
            return {}
        sk = self._skill(handler)
        self.assertEqual(sk.start(), "cdp")
        sk.goto("https://example.com")
        sk.click("#btn")
        sk.type_text("#input", "用户输入的明文")
        sk.screenshot(self.out)
        self.assertTrue(sk.wait_for("#done", timeout=1.0))
        evs = self.store.events(self.tid)
        self.assertEqual(len(evs), 5)
        for e in evs:
            self.assertEqual(e["kind"], "tool_call")
            self.assertEqual(e["task_id"], "t-1")
            self.assertEqual(e["actor"], "browser")
            self.assertIn("duration_s", e["detail"])
        self.assertEqual(evs[0]["summary"], "goto https://example.com")
        self.assertEqual(evs[1]["detail"]["at"], {"x": 10.0, "y": 20.0})
        self.assertEqual(evs[2]["detail"]["chars"], 7)
        self.assertEqual(evs[3]["detail"]["bytes"], 9)
        self.assertTrue(evs[4]["detail"]["found"])
        # type_text 的明文只留指纹
        self.assertIsNotNone(evs[2]["content_hash"])
        self.assertNotIn("用户输入的明文".encode("utf-8"), _read_raw(self.trace_db))
        self.assertTrue(self.store.verify(self.tid, evs[2]["seq"],
                                          "用户输入的明文"))

    def test_failed_click_traced_then_raises(self):
        sk = self._skill(lambda msg: {"result": {"value": None}})
        sk.start()
        with self.assertRaises(BrowserError):
            sk.click("#nope")
        evs = self.store.events(self.tid)
        self.assertEqual(len(evs), 1)
        self.assertIn("（失败）", evs[0]["summary"])
        self.assertIn("找不到元素", evs[0]["detail"]["error"])

    def test_no_tracer_still_works(self):
        def launcher(edge, port, headless, profile):
            return _FakeProc()
        sk = BrowserSkill(edge_path="/fake/msedge.exe", timeout=0.4,
                          launcher=launcher,
                          ws_factory=lambda url: _CdpFakeWS(
                              lambda msg: {"result": {"value": {"x": 1.0, "y": 2.0}}}),
                          http_get=_fake_http())
        self.addCleanup(sk.close)
        sk.start()
        sk.goto("https://example.com")
        sk.click("#btn")
        self.assertEqual(self.store.events(self.tid), [])


# ================= 入口：MCP + REST =================

class TraceEntrypointsTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sectrust-tw-entry-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.trace_db = os.path.join(self.tmp, "trace.db")
        self._old_env = os.environ.get("TRACE_DB_PATH")
        os.environ["TRACE_DB_PATH"] = self.trace_db

        def restore():
            if self._old_env is None:
                os.environ.pop("TRACE_DB_PATH", None)
            else:
                os.environ["TRACE_DB_PATH"] = self._old_env
        self.addCleanup(restore)

    def test_mcp_tools_declared_and_roundtrip(self):
        names = [t["name"] for t in mcp_mod._TOOLS]
        for n in ("trace_start", "trace_record", "trace_show", "trace_list"):
            self.assertIn(n, names)
        out = json.loads(mcp_mod._dispatch(
            "trace_start", {"title": "入口测试", "started_by": "test"}))
        tid = out["trace_id"]
        rec = json.loads(mcp_mod._dispatch(
            "trace_record", {"trace_id": tid, "kind": "approval_resolved",
                             "task_id": "t-9", "actor": "human",
                             "summary": "批准外发", "content": "密钥原文"}))
        self.assertEqual(rec["seq"], 1)
        shown = json.loads(mcp_mod._dispatch("trace_show", {"trace_id": tid}))
        self.assertEqual(shown["trace"]["title"], "入口测试")
        self.assertEqual(shown["events"][0]["task_id"], "t-9")
        by_task = json.loads(mcp_mod._dispatch("trace_show", {"task_id": "t-9"}))
        self.assertEqual(by_task["trace_id"], tid)
        listed = json.loads(mcp_mod._dispatch("trace_list", {}))
        self.assertEqual(len(listed), 1)
        missing = json.loads(mcp_mod._dispatch("trace_show", {"trace_id": "tr_none"}))
        self.assertEqual(missing["error"], "trace_rejected")
        bad = json.loads(mcp_mod._dispatch(
            "trace_record", {"trace_id": "", "kind": "x"}))
        self.assertEqual(bad["error"], "trace_rejected")
        self.assertNotIn("密钥原文".encode("utf-8"), _read_raw(self.trace_db))

    def test_rest_endpoints(self):
        api = RegistryAPI(db_path=os.path.join(self.tmp, "tasks.db"))
        store = TraceStore(db_path=self.trace_db)
        tid = store.start_trace(title="REST 测试")
        store.record(tid, "task_completed", task_id="t-1")
        store.close()
        code, rows = api._trace_list([], {}, {})
        self.assertEqual(code, 200)
        self.assertEqual(rows[0]["trace_id"], tid)
        code, payload = api._trace_show([tid], {}, {})
        self.assertEqual(code, 200)
        self.assertEqual(len(payload["events"]), 1)
        code, payload = api._trace_show([tid], {}, {"after_seq": ["1"]})
        self.assertEqual(payload["events"], [])
        code, err = api._trace_show(["tr_missing"], {}, {})
        self.assertEqual(code, 404)


if __name__ == "__main__":
    unittest.main()
