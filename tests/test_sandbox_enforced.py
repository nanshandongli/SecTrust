"""EnforcedLocalSandbox 测试：真实资源上限 + 进程树强杀 + 输出截断 + 降级路由。"""

import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sandbox import (EnforcedLocalSandbox, LocalSandbox, SandboxManager,  # noqa: E402
                     SandboxResult)

IS_POSIX = os.name == "posix"


class TestEnforcedLocalSandbox(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="enf_sb_")
        self.sb = EnforcedLocalSandbox(memory_mb=256, cpu_seconds=10,
                                       max_output_bytes=2048)

    # ---- 基本执行与标注 ----
    def test_normal_run_and_containment_label(self):
        r = self.sb.run([sys.executable, "-c", "print('hello')"], self.tmp, timeout=20)
        self.assertTrue(r.ok, r.error)
        self.assertIn("hello", r.content)
        self.assertEqual(r.backend, "local-enforced")
        # 关键：如实标注"有限强制"，绝不让调用方误以为 OS 级隔离
        self.assertEqual(r.containment, "limited")

    def test_jail_check_still_applies(self):
        r = self.sb.run([sys.executable, "-c", "print(open('/etc/passwd').read())"],
                        self.tmp, timeout=20)
        self.assertFalse(r.ok)
        self.assertIn("越权", r.error)

    def test_env_scrubbed(self):
        os.environ["SECRET_TEST_VALUE"] = "leak-me"
        try:
            r = self.sb.run(
                [sys.executable, "-c",
                 "import os;print(os.environ.get('SECRET_TEST_VALUE','<none>'))"],
                self.tmp, timeout=20)
            self.assertTrue(r.ok, r.error)
            self.assertIn("<none>", r.content)
        finally:
            os.environ.pop("SECRET_TEST_VALUE", None)

    # ---- 超时强杀 ----
    def test_timeout_kills_process_tree(self):
        """子进程再开孙进程；超时后整棵树都必须死（不只杀直接子进程）。"""
        marker = os.path.join(self.tmp, "gc.alive")
        # 孙进程每 0.2s 写一次时间戳；主进程 sleep 60
        inner = (f"import time\n"
                 f"while True:\n"
                 f"    open({marker!r},'w').write(str(time.time()))\n"
                 f"    time.sleep(0.2)\n")
        outer = (f"import subprocess,sys,time\n"
                 f"subprocess.Popen([sys.executable,'-c',{inner!r}])\n"
                 f"time.sleep(60)\n")
        t0 = time.time()
        r = self.sb.run([sys.executable, "-c", outer], self.tmp, timeout=2)
        self.assertFalse(r.ok)
        self.assertIn("超时强杀", r.error)
        self.assertLess(time.time() - t0, 20)  # 强杀要快，不能真等 60s
        time.sleep(1.5)  # 给孙进程"如果还活着"一次写盘机会
        if os.path.exists(marker):
            last = float(open(marker).read())
            # 死了：最后一次写盘停在强杀时刻，距今 ≈1.5s（我们的休眠）；
            # 活着：它每 0.2s 写一次，距今必然 <0.5s。
            self.assertGreater(time.time() - last, 1.0,
                               "孙进程仍在定期写盘——进程树没杀干净")

    # ---- 输出截断 ----
    def test_output_truncated(self):
        r = self.sb.run([sys.executable, "-c",
                         "print('x'*100000)"], self.tmp, timeout=20)
        self.assertTrue(r.ok, r.error)
        self.assertLess(len(r.content), 100000)
        self.assertIn("输出被截断", r.content)
        self.assertIn("输出超限已截断", r.error)

    # ---- 内存上限（POSIX rlimit）----
    @unittest.skipUnless(IS_POSIX, "RLIMIT_AS 仅 POSIX 可用")
    def test_memory_limit_enforced(self):
        r = self.sb.run([sys.executable, "-c",
                         "a = bytearray(400*1024*1024); print('should not reach')"],
                        self.tmp, timeout=30)
        self.assertFalse(r.ok)  # MemoryError -> 非 0 退出或被 kill

    # ---- 自动路由 ----
    def test_auto_prefers_enforced_local(self):
        b = SandboxManager.auto()
        self.assertIsInstance(b, (EnforcedLocalSandbox,)) if not (
            isinstance(b, (LocalSandbox, EnforcedLocalSandbox))
        ) else None
        # docker/e2b 不可用时必须是 enforced 而不是 advisory
        if b.name == "local":
            self.assertEqual(b.containment, "limited")

    def test_auto_can_opt_back_to_advisory(self):
        old = os.environ.get("SANDBOX_LOCAL_MODE")
        os.environ["SANDBOX_LOCAL_MODE"] = "advisory"
        try:
            b = SandboxManager.auto()
            if b.name == "local":
                self.assertEqual(b.containment, "advisory")
        finally:
            if old is None:
                os.environ.pop("SANDBOX_LOCAL_MODE", None)
            else:
                os.environ["SANDBOX_LOCAL_MODE"] = old

    def test_result_dataclass_compat(self):
        r = SandboxResult()
        self.assertEqual(r.containment, "advisory")  # 默认值不变（向后兼容）


if __name__ == "__main__":
    unittest.main()
