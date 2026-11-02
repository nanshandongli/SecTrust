"""执行器韧性测试：僵尸 running 任务回收 + LLM 调用超时重试。

锁定承诺：
1. TaskRegistry.reset_stuck_running 只回收「陈旧 running」，不动 fresh running
   / completed / registered；
2. ExecutionDaemon._reap_stuck_tasks 把陈旧 running 重置为 registered，但绝不动
   本进程正在处理的任务（_current_claim 非空时跳过）；
3. _execute_llm 在 provider 偶发异常时按 llm_max_retries 重试，重试后成功即返回
   正常产出；持久失败则如实带回 executed_by + retry_attempts 且错误信息标注重试。
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from execution.daemon import ExecutionDaemon                              # noqa: E402
from task_registry import TaskRegistry                                    # noqa: E402
from transport.distributed import ClusterCoordinator                      # noqa: E402


def _disabled_cluster(url="redis://127.0.0.1:1/0"):
    return ClusterCoordinator(url=url, enabled=False)


class _FlakyEngine:
    """模拟 provider：前 fail_times 次调用抛异常（read timeout / 429），之后成功。"""

    def __init__(self, fail_times: int = 0):
        self.fail_times = fail_times
        self.calls = 0

    def execute(self, instruction, ctx=None, model=None):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RuntimeError("read operation timed out")
        return {"answer": "稳定产出", "model": "fake"}


class StuckReaperTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sectrust-reap-")
        self.db = os.path.join(self.tmp, "tasks.db")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.reg = TaskRegistry(db_path=self.db)
        self.addCleanup(self.reg.close)

    def _make_running(self, task_id: str, age_s: float):
        self.reg.register_task(task_id, payload={"op": "llm", "instruction": "x"})
        with self.reg._connect() as conn:
            conn.execute(
                "UPDATE tasks SET status='running', updated_at=? WHERE task_id=?",
                (time.time() - age_s, task_id),
            )

    def test_reaps_only_stale_running(self):
        self._make_running("stale", age_s=1000)          # 陈旧 → 回收
        self._make_running("fresh", age_s=1)             # 刚认领 → 不动
        self.reg.register_task("done", payload={"op": "noop"})
        self.reg.update_task_status("done", "completed")  # 终态 → 不动
        n = self.reg.reset_stuck_running(cutoff_ts=time.time() - 300)
        self.assertEqual(n, 1)
        self.assertEqual(self.reg.get_task("stale")["status"], "registered")
        self.assertEqual(self.reg.get_task("fresh")["status"], "running")
        self.assertEqual(self.reg.get_task("done")["status"], "completed")

    def test_no_stale_returns_zero(self):
        self._make_running("fresh", age_s=1)
        self.assertEqual(self.reg.reset_stuck_running(time.time() - 300), 0)
        self.assertEqual(self.reg.get_task("fresh")["status"], "running")

    def test_daemon_reaper_skips_current_claim(self):
        d = ExecutionDaemon(db_path=self.db, cluster=_disabled_cluster())
        self.addCleanup(d.close)
        self._make_running("stale", age_s=1000)
        d._current_claim = ("some-task", "tok")   # 正在处理 → 不应回收任何
        d._reap_stuck_tasks()
        self.assertEqual(d.reg.get_task("stale")["status"], "running")
        d._current_claim = None
        d._reap_stuck_tasks()                       # 解除后正常回收
        self.assertEqual(d.reg.get_task("stale")["status"], "registered")


class LlmRetryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sectrust-retry-")
        self.db = os.path.join(self.tmp, "tasks.db")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))

    def _daemon(self, engine, max_retries: int = 1):
        d = ExecutionDaemon(
            db_path=self.db,
            cluster=_disabled_cluster(),
            llm_engine_factory=lambda: engine,
            llm_max_retries=max_retries,
        )
        self.addCleanup(d.close)
        return d

    def test_retry_then_success(self):
        eng = _FlakyEngine(fail_times=1)   # 第 1 次超时，第 2 次成功
        d = self._daemon(eng, max_retries=2)
        ok, result, err = d._execute_llm("t1", {"op": "llm", "instruction": "写一篇文案"})
        self.assertTrue(ok, err)
        self.assertIsNone(err)
        self.assertEqual(result["output"], "稳定产出")
        self.assertEqual(eng.calls, 2)     # 确实重试了一次

    def test_persistent_failure_reports_attempts(self):
        eng = _FlakyEngine(fail_times=9)   # 永远失败
        d = self._daemon(eng, max_retries=2)
        ok, result, err = d._execute_llm("t2", {"op": "llm", "instruction": "写一篇文案"})
        self.assertFalse(ok)
        self.assertEqual(eng.calls, 2)     # 最多尝试 2 次
        self.assertIn("已重试 2 次仍失败", err)
        self.assertEqual(result["retry_attempts"], 2)

    def test_no_retry_default_fails_fast(self):
        eng = _FlakyEngine(fail_times=9)
        d = self._daemon(eng, max_retries=1)
        ok, result, err = d._execute_llm("t3", {"op": "llm", "instruction": "写一篇文案"})
        self.assertFalse(ok)
        self.assertEqual(eng.calls, 1)     # 默认不重试
        self.assertNotIn("已重试", err)


if __name__ == "__main__":
    unittest.main()
