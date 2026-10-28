"""tests/test_secure_dev_hook.py —— secure_dev 模板「提交即扫描」Git hook 测试

覆盖：
  1. 扫描闸门：critical/high 密钥命中即阻断；干净文件通过。
  2. CLI 端到端：真实临时 git 仓库里，含密钥的暂存提交被拦（exit 1），干净通过（exit 0）。
  3. install：写出可执行 pre-commit，且内嵌本工具脚本的**绝对路径**（任意仓库可定位）。
  4. 模板纪律：secure_dev 的 secret_scan 阶段是 op=scan 且**不依赖** code_review
     （提交即扫描闸门不能因 LLM 不可用而失效）。
"""
import os
import subprocess
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT = os.path.dirname(_HERE)
if _PROJECT not in sys.path:
    sys.path.insert(0, _PROJECT)

import tools.secure_dev_hook as hook  # noqa: E402

HOOK_SCRIPT = os.path.join(_PROJECT, "tools", "secure_dev_hook.py")
# 一个稳定触发 critical 的令牌形态（与 daemon 测试同源）。
# 注意：拆成两段字面量，避免被 GitHub push protection 当作真实 Slack 令牌拦截
# （运行时拼接后仍是完整令牌，scanner 仍能检出 critical）。
_SLACK_SECRET = ('K="xoxb-1234567890-1234567890123-'
                 'aB3kZ9mQx2LpR7nW4tV8yU6sD1fG5hJ0cE4"\n')


class ScanGateTest(unittest.TestCase):
    def _scan(self, content: str, fail_on: str = "high") -> dict:
        d = tempfile.mkdtemp()
        f = os.path.join(d, "config.py")
        with open(f, "w", encoding="utf-8") as fh:
            fh.write(content)
        return hook.scan_paths([f], root=d, fail_on=fail_on)

    def test_blocks_critical_secret(self):
        res = self._scan(_SLACK_SECRET)
        self.assertTrue(res["should_fail"], "critical 密钥必须触发阻断")
        self.assertTrue(res["findings"])

    def test_passes_clean_file(self):
        res = self._scan('print("hello")\n')
        self.assertFalse(res["should_fail"])
        self.assertEqual(len(res["findings"]), 0)

    def test_fail_on_critical_still_blocks(self):
        res = self._scan(_SLACK_SECRET, fail_on="critical")
        self.assertTrue(res["should_fail"],
                        "critical 级在任何 fail_on>=critical 都应阻断")


class HookCLITest(unittest.TestCase):
    def _make_repo(self, files: dict) -> str:
        d = tempfile.mkdtemp()
        subprocess.run(["git", "init", "-q"], cwd=d, check=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=d,
                       check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=d, check=True)
        for name, content in files.items():
            with open(os.path.join(d, name), "w", encoding="utf-8") as fh:
                fh.write(content)
            subprocess.run(["git", "add", name], cwd=d, check=True)
        return d

    def _run(self, d: str, *args):
        return subprocess.run([sys.executable, HOOK_SCRIPT, "--scope", "staged",
                               *args], cwd=d, capture_output=True, text=True)

    def test_blocks_secret_commit(self):
        d = self._make_repo({"config.py": _SLACK_SECRET})
        r = self._run(d)
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("拦截", r.stderr)

    def test_clean_commit_passes(self):
        d = self._make_repo({"app.py": 'print(1)\n'})
        r = self._run(d)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("未发现阻断级密钥", r.stdout)

    def test_install_writes_executable_hook(self):
        d = self._make_repo({"app.py": 'print(1)\n'})
        r = subprocess.run([sys.executable, HOOK_SCRIPT, "install"], cwd=d,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        target = os.path.join(d, ".git", "hooks", "pre-commit")
        self.assertTrue(os.path.isfile(target))
        # 必须是可执行
        self.assertTrue(os.access(target, os.X_OK))
        content = open(target, encoding="utf-8").read()
        self.assertIn("secure_dev_hook", content)
        # 内嵌绝对路径：任意仓库都能定位到本工具脚本
        self.assertIn(os.path.abspath(HOOK_SCRIPT), content)

    def test_plan_inprocess_runs_scan_independently(self):
        # --plan 本进程模式：无 LLM 时 secret_scan 仍应 completed（独立闸门）
        d = self._make_repo({"app.py": "def add(a, b):\n    return a + b\n"})
        r = subprocess.run([sys.executable, HOOK_SCRIPT, "--scope", "staged",
                            "--plan", "--timeout", "30"], cwd=d,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        # 关键：扫描阶段独立完成，不被 LLM 代码审查拖垮
        self.assertIn("secret_scan=completed", r.stdout)


class SecureDevTemplateTest(unittest.TestCase):
    def test_secret_scan_runs_independent_of_code_review(self):
        import planner
        plan = planner.Planner().decompose(
            "对代码仓库做安全审计", params={"repo_path": "./r"}, tier="complex")
        ss = next(s for s in plan.subtasks if s["key"] == "secret_scan")
        self.assertEqual(ss.get("op"), "scan",
                        "secure_dev 的密钥扫描必须是 op=scan")
        self.assertEqual(ss.get("depends_on"), [],
                        "密钥扫描不能依赖 LLM 代码审查（提交即扫描须独立可跑）")


if __name__ == "__main__":
    unittest.main()
