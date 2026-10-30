"""B1 · 标准 SKILL.md 运行时回归测试。

覆盖用户 P0 第一件事的三项硬要求：
  1. 渐进式 L1/L2/L3 加载（L1 必须证明「真的没读正文」，不是嘴上说）；
  2. manifest 安装期校验（与 governance.rbac / content_guard 对齐）；
  3. L3 路径穿越防护（realpath + 一级目录白名单，多向量攻击全拒）。

外加：脚本必须走 sandbox/、两个加载器并存互不破坏、诚实边界（本地降级
containment=advisory 不冒充真沙箱）。

零第三方依赖；全部离线可跑。
"""

import builtins
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from agentskills.skill_md import (  # noqa: E402
    SkillMdError, SkillSecurityError, StandardSkillLibrary, StandardSkillSpec,
    parse_frontmatter_standard, parse_skill_md, read_frontmatter_only,
    validate_manifest,
)
from agentskills.loader import SkillLibrary  # noqa: E402


class TestStandardFrontmatter(unittest.TestCase):
    """标准解析器：支持行内列表，遇到复杂 YAML 明确报错（不猜测语义）。"""

    def test_list_value_supported(self):
        meta, body = parse_frontmatter_standard(
            "---\nname: demo\ndescription: Use when x\n"
            "allowed-tools: [web_search, file_read, file_write]\n---\n\n# body\n")
        self.assertEqual(meta["allowed-tools"],
                         ["web_search", "file_read", "file_write"])
        self.assertIn("# body", body)

    def test_empty_list_value(self):
        meta, _ = parse_frontmatter_standard(
            "---\nname: demo\ndescription: x\nallowed-tools: []\n---\nbody")
        self.assertEqual(meta["allowed-tools"], [])

    def test_scalar_quotes_stripped(self):
        meta, _ = parse_frontmatter_standard(
            '---\nname: "demo"\ndescription: \'Use when x\'\n---\nbody')
        self.assertEqual(meta["name"], "demo")
        self.assertEqual(meta["description"], "Use when x")

    def test_complex_yaml_rejected(self):
        for v in ("{a: 1}", "|", ">", "&anchor", "*alias"):
            with self.subTest(v=v), self.assertRaises(SkillMdError):
                parse_frontmatter_standard(
                    "---\nname: demo\nmeta: %s\n---\nbody" % v)

    def test_missing_frontmatter_raises(self):
        with self.assertRaises(SkillMdError):
            parse_frontmatter_standard("# no frontmatter\n")

    def test_unclosed_frontmatter_raises(self):
        with self.assertRaises(SkillMdError):
            parse_frontmatter_standard("---\nname: demo\n\nbody without close")

    def test_hash_comment_line_ignored(self):
        meta, _ = parse_frontmatter_standard(
            "---\n# comment\nname: demo\ndescription: x\n---\nbody")
        self.assertEqual(meta["name"], "demo")

    def test_l1_proven_not_to_read_body(self):
        """硬核证明：patch 文件迭代器计数，正文一行都不许被读到。"""
        d = tempfile.mkdtemp()
        p = os.path.join(d, "SKILL.md")
        fm_lines = 5  # --- + 3 keys + ---
        with open(p, "w", encoding="utf-8") as f:
            f.write("---\nname: l1-probe\ndescription: Use when 探测 L1\n"
                    "version: 1.0.0\n---\n")
            f.write("SECRET_BODY_MARKER\n" * 1000)

        real_open = builtins.open
        counter = {"n": 0}

        class _CountingFH:
            def __init__(self, fh):
                self._fh = fh

            def __iter__(self):
                return self

            def __next__(self):
                line = next(self._fh)      # 成功取到才计数
                counter["n"] += 1
                return line

            def __enter__(self):
                return self

            def __exit__(self, *a):
                self._fh.close()
                return False

        def _fake_open(path, *a, **kw):
            fh = real_open(path, *a, **kw)
            if os.path.abspath(str(path)) == os.path.abspath(p):
                return _CountingFH(fh)
            return fh

        with mock.patch("builtins.open", side_effect=_fake_open):
            meta, size = read_frontmatter_only(p)

        self.assertEqual(counter["n"], fm_lines,
                         "L1 读取了 frontmatter 之外的行：%d（应为 %d）"
                         % (counter["n"], fm_lines))
        self.assertEqual(meta["name"], "l1-probe")
        self.assertNotIn("SECRET_BODY_MARKER", repr(meta))
        self.assertGreater(size, 1000)


