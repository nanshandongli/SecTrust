"""通用结果汇总 + 端到端流水线的单元测试。

覆盖：
  - ResultSummarizer.collect：状态汇总、产出收集、产物抽取、未完成检测；
  - _extract_artifacts：result["artifacts"] 列表 / stdout 路径两种约定；
  - 端到端：tools/pipeline_run.run_pipeline（真实 ExecutionDaemon + noop DAG）
    → 汇总 complete=True、total==5；
  - 部分失败：注入一个 failed 任务 → complete=False 且进入未完成清单。
"""

import os
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from task_registry import TaskRegistry                # noqa: E402
from summarizer import ResultSummarizer, _extract_artifacts  # noqa: E402
from tools.pipeline_run import run_pipeline           # noqa: E402


def _reg_with(tasks: dict) -> str:
    """造一个临时库，按 {tid: (status, payload)} 登记任务，返回 db_path。"""
    db = os.path.join(tempfile.mkdtemp(prefix="summ_"), "t.db")
    reg = TaskRegistry(db_path=db, runner_id="test")
    for tid, (status, payload) in tasks.items():
        reg.register_task(tid, payload=payload)
        if status != "registered":
            reg.update_task_status(tid, status)
    reg.close()
    return db


class SummarizerCollectTest(unittest.TestCase):
    def test_status_rollup_and_results(self):
        db = _reg_with({
            "a": ("completed", {"name": "A", "result": {"x": 1}}),
            "b": ("completed", {"name": "B", "result": {"y": 2}}),
            "c": ("registered", {"name": "C"}),
        })
        reg = TaskRegistry(db_path=db)
        try:
            data = ResultSummarizer().collect(reg)
        finally:
            reg.close()
        self.assertEqual(data["total"], 3)
        self.assertEqual(data["by_status"].get("completed"), 2)
        self.assertEqual(data["by_status"].get("registered"), 1)
        names = {t["name"] for t in data["tasks"]}
        self.assertEqual(names, {"A", "B", "C"})
        # 未完成检测：C 还是 registered
        self.assertFalse(data["complete"])
        self.assertEqual({i["task_id"] for i in data["incomplete"]}, {"c"})

    def test_artifact_from_explicit_list(self):
        arts = _extract_artifacts(
            "t1", {"artifacts": ["/tmp/out1.json", "/tmp/out2.md"]})
        self.assertEqual(len(arts), 2)
        self.assertEqual(arts[0]["path"], "/tmp/out1.json")
        self.assertEqual(arts[0]["task_id"], "t1")

    def test_artifact_from_stdout_path(self):
        res = {"op": "shell",
               "stdout": "wrote /tmp/report.md\n",
               "backend": "local-enforced"}
        arts = _extract_artifacts("t2", res)
        self.assertTrue(any(a["path"].endswith("report.md") for a in arts))

    def test_artifact_none_for_plain_dict(self):
        self.assertEqual(_extract_artifacts("t3", {"op": "noop"}), [])

    def test_markdown_contains_names_and_incomplete(self):
        db = _reg_with({
            "a": ("completed", {"name": "调研", "result": {"ok": True}}),
            "b": ("failed", {"name": "抓取", "error": "网络超时"}),
        })
        reg = TaskRegistry(db_path=db)
        try:
            md, data = ResultSummarizer().summarize(reg, title="测试交付物")
        finally:
            reg.close()
        self.assertIn("测试交付物", md)
        self.assertIn("调研", md)
        self.assertIn("抓取", md)
        self.assertIn("网络超时", md)                  # 失败原因进了汇总
        self.assertIn("未完成项", md)                  # 有未完成章节
        self.assertFalse(data["complete"])


class PipelineEndToEndTest(unittest.TestCase):
    def test_real_daemon_noop_pipeline_closes_loop(self):
        db = os.path.join(tempfile.mkdtemp(prefix="pe2e_"), "p.db")
        md, data = run_pipeline(db, title="E2E", with_shell=False, timeout=30)
        # 默认 DAG：research/lint/fetch/analyze/publish = 5 个 noop 任务
        self.assertEqual(data["total"], 5)
        self.assertEqual(data["by_status"].get("completed"), 5)
        self.assertTrue(data["complete"], "流水线应当全部完成")
        self.assertIn("发布交付物", md)               # 末端任务进汇总
        self.assertIn("5 / 5", md)


if __name__ == "__main__":
    unittest.main()
