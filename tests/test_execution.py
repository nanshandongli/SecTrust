"""执行层回归测试（2026-10-25 对照 Grok Bot 差距分析开建执行层时新增）。

锁定的承诺：
1. BotWorkspace：bot_id 白名单防路径穿越；state 原子写+损坏不静默重置；
   命令执行一律走沙箱（带 containment 诚实标注）；产物目录隔离；
2. ExecutionDaemon：认领→执行→complete/fail 全链路；op 派发（shell/skill/noop）；
   **审批门红线**——payload 声明 approval_action 时，批准才执行、拒绝/超时
   转 needs_human 不执行；未知 op 明确失败不假装成功。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from execution.daemon import ExecutionDaemon                              # noqa: E402
from execution.workspace import BotWorkspace, WorkspaceError              # noqa: E402
from orchestration import DagScheduler                                    # noqa: E402
from sandbox import SandboxManager                                        # noqa: E402


class _TmpEnv:
    """临时目录 + 环境变量托管，tearDown 自动清理。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sectrust-exec-test-")
        self.db = os.path.join(self.tmp, "tasks.db")
        self.ws_root = os.path.join(self.tmp, "workspaces")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)


# ---------------------------------------------------------------- workspace
class WorkspaceTest(_TmpEnv, unittest.TestCase):

    def test_workspace_layout(self):
        ws = BotWorkspace("sales-bot", root=self.ws_root)
        self.assertTrue(os.path.isdir(ws.work_dir))
        self.assertTrue(os.path.isdir(ws.outbox_dir))

    def test_bot_id_rejects_traversal(self):
        for bad in ["../evil", "a/b", "", "x" * 65, ".hidden", "bot$"]:
            with self.assertRaises(WorkspaceError, msg=bad):
                BotWorkspace(bad, root=self.ws_root)

    def test_state_roundtrip_and_merge(self):
        ws = BotWorkspace("b1", root=self.ws_root)
        self.assertEqual(ws.read_state(), {})
        ws.write_state({"voice": "简洁"})
        ws.write_state({"lang": "zh"})
        st = ws.read_state()
        self.assertEqual(st["voice"], "简洁")     # merge 语义
        self.assertEqual(st["lang"], "zh")
        self.assertIn("updated_at", st)

    def test_state_corrupt_not_silently_reset(self):
        ws = BotWorkspace("b2", root=self.ws_root)
        with open(ws.state_path, "w", encoding="utf-8") as f:
            f.write("{not json")
        st = ws.read_state()
        self.assertEqual(st, {"_corrupt": True})   # 原样标记，不静默清空

    def test_output_path_escape_blocked(self):
        ws = BotWorkspace("b3", root=self.ws_root)
        with self.assertRaises(WorkspaceError):
            ws.put_output("../escape.txt", "x")
        with self.assertRaises(WorkspaceError):
            ws.put_output("/abs/path.txt", "x")
        p = ws.put_output("ok.txt", "hello")
        self.assertTrue(p.startswith(ws.outbox_dir))
        self.assertEqual(ws.read_output("ok.txt"), "hello")
        self.assertEqual(ws.list_outputs(), ["ok.txt"])

    def test_run_goes_through_sandbox_with_honest_containment(self):
        ws = BotWorkspace("b4", root=self.ws_root)
        res = ws.run([sys.executable, "-c", "print('hi-from-sandbox')"])
        self.assertTrue(res.ok, res.error)
        self.assertIn("hi-from-sandbox", res.content)
        # 诚实标注：containment 必须是已文档化的三档之一，不得为空/伪造
        # （advisory=仅预检 / limited=有资源上限但非 OS 隔离 / os=Docker/E2B）
        self.assertIn(res.containment, ("advisory", "limited", "os"))
        self.assertTrue(res.backend)

    def test_run_writes_only_inside_workdir(self):
        ws = BotWorkspace("b5", root=self.ws_root)
        res = ws.run([sys.executable, "-c",
                      "open('artifact.txt','w').write('x')"])
        self.assertTrue(res.ok, res.error)
        self.assertTrue(os.path.exists(os.path.join(ws.work_dir, "artifact.txt")))