class TestStandardSkillLibraryL1L2(unittest.TestCase):
    def setUp(self):
        self.lib = StandardSkillLibrary()

    def test_l1_metadata_has_no_body(self):
        items = self.lib.load_l1_metadata()
        self.assertTrue(items, "标准技能库 L1 不应为空")
        for it in items:
            self.assertNotIn("body", it)
            self.assertIn("risk_level", it)
            self.assertIn("cost_tier", it)
            self.assertIn("allowed_tools", it)
            self.assertIn("installed_ok", it)

    def test_l1_includes_standard_sample(self):
        names = [x["name"] for x in self.lib.load_l1_metadata()]
        self.assertIn("competitor-analysis", names)
        sample = next(x for x in self.lib.load_l1_metadata()
                      if x["name"] == "competitor-analysis")
        self.assertTrue(sample["ok"])
        self.assertTrue(sample["installed_ok"])
        self.assertEqual(sample["allowed_tools"],
                         ["web_search", "file_read", "file_write"])
        self.assertEqual(sample["risk_level"], "medium")

    def test_match_l1_scores_without_reading_body(self):
        hits = self.lib.match_l1("竞品分析报告 竞品矩阵")
        self.assertTrue(hits)
        self.assertEqual(hits[0]["name"], "competitor-analysis")
        for h in hits:
            self.assertNotIn("body", h)
        self.assertNotIn("competitor-analysis",
                         self.lib.stats()["l2_activated"])

    def test_l2_loads_body_and_caches(self):
        self.assertEqual(self.lib.get("competitor-analysis").body, "")
        body = self.lib.load_l2_body("competitor-analysis")
        self.assertIn("竞品分析技能", body)
        self.assertGreater(len(body), 200)
        self.assertIn("competitor-analysis", self.lib.stats()["l2_activated"])
        # 第二次命中缓存，仍是同一份正文
        self.assertEqual(self.lib.load_l2_body("competitor-analysis"), body)

    def test_l2_unknown_raises(self):
        with self.assertRaises(KeyError):
            self.lib.load_l2_body("no-such-skill")

    def test_parse_skill_md_body_flag(self):
        p = os.path.join(ROOT, "agentskills", "library",
                         "competitor-analysis", "SKILL.md")
        self.assertEqual(parse_skill_md(p).body, "")
        self.assertGreater(len(parse_skill_md(p, include_body=True).body), 200)


class TestL3PathSecurity(unittest.TestCase):
    """路径穿越防护：realpath 规范化 + 一级目录白名单，多向量全拒。"""

    def setUp(self):
        self.lib = StandardSkillLibrary()

    def test_reads_references_and_assets(self):
        src = self.lib.load_l3_resource("competitor-analysis",
                                        "references/sources.md")
        tpl = self.lib.load_l3_resource("competitor-analysis",
                                        "assets/report_template.md")
        self.assertIsInstance(src, str)
        self.assertIsInstance(tpl, str)
        self.assertTrue(src.strip())
        self.assertTrue(tpl.strip())

    def test_binary_mode(self):
        data = self.lib.load_l3_resource("competitor-analysis",
                                         "references/sources.md", binary=True)
        self.assertIsInstance(data, bytes)

    def test_traversal_vectors_all_rejected(self):
        for p in ("../secret.txt",
                  "references/../../secret.txt",
                  "../../etc/passwd",
                  "/etc/passwd",
                  "C:\\Windows\\win.ini",
                  "",
                  "SKILL.md",
                  "manifest.json",
                  "references",                 # 是目录不是文件
                  "references/../manifest.json"):
            with self.subTest(p=p), self.assertRaises(SkillSecurityError):
                self.lib.load_l3_resource("competitor-analysis", p)

    def test_normalized_path_still_inside_is_allowed(self):
        # 归一化后仍落在 scripts/ 内的写法允许（不是穿越）
        data = self.lib.load_l3_resource(
            "competitor-analysis", "references/../scripts/collect_outline.py")
        self.assertIn("REQUIRED_ARTIFACTS", data)

    def test_unknown_skill_raises_keyerror(self):
        with self.assertRaises(KeyError):
            self.lib.load_l3_resource("no-such-skill", "references/x.md")


