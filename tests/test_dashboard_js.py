r"""看板页面 JS 语法回归测试（2026-10-25 截屏交付时发现并修复）。

背景：task_center_web.py 的 HTML 模板是**普通三引号 Python 字符串**（非 raw）。
模板里 JS 代码 `'\n等待: '` 的 `\n` 被 Python 解释成**真换行**，嵌进
JS 单引号字符串——而 JS 单引号字符串**不允许字面换行**，整个 <script> 块
解析失败。后果：看板在真实浏览器里 tables 全空、meta 永远"加载中…"、
自动刷新死亡。此前只验证过 HTTP 200 和界面骨架（本环境 Chromium 起不来，
从未真正渲染过），所以这个缺陷一直潜伏。

修法：Python 源码里写成 `'\\n等待: '`（双反斜杠），served HTML 里 JS
收到的就是两字符转义 `\n`，JS 语义恰好就是想要的换行。

本测试锁定的承诺：
1. served HTML 的每个 <script> 块内，单/双引号字符串中**不允许出现裸换行**
   （逐字符扫描引号状态，O(n)，不用正则——避免 \s 与量词重叠的灾难性回溯）；
2. 模板里所有反斜杠转义必须"为 JS 而双写"：扫描 Python 模板中的 JS 区域，
   单反斜杠后跟 n/t 等字符即视为泄漏（此条为白名单式断言，命中即失败）；
3. 页面必须仍然请求四个 API 端点（tasks/runners/approvals/dag）——
   防有人改错端点名把看板静默打断。
"""

from __future__ import annotations

import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import task_center_web                                          # noqa: E402


def _script_blocks(html: str):
    return re.findall(r"<script>(.*?)</script>", html, re.S)


def _raw_newlines_in_quoted_strings(js: str):
    """返回 [(offset, quote_char), ...]：引号字符串内部出现的裸换行位置。"""
    bad = []
    i, quote = 0, None
    n = len(js)
    while i < n:
        ch = js[i]
        if quote is None:
            if ch in "'\"`":
                quote = ch
        else:
            if ch == "\\":            # 跳过转义对（\\ 后一个字符原样吞掉）
                i += 2
                continue
            if ch == "\n" and quote in "'\"":
                bad.append((i, quote))
            if ch == quote:
                quote = None
        i += 1
    return bad


class DashboardJsSyntaxTest(unittest.TestCase):

    def test_served_html_has_script_blocks(self):
        blocks = _script_blocks(task_center_web.HTML)
        self.assertGreaterEqual(len(blocks), 1, "served HTML 应包含至少一个 <script> 块")

    def test_no_raw_newline_inside_quoted_js_strings(self):
        # 本测试正是本次 bug 的守卫：'\n等待: ' 曾让 JS 整块解析失败
        for idx, block in enumerate(_script_blocks(task_center_web.HTML)):
            bad = _raw_newlines_in_quoted_strings(block)
            self.assertEqual(
                bad, [],
                "script 块 %d 的引号字符串内出现裸换行（JS 会解析失败）：%r"
                % (idx, bad[:3]),
            )

    def test_waiting_prefix_uses_two_char_escape_in_served_html(self):
        # served HTML 中必须是两字符序列 反斜杠+n，而不是真换行
        self.assertIn("\\n等待: ", task_center_web.HTML)
        self.assertNotIn("'\n等待: '", task_center_web.HTML)

    def test_python_template_does_not_leak_single_backslash_escapes(self):
        # 模板是普通（非 raw）三引号字符串：任何"给 JS 用"的 \n \t 都必须双写。
        # 单反斜杠 + n/t 出现在模板里 = Python 会把它变成控制字符 = 潜在同类 bug。
        with open(task_center_web.__file__, encoding="utf-8") as f:
            src = f.read()
        m = re.search(r'HTML = """(.*?)"""', src, re.S)
        self.assertIsNotNone(m, "未找到 HTML 模板定义")
        leaks = re.findall(r"(?<!\\)\\[nt]", m.group(1))
        self.assertEqual(
            leaks, [],
            "HTML 模板中存在单反斜杠转义（会被 Python 解释成控制字符，"
            "若在 JS 字符串内即语法错误）：%r" % (leaks[:5],),
        )

    def test_page_still_requests_all_four_api_endpoints(self):
        for ep in ("/api/tasks", "/api/runners", "/api/approvals", "/api/dag"):
            self.assertIn(ep, task_center_web.HTML, "看板页面缺少 API 请求：%s" % ep)


if __name__ == "__main__":
    unittest.main()
