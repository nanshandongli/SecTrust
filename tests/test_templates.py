"""
模板库 + 编排回归测试（零依赖、可离线跑）
覆盖：
- 模板注册表（list/get）
- 编排：request_scan(pending) -> execute_scan 未批准则 blocked
- run_scan(auto_approve) 端到端：沙箱隔离执行 + 结果写回登记处
- 沙箱脚本可直接在 LocalSandbox 跑出 JSON 报告
"""

import json
import os
import shutil
import tempfile
import unittest

import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sandbox import LocalSandbox  # noqa: E402
from templates import (  # noqa: E402
    execute_scan,
    get_template,
    list_templates,
    request_scan,
    run_scan,
)


class TemplatesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "test.db")
        os.environ["DB_PATH"] = self.db
        # 准备一个「有问题」的样例仓库
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        with open(os.path.join(self.repo, ".env"), "w", encoding="utf-8") as f:
            # AKIAIOSFODNN7EXAMPLE 是 AWS 官方文档示例密钥，属占位符，不应被判泄露
            f.write('DB_PASSWORD=SuperSecret123\nAPI_KEY=AKIAIOSFODNN7EXAMPLE\n')
        with open(os.path.join(self.repo, "app.py"), "w", encoding="utf-8") as f:
            # 真实形态的（假）密钥：长度够、不含 example/your/changeme 等占位词
            f.write('SECRET = "7fQ2xLp9VbN3mRkT"\n# TODO: fix\n')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        shutil.rmtree(".scan_jail", ignore_errors=True)
        os.environ.pop("DB_PATH", None)

    def test_list_templates(self):
        ids = {t["id"] for t in list_templates()}
        for expected in ("generic_sast", "secret_leak", "hardcoded_credential",
                         "dependency_vuln", "cicd_config"):
            self.assertIn(expected, ids)

    def test_get_template(self):
        self.assertIsNotNone(get_template("secret_leak"))
        self.assertIsNone(get_template("does_not_exist"))

    def test_sandbox_script_runs(self):
        tmpl = get_template("secret_leak")
        wd = os.path.join(self.tmp, "w")
        shutil.copytree(self.repo, wd)
        res = LocalSandbox().run(tmpl.build_command(wd), wd, timeout=30)
        self.assertTrue(res.ok, res.error)
        self.assertIn("findings", res.content)

    def test_request_scan_pending(self):
        req = request_scan("secret_leak", self.repo, db_path=self.db, channel="test")
        self.assertEqual(req["status"], "pending")
        self.assertTrue(req["approval_id"].startswith("ap-"))
        # 未批准就执行 -> blocked
        out = execute_scan(req["task_id"], req["approval_id"], self.repo, db_path=self.db)
        self.assertEqual(out["status"], "blocked")

    def test_run_scan_auto_approve_finds_secret(self):
        out = run_scan(
            "secret_leak", self.repo, db_path=self.db,
            sandbox_backend="local", auto_approve=True, channel="test",
        )
        self.assertEqual(out["status"], "done", out)
        report = out.get("report") or {}
        rules = {f["rule"] for f in report.get("findings", [])}
        self.assertIn("GENERIC_SECRET", rules)

    def test_example_credentials_not_reported(self):
        """官方示例/占位凭据不得报为泄露（精度：避免 AWS AKIAIOSFODNN7EXAMPLE 之类误报）。"""
        out = run_scan(
            "secret_leak", self.repo, db_path=self.db,
            sandbox_backend="local", auto_approve=True, channel="test",
        )
        matches = " ".join(
            str(f.get("match", "")).lower()
            for f in (out.get("report") or {}).get("findings", [])
        )
        self.assertNotIn("akiaiosfodnn7example", matches,
                         "AWS 官方文档示例密钥不应被判为泄露")


if __name__ == "__main__":
    unittest.main(verbosity=2)