class TestManifestValidation(unittest.TestCase):
    """安装期校验：结构 + 与 rbac.PERMISSIONS / content_guard 对齐。"""

    def setUp(self):
        self.lib = StandardSkillLibrary()
        self.base = {
            "name": "demo", "version": "1.0.0",
            "permissions": ["web_search", "task.read"],
            "allowed_domains": ["*.com"], "blocked_patterns": ["secret_key"],
            "requires_approval": False, "cost_estimate": "low",
        }
        self.spec = StandardSkillSpec(name="demo", description="x",
                                      dir_path=".", version="1.0.0")

    def test_valid_manifest(self):
        self.assertEqual(validate_manifest(self.base, self.spec), [])

    def test_install_validate_sample_ok(self):
        r = self.lib.install_validate("competitor-analysis")
        self.assertTrue(r["ok"], r["problems"])
        self.assertEqual(r["manifest"]["name"], "competitor-analysis")
        self.assertEqual(r["problems"], [])

    def test_install_validate_unknown(self):
        r = self.lib.install_validate("no-such-skill")
        self.assertFalse(r["ok"])
        self.assertIn("技能不存在", r["problems"])

    def test_missing_manifest_flagged(self):
        probs = validate_manifest({}, self.spec)
        self.assertTrue(any("缺少 manifest" in p for p in probs))

    def test_problem_cases(self):
        cases = [
            ({"name": "other"}, "不一致"),
            ({"version": ""}, "缺少 version"),
            ({"permissions": ["nope.unknown.perm"]}, "未知平台权限"),
            ({"permissions": "not-a-list"}, "列表"),
            ({"blocked_patterns": ["[unclosed"]}, "非法正则"),
            ({"requires_approval": "yes"}, "布尔值"),
            ({"cost_estimate": ""}, "cost_estimate"),
        ]
        for patch, keyword in cases:
            with self.subTest(patch=patch):
                m = dict(self.base)
                m.update(patch)
                probs = validate_manifest(m, self.spec)
                self.assertTrue(probs, "应检出问题：%r" % patch)
                self.assertTrue(any(keyword in p for p in probs),
                                (patch, probs))

    def test_old_format_skills_parse_but_not_installed(self):
        """旧 4 个技能没有 manifest：标准加载器能解析其 frontmatter（ok），
        但安装期校验不通过（installed_ok=False），不算 broken。"""
        st = self.lib.stats()
        self.assertEqual(st["broken"], 0, "标准解析器不应把旧格式技能判为坏包")
        self.assertEqual(st["installed"], 1, "只有带 manifest 的标准样板可安装")
        l1_names = [x["name"] for x in self.lib.load_l1_metadata()]
        self.assertEqual(l1_names, ["competitor-analysis"])


class TestSandboxScript(unittest.TestCase):
    """脚本必须走 sandbox/；沙箱外/非 scripts 一律拒绝。"""

    def setUp(self):
        self.lib = StandardSkillLibrary()

    def test_run_script_uses_sandbox_manager_by_default(self):
        from sandbox import LocalSandbox
        with mock.patch("sandbox.SandboxManager.auto",
                        return_value=LocalSandbox()) as auto:
            r = self.lib.run_script("competitor-analysis",
                                    "scripts/collect_outline.py",
                                    args=["AI 平台"])
        auto.assert_called_once()
        self.assertTrue(r["ok"], r)
        self.assertTrue(r["sandboxed"])
        self.assertEqual(r["script"], "scripts/collect_outline.py")
        self.assertEqual(r["backend"], "local")
        # 诚实边界：本地降级是 advisory，不是 OS 级隔离，绝不冒充真沙箱
        self.assertEqual(r["containment"], "advisory")
        s = r["stdout"]
        data = json.loads(s[s.index("{"): s.rindex("}") + 1])
        self.assertEqual(data["topic"], "AI 平台")
        self.assertEqual(len(data["outline"]), 5)
        self.assertEqual(data["required_artifacts"],
                         ["竞品功能对比表", "来源链接清单", "成本估算"])
        self.assertTrue(data["evidence_ready"])

    def test_non_script_rejected(self):
        with self.assertRaises(SkillSecurityError):
            self.lib.run_script("competitor-analysis", "references/sources.md")

    def test_traversal_script_rejected(self):
        with self.assertRaises(SkillSecurityError):
            self.lib.run_script("competitor-analysis", "../evil.py")


class TestTwoLoadersCoexist(unittest.TestCase):
    """两个加载器并存、互不破坏：严格写作规范 + 标准格式。"""

    def test_old_loader_still_valid(self):
        old = SkillLibrary()
        st = old.stats()
        self.assertEqual(st["broken"], 0)
        self.assertEqual(st["total"], 5)   # 4 个旧技能 + 标准样板
        self.assertEqual(old.get("competitor-analysis").ok, True)

    def test_standard_loader_sees_sample(self):
        new = StandardSkillLibrary()
        self.assertIsNotNone(new.get("competitor-analysis"))
        self.assertTrue(new.install_validate("competitor-analysis")["ok"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