# ------------------------------------------------------------------- daemon
class DaemonTest(_TmpEnv, unittest.TestCase):

    def _daemon(self, **kw):
        kw.setdefault("workspace_root", self.ws_root)
        kw.setdefault("approval_wait", 1.0)
        d = ExecutionDaemon(db_path=self.db, **kw)
        # 无论测试成败都关连接（本项目纪律：测试自身不泄漏）
        self.addCleanup(d.close)
        return d

    def _register(self, sched, task_id, payload):
        sched.add_task(task_id, payload=payload)

    def test_noop_task_completes(self):
        d = self._daemon()
        sched = d.sched
        self._register(sched, "t-noop", {"op": "noop"})
        out = d.run_once()
        self.assertEqual(out, [("t-noop", "done")])
        t = d.reg.get_task("t-noop")
        self.assertEqual(t["status"], "completed")
        self.assertEqual(t["payload"]["result"]["op"], "noop")
        d.close()

    def test_shell_task_really_executes(self):
        d = self._daemon()
        d.sched.add_task("t-shell", payload={
            "op": "shell",
            "cmd": [sys.executable, "-c", "print('daemon-was-here')"],
        })
        out = d.run_once()
        self.assertEqual(out, [("t-shell", "done")])
        t = d.reg.get_task("t-shell")
        self.assertIn("daemon-was-here", t["payload"]["result"]["stdout"])
        d.close()

    def test_unknown_op_fails_loudly(self):
        d = self._daemon()
        d.sched.add_task("t-fly", payload={"op": "fly-to-moon"})
        out = d.run_once()
        self.assertEqual(out, [("t-fly", "failed")])
        t = d.reg.get_task("t-fly")
        self.assertEqual(t["status"], "failed")
        self.assertIn("未知 op", t["payload"].get("error", ""))
        d.close()

    def test_skill_op_activates_registered_skill(self):
        # 临时技能目录 + 环境变量指向它（不碰真实技能库）
        skill_dir = os.path.join(self.tmp, "skills", "echo-skill")
        os.makedirs(skill_dir)
        with open(os.path.join(skill_dir, "SKILL.md"), "w", encoding="utf-8") as f:
            f.write("---\nname: echo-skill\ndescription: 回声技能\n---\n正文：原样回复。\n")
        os.environ["SECTRUST_SKILLS_DIR"] = os.path.join(self.tmp, "skills")
        try:
            d = self._daemon()
            d.sched.add_task("t-skill", payload={"op": "skill", "name": "echo-skill"})
            out = d.run_once()
            self.assertEqual(out, [("t-skill", "done")])
            t = d.reg.get_task("t-skill")
            self.assertEqual(t["payload"]["result"]["description"], "回声技能")
            st = d.workspace.read_state()
            self.assertEqual(st["last_skill"], "echo-skill")
            d.close()
        finally:
            os.environ.pop("SECTRUST_SKILLS_DIR", None)

    # ---- 审批门：HITL 红线，两条路径都要锁死 ----
    def _approver_thread(self, db_path, decision):
        """后台线程：轮询 pending 审批并给出决定。

        注意 decision 用 decide() 的入参语义：approve / reject（不是 approved）。
        """

        def _loop():
            from approval import ApprovalCenter
            deadline = time.time() + 8
            while time.time() < deadline:
                center = ApprovalCenter(db_path=db_path)
                try:
                    for a in center.list_approvals(status="pending", limit=50):
                        center.decide(a["approval_id"], decision, by="test")
                        return
                finally:
                    center.close()
                time.sleep(0.1)

        th = threading.Thread(target=_loop, daemon=True)
        th.start()
        return th

    def test_approval_gate_blocks_until_approved(self):
        # approval_wait 必须大于 wait_for_decision 的 poll 间隔(1s) +
        # approver 动作延迟，否则审批刚到就超时（假阴性）。5s 仍很快。
        d = self._daemon(approval_wait=5.0)
        th = self._approver_thread(self.db, "approve")
        d.sched.add_task("t-send-ok", payload={
            "op": "shell",
            "cmd": [sys.executable, "-c", "print('sent')"],
            "approval_action": "send",
        })
        out = d.run_once()
        th.join(timeout=8)
        self.assertEqual(out, [("t-send-ok", "done")])   # 批准后才执行
        d.close()

    def test_approval_gate_rejection_suspends_task(self):
        d = self._daemon(approval_wait=5.0)
        th = self._approver_thread(self.db, "reject")
        d.sched.add_task("t-send-no", payload={
            "op": "shell",
            "cmd": [sys.executable, "-c", "print('should-not-run')"],
            "approval_action": "send",
        })
        out = d.run_once()
        th.join(timeout=8)
        self.assertEqual(out, [("t-send-no", "needs_human")])
        t = d.reg.get_task("t-send-no")
        self.assertEqual(t["status"], "needs_human")     # 挂起等人工，不级联
        # 被拒的任务绝不能已执行
        self.assertNotIn("result", t["payload"])
        d.close()

    def test_unknown_approval_action_fails(self):
        d = self._daemon()
        d.sched.add_task("t-weird", payload={
            "op": "noop", "approval_action": "launch-missiles",
        })
        out = d.run_once()
        self.assertEqual(out, [("t-weird", "failed")])
        d.close()

    def test_approval_timeout_suspends(self):
        # 无人审批：等待上限用 0.6s，超时 → needs_human
        d = self._daemon(approval_wait=0.6)
        d.sched.add_task("t-send-wait", payload={
            "op": "noop", "approval_action": "publish",
        })
        out = d.run_once()
        self.assertEqual(out, [("t-send-wait", "needs_human")])
        d.close()

    def test_dag_downstream_runs_after_upstream(self):
        # 执行层与 DAG 的集成：上游 done 后下游才就绪
        d = self._daemon()
        d.sched.add_task("u", payload={"op": "noop"})
        d.sched.add_task("v", payload={"op": "noop"}, depends_on=["u"])
        out = d.run_once(max_tasks=4)
        self.assertEqual(sorted(s for _, s in out), ["done", "done"])
        self.assertEqual(d.reg.get_task("v")["status"], "completed")
        d.close()


if __name__ == "__main__":
    unittest.main()
