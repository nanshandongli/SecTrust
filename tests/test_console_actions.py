"""
tests/test_console_actions.py —— 审计动作执行器 + 定时任务真实触发 测试

覆盖：
  1. 四个动作（self_scan / cost_snapshot / whitepaper / closure_report）执行成功并落档到
     reports/console_actions/<action>_<ts>/，被资料库只读索引收录（归档即入库）。
  2. 自扫反馈环断开：本动作归档目录必须被排除，重复扫同一根目录计数不膨胀。
  3. execute_action 分发：未知动作诚实报错；已知动作全部 ok。
  4. scheduler.fire_due(executor=...) 真实回调：到期任务调用 executor 并把结果随返回值带回。
"""

import json
import os
import shutil
import sqlite3
import tempfile
import time
import unittest

import console_actions as CA
import scheduler
from task_registry import TaskRegistry


class FakeCost:
    """极简 cost_store 替身（有 dashboard 方法即可）。"""

    def dashboard(self, days=30):
        return {
            "totals": {
                "tasks": 1, "model_calls": 2, "tokens": 3, "tool_calls": 4,
                "retries": 0, "cost_estimate": 0.5, "unpriced_calls": 1,
            },
            "outliers": {},
        }


class TestConsoleActions(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.out = os.path.join(self.tmp, "out")
        os.makedirs(self.out)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _ctx(self, project_root, cost_store=None, context=None):
        return {
            "project_root": project_root, "tracer": None,
            "cost_store": cost_store, "days": 30, "context": context or {},
        }

    # ---- 1) self_scan ----
    def test_self_scan_runs_and_archives(self):
        r = CA.run_self_scan(self.tmp, tracer=None, out_dir=self.out, operator="tester")
        self.assertTrue(r["ok"])
        self.assertTrue(os.path.isdir(r["report_dir"]))
        for f in r["files"]:
            self.assertTrue(os.path.isfile(os.path.join(r["report_dir"], f)))
        # 诚实边界：real_leaks 始终声明为 0，绝不伪造泄露数字
        self.assertEqual(r["real_leaks"], 0)

    def test_self_scan_excludes_own_archive(self):
        # 构造含「历史归档（带假密钥）」+「源码（带假密钥）」的临时项目，
        # 验证归档目录被 allowlist 排除，否则重复自扫会自我膨胀。
        root = os.path.join(self.tmp, "proj")
        os.makedirs(os.path.join(root, "reports", "console_actions", "old"))
        with open(os.path.join(root, "reports", "console_actions", "old", "self_scan.py"),
                  "w", encoding="utf-8") as fh:
            fh.write("KEY = 'AKIALALEMEL33243OLIA'  # 旧归档里的假密钥\n")
        os.makedirs(os.path.join(root, "src"))
        with open(os.path.join(root, "src", "app.py"), "w", encoding="utf-8") as fh:
            fh.write("aws_key = 'AKIALALEMEL33243OLIA'\n")
        r = CA.run_self_scan(root, tracer=None,
                             out_dir=os.path.join(root, "out"), operator="x")
        self.assertTrue(r["ok"])
        j = json.load(open(os.path.join(r["report_dir"], "self_scan.json"),
                           encoding="utf-8"))
        hit_paths = [f.get("path", "") for f in j.get("findings", [])]
        norm = [p.replace("\\", "/") for p in hit_paths]
        # 旧归档（reports/console_actions/old）必须被排除
        self.assertTrue(
            all("reports/console_actions/old" not in p for p in norm),
            msg="旧归档未被排除：%s" % hit_paths)
        # 源码必须被命中（扫描器返回绝对路径，按文件名判断）
        self.assertTrue(any(p.endswith("app.py") for p in hit_paths),
                        msg="源码命中未出现：%s" % hit_paths)

    def test_self_scan_stable_across_repeats(self):
        # 同一根目录重复自扫，总数必须稳定（反馈环已断开）。
        root = os.path.join(self.tmp, "proj2")
        os.makedirs(os.path.join(root, "src"))
        with open(os.path.join(root, "src", "app.py"), "w", encoding="utf-8") as fh:
            fh.write("aws_key = 'AKIALALEMEL33243OLIA'\n")
        totals = []
        for i in range(3):
            r = CA.run_self_scan(root, tracer=None,
                                 out_dir=os.path.join(root, "out%d" % i), operator="x")
            totals.append(r["total"])
        self.assertEqual(totals[0], totals[1])
        self.assertEqual(totals[1], totals[2])

    # ---- 2) cost_snapshot ----
    def test_cost_snapshot_archives(self):
        r = CA.run_cost_snapshot(FakeCost(), tracer=None, out_dir=self.out)
        self.assertTrue(r["ok"])
        self.assertEqual(r["tasks"], 1)
        self.assertTrue(os.path.isfile(
            os.path.join(r["report_dir"], "cost_snapshot.json")))

    # ---- 3) whitepaper ----
    def test_whitepaper_archives(self):
        ctx = {"security": {"self_scan_hits": 5, "sandbox_backend": "limited",
                            "sandbox_containment": "limited"}}
        r = CA.run_whitepaper(tracer=None, out_dir=self.out, context=ctx)
        self.assertTrue(r["ok"])
        self.assertTrue(os.path.isfile(
            os.path.join(r["report_dir"], "whitepaper_snapshot.md")))

    # ---- 4) closure_report ----
    def test_closure_archives(self):
        context = {
            "tasks": [{"status": "done"}],
            "approvals": [{"status": "approved"}, {"status": "pending"}],
            "traces": [{}, {}],
            "cost": {"totals": {"tasks": 1, "cost_estimate": 0.1,
                                "unpriced_calls": 0}},
        }
        r = CA.run_closure(context, tracer=None, out_dir=self.out, days=30)
        self.assertTrue(r["ok"])
        self.assertEqual(r["completed"], 1)
        self.assertEqual(r["approved"], 1)
        self.assertTrue(os.path.isfile(
            os.path.join(r["report_dir"], "closure_report.md")))

    # ---- 5) execute_action 分发 ----
    def test_execute_action_unknown_reports_error(self):
        r = CA.execute_action("bogus", self._ctx(self.tmp))
        self.assertFalse(r["ok"])
        self.assertIn("未知动作", r["error"])

    def test_execute_action_all_ok(self):
        ctx = self._ctx(
            self.tmp, cost_store=FakeCost(),
            context={"security": {"self_scan_hits": 0, "sandbox_backend": "limited",
                                  "sandbox_containment": "limited"},
                     "tasks": [], "approvals": [], "traces": [],
                     "cost": {"totals": {}}})
        for a in ("self_scan", "cost_snapshot", "whitepaper", "closure_report"):
            r = CA.execute_action(a, ctx, out_dir=self.out)
            self.assertTrue(r["ok"], msg="action %s failed: %s" % (a, r))
            self.assertEqual(r["action"], a)


class TestSchedulerExecutor(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_fire_due_calls_executor(self):
        reg = TaskRegistry(db_path=os.path.join(self.tmp, "reg.db"),
                            runner_id="t")
        s = scheduler.Scheduler(db_path=os.path.join(self.tmp, "sched.db"),
                                registry=reg)
        s.remove_job("j")
        s.add_job("j", interval=1, payload={"action": "cost_snapshot"}, name="c")
        # 强制到期
        with sqlite3.connect(os.path.join(self.tmp, "sched.db")) as c:
            c.execute("UPDATE jobs SET next_run=? WHERE job_id=?",
                      (time.time() - 5, "j"))
        called = []
        results = []

        def executor(job, payload, task_id):
            called.append(payload.get("action"))
            res = {"ok": True, "ran": payload.get("action")}
            results.append(res)
            return res

        fired = s.fire_due(executor=executor)
        self.assertEqual(len(fired), 1)
        self.assertEqual(called, ["cost_snapshot"])
        self.assertEqual(fired[0][0], "j")
        self.assertTrue(fired[0][1]["ok"])
        # 到期任务被推进（last_run 更新）
        self.assertGreater(s.list_jobs()[0]["last_run"], 0)
        reg.close()
        s.close()

    def test_fire_due_executor_failure_does_not_advance(self):
        reg = TaskRegistry(db_path=os.path.join(self.tmp, "reg2.db"),
                            runner_id="t")
        s = scheduler.Scheduler(db_path=os.path.join(self.tmp, "sched2.db"),
                                registry=reg)
        s.remove_job("j2")
        s.add_job("j2", interval=1, payload={}, name="c2")
        # 强制到期
        with sqlite3.connect(os.path.join(self.tmp, "sched2.db")) as c:
            c.execute("UPDATE jobs SET next_run=? WHERE job_id=?",
                      (time.time() - 5, "j2"))

        def boom(job, payload, task_id):
            raise RuntimeError("kaboom")

        fired = s.fire_due(executor=boom)
        self.assertEqual(len(fired), 1)
        self.assertFalse(fired[0][1]["ok"])
        # 失败不推进：last_run 仍保持初始值（未触发）
        self.assertIsNone(s.list_jobs()[0]["last_run"])
        reg.close()
        s.close()


if __name__ == "__main__":
    unittest.main()
