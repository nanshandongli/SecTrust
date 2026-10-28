"""
tests/test_updates.py —— 「检查更新」与「手动落盘 CLI」回归测试（2026-10-29）。

两件事各有一条不可退让的纪律：

1. updates.UpdateChecker —— **查不到就说查不到**。
   不是 git 仓库 / 没配 remote / 网络不通 / 超时 / 远端没返回 HEAD，
   一律 state="unknown" 并给原因。绝不因为"没查到差异"就报"已是最新"——
   那比不检查更危险：用户会以为自己在最新版，其实只是没连上。
   （本测试用 monkeypatch 替换 git 调用，不依赖真实网络。）

2. tools/emit_files.py —— 退出码语义必须准确：
   0 落盘成功且闸门通过；1 落盘成功但命中高危（文件**仍在磁盘**，不回滚）；
   2 用法错误或全部被拒写（磁盘上不会出现任何文件）。
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "tools"))

import updates                                                # noqa: E402
from updates import UpdateChecker                             # noqa: E402

# 假凭证：相邻字面量拼接，避免 push protection / 自扫把测试文件本身判为泄漏
_FAKE = ("xoxb-" "1000000000000-" "2000000000000-" "abcdefghijklmnopqrstuvwx")

_LOCAL_SHA = "1111111111111111111111111111111111111111"
_OTHER_SHA = "2222222222222222222222222222222222222222"


def _fake_git(ls_remote=None, remote_out="origin", status_out="",
              rev_out=_LOCAL_SHA):
    """造一个假的 _run：按命令前缀返回预设结果。

    ls_remote 传 (stdout, err) 元组；None 表示模拟"命令失败"。
    """
    def _run(args, cwd, timeout=None):
        joined = " ".join(args)
        if joined.startswith("git rev-parse HEAD"):
            return rev_out, None
        if joined.startswith("git rev-parse --abbrev-ref"):
            return "main", None
        if joined.startswith("git describe"):
            return "v1.0", None
        if joined.startswith("git status"):
            return status_out, None
        if joined.startswith("git remote"):
            return remote_out, None
        if joined.startswith("git ls-remote"):
            if ls_remote is None:
                return None, "模拟网络不可达"
            return ls_remote
        return None, "unmocked: %s" % joined
    return _run


class _Tmp:
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sectrust-upd-")
        self.data = os.path.join(self.tmp, "data")
        os.makedirs(self.data, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _repo(self):
        """造一个"看起来像 git 仓库"的目录（有 .git 即可通过 _is_repo）。"""
        d = os.path.join(self.tmp, "repo")
        os.makedirs(os.path.join(d, ".git"), exist_ok=True)
        return d


# ------------------------------------------------------- 1. 更新检查
class UpdateCheckerTest(_Tmp, unittest.TestCase):

    def _checker(self):
        return UpdateChecker(repo_dir=self._repo(), data_dir=self.data)

    def test_non_git_dir_is_unknown_not_up_to_date(self):
        """不是 git 仓库 → unknown，绝不能说"已是最新"。"""
        plain = os.path.join(self.tmp, "plain")
        os.makedirs(plain, exist_ok=True)
        c = UpdateChecker(repo_dir=plain, data_dir=self.data)
        r = c.check()
        self.assertEqual(r["state"], "unknown")
        self.assertTrue(r.get("reason"), "unknown 必须给出原因")
        self.assertNotEqual(r["state"], "up_to_date")

    def test_no_remote_is_unknown(self):
        orig = updates._run
        updates._run = _fake_git(remote_out="")
        try:
            r = self._checker().check()
        finally:
            updates._run = orig
        self.assertEqual(r["state"], "unknown")
        self.assertIn("remote", r["reason"])

    def test_network_failure_is_unknown(self):
        """远端访问失败 → unknown，不能因为"没查到"就报最新。"""
        orig = updates._run
        updates._run = _fake_git(ls_remote=None)
        try:
            r = self._checker().check()
        finally:
            updates._run = orig
        self.assertEqual(r["state"], "unknown")
        self.assertIn("无法访问远端", r["reason"])

    def test_matching_remote_reports_up_to_date(self):
        orig = updates._run
        updates._run = _fake_git(ls_remote=("%s\tHEAD" % _LOCAL_SHA, None))
        try:
            r = self._checker().check()
        finally:
            updates._run = orig
        self.assertEqual(r["state"], "up_to_date")
        self.assertIn("已是最新", r["message"])

    def test_differing_remote_reports_remote_newer(self):
        orig = updates._run
        updates._run = _fake_git(ls_remote=("%s\tHEAD" % _OTHER_SHA, None))
        try:
            r = self._checker().check()
        finally:
            updates._run = orig
        self.assertEqual(r["state"], "remote_newer")
        # 必须说明两种可能，不替用户猜该 pull 还是 push
        self.assertIn("未推送", r["message"])

    def test_dirty_worktree_is_surfaced(self):
        orig = updates._run
        updates._run = _fake_git(ls_remote=("%s\tHEAD" % _LOCAL_SHA, None),
                                 status_out=" M a.py\n?? b.py")
        try:
            r = self._checker().check()
        finally:
            updates._run = orig
        self.assertTrue(r["local"]["dirty"])
        self.assertEqual(r["local"]["dirty_files"], 2)
        self.assertIn("未提交改动", r.get("warning", ""))

    def test_settings_roundtrip(self):
        c = self._checker()
        self.assertFalse(c.settings()["auto_check"])      # 默认关闭
        self.assertTrue(c.save_settings({"auto_check": True})["auto_check"])
        # 新实例读到同一份文件（持久化生效）
        self.assertTrue(self._checker().settings()["auto_check"])
        self.assertFalse(c.save_settings({"auto_check": False})["auto_check"])

    def test_settings_ignores_unknown_keys(self):
        """不存凭据，也不吃来路不明的键。"""
        c = self._checker()
        c.save_settings({"auto_check": True, "token": "secret", "cmd": "rm -rf"})
        s = self._checker().settings()
        self.assertTrue(s["auto_check"])
        self.assertNotIn("token", s)
        self.assertNotIn("cmd", s)


# ------------------------------------------------------- 2. 落盘 CLI
class EmitFilesCliTest(_Tmp, unittest.TestCase):

    @staticmethod
    def _call(argv):
        """跑 CLI 并吞掉输出，返回 (退出码, 输出文本)。"""
        import emit_files
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = emit_files.main(argv)
        return code, buf.getvalue()

    def _md(self, name="out.md", body=None):
        p = os.path.join(self.tmp, name)
        with open(p, "w", encoding="utf-8") as f:
            f.write(body or ('# 产出\n\n```python title="src/calc.py"\n'
                             "def add(a, b):\n    return a + b\n```\n"))
        return p

    def test_writes_files_and_exits_zero(self):
        target = os.path.join(self.tmp, "repo")
        os.makedirs(target, exist_ok=True)
        code, out = self._call(["--from-file", self._md(), "--target", target])
        self.assertEqual(code, 0, out)
        self.assertIn("已落盘 1 个文件", out)
        self.assertTrue(os.path.isfile(os.path.join(target, "src", "calc.py")))

    def test_secret_triggers_gate_exit_one_and_keeps_file(self):
        """命中高危：退 1，且文件**必须还在**（不许偷偷回滚）。"""
        target = os.path.join(self.tmp, "repo")
        os.makedirs(target, exist_ok=True)
        body = ('```python title="src/leak.py"\nTOKEN = "%s"\nprint(TOKEN)\n```'
                % _FAKE)
        code, out = self._call(["--from-file", self._md("leak.md", body),
                                "--target", target])
        self.assertEqual(code, 1, out)
        self.assertIn("闸门触发", out)
        self.assertTrue(os.path.isfile(os.path.join(target, "src", "leak.py")),
                        "命中高危也不许偷偷删文件——回滚与否由人决定")

    def test_missing_target_exits_two(self):
        code, out = self._call(["--from-file", self._md(),
                                "--target", os.path.join(self.tmp, "nope")])
        self.assertEqual(code, 2)
        self.assertIn("--create", out)

    def test_filesystem_root_rejected(self):
        code, out = self._call(["--from-file", self._md(), "--target", "/"])
        self.assertEqual(code, 2, out)
        self.assertIn("文件系统根目录", out)

    def test_no_input_source_exits_two(self):
        target = os.path.join(self.tmp, "repo")
        os.makedirs(target, exist_ok=True)
        code, out = self._call(["--target", target])
        self.assertEqual(code, 2)
        self.assertIn("输入来源", out)

    def test_sensitive_path_blocked_but_others_written(self):
        target = os.path.join(self.tmp, "repo")
        os.makedirs(target, exist_ok=True)
        jp = os.path.join(self.tmp, "files.json")
        with open(jp, "w", encoding="utf-8") as f:
            json.dump([{"path": ".env", "content": "SECRET=1"},
                       {"path": "ok.py", "content": "x = 1"}], f)
        code, out = self._call(["--from-json", jp, "--target", target])
        self.assertEqual(code, 0, out)
        self.assertFalse(os.path.isfile(os.path.join(target, ".env")),
                         "敏感文件绝不能落到磁盘")
        self.assertTrue(os.path.isfile(os.path.join(target, "ok.py")))
        self.assertIn("拒写", out)

    def test_json_output_is_parseable(self):
        target = os.path.join(self.tmp, "repo")
        os.makedirs(target, exist_ok=True)
        code, out = self._call(["--from-file", self._md(), "--target", target,
                                "--json"])
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(data["op"], "write_files")
        self.assertEqual(len(data["written"]), 1)

    def test_all_blocked_exits_two_with_nothing_written(self):
        target = os.path.join(self.tmp, "repo")
        os.makedirs(target, exist_ok=True)
        jp = os.path.join(self.tmp, "bad.json")
        with open(jp, "w", encoding="utf-8") as f:
            json.dump([{"path": ".env", "content": "x"}], f)
        code, out = self._call(["--from-json", jp, "--target", target])
        self.assertEqual(code, 2, out)
        self.assertFalse(os.path.isfile(os.path.join(target, ".env")))


# ------------------------------------------------------- 3. 控制台端点（真实 HTTP）
class ConsoleUpdatesApiTest(unittest.TestCase):
    """真起一个 HTTP 服务打端点——源码里写了字符串不等于接口能用。"""

    @classmethod
    def setUpClass(cls):
        import threading
        from http.server import ThreadingHTTPServer
        from task_registry import TaskRegistry
        from orchestration import DagScheduler
        from console_web import Handler, APP_HTML

        cls.tmp = tempfile.mkdtemp(prefix="sectrust-api-")
        db = os.path.join(cls.tmp, "tasks.db")
        reg = TaskRegistry(db_path=db, runner_id="test-api")
        sched = DagScheduler(db_path=db, registry=reg)
        sched.add_task("t1", payload={"op": "write_files", "name": "代码落盘"})
        # 直接把落盘结果塞进 payload，模拟执行层写回的 result
        reg.set_payload("t1", {
            "op": "write_files", "name": "代码落盘",
            "result": {"op": "write_files", "root": "/tmp/x",
                       "written": [{"path": "a.py", "bytes": 3,
                                    "sha256": "f" * 64, "overwrote": False}],
                       "blocked": [], "scan": {"should_fail": False,
                                               "summary": {"total": 0}}},
        })

        Handler.registry = reg
        Handler.project_root = _ROOT
        Handler.assistant_data_dir = os.path.join(cls.tmp, "data")
        Handler.app_html = APP_HTML
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.base = "http://127.0.0.1:%d" % cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever,
                         daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.server.shutdown()
            cls.server.server_close()
        except Exception:                                  # noqa: BLE001
            pass
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _get(self, path):
        from urllib.request import urlopen
        with urlopen(self.base + path, timeout=15) as r:
            return json.loads(r.read().decode("utf-8"))

    def _post(self, path, body):
        from urllib.request import Request, urlopen
        req = Request(self.base + path,
                      data=json.dumps(body).encode("utf-8"),
                      headers={"Content-Type": "application/json"})
        with urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode("utf-8"))

    def test_task_detail_returns_write_files_result(self):
        d = self._get("/api/task/t1")
        self.assertEqual(d["task_id"], "t1")
        self.assertEqual(d["op"], "write_files")
        self.assertEqual(d["result"]["op"], "write_files")
        self.assertEqual(d["result"]["written"][0]["path"], "a.py")

    def test_task_detail_missing_task_reports_error(self):
        d = self._get("/api/task/does-not-exist")
        self.assertIn("error", d)

    def test_updates_settings_get_and_post(self):
        d = self._get("/api/updates/settings")
        self.assertIn("auto_check", d)
        self.assertFalse(d["auto_check"])
        d2 = self._post("/api/updates/settings", {"auto_check": True})
        self.assertTrue(d2["auto_check"])
        # 回读确认持久化
        self.assertTrue(self._get("/api/updates/settings")["auto_check"])

    def test_updates_check_returns_known_state(self):
        """真实仓库有 origin；网络不通时必须是 unknown + reason，不许谎报最新。"""
        d = self._get("/api/updates/check")
        self.assertIn(d["state"], ("up_to_date", "remote_newer", "unknown"))
        if d["state"] == "unknown":
            self.assertTrue(d.get("reason"), "unknown 必须给原因")


if __name__ == "__main__":
    unittest.main()
