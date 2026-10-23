"""self_audit 自检工具回归：防 ReDoS 复发 + 锁定沙箱探针与密钥豁免的正确性。

背景（实测踩过的两个坑）：
1. `audit_inventory` 曾用 `try:\\s*\\n(?:\\s+.*\\n)*?\\s+(?:from|import)\\s+mod`
   判断可选依赖，`\\s` 含 `\\n` 与后续量词重叠 → 灾难性回溯，
   在本仓库源码上卡死 15 分钟无输出。改用逐行扫描。
2. `audit_security` 的沙箱越权探针曾用 SAM 文件，测到的是 Windows ACL
   而非沙箱能力 → 得出"沙箱已拦住"的假阳性。改用 jail 外普通可读文件。
"""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools import self_audit as sa  # noqa: E402


class OptionalImportScanTest(unittest.TestCase):
    """逐行扫描版可选依赖判定：语义正确 + 线性时间。"""

    def test_detects_import_inside_try(self):
        src = "try:\n    import requests\nexcept ImportError:\n    requests = None\n"
        self.assertIn("requests", sa._optional_import_tops(src))

    def test_plain_import_not_optional(self):
        src = "import os\nimport json\n"
        self.assertEqual(sa._optional_import_tops(src), set())

    def test_from_import_inside_try(self):
        src = "try:\n    from redis import Redis\nexcept Exception:\n    pass\n"
        self.assertIn("redis", sa._optional_import_tops(src))

    def test_nested_try_indent(self):
        """嵌套 try：只看直接包裹该 import 的那层。"""
        src = ("def f():\n"
               "    try:\n"
               "        import numpy\n"
               "    except ImportError:\n"
               "        numpy = None\n"
               "    return numpy\n")
        self.assertIn("numpy", sa._optional_import_tops(src))

    def test_no_catastrophic_backtracking(self):
        """回归防护：长文件必须线性时间完成（原正则在此卡死 15 分钟）。"""
        # 构造最坏情形：大量缩进行 + 一个无匹配的 try 头
        src = "try:\n" + "".join(f"    x{i} = {i}\n" for i in range(20000))
        t0 = time.perf_counter()
        sa._optional_import_tops(src)
        elapsed = time.perf_counter() - t0
        self.assertLess(elapsed, 2.0,
                        f"扫描 2 万行耗时 {elapsed:.1f}s，疑似回溯爆炸")


class KnownVectorTest(unittest.TestCase):
    """公开测试向量豁免：不能把全世界都知道的假密钥算成泄露。"""

    def test_known_vector_recognized(self):
        """含官方向量的行必须被识别（用于豁免而非报泄露）。"""
        # 直接验证常量与匹配逻辑的一致性
        self.assertIn("AKIALALEMEL33243OLIA", sa.KNOWN_PUBLIC_TEST_VECTORS)
        self.assertTrue(any(v in 'awsToken := "AKIALALEMEL33243OLIA"'
                            for v in sa.KNOWN_PUBLIC_TEST_VECTORS))

    def test_ordinary_line_not_matched(self):
        self.assertFalse(any(v in "import redis" for v in sa.KNOWN_PUBLIC_TEST_VECTORS))


class InventoryRuntimeTest(unittest.TestCase):
    """audit_inventory 必须能跑完（原缺陷：直接卡死）。"""

    def test_inventory_completes_quickly(self):
        t0 = time.perf_counter()
        result = sa.audit_inventory()
        elapsed = time.perf_counter() - t0
        self.assertLess(elapsed, 30, f"inventory 耗时 {elapsed:.1f}s，疑似又卡住了")
        # 工具数不写死：加工具是常态，写死会让每次新增都红一次。
        # 真正该守的是"数出来的数 == mcp_server 实际声明数"（一致性问题）。
        sys.path.insert(0, sa.ROOT)
        import mcp_server
        self.assertEqual(result["mcp_tools"]["count"], len(mcp_server._TOOLS),
                         "inventory 数出的工具数应与 mcp_server._TOOLS 一致")
        self.assertGreaterEqual(result["mcp_tools"]["count"], 30)
        self.assertGreaterEqual(result["tests"], 400)


class SandboxProbeTest(unittest.TestCase):
    """沙箱探针：必须用 jail 外可读文件，且如实对照声明的 containment。"""

    def test_probe_reads_outside_jail_file(self):
        """探针要能真实读到 jail 外文件——否则测的是 OS ACL（假阳性陷阱）。"""
        import tempfile
        from sandbox import SandboxManager
        d = tempfile.mkdtemp(prefix="probe_")
        jail = os.path.join(d, "jail")
        os.makedirs(jail, exist_ok=True)
        outside = os.path.join(d, "canary.txt")
        with open(outside, "w", encoding="utf-8") as f:
            f.write("CANARY-XYZ")
        sb = SandboxManager.auto()
        r = sb.run(["python", "-c", f"print(open(r'{outside}').read())"],
                   jail, timeout=20)
        self.assertIn("CANARY-XYZ", r.content or "",
                      "探针文件必须可读——否则说明命令没跑起来，测试无意义")


if __name__ == "__main__":
    unittest.main()
