"""发布物料测试（⑩ PyPI / MCP Registry 上架物料）。

守住三条线：
1. **版本一致**：pyproject / mcpserver.json / release_meta 副本 三处版本必须相同。
   版本漂移会让 Registry 收录到一个自称旧版、实际是新版的包。
2. **包内容完整**：构建产物里必须有 MCP 元数据、许可证、技能库、模板脚本。
   少了 mcpserver.json，MCP Registry 收录直接失败。
3. **工具清单一致**：mcpserver.json 声明的工具必须与 mcp_server._TOOLS 完全一致。
"""

import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import mcp_server  # noqa: E402
from release_meta import load_mcpserver, mcpserver_path, repo_root  # noqa: E402


def _pyproject_version():
    try:
        import tomllib
    except ImportError:  # pragma: no cover - Python < 3.11
        return None
    with open(os.path.join(ROOT, "pyproject.toml"), "rb") as f:
        return tomllib.load(f)["project"]["version"]


class TestReleaseMetadataFiles(unittest.TestCase):
    def test_required_files_exist(self):
        for name in ("pyproject.toml", "MANIFEST.in", "LICENSE",
                     "CHANGELOG.md", "mcpserver.json"):
            self.assertTrue(os.path.isfile(os.path.join(ROOT, name)),
                            f"缺少发布必需文件: {name}")

    def test_license_is_agpl(self):
        with open(os.path.join(ROOT, "LICENSE"), encoding="utf-8") as f:
            text = f.read()
        self.assertIn("GNU AFFERO GENERAL PUBLIC LICENSE", text)
        self.assertIn("Version 3", text)
        # 全仓库不得残留 MIT 许可声明（防止许可声明漂移）
        with open("pyproject.toml", encoding="utf-8") as f:
            self.assertNotIn('"MIT"', f.read())

    def test_mcpserver_json_locatable(self):
        p = mcpserver_path()
        self.assertIsNotNone(p, "mcpserver.json 必须可定位（包内或仓库根）")
        self.assertTrue(os.path.isfile(p))

    def test_repo_root_detected_in_source_tree(self):
        self.assertIsNotNone(repo_root())


class TestVersionConsistency(unittest.TestCase):
    """三处版本必须一致——漂移会让 Registry 收录到错误版本。"""

    def test_pyproject_version_matches_mcpserver(self):
        pv = _pyproject_version()
        if pv is None:
            self.skipTest("当前 Python 无 tomllib")
        mv = load_mcpserver().get("version")
        self.assertEqual(pv, mv, f"版本漂移: pyproject={pv} vs mcpserver={mv}")

    def test_packaged_copy_matches_root_copy(self):
        """release_meta/ 里的副本必须与仓库根一致（打包时会被复制进 wheel）。"""
        root = os.path.join(ROOT, "mcpserver.json")
        pkg = os.path.join(ROOT, "release_meta", "mcpserver.json")
        self.assertTrue(os.path.isfile(pkg), "release_meta 副本缺失")
        with open(root, encoding="utf-8") as f:
            a = json.load(f)
        with open(pkg, encoding="utf-8") as f:
            b = json.load(f)
        self.assertEqual(a, b, "release_meta 副本与仓库根不一致")

    def test_changelog_has_current_version(self):
        pv = _pyproject_version()
        if pv is None:
            self.skipTest("当前 Python 无 tomllib")
        with open(os.path.join(ROOT, "CHANGELOG.md"), encoding="utf-8") as f:
            self.assertIn(f"[{pv}]", f.read(), f"CHANGELOG 缺少 {pv} 条目")


class TestToolManifestConsistency(unittest.TestCase):
    def test_declared_tools_match_implementation(self):
        data = load_mcpserver()
        declared = set(data.get("tools", []))
        actual = {t["name"] for t in mcp_server._TOOLS}
        self.assertEqual(declared, actual,
                         f"工具清单不一致: 仅声明 {declared - actual} / 仅实现 {actual - declared}")

    def test_manifest_has_required_registry_fields(self):
        """MCP Registry 收录必需字段。"""
        data = load_mcpserver()
        for field in ("name", "version", "description", "tools", "transport"):
            self.assertIn(field, data, f"mcpserver.json 缺少必需字段: {field}")

    def test_protocol_version_declared(self):
        data = load_mcpserver()
        self.assertIn("protocolVersion", data)
        self.assertIn(data["protocolVersion"], data.get("supportedProtocolVersions", []))


class TestPackagingConfig(unittest.TestCase):
    """打包配置必须覆盖全部实际模块——漏包 = 装完 import 不到。"""

    def test_all_root_modules_declared(self):
        if _pyproject_version() is None:
            self.skipTest("当前 Python 无 tomllib")
        import tomllib
        with open(os.path.join(ROOT, "pyproject.toml"), "rb") as f:
            cfg = tomllib.load(f)
        declared = set(cfg["tool"]["setuptools"]["py-modules"])
        actual = {fn[:-3] for fn in os.listdir(ROOT)
                  if fn.endswith(".py") and fn != "setup.py"}
        missing = actual - declared
        self.assertFalse(missing, f"这些根模块未在 pyproject 声明: {sorted(missing)}")

    def test_all_packages_declared(self):
        if _pyproject_version() is None:
            self.skipTest("当前 Python 无 tomllib")
        import tomllib
        with open(os.path.join(ROOT, "pyproject.toml"), "rb") as f:
            cfg = tomllib.load(f)
        declared = set(cfg["tool"]["setuptools"]["packages"])
        actual = {d for d in os.listdir(ROOT)
                  if os.path.isdir(os.path.join(ROOT, d))
                  and os.path.isfile(os.path.join(ROOT, d, "__init__.py"))
                  and d not in ("tests",)}
        missing = actual - declared
        self.assertFalse(missing, f"这些包未在 pyproject 声明: {sorted(missing)}")

    def test_package_name_does_not_shadow_pypa_packaging(self):
        """包名不能叫 packaging——那是 PyPA 核心库名，同名会让 pip 全线崩溃。"""
        self.assertNotIn("packaging", os.listdir(ROOT),
                         "存在名为 packaging 的目录/文件，会遮蔽 PyPA packaging 库")

    def test_console_scripts_point_to_existing_callables(self):
        if _pyproject_version() is None:
            self.skipTest("当前 Python 无 tomllib")
        import importlib
        import tomllib
        with open(os.path.join(ROOT, "pyproject.toml"), "rb") as f:
            cfg = tomllib.load(f)
        for ep, target in cfg["project"].get("scripts", {}).items():
            mod, func = target.split(":")
            m = importlib.import_module(mod)
            self.assertTrue(callable(getattr(m, func, None)),
                            f"入口 {ep}={target} 不可调用（{mod}.{func} 缺失）")

    def test_manifest_includes_registry_metadata(self):
        with open(os.path.join(ROOT, "MANIFEST.in"), encoding="utf-8") as f:
            text = f.read()
        self.assertIn("mcpserver.json", text)
        self.assertIn("LICENSE", text)
        self.assertIn("CHANGELOG.md", text)

    def test_manifest_excludes_tests(self):
        """测试不进包（体积大且依赖仓库根路径）。"""
        with open(os.path.join(ROOT, "MANIFEST.in"), encoding="utf-8") as f:
            text = f.read()
        self.assertIn("prune tests", text)


if __name__ == "__main__":
    unittest.main()
