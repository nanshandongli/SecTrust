"""
跨进程任务登记处 —— 单元测试（回归保障）

覆盖需求方列出的 5 条最小验收，外加心跳/runner_id 的边界检查。
零第三方依赖：用 unittest 编写，pytest 也能直接收集；
第 4 条用 multiprocessing 真正拉起多个独立进程验证「跨进程并发不报错」。

运行：
    python -m unittest discover -s tests -v
    python tests/test_registry.py
    pytest tests/
"""

import multiprocessing as mp
import os
import tempfile
import unittest

from task_registry import TaskRegistry, make_runner_id


# ---- 跨进程 worker：在独立进程中各自持有独立连接 ----
def _writer(db_path, base, n, err_q):
    try:
        reg = TaskRegistry(db_path=db_path, runner_id=f"w{base}")
        for i in range(n):
            tid = f"{base}-{i}"
            reg.register_task(tid, payload={"i": i})
            if i % 3 == 0:
                reg.update_task_status(tid, "running")
            if i % 5 == 0:
                reg.update_task_status(tid, "done")
        reg.close()
    except Exception as e:  # noqa: BLE001
        err_q.put(f"w{base}: {type(e).__name__}: {e}")


def _reader(db_path, iters, err_q):
    try:
        reg = TaskRegistry(db_path=db_path, runner_id="reader")
        for _ in range(iters):
            reg.list_tasks(limit=50)
        reg.close()
    except Exception as e:  # noqa: BLE001
        err_q.put(f"reader: {type(e).__name__}: {e}")


class RegistryTestBase(unittest.TestCase):
    def setUp(self):
        # 用 Windows 绝对路径（tempfile），避开 Git Bash /tmp 与 Python 解析不一致
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "reg.db")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestAcceptance(RegistryTestBase):
    # 1. runner 收到 mock 消息后，登记处出现对应任务记录
    def test_1_register_appears(self):
        runner = TaskRegistry(db_path=self.db, runner_id="runner-A")
        runner.register_task("m1", payload={"x": 1})
        runner.register_task("m2", payload={"x": 2})
        runner.close()

        center = TaskRegistry(db_path=self.db, runner_id="center")
        rows = center.list_tasks()
        center.close()

        self.assertEqual({r["task_id"] for r in rows}, {"m1", "m2"})
        self.assertTrue(all(r["payload"] is not None for r in rows))

    # 2. task_center 能查到该任务（含 runner_id 过滤）
    def test_2_center_lists(self):
        runner = TaskRegistry(db_path=self.db, runner_id="runner-A")
        runner.register_task("m1")
        runner.register_task("m2")
        runner.close()

        center = TaskRegistry(db_path=self.db, runner_id="center")
        found = center.list_tasks(runner_id="runner-A")
        center.close()

        self.assertEqual(len(found), 2)

    # 3. 任务状态变更后，task_center 能看到最新状态
    def test_3_status_change_visible(self):
        runner = TaskRegistry(db_path=self.db, runner_id="runner-A")
        runner.register_task("m1")
        runner.update_task_status("m1", "done")
        runner.close()

        center = TaskRegistry(db_path=self.db, runner_id="center")
        m1 = next(r for r in center.list_tasks() if r["task_id"] == "m1")
        center.close()

        self.assertEqual(m1["status"], "done")

    # 4. 两个进程同时读写不报错（真·跨进程）
    def test_4_concurrent_no_error(self):
        err_q = mp.Queue()
        ctx = mp.get_context("spawn")
        procs = [
            ctx.Process(target=_writer, args=(self.db, b, 50, err_q))
            for b in range(4)
        ]
        procs.append(ctx.Process(target=_reader, args=(self.db, 200, err_q)))
        for p in procs:
            p.start()
        for p in procs:
            p.join()

        errs = []
        while not err_q.empty():
            errs.append(err_q.get())

        center = TaskRegistry(db_path=self.db, runner_id="center")
        writer_count = sum(
            1 for r in center.list_tasks(limit=10000)
            if r["runner_id"].startswith("w")
        )
        center.close()

        self.assertEqual(errs, [])
        self.assertEqual(writer_count, 200)

    # 5. 删除登记处文件后，runner 能重建表
    def test_5_rebuild_after_delete(self):
        for f in (self.db, self.db + "-wal", self.db + "-shm"):
            try:
                os.remove(f)
            except FileNotFoundError:
                pass

        runner = TaskRegistry(db_path=self.db, runner_id="rebuild")
        runner.register_task("r1", payload={"note": "after delete"})
        runner.close()

        center = TaskRegistry(db_path=self.db, runner_id="center")
        rows = center.list_tasks()
        center.close()

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["task_id"], "r1")


