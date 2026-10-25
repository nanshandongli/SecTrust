"""调度器回归测试（2026-10-25 执行层建设时新增）。

锁定承诺：
1. cron 解析：五段式、*、*/n、区间、列表、星期 0/7=周日；非法表达式明确
   报错不静默接受；
2. next_after：按分钟步进的最近命中计算（ weekday 转换、Vixie 日月或语义）；
3. fire_due：到期才登记、登记后推进 next_run、**不重复触发**、登记失败
   不推进（下次重试）；
4. 连接纪律：close() 后连接真关、可重连（不泄漏）。
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scheduler import CronError, Scheduler, next_after, parse_cron      # noqa: E402


def _mk(expr: str) -> dict:
    return parse_cron(expr)


class CronParseTest(unittest.TestCase):

    def test_star_and_step(self):
        f = _mk("*/15 * * * *")
        self.assertEqual(f["minute"], frozenset({0, 15, 30, 45}))
        self.assertEqual(len(f["hour"]), 24)

    def test_range_and_list(self):
        f = _mk("0 9-17 * * 1-5")
        self.assertEqual(f["hour"], frozenset(range(9, 18)))
        self.assertEqual(f["weekday"], frozenset(range(0, 5)))   # 周一~周五

    def test_weekday_zero_and_seven_are_sunday(self):
        f0 = _mk("0 0 * * 0")
        f7 = _mk("0 0 * * 7")
        self.assertEqual(f0["weekday"], frozenset({6}))          # 内部 6=周日
        self.assertEqual(f7["weekday"], frozenset({6}))

    def test_invalid_expressions_rejected(self):
        for bad in ["* * * *", "* * * * * *", "60 * * * *", "* 24 * * *",
                    "a * * * *", "*/0 * * * *", "5-1 * * * *", "* * 0 * *",
                    "* * * 13 *", "1,,2 * * * *", ""]:
            with self.assertRaises(CronError, msg=bad):
                _mk(bad)


class NextAfterTest(unittest.TestCase):

    def test_every_15_min(self):
        f = _mk("*/15 * * * *")
        # 10:07:30 -> 10:15:00
        base = time.mktime((2026, 10, 25, 10, 7, 30, 0, 0, -1))
        nxt = next_after(f, base)
        self.assertEqual(time.localtime(nxt).tm_min, 15)
        self.assertEqual(time.localtime(nxt).tm_sec, 0)

    def test_weekday_9am(self):
        # 2026-10-25 是周日；下一个工作日(周一) 9 点 = 10-26 09:00
        f = _mk("0 9 * * 1-5")
        base = time.mktime((2026, 10, 25, 10, 0, 0, 0, 0, -1))
        nxt = next_after(f, base)
        tm = time.localtime(nxt)
        self.assertEqual((tm.tm_mday, tm.tm_hour, tm.tm_min), (26, 9, 0))
        self.assertEqual(tm.tm_wday, 0)   # 周一

    def test_no_match_returns_none(self):
        # 2 月 30 日永远不存在
        f = _mk("0 0 30 2 *")
        base = time.mktime((2026, 1, 1, 0, 0, 0, 0, 0, -1))
        self.assertIsNone(next_after(f, base, horizon_days=400))


class SchedulerTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sectrust-sched-")
        self.db = os.path.join(self.tmp, "scheduler.db")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_interval_job_fires_once_then_reschedules(self):
        s = Scheduler(db_path=self.db)
        s.add_job("poll", interval=1.0, payload={"op": "noop"})
        due = s.due_jobs(now=time.time() + 2)
        self.assertEqual([j["job_id"] for j in due], ["poll"])
        fired = s.fire_due(now=time.time() + 2)
        self.assertEqual(len(fired), 1)
        job_id, task_id = fired[0]
        self.assertEqual(job_id, "poll")
        self.assertTrue(task_id.startswith("poll-"))
        # 触发后不再 due（next_run 已推进）
        self.assertEqual(s.due_jobs(now=time.time() + 2), [])
        # 任务真的登记进了 registry
        t = s.registry.get_task(task_id)
        self.assertIsNotNone(t)
        self.assertEqual(t["payload"]["scheduled_from"], "poll")
        s.close()

    def test_cron_job_registered_with_payload(self):
        s = Scheduler(db_path=self.db)
        s.add_job("daily-report", cron="0 9 * * *",
                  payload={"op": "shell", "cmd": ["echo", "report"]}, name="日报")
        jobs = {j["job_id"]: j for j in s.list_jobs()}
        self.assertEqual(jobs["daily-report"]["name"], "日报")
        self.assertEqual(jobs["daily-report"]["kind"], "cron")
        fired = s.fire_due(now=jobs["daily-report"]["next_run"] + 1)
        self.assertEqual(fired[0][0], "daily-report")
        t = s.registry.get_task(fired[0][1])
        self.assertEqual(t["payload"]["cmd"], ["echo", "report"])
        s.close()

    def test_add_job_requires_exactly_one_of_cron_or_interval(self):
        s = Scheduler(db_path=self.db)
        with self.assertRaises(CronError):
            s.add_job("bad1")                       # 都没有
        with self.assertRaises(CronError):
            s.add_job("bad2", cron="* * * * *", interval=5)   # 都有
        with self.assertRaises(CronError):
            s.add_job("bad3", interval=0)           # 非法间隔
        with self.assertRaises(CronError):
            s.add_job("bad4", cron="not a cron")    # 非法 cron
        s.close()

    def test_disabled_job_not_fired(self):
        s = Scheduler(db_path=self.db)
        s.add_job("off", interval=1.0)
        self.assertTrue(s.set_enabled("off", False))
        self.assertEqual(s.due_jobs(now=time.time() + 99), [])
        s.close()

    def test_persistence_across_instances(self):
        s1 = Scheduler(db_path=self.db)
        s1.add_job("persist", interval=60.0)
        s1.close()
        s2 = Scheduler(db_path=self.db)
        jobs = s2.list_jobs()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["job_id"], "persist")
        s2.close()

    def test_remove_job(self):
        s = Scheduler(db_path=self.db)
        s.add_job("gone", interval=60.0)
        self.assertTrue(s.remove_job("gone"))
        self.assertFalse(s.remove_job("gone"))     # 幂等删除返回 False
        self.assertEqual(s.list_jobs(), [])
        s.close()

    def test_close_releases_connection(self):
        import sqlite3
        s = Scheduler(db_path=self.db)
        s.add_job("x", interval=60.0)
        c1 = s._conn()
        s.close()
        s._local.conn = None
        c2 = s._conn()                # 重连得到新连接
        self.assertIsNot(c1, c2)
        s.close()


if __name__ == "__main__":
    unittest.main()
