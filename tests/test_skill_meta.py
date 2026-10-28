"""
tests/test_skill_meta.py —— 技能 SKILL.md 元数据解析（展示名 + 功能简介）

覆盖 console_web.Handler._read_skill_meta 的不变量：
  1. frontmatter 的 description_zh 优先于 description（中文 UI）。
  2. '>' / '|' 多行块标量都能正确折叠成一段。
  3. 引号被去掉。
  4. 无 frontmatter 时回退到首个非空内容段落（跳过标题行）。
  5. 文件不存在 / 解析失败不抛异常，返回空串。

不依赖 PyYAML（产品运行环境未装），用针对性解析。
"""

import os
import tempfile
import unittest

import console_web


def _write_skill(content):
    d = tempfile.mkdtemp()
    p = os.path.join(d, "SKILL.md")
    with open(p, "w", encoding="utf-8") as f:
        f.write(content)
    return p


class TestReadSkillMeta(unittest.TestCase):

    def test_description_zh_preferred(self):
        p = _write_skill("---\nname: x\ndescription_zh: 中文简介优先\ndescription: EN desc\n---\n# 标题\n")
        dn, desc = console_web.Handler._read_skill_meta(p)
        self.assertEqual(desc, "中文简介优先")

    def test_description_fallback_when_no_zh(self):
        p = _write_skill("---\nname: x\ndescription: 只有英文简介\n---\n# 标题\n")
        dn, desc = console_web.Handler._read_skill_meta(p)
        self.assertEqual(desc, "只有英文简介")

    def test_folded_block_scalar_gt(self):
        p = _write_skill(
            "---\nname: x\ndescription: >\n  第一句。\n  第二句。\n---\n# 标题\n")
        dn, desc = console_web.Handler._read_skill_meta(p)
        self.assertEqual(desc, "第一句。 第二句。")

    def test_literal_block_scalar_pipe(self):
        p = _write_skill(
            "---\nname: x\ndescription: |\n  第一行\n  第二行\n---\n# 标题\n")
        dn, desc = console_web.Handler._read_skill_meta(p)
        self.assertEqual(desc, "第一行\n第二行")

    def test_quotes_stripped(self):
        p = _write_skill('---\nname: x\ndescription: "带引号简介"\n---\n')
        dn, desc = console_web.Handler._read_skill_meta(p)
        self.assertEqual(desc, "带引号简介")

    def test_display_name_extracted(self):
        p = _write_skill(
            "---\nname: my-skill\ndisplay_name: 我的技能\n---\n# 标题\n")
        dn, desc = console_web.Handler._read_skill_meta(p)
        self.assertEqual(dn, "我的技能")

    def test_fallback_to_first_paragraph(self):
        # 没有 frontmatter，回退到首个内容段落（跳过 # 标题）
        p = _write_skill("# 标题\n\n这是一个没有 frontmatter 的技能简介。\n")
        dn, desc = console_web.Handler._read_skill_meta(p)
        self.assertEqual(desc, "这是一个没有 frontmatter 的技能简介。")

    def test_missing_file_returns_empty(self):
        dn, desc = console_web.Handler._read_skill_meta(
            "/nonexistent/path/SKILL.md")
        self.assertEqual(dn, "")
        self.assertEqual(desc, "")

    def test_old_first_line_extraction_is_replaced(self):
        # 回归：旧实现取"第一行去 #"会把 '---' 或 'name: x' 当简介。
        p = _write_skill(
            "---\nname: agent-x\ndescription_zh: 真实功能简介\n---\n# 标题\n")
        dn, desc = console_web.Handler._read_skill_meta(p)
        self.assertNotIn("---", desc)
        self.assertNotIn("name:", desc)
        self.assertEqual(desc, "真实功能简介")


class TestSkillOverride(unittest.TestCase):
    """中文展示覆盖：把纯英文/中英混杂的元数据替换为中文。"""

    def test_summarize_display_name_chinese(self):
        dn, desc = console_web.Handler._apply_skill_override(
            "summarize__skillhub", "summarize", "智能内容摘要工具……")
        self.assertEqual(dn, "内容摘要")
        self.assertEqual(desc, "智能内容摘要工具……")  # 简介未被覆盖

    def test_multi_search_engine_description_chinese(self):
        dn, desc = console_web.Handler._apply_skill_override(
            "multi-search-engine__skillhub", "搜索引擎",
            "Multi search engine integration with 16 engines……")
        self.assertEqual(dn, "搜索引擎")  # 展示名未被覆盖
        self.assertIn("多搜索引擎集成工具", desc)
        self.assertNotIn("Multi search engine", desc)

    def test_no_override_for_unknown(self):
        dn, desc = console_web.Handler._apply_skill_override(
            "some-other-skill", "原名", "原简介")
        self.assertEqual(dn, "原名")
        self.assertEqual(desc, "原简介")


if __name__ == "__main__":
    unittest.main()
