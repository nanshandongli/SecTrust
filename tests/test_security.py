"""
安全特性回归测试：审批中心（Human-in-the-loop）与沙箱（隔离 + 越权拦截）。

覆盖核心安全契约：
- 审批默认拒绝（超时 expire，绝不自动放行）
- 审批通过后才放行；拒绝/过期则不放行
- 本地沙箱能正常执行；能拦截明显越权路径；超时被强杀

零依赖，unittest / pytest 均可收集。
"""

import os
import sys
import tempfile
import unittest

# 让测试能从仓库根导入模块（与 tests/test_runtime.py 保持一致）
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from approval import ApprovalCenter          # noqa: E402
from sandbox import LocalSandbox, SandboxManager, safe_scan_command  # noqa: E402


class TestApproval(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "reg.db")
        self.ac = ApprovalCenter(db_path=self.db)

    def tearDown(self):
        self.ac.close()
        for f in (self.db, self.db + "-wal", self.db + "-shm"):
            try:
                os.remove(f)
            except FileNotFoundError:
                pass
        try:
            os.rmdir(self.tmp)
        except OSError:
            pass

    def test_request_and_pending(self):
        aid = self.ac.request("scan_repo", "https://example.com/repo")
        self.assertTrue(aid)
        self.assertEqual(self.ac.status(aid), "pending")
        pending = self.ac.list_approvals(status="pending")
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["action"], "scan_repo")

    def test_decide_approve(self):
        aid = self.ac.request("scan_repo", "x")
        self.assertTrue(self.ac.decide(aid, "approve", by="alice"))
        self.assertTrue(self.ac.is_approved(aid))
        self.assertEqual(self.ac.status(aid), "approved")

    def test_decide_reject(self):
        aid = self.ac.request("scan_repo", "x")
        self.assertTrue(self.ac.decide(aid, "reject", by="bob"))
        self.assertFalse(self.ac.is_approved(aid))
        self.assertEqual(self.ac.status(aid), "rejected")

    def test_cannot_decide_twice(self):
        aid = self.ac.request("scan_repo", "x")
        self.assertTrue(self.ac.decide(aid, "approve"))
        # 第二次决策（已非 pending）应失败
        self.assertFalse(self.ac.decide(aid, "reject"))

    def test_timeout_default_deny(self):
        aid = self.ac.request("scan_repo", "x")
        # 0 秒超时：立即过期，绝不返回 approved
        decision = self.ac.wait_for_decision(aid, timeout=0)
        self.assertEqual(decision, "expired")
        self.assertFalse(self.ac.is_approved(aid))


class TestSandbox(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.workdir = os.path.join(self.tmp, "jail")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_local_runs_ok(self):
        sb = LocalSandbox()
        res = sb.run([sys.executable, "-c", "print('hi')"], self.workdir)
        self.assertTrue(res.ok, res.error)
        self.assertIn("hi", res.content)
        self.assertEqual(res.backend, "local")

    def test_block_privilege_escape(self):
        sb = LocalSandbox()
        # 命令文本含明显越权路径 token，应被拦截
        res = sb.run(["cat", "../../etc/passwd"], self.workdir)
        self.assertFalse(res.ok)
        self.assertIn("越权", res.error)

    def test_timeout_kill(self):
        sb = LocalSandbox()
        res = sb.run([sys.executable, "-c", "import time; time.sleep(5)"], self.workdir, timeout=1)
        self.assertFalse(res.ok)
        self.assertIn("超时", res.error)

    def test_safe_scan_command_runs_in_sandbox(self):
        # 把样例脚本跑在沙箱里，确认 safe_scan_command 可正常产出 JSON 报告
        sb = SandboxManager(LocalSandbox())
        # 用一个临时含 py 文件的目录作为扫描目标
        target = os.path.join(self.tmp, "src")
        os.makedirs(target)
        with open(os.path.join(target, "a.py"), "w", encoding="utf-8") as f:
            f.write("# TODO: fix\nx = 1\n")
        res = sb.run(safe_scan_command(target), self.workdir, timeout=20)
        self.assertTrue(res.ok, res.error)
        self.assertIn("scanned_files", res.content)

    def test_safe_scan_respects_dot_sandbox_parent(self):
        # 回归：扫描目标位于 .sandbox/<tid> 这类「父目录名含点」的路径时，
        # 不应因隐藏目录跳过逻辑而漏扫。确认能扫到子目录内的文件。
        sb = SandboxManager(LocalSandbox())
        parent = os.path.join(self.tmp, ".sandbox")
        target = os.path.join(parent, "repo-abc")
        os.makedirs(target)
        with open(os.path.join(target, "a.py"), "w", encoding="utf-8") as f:
            f.write("# TODO: fix\nx = 1\n")
        res = sb.run(safe_scan_command(target), self.workdir, timeout=20)
        self.assertTrue(res.ok, res.error)
        import json
        report = json.loads(res.content.strip().splitlines()[-1])
        self.assertEqual(report["scanned_files"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
