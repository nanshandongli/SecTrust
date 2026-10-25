"""真实多机压测的回归测试（小规模、真实多进程 + 真实 TCP 协调）。

复用 tools/cluster_stress_test.run，以 2 机 / 20 任务 / 1.5s 的小规模跑一遍，
断言跨机核心不变量：
  - 基准 A：同一把锁任意时刻只有一个持有者（violations == 0）；
  - 基准 B：N 台真实 daemon 跑共享库，total 完成 == seed，跨机 0 重复执行。

这是真·多进程（Windows 上 spawn = 进程隔离），不是线程模拟。
"""

import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from tools.cluster_stress_test import run  # noqa: E402


class ClusterStressTest(unittest.TestCase):
    def test_real_multi_machine_invariants(self):
        # 小规模、短时长，CI/本地都能快速跑完
        report = run(machines=2, tasks=24, duration=1.5, pool_size=6,
                     out_dir="data", bench_b=True)
        a = report["bench_a_coordination"]
        b = report["bench_b_pipeline"]

        # 基准 A：跨机锁互斥（同锁最大并发持有者必须 == 1，违规 == 0）
        self.assertEqual(a["lock_exclusion"]["max_concurrent_per_lock"], 1,
                         "同一把锁出现了 >1 个并发持有者（双抢）")
        self.assertEqual(a["lock_exclusion"]["violations"], 0,
                         "跨机锁互斥被破坏")
        self.assertGreater(a["total_lock_ops"], 0, "没有产生锁操作")
        # 死信阈值行为：达阈值入队、recover 后消失
        self.assertTrue(a["dead_letter"]["present_after_threshold"])
        self.assertTrue(a["dead_letter"]["gone_after_recover"])

        # 基准 B：跨机零重复执行 + 全部完成
        self.assertEqual(b["double_execution_count"], 0,
                         "跨机重复执行了同一任务：%s" % b["double_executions"])
        self.assertEqual(b["tasks_completed_unique"], b["tasks_seeded"],
                         "完成的任务数与 seed 不一致")
        self.assertTrue(b["correct"], "跨机流水线正确性未通过")

        # 落盘产物存在（"留下数据"）
        self.assertTrue(os.path.exists(report["_artifact_json"]))
        self.assertTrue(os.path.exists(report["_artifact_md"]))

        # 清理落盘产物（避免污染 data/）
        for p in (report["_artifact_json"], report["_artifact_md"]):
            try:
                os.remove(p)
            except OSError:
                pass


if __name__ == "__main__":
    unittest.main()
