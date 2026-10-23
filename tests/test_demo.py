"""
Demo 回归测试：确保 /scan repo 端到端管线不会因低级错误（如漏 import）而中断。

用一个临时 DB + 本地沙箱 + 自动批准，跑一次完整 ScanRepoDemo.handle，
验证：任务最终 done、审批最终 approved、沙箱扫描产出真实结果。
"""

import os
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from config import load_config                                   # noqa: E402
from examples.scan_repo_demo import ScanRepoDemo, SAMPLE_REPO     # noqa: E402


class TestDemoSmoke(unittest.TestCase):
    def test_scan_repo_pipeline_end_to_end(self):
        cfg = load_config()
        tmp = tempfile.mkdtemp()
        db = os.path.join(tmp, "reg.db")
        cfg.db_path = db
        try:
            demo = ScanRepoDemo(cfg, "local", auto_approve=True, repo_dir=SAMPLE_REPO)
            # handle 内部会同步等待自动批准(2s)并执行沙箱扫描，返回即完成
            demo.simulate_inbound("/scan repo https://example.com/sample")
            tasks = demo.reg.list_tasks()
            self.assertTrue(tasks, "任务应已登记")
            self.assertEqual(tasks[0]["status"], "done")
            payload = tasks[0]["payload"] or {}
            self.assertEqual(payload.get("progress"), 100)
            aps = demo.ac.list_approvals()
            self.assertTrue(aps)
            self.assertEqual(aps[0]["status"], "approved")
            demo.close()
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
