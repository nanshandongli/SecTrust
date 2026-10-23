"""压测工具自测：验证 worker 参数传递、错误汇总、场景结果结构。

刻意用小规模（进程少、ops 少）——本测试的目的是**工具本身正确**，
真实性能曲线靠人工跑 `tools/stress_test.py --scenario all`。
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools import stress_test as st  # noqa: E402


class ErrSummaryTest(unittest.TestCase):
    """错误汇总：分类计数 + 样文本（现场排查靠样本）。"""

    def test_counts_by_kind_and_keeps_sample(self):
        results = [
            {"ok": 0, "errors": ["OperationalError: attempt to write a readonly database"],
             "elapsed": 1.0},
            {"ok": 1, "errors": ["OperationalError: database is locked",
                                 "ValueError: bad"], "elapsed": 2.0},
        ]
        s = st._err_summary(results)
        self.assertEqual(s["total"], 3)
        self.assertEqual(s["by_kind"],
                         {"OperationalError": 2, "ValueError": 1})
        self.assertEqual(len(s["sample"]), 3, "样本最多留 3 条")
        self.assertIn("readonly", s["sample"][0])


def _echo_worker(a, b, c, out_file):
    """回显 worker：把参数写进结果（验证散装传递）。"""
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump({"ok": a + b + c, "errors": [], "elapsed": 0.1}, f)


def _boom_worker(out_file):
    """直接崩溃的 worker（不写结果文件）——验证 exitcode 上报。"""
    os._exit(3)


class RunParallelTest(unittest.TestCase):
    """_run_parallel：散装参数必须正确传递（曾在这里栽过：整体传 tuple）。"""

    def test_args_are_unpacked_not_nested(self):
        """worker 的位置参数应是散装的，不是一个 tuple。"""
        res = st._run_parallel(_echo_worker, [(1, 2, 3)])
        self.assertEqual(res[0]["ok"], 6,
                         "参数必须散装传递（*t），不能整体当一个 tuple")

    def test_crashed_worker_reports_exitcode(self):
        """worker 崩到没写结果文件时，应记录退出码而非笼统的"无结果文件"。"""
        res = st._run_parallel(_boom_worker, [()])
        self.assertEqual(res[0]["ok"], 0)
        self.assertTrue(any("exitcode" in e for e in res[0]["errors"]),
                        f"应记录 exitcode，实际: {res[0]['errors']}")


class ScenarioTest(unittest.TestCase):
    """小规模真实场景：验证结果结构与正确性（非性能）。"""

    def setUp(self):
        self.db = os.path.join(tempfile.mkdtemp(prefix="stress_t_"), "t.db")

    def test_write_scenario_structure(self):
        r = st.scenario_write(self.db, n_procs=2, n_ops=20)
        self.assertEqual(r["scenario"], "write")
        self.assertEqual(r["total_ops"], 40)
        self.assertEqual(r["succeeded"], 40, "小规模无竞争，应全部成功")
        self.assertEqual(r["failed"], 0)
        self.assertEqual(r["errors"]["total"], 0)
        self.assertGreater(r["tps"], 0)

    def test_claim_scenario_cas_correct(self):
        """CAS 正确性：N 个进程抢同一批任务，每个只能被抢到一次。"""
        from task_registry import TaskRegistry
        reg = TaskRegistry(db_path=self.db, runner_id="seed")
        for i in range(20):
            reg.register_task(f"cas-{i}")
        reg.close()

        r = st.scenario_claim(self.db, n_procs=4, n_tasks=20)
        self.assertEqual(r["claimed_unique"], 20,
                         "20 个任务应各被抢到一次")
        self.assertEqual(r["duplicated"], [], "不允许有任务被重复认领")
        self.assertTrue(r["correct"])

    def test_read_scenario_structure(self):
        r = st.scenario_read(self.db, n_procs=2, n_ops=10)
        self.assertEqual(r["scenario"], "read")
        self.assertEqual(r["succeeded"], 20)
        self.assertEqual(r["failed"], 0)


if __name__ == "__main__":
    unittest.main()
