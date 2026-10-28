"""write_files 落盘链路回归测试（2026-10-29）。

补齐的缺口：software_dev 模板能"产出代码"，但代码只躺在模型返回的 markdown
文本里，没有任何人把它变成磁盘上的文件。此前唯一能落盘的路子是 op="shell"
让模型自己拼 `cat >` 命令——不可控（引号/换行随时炸）也不安全（无从预检）。

本测试证明 op="write_files" 这条链路真的成立：
  1. extract_code_blocks —— 从模型产出原文里解析 fenced 代码块并推断文件名；
  2. 路径安全 —— 越界（../、绝对路径）与敏感目标（.git/、data/、密钥文件）
     一律拒写并说明原因，绝不静默跳过；
  3. 落盘 —— 原子写、内容正确、sha256 可审计、overwrite 语义清晰；
  4. 落盘后密钥扫描 —— AI 写进去的凭证当场现形，命中高危就地挂起等人工；
  5. daemon 集成 —— 算子真的被派发，且结果进任务 result 供审计。

诚实边界：本测试不含任何 git 操作（commit/push 仍归人工）。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from execution.daemon import ExecutionDaemon                              # noqa: E402
from execution.writer import (extract_code_blocks, is_sensitive,           # noqa: E402
                              normalize_files, plan_from_payload,
                              write_files)


# 假凭证：拆成相邻字面量写，避免 push protection / 自扫把测试文件本身判为泄漏。
# 拼起来是标准 Slack bot token 形态，足以触发 scanner 的结构化规则。
_FAKE = ("xoxb-" "1000000000000-" "2000000000000-" "abcdefghijklmnopqrstuvwx")


class _TmpEnv:
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sectrust-writer-")
        self.db = os.path.join(self.tmp, "tasks.db")
        self.ws_root = os.path.join(self.tmp, "workspaces")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _daemon(self, **kw):
        kw.setdefault("workspace_root", self.ws_root)
        kw.setdefault("approval_wait", 1.0)
        d = ExecutionDaemon(db_path=self.db, **kw)
        self.addCleanup(d.close)
        return d


# ------------------------------------------------------- 1. 代码块提取
class ExtractCodeBlocksTest(unittest.TestCase):

    def test_parses_fenced_block_and_infers_name_from_lang(self):
        text = "说明文字\n\n```python\nprint('hi')\n```\n"
        blocks = extract_code_blocks(text)
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["path"], "main.py")
        self.assertEqual(blocks[0]["content"], "print('hi')")
        self.assertTrue(blocks[0]["inferred"], "语言推断的名字必须标明 inferred")

    def test_honors_explicit_filename_in_info_string(self):
        text = '```python title="src/app.py"\nx = 1\n```'
        blocks = extract_code_blocks(text)
        self.assertEqual(blocks[0]["path"], "src/app.py")
        self.assertFalse(blocks[0]["inferred"], "显式给了名字就不该标 inferred")

    def test_honors_filename_in_leading_comment(self):
        text = "```python\n# file: pkg/util.py\ndef f(): pass\n```"
        blocks = extract_code_blocks(text)
        self.assertEqual(blocks[0]["path"], "pkg/util.py")
        self.assertNotIn("file:", blocks[0]["content"].splitlines()[0],
                         "文件名标注行不该混进代码正文")

    def test_multiple_blocks_get_distinct_names(self):
        text = "```python\na=1\n```\n\n```python\nb=2\n```"
        blocks = extract_code_blocks(text)
        self.assertEqual([b["path"] for b in blocks], ["main.py", "main_2.py"],
                         "重复推断名必须加序号，不互相覆盖")

    def test_returns_empty_for_plain_prose(self):
        self.assertEqual(extract_code_blocks("没有代码块的一段说明。"), [])

    def test_unknown_lang_falls_back_to_txt(self):
        blocks = extract_code_blocks("```brainfuck\n+++\n```")
        self.assertEqual(blocks[0]["path"], "main.txt")


# ------------------------------------------------------- 2. 路径安全
class PathSafetyTest(unittest.TestCase):

    def test_rejects_traversal(self):
        items, blocked = normalize_files([{"path": "../../evil.py", "content": "x"}])
        self.assertEqual(items, [])
        self.assertTrue(blocked, "越界必须记拒写原因，不静默丢弃")
        self.assertIn("路径不合法", blocked[0]["reason"])

    def test_rejects_absolute_and_drive_paths(self):
        for bad in ("/etc/passwd", "C:\\Windows\\x.txt"):
            items, _ = normalize_files([{"path": bad, "content": "x"}])
            self.assertEqual(items, [], "绝对路径必须拒写：%r" % bad)

    def test_rejects_non_string_content(self):
        items, blocked = normalize_files([{"path": "a.py", "content": 123}])
        self.assertEqual(items, [])
        self.assertIn("content", blocked[0]["reason"])

    def test_requires_non_empty_list(self):
        from execution.writer import FileWriteError
        for bad in ([], None, "not-a-list"):
            with self.assertRaises(FileWriteError):
                normalize_files(bad)

    def test_sensitive_targets_blocked(self):
        for path in (".git/config", "data/task_registry.db", ".env",
                     "id_rsa", "cert/server.pem", "keys/k.key"):
            self.assertIsNotNone(is_sensitive(path),
                                 "敏感目标必须拒写：%s" % path)

    def test_normal_code_path_allowed(self):
        self.assertIsNone(is_sensitive("src/app.py"))
        self.assertIsNone(is_sensitive("tests/test_x.py"))


# ------------------------------------------------------- 3. 落盘
class WriteFilesTest(_TmpEnv, unittest.TestCase):

    def test_writes_content_and_reports_sha256(self):
        d = self._daemon()
        res = write_files(d.workspace, [
            {"path": "src/app.py", "content": "print('hi')\n"},
        ])
        self.assertEqual(len(res["written"]), 1)
        self.assertEqual(res["blocked"], [])
        target = os.path.join(d.workspace.work_dir, "src", "app.py")
        self.assertTrue(os.path.isfile(target), "文件必须真的落盘")
        with open(target, encoding="utf-8") as f:
            self.assertEqual(f.read(), "print('hi')\n")
        self.assertEqual(len(res["written"][0]["sha256"]), 64)

    def test_refuses_overwrite_by_default(self):
        d = self._daemon()
        write_files(d.workspace, [{"path": "a.py", "content": "v1"}])
        res = write_files(d.workspace, [{"path": "a.py", "content": "v2"}])
        self.assertEqual(res["written"], [])
        self.assertIn("已存在", res["blocked"][0]["reason"])
        with open(os.path.join(d.workspace.work_dir, "a.py"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "v1", "未声明覆盖时不得改写原文件")

    def test_overwrite_when_declared(self):
        d = self._daemon()
        write_files(d.workspace, [{"path": "a.py", "content": "v1"}])
        res = write_files(d.workspace, [{"path": "a.py", "content": "v2"}],
                          overwrite=True)
        self.assertEqual(res["written"][0]["overwrote"], True)
        with open(os.path.join(d.workspace.work_dir, "a.py"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "v2")

    def test_refuses_root_outside_workspace(self):
        d = self._daemon()
        outside = os.path.join(self.tmp, "somewhere-else")
        res = write_files(d.workspace, [{"path": "a.py", "content": "x"}],
                          root=outside)
        self.assertEqual(res["written"], [])
        self.assertIsNotNone(res.get("error"))
        self.assertFalse(os.path.isdir(outside),
                         "被拒的落盘不该顺手把目录建出来")

    def test_allows_outside_only_when_explicitly_declared(self):
        d = self._daemon()
        outside = os.path.join(self.tmp, "explicit-ok")
        res = write_files(d.workspace, [{"path": "a.py", "content": "x"}],
                          root=outside, allow_outside=True)
        self.assertEqual(len(res["written"]), 1)
        self.assertTrue(os.path.isfile(os.path.join(outside, "a.py")))

    def test_sensitive_path_blocked_end_to_end(self):
        d = self._daemon()
        res = write_files(d.workspace, [
            {"path": ".git/config", "content": "malicious"},
            {"path": "src/ok.py", "content": "fine"},
        ])
        self.assertEqual([w["path"] for w in res["written"]], ["src/ok.py"])
        self.assertEqual(res["blocked"][0]["path"], ".git/config")

    def test_summary_text_mentions_writes_and_blocks(self):
        d = self._daemon()
        res = write_files(d.workspace, [
            {"path": "a.py", "content": "x"},
            {"path": ".env", "content": "y"},
        ])
        self.assertIn("已落盘 1 个文件", res["output"])
        self.assertIn("拒写 1 项", res["output"])
        self.assertIn(".env", res["output"])


# ------------------------------------------- 4. 落盘后密钥扫描（产品灵魂）
class ScanAfterWriteTest(_TmpEnv, unittest.TestCase):

    def test_clean_code_passes_gate(self):
        d = self._daemon()
        res = write_files(d.workspace, [
            {"path": "src/add.py", "content": "def add(a, b):\n    return a + b\n"},
        ])
        scan = res.get("scan") or {}
        self.assertEqual(scan.get("should_fail"), False)
        self.assertEqual((scan.get("summary") or {}).get("total", 0), 0)
        self.assertIn("闸门通过", res["output"])

    def test_secret_in_written_code_triggers_gate(self):
        d = self._daemon()
        res = write_files(d.workspace, [
            {"path": "src/leak.py",
             "content": 'TOKEN = "%s"\nprint(TOKEN)\n' % _FAKE},
        ])
        scan = res.get("scan") or {}
        # 文件确实落盘了（不假装没发生），但闸门必须触发
        self.assertEqual(len(res["written"]), 1)
        self.assertTrue(scan.get("should_fail"),
                        "写进去的真实凭证必须触发闸门，不能悄悄放行")
        self.assertGreater((scan.get("summary") or {}).get("total", 0), 0)
        self.assertTrue(scan.get("findings"))
        # 证据行是脱敏的：报告里不该复现完整凭证
        self.assertNotIn(_FAKE, str(scan["findings"]))


# ------------------------------------------- 5. daemon 集成
class DaemonWriteFilesTest(_TmpEnv, unittest.TestCase):

    def test_daemon_dispatches_write_files_and_writes(self):
        d = self._daemon()
        d.sched.add_task("w1", payload={
            "op": "write_files",
            "files": [{"path": "pkg/mod.py", "content": "X = 1\n"}],
        })
        self.assertEqual(d.run_once(), [("w1", "done")])
        target = os.path.join(d.workspace.work_dir, "pkg", "mod.py")
        self.assertTrue(os.path.isfile(target))
        with open(target, encoding="utf-8") as f:
            self.assertEqual(f.read(), "X = 1\n")

    def test_secret_write_suspends_for_human(self):
        """落盘发生，但含凭证 → needs_human，不让它顺畅走到下一步。"""
        d = self._daemon()
        d.sched.add_task("w2", payload={
            "op": "write_files",
            "files": [{"path": "pkg/leak.py",
                       "content": 'KEY = "%s"\n' % _FAKE}],
        })
        self.assertEqual(d.run_once(), [("w2", "needs_human")])
        t = d.reg.get_task("w2")
        self.assertEqual(t["status"], "needs_human")
        # 文件已写入这件事如实记录，执行层不偷偷删（那才是真危险）
        self.assertTrue(os.path.isfile(
            os.path.join(d.workspace.work_dir, "pkg", "leak.py")))
        # 挂起原因必须写进任务日志，供人工一眼看到为什么被拦
        logs = (t.get("payload") or {}).get("log") or []
        self.assertTrue(any("密钥闸门" in str(x) for x in logs),
                        "挂起原因应写入任务日志，实际日志：%r" % (logs,))

    def test_extract_payload_writes_from_model_text(self):
        d = self._daemon()
        model_text = (
            "下面给出实现：\n\n"
            '```python title="calc.py"\n'
            "def add(a, b):\n    return a + b\n"
            "```\n"
        )
        d.sched.add_task("w3", payload={
            "op": "write_files", "extract": model_text,
        })
        self.assertEqual(d.run_once(), [("w3", "done")])
        target = os.path.join(d.workspace.work_dir, "calc.py")
        self.assertTrue(os.path.isfile(target))
        with open(target, encoding="utf-8") as f:
            self.assertEqual(f.read(), "def add(a, b):\n    return a + b")

    def test_from_upstream_consumes_dependency_output(self):
        """落盘任务挂在 llm 任务之后：直接把上游产出的代码块落成文件。"""
        from execution.daemon import ExecutionDaemon as _D

        class _Eng:
            def execute(self, instruction, context, model=None):
                return {"answer": '```python title="gen/app.py"\nY = 2\n```',
                        "model": "fake", "usage": {}}

        d = self._daemon(llm_engine_factory=lambda: _Eng())
        d.sched.add_task("code", payload={
            "op": "llm", "instruction": "写一个模块",
        })
        d.sched.add_task("save", payload={
            "op": "write_files", "from_upstream": True, "overwrite": True,
        }, depends_on=["code"])
        for _ in range(4):
            out = d.run_once()
            if not out:
                break
        self.assertEqual(d.reg.get_task("code")["status"], "completed")
        self.assertEqual(d.reg.get_task("save")["status"], "completed")
        target = os.path.join(d.workspace.work_dir, "gen", "app.py")
        self.assertTrue(os.path.isfile(target), "上游产出的代码必须真的落盘")
        with open(target, encoding="utf-8") as f:
            self.assertEqual(f.read(), "Y = 2")

    def test_software_dev_plan_writes_real_files(self):
        """最有力的一条：planner 拆的 software_dev 计划跑完，磁盘上真有文件。

        这正是此前断掉的一环——模型"写"了代码，但没人把它变成文件。
        """
        from planner import Planner

        class _Eng:
            """前端/后端工序吐带文件名的代码块；其余工序吐合规长文本。"""
            def execute(self, instruction, context, model=None):
                if "前端" in instruction:
                    body = ('```python title="web/login.py"\n'
                            "def render():\n    return '<login/>'\n```")
                elif "后端" in instruction:
                    body = ('```python title="api/auth.py"\n'
                            "def verify(tok):\n    return bool(tok)\n```")
                else:
                    body = "# 通用产出\n\n本工序产出已完成。"
                # 垫文要够长（模板里最严的是架构工序 min_chars=300），
                # 否则假引擎产出会被质量门打回，测不到落盘这一步。
                pad = ("补充说明：本工序产出完整，满足验收标准，架构清晰、接口明确、"
                       "组件齐备、测试通过、文档完整，可直接进入下一环节使用。" * 8)
                return {"answer": "# 产出\n\n" + body + "\n\n" + pad,
                        "model": "fake", "usage": {}}

        d = self._daemon(llm_engine_factory=lambda: _Eng())
        # 显式给 topic 槽位：planner 抽不到就留 <待补:topic>，执行层会如实
        # 拒跑（"缺参数是等人补，不是让模型猜"），那样就测不到落盘了。
        plan = Planner().decompose("开发一个登录功能",
                                   params={"topic": "登录功能"}, tier="complex")
        self.assertEqual(plan.template, "software_dev")
        plan.build(d.sched, plan_id="p1")

        for _ in range(40):
            if not d.run_once(max_tasks=8):
                break

        # 关键断言：AI 同事产出的两个文件真的落在了磁盘上
        web = os.path.join(d.workspace.work_dir, "web", "login.py")
        api = os.path.join(d.workspace.work_dir, "api", "auth.py")
        self.assertTrue(os.path.isfile(web), "前端代码应落盘到 web/login.py")
        self.assertTrue(os.path.isfile(api), "后端代码应落盘到 api/auth.py")
        with open(api, encoding="utf-8") as f:
            self.assertIn("def verify", f.read())
        # 落盘任务自身也走质量门并记账
        self.assertEqual(d.reg.get_task("p1/write_code")["status"], "completed")

    def test_all_blocked_means_failure_not_silent_success(self):
        d = self._daemon()
        d.sched.add_task("w4", payload={
            "op": "write_files",
            "files": [{"path": ".env", "content": "x"}],
        })
        self.assertEqual(d.run_once(), [("w4", "failed")])
        self.assertFalse(os.path.isfile(
            os.path.join(d.workspace.work_dir, ".env")),
            "被拒的文件绝不能出现在磁盘上")


if __name__ == "__main__":
    unittest.main()