class TestHeartbeat(RegistryTestBase):
    def test_heartbeat_alive(self):
        reg = TaskRegistry(db_path=self.db, runner_id="hb")
        reg.beat()
        rows = reg.list_runners(stale_after=90)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "alive")
        reg.close()

    def test_stale_detection(self):
        import time
        reg = TaskRegistry(db_path=self.db, runner_id="hb")
        # 直接写一条很旧的心跳，制造 stale
        now = time.time()
        with reg._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO runners VALUES (?, ?, 'alive')",
                ("hb", now - 120),
            )
        rows = reg.list_runners(stale_after=90)
        self.assertEqual(rows[0]["status"], "stale")
        reg.close()

    def test_runner_id_format(self):
        rid = make_runner_id()
        self.assertIn("-", rid)  # hostname-pid


class TestHardening(RegistryTestBase):
    # 备份：sqlite3.backup 自动合并主库 + 未 checkpoint 的 WAL，单文件即完整
    def test_backup_includes_wal(self):
        runner = TaskRegistry(db_path=self.db, runner_id="r")
        for i in range(10):
            runner.register_task(f"t{i}", payload={"i": i})
        runner.close()

        dest = os.path.join(self.tmp, "backup.db")
        TaskRegistry(db_path=self.db).backup(dest)

        # 直接打开备份文件（不依赖原 -wal），应能看到全部 10 条
        bk = TaskRegistry(db_path=dest, runner_id="bk")
        self.assertEqual(len(bk.list_tasks()), 10)
        bk.close()

    # CAS 认领：谁先抢到谁赢，靠影响行数判定，无需分布式锁
    def test_claim_cas(self):
        runner = TaskRegistry(db_path=self.db, runner_id="r")
        runner.register_task("job1")
        runner.close()

        w1 = TaskRegistry(db_path=self.db, runner_id="worker-1")
        w2 = TaskRegistry(db_path=self.db, runner_id="worker-2")
        self.assertTrue(w1.claim_task("job1", runner_id="worker-1"))
        self.assertFalse(w2.claim_task("job1", runner_id="worker-2"))  # 已被认领
        w1.close()
        w2.close()

        center = TaskRegistry(db_path=self.db, runner_id="center")
        job = next(t for t in center.list_tasks() if t["task_id"] == "job1")
        center.close()
        self.assertEqual(job["status"], "running")
        self.assertEqual(job["runner_id"], "worker-1")

    # 长读事务监控：checkpoint 返回 (total, ckpt, status) 三元组
    def test_checkpoint_tuple(self):
        reg = TaskRegistry(db_path=self.db, runner_id="r")
        reg.register_task("x")
        res = reg.checkpoint("PASSIVE")
        reg.close()
        self.assertEqual(len(res), 3)

    # synchronous 切换不应破坏契约（NORMAL 在 WAL 下安全）
    def test_synchronous_normal(self):
        reg = TaskRegistry(db_path=self.db, synchronous="NORMAL")
        reg.register_task("y")
        self.assertEqual(len(reg.list_tasks()), 1)
        reg.close()

    def test_synchronous_invalid(self):
        with self.assertRaises(ValueError):
            TaskRegistry(db_path=self.db, synchronous="WEIRD")


if __name__ == "__main__":
    unittest.main(verbosity=2)
