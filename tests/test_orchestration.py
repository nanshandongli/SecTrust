"""
编排层 DAG 调度测试（tests/test_orchestration.py）

覆盖：串行链就绪传播、菱形并行/汇合、环/未知依赖/重复拒绝、失败级联阻塞、
并发认领不重、complete 幂等与前置校验、旧库迁移、波次分层、Broker 事件。
"""

import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration import DagScheduler, DagError  # noqa: E402


class DagTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dag_test_")
        self.db = os.path.join(self.tmp, "tasks.db")

    def scheduler(self):
        return DagScheduler(db_path=self.db)


class TestStructure(DagTestBase):
    """建图校验：未知依赖 / 成环 / 自依赖 / 重复"""

    def test_serial_chain(self):
        s = self.scheduler()
        s.add_task("a", payload={"n": 1})
        s.add_task("b", depends_on=["a"])
        s.add_task("c", depends_on=["b"])
        self.assertEqual([t["task_id"] for t in s.ready_tasks()], ["a"])
        self.assertEqual(s.waiting_on("c"), ["b"])
        s.close()

    def test_diamond(self):
        s = self.scheduler()
        s.add_task("a")
        s.add_task("b", depends_on=["a"])
        s.add_task("c", depends_on=["a"])
        s.add_task("d", depends_on=["b", "c"])
        self.assertEqual(s.waves(), [["a"], ["b", "c"], ["d"]])
        s.close()

    def test_unknown_dep_rejected(self):
        s = self.scheduler()
        with self.assertRaises(DagError) as ctx:
            s.add_task("b", depends_on=["ghost"])
        self.assertIn("未知依赖", str(ctx.exception))
        s.close()

    def test_self_dep_rejected(self):
        s = self.scheduler()
        with self.assertRaises(DagError):
            s.add_task("a", depends_on=["a"])
        s.close()

    def test_cycle_rejected(self):
        """正常建图不可能成环（新任务尚无下游 + 重复添加被拒），环检测是防御性兜底。
        这里用"登记处直接写入悬空依赖"构造一处不一致的底图，验证 add_task 的
        环检测确实会拦截"闭合回路"的尝试。"""
        s = self.scheduler()
        s.registry.register_task("p", depends_on=["t"])  # p 引用了尚未存在的 t
        with self.assertRaises(DagError) as ctx:
            s.add_task("t", depends_on=["p"])            # 若允许则成环 t->p->t
        self.assertIn("成环", str(ctx.exception))
        s.close()

    def test_normal_building_cannot_form_cycle(self):
        """不变量验证：只经 add_task 建图时，重复与未知依赖被拒，环无从产生。"""
        s = self.scheduler()
        s.add_task("a")
        s.add_task("b", depends_on=["a"])
        with self.assertRaises(DagError):
            s.add_task("a", depends_on=["b"])  # 重复即被拒，来不及成环
        s.close()

    def test_duplicate_rejected(self):
        s = self.scheduler()
        s.add_task("a")
        with self.assertRaises(DagError):
            s.add_task("a")
        s.close()


