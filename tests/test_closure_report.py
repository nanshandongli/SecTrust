"""任务结案报告（closure report）测试。

验证 ResultSummarizer 把 Trace 回放 / 成本看板 / 审批链统一落盘为一份
结构化产物（JSON + 标准化 Markdown）。覆盖：
  - 三路存储为 None 时不崩、章节标注「未接入」（向后兼容）；
  - 用真实 TraceStore/CostStore/ApprovalCenter 建场景，closure_collect 把三路
    数据并进来，write_closure 落盘 json+md 且内容齐全；
  - ApprovalCenter.approvals_for_task 按任务反查正确。
"""
import json
import os
import shutil
import tempfile
import unittest

from summarizer import ResultSummarizer
from task_registry import TaskRegistry
from trace import TraceStore, new_trace_id
from cost import CostStore
from approval import ApprovalCenter
from tools.pipeline_run import run_pipeline


class FakeTrace:
    def __init__(self, tid, events):
        self.tid = tid
        self.events = events

    def by_task(self, task_id):
        return {"trace_id": self.tid, "task_id": task_id, "events": self.events}

    def events(self, trace_id):
        return self.events

    def get(self, trace_id):
        return {"trace_id": self.tid, "status": "completed", "event_count": len(self.events)}


class FakeCost:
    def __init__(self, rec):
        self.rec = rec

    def task_cost(self, task_id):
        return self.rec

    def dashboard(self, days=30, top=5):
        return {"days": days, "totals": {"tasks": 1, "cost_estimate": 0.001},
                "price_note": "x"}


class FakeApproval:
    def __init__(self, chains):
        self.chains = chains

    def approvals_for_task(self, task_id):
        return [c["approval"] for c in self.chains]

    def audit(self, aid):
        for c in self.chains:
            if c["approval"]["approval_id"] == aid:
                return c
        return {"approval": {"approval_id": aid}, "events": []}


class ClosureReportUnitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.reg = TaskRegistry(os.path.join(self.tmp, "t.db"))

    def tearDown(self):
        self.reg.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _reg(self, tid):
        self.reg.register_task("t1", payload={"name": "调研", "trace_id": tid,
                                              "result": {"artifacts": ["a.json"]}})
        self.reg.update_task_status("t1", "completed")

    def test_no_stores_marks_unavailable(self):
        tid = new_trace_id()
        self._reg(tid)
        data = ResultSummarizer().closure_collect(self.reg)
        self.assertEqual(data["summary"]["total"], 1)
        self.assertTrue(data["summary"]["complete"])
        t = data["tasks"][0]
        self.assertFalse(t["trace"]["available"])
        self.assertFalse(t["cost"]["available"])
        self.assertFalse(t["approval"]["available"])
        md = ResultSummarizer().closure_markdown(data)
        self.assertIn("任务结案报告", md)
        self.assertIn("未接入轨迹层", md)

    def test_fake_stores_unify_sections(self):
        tid = new_trace_id()
        self._reg(tid)
        evs = [{"seq": 1, "ts": 1.0, "kind": "plan_built", "actor": "sys",
                "summary": "拆解完成", "detail": None}]
        trace = FakeTrace(tid, evs)
        cost = FakeCost({"record": {"model_calls": 1, "input_tokens": 10,
                                    "output_tokens": 5, "cost_estimate": 0.001}})
        chains = [{"approval": {"approval_id": "ap-1", "action": "publish",
                                "status": "approved", "decided_by": "human"},
                   "events": [{"kind": "approval_requested", "detail": {"risk": "high"}}]}]
        appr = FakeApproval(chains)
        data = ResultSummarizer().closure_collect(
            self.reg, trace=trace, cost=cost, approval=appr, trace_id=tid)
        t = data["tasks"][0]
        self.assertTrue(t["trace"]["available"])
        self.assertTrue(t["cost"]["available"])
        self.assertTrue(t["approval"]["available"])
        md = ResultSummarizer().closure_markdown(data)
        self.assertIn("Trace 回放", md)
        self.assertIn("成本", md)
        self.assertIn("审批链", md)
        self.assertIn("risk=high", md)