class TestReadinessAndClaim(DagTestBase):
    """就绪传播与认领"""

    def test_ready_propagates_through_chain(self):
        s = self.scheduler()
        s.add_task("a"); s.add_task("b", depends_on=["a"]); s.add_task("c", depends_on=["b"])
        self.assertEqual([t["task_id"] for t in s.ready_tasks()], ["a"])
        self.assertTrue(s.complete("a"))
        self.assertEqual([t["task_id"] for t in s.ready_tasks()], ["b"])
        self.assertTrue(s.complete("b"))
        self.assertEqual([t["task_id"] for t in s.ready_tasks()], ["c"])
        self.assertTrue(s.complete("c"))
        self.assertEqual(s.ready_tasks(), [])
        s.close()

    def test_claim_fifo_and_turn_lock(self):
        s = self.scheduler()
        s.add_task("a"); s.add_task("b")  # 两个无依赖任务，a 先登记
        t = s.claim_ready(runner_id="w1")
        self.assertEqual(t["task_id"], "a")
        self.assertEqual(t["status"], "running")
        # a 正在 running，不可再领；下一个就绪是 b
        self.assertIsNone(s.claim_ready(task_id="a"))
        t2 = s.claim_ready(runner_id="w2")
        self.assertEqual(t2["task_id"], "b")
        self.assertIsNone(s.claim_ready())  # 就绪集空了
        s.close()

    def test_claim_specific_not_ready_task(self):
        s = self.scheduler()
        s.add_task("a"); s.add_task("b", depends_on=["a"])
        self.assertIsNone(s.claim_ready(task_id="b"))  # 依赖未满足
        s.close()

    def test_complete_requires_deps_done(self):
        s = self.scheduler()
        s.add_task("a"); s.add_task("b", depends_on=["a"])
        # b 未认领、上游未完成 -> 不允许完成（保证图一致性）
        self.assertFalse(s.complete("b"))
        self.assertTrue(s.complete("a"))
        self.assertTrue(s.complete("b"))
        s.close()

    def test_complete_idempotent(self):
        s = self.scheduler()
        s.add_task("a")
        self.assertTrue(s.complete("a", result={"ok": 1}))
        self.assertFalse(s.complete("a"))  # 第二次 False
        t = s.registry.get_task("a")
        self.assertEqual(t["payload"]["result"], {"ok": 1})
        self.assertEqual(t["status"], "completed")
        s.close()

    def test_complete_unregistered_task(self):
        s = self.scheduler()
        self.assertFalse(s.complete("ghost"))
        s.close()


class TestFailCascade(DagTestBase):
    """失败级联阻塞"""

    def test_fail_blocks_downstream(self):
        s = self.scheduler()
        s.add_task("a")
        s.add_task("b", depends_on=["a"])
        s.add_task("c", depends_on=["b"])
        s.add_task("x")  # 独立任务不应被波及
        s.claim_ready(runner_id="w1")  # 领 a（或 x，按 FIFO 是 a）
        blocked = s.fail("a", error="boom")
        self.assertEqual(sorted(blocked), ["b", "c"])
        st = {t["task_id"]: t["status"] for t in s.registry.list_tasks()}
        self.assertEqual(st["a"], "failed")
        self.assertEqual(st["b"], "blocked")
        self.assertEqual(st["c"], "blocked")
        self.assertEqual(st["x"], "registered")  # 独立任务不受影响
        self.assertIsNone(s.claim_ready(task_id="b"))  # blocked 永不可领
        s.close()

    def test_fail_requires_running(self):
        s = self.scheduler()
        s.add_task("a")
        self.assertEqual(s.fail("a"), [])  # 未认领不能失败
        s.close()

    def test_fail_partial_diamond(self):
        s = self.scheduler()
        s.add_task("a")
        s.add_task("b", depends_on=["a"])
        s.add_task("c", depends_on=["a"])
        s.add_task("d", depends_on=["b", "c"])
        s.claim_ready(runner_id="w1")     # a
        s.complete("a")
        s.claim_ready(runner_id="w2")     # b
        s.fail("b", error="x")
        st = {t["task_id"]: t["status"] for t in s.registry.list_tasks()}
        self.assertEqual(st["c"], "registered")  # c 与 b 并行，不受影响
        self.assertEqual(st["d"], "blocked")     # d 有一个上游失败 -> 阻塞
        s.close()