class ClosureReportIntegrationTest(unittest.TestCase):
    """用真实 TraceStore/CostStore/ApprovalCenter 证明三路数据真正统一落盘。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.reg_db = os.path.join(self.tmp, "task_registry.db")
        self.reg = TaskRegistry(self.reg_db)
        self.trace = TraceStore(self.reg_db)
        self.cost = CostStore(os.path.join(self.tmp, "cost.db"))
        self.appr = ApprovalCenter(self.reg_db, tracer=self.trace)
        self.tid = new_trace_id()

    def tearDown(self):
        self.reg.close()
        self.trace.close()
        self.cost.close()
        self.appr.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _scenario(self):
        for tid_, name, res in (
            ("t1", "调研", {"artifacts": ["/tmp/raw.json"]}),
            ("t2", "发布", {"op": "noop"}),
        ):
            self.reg.register_task(tid_, payload={"name": name, "trace_id": self.tid,
                                                "result": res})
            self.reg.update_task_status(tid_, "completed")
        # Trace 回放
        self.trace.start_trace(title="demo", trace_id=self.tid)
        for t in ("t1", "t2"):
            self.trace.record(self.tid, "plan_built", task_id=t, summary="拆解")
            self.trace.record(self.tid, "task_completed", task_id=t, summary="完成")
        self.trace.finish(self.tid, status="completed")
        # 成本
        self.cost.record_model_call(task_id="t1", trace_id=self.tid,
                                    model="gpt-4o-mini", input_tokens=120,
                                    output_tokens=60)
        self.cost.finalize_task("t1", trace_id=self.tid, outcome="ok")
        # 审批链：publish 高危动作
        aid = self.appr.request("publish", "发布报告", task_id="t2",
                                trace_id=self.tid)
        self.appr.decide(aid, "approve", by="human")
        return aid

    def test_unified_artifact(self):
        aid = self._scenario()
        rs = ResultSummarizer()
        data = rs.closure_collect(self.reg, trace=self.trace, cost=self.cost,
                                 approval=self.appr, trace_id=self.tid)
        self.assertEqual(data["summary"]["total"], 2)
        self.assertTrue(data["summary"]["complete"])
        by_id = {t["task_id"]: t for t in data["tasks"]}
        self.assertTrue(by_id["t1"]["trace"]["available"])
        self.assertTrue(by_id["t1"]["cost"]["available"])
        self.assertEqual(by_id["t2"]["approval"]["approvals"][0]["approval"]["approval_id"], aid)
        # 高危标记应出现在审核汇总
        md = rs.closure_markdown(data)
        self.assertIn("risk=high", md)
        self.assertIn("成本看板", md)
        self.assertIn("吞金榜", md)

    def test_write_closure_files(self):
        self._scenario()
        mp, jp = ResultSummarizer().write_closure(
            self.reg, out_dir=self.tmp, trace=self.trace, cost=self.cost,
            approval=self.appr, trace_id=self.tid)
        self.assertTrue(os.path.exists(mp))
        self.assertTrue(os.path.exists(jp))
        with open(jp, encoding="utf-8") as f:
            j = json.load(f)
        self.assertEqual(j["report_type"], "task_closure_report")
        self.assertEqual(len(j["tasks"]), 2)
        self.assertIsNotNone(j["cost_dashboard"])
        with open(mp, encoding="utf-8") as f:
            md = f.read()
        self.assertIn("## 一、概览", md)
        self.assertIn("## 二、逐任务明细", md)
        self.assertIn("全局轨迹回放", md)        # 整条流水线回放一次
        self.assertIn("成本看板", md)             # 章节号随是否有全局轨迹浮动
        self.assertIn("审批链审计汇总", md)

    def test_approvals_for_task(self):
        self._scenario()
        rows = self.appr.approvals_for_task("t2")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["action"], "publish")
        self.assertEqual(rows[0]["status"], "approved")
        # 无审批的任务返回空
        self.assertEqual(self.appr.approvals_for_task("t1"), [])


class PipelineClosureTest(unittest.TestCase):
    """真实 ExecutionDaemon 跑 DAG + --closure 落盘统一结案报告的端到端路径。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "p.db")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_run_pipeline_closure(self):
        result = run_pipeline(self.db, title="E2E结案", with_shell=False,
                              timeout=30, closure=True, closure_out_dir=self.tmp)
        self.assertEqual(len(result), 3)
        md, data, paths = result
        self.assertTrue(data["complete"])
        self.assertIsNotNone(paths)
        mp, jp = paths
        self.assertTrue(os.path.exists(mp))
        self.assertTrue(os.path.exists(jp))
        with open(mp, encoding="utf-8") as f:
            txt = f.read()
        self.assertIn("关联轨迹", txt)          # 结案报告头部字段
        self.assertIn("risk=high", txt)        # publish 高危审批
        self.assertIn("全局轨迹回放", txt)      # 整条流水线回放一次


if __name__ == "__main__":
    unittest.main()