class TestConcurrency(DagTestBase):
    """并发认领：多线程抢单不重不漏"""

    def test_concurrent_claim_no_duplicate(self):
        s = self.scheduler()
        n = 12
        for i in range(n):
            s.add_task(f"t{i}")
        claimed = []
        lock = threading.Lock()

        def worker(wid):
            got = []
            while True:
                t = s.claim_ready(runner_id=f"w{wid}")
                if t is None:
                    break
                got.append(t["task_id"])
            with lock:
                claimed.extend(got)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=30)
        self.assertEqual(len(claimed), n)                    # 不漏
        self.assertEqual(len(set(claimed)), n)               # 不重
        s.close()

    def test_concurrent_complete_no_double_unblock(self):
        """并发 complete 同一任务：只有一个返回 True，下游只就绪一次。"""
        s = self.scheduler()
        s.add_task("a"); s.add_task("b", depends_on=["a"])
        s.claim_ready(runner_id="w1")
        results = []

        def do_complete():
            results.append(s.complete("a"))

        threads = [threading.Thread(target=do_complete) for _ in range(4)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=30)
        self.assertEqual(results.count(True), 1)
        self.assertEqual([t["task_id"] for t in s.ready_tasks()], ["b"])
        s.close()


class TestCompatAndObservability(DagTestBase):
    """旧库迁移 / 波次 / 事件"""

    def test_old_db_migration(self):
        # 手工建"旧版"tasks 表（无 depends_on 列），DagScheduler 打开后应自动迁移
        conn = sqlite3.connect(self.db)
        conn.execute(
            "CREATE TABLE tasks (task_id TEXT PRIMARY KEY, runner_id TEXT NOT NULL,"
            " status TEXT NOT NULL DEFAULT 'registered', created_at REAL NOT NULL,"
            " updated_at REAL NOT NULL, payload TEXT)")
        conn.execute(
            "INSERT INTO tasks VALUES('old-1','r','registered',0,0,'{\"k\":1}')")
        conn.commit(); conn.close()

        s = self.scheduler()
        cols = {r[1] for r in sqlite3.connect(self.db).execute(
            "PRAGMA table_info(tasks)").fetchall()}
        self.assertIn("depends_on", cols)
        # 旧任务可读、无依赖、可完成
        t = s.registry.get_task("old-1")
        self.assertEqual(t["depends_on"], [])
        self.assertTrue(s.complete("old-1"))
        # 新任务可挂到旧任务下游
        s.add_task("new-1", depends_on=["old-1"])
        self.assertEqual([x["task_id"] for x in s.ready_tasks()], ["new-1"])
        s.close()

    def test_waves_layering(self):
        s = self.scheduler()
        # r1,r2 无依赖; m1<-r1; m2<-r1,r2; j<-m1,m2
        s.add_task("r1"); s.add_task("r2")
        s.add_task("m1", depends_on=["r1"])
        s.add_task("m2", depends_on=["r1", "r2"])
        s.add_task("j", depends_on=["m1", "m2"])
        self.assertEqual(s.waves(), [["r1", "r2"], ["m1", "m2"], ["j"]])
        s.close()

    def test_stats(self):
        s = self.scheduler()
        s.add_task("a"); s.add_task("b", depends_on=["a"])
        s.claim_ready(runner_id="w1")
        st = s.stats()
        self.assertEqual(st["total"], 2)
        self.assertEqual(st["by_status"]["running"], 1)
        self.assertEqual(st["by_status"]["registered"], 1)
        self.assertEqual(st["ready"], 0)  # b 还在等 a
        s.close()

    def test_broker_event_on_complete(self):
        if "MQ_DB_PATH" in os.environ:
            del os.environ["MQ_DB_PATH"]  # 先确保关掉，再按需打开
        q = os.path.join(self.tmp, "mq.db")
        os.environ["MQ_DB_PATH"] = q
        try:
            s = self.scheduler()
            received = []
            worker = s.broker.spawn_worker("task", lambda ev: received.append(ev),
                                           poll_interval=0.02)
            s.add_task("a")
            s.claim_ready(runner_id="w1")
            s.complete("a", result={"v": 1})
            time.sleep(0.3)
            s.broker.stop_worker(worker)
            self.assertTrue(any(e.get("event_type") == "completed" and
                                e.get("data", {}).get("task_id") == "a"
                                for e in received), f"未收到完成事件: {received}")
            s.close()
        finally:
            os.environ.pop("MQ_DB_PATH", None)


if __name__ == "__main__":
    unittest.main()
