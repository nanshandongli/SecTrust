"""跨机协同原语测试（transport/distributed.py）—— 全程假 Redis 服务器，无需真实服务。

覆盖：
- 分布式锁：SET NX 原子抢占 / 持 token 续期释放 / 假 token 拒动 /
  锁过期后被别人抢走的经典竞态（旧 token 不能续也不能删别人的锁）
- WATCH/MULTI/EXEC 乐观事务：WATCH 后键被**写**改动 → 事务作废；
  只被**读**不改动 → 事务照常（读者不该杀死续期）
- 心跳：TTL 过期判死 + 注册集合自清理 / 主动下线 / runner_info
- 死信：阈值才入队 / 恢复（含重复入队一次清干净）/ 丢弃 / 读侧去重 / O(1) contains
- 开关纪律（不可破坏）：未启用 = 明确未启用语义（False/None/[]）；
  强开（SECTRUST_REDIS=1）但不可达 = 构造即抛 RuntimeError，绝不静默降级
- ClusterCoordinator：from_env 接线 / 线程隔离连接 / close 全关 /
  两个 coordinator 抢同一任务（"2–3 台机器"的最小拓扑）
"""

import os
import socket
import sys
import threading
import time
import unittest
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from transport.resp import RESPClient  # noqa: E402
from transport.distributed import (  # noqa: E402
    ClusterCoordinator, DeadLetterQueue, DistributedLock, HeartbeatRegistry)
from tests.test_redis_queue import FakeRedisServer  # noqa: E402


class FakeRedisCase(unittest.TestCase):
    """每个用例一台线程内假 Redis；expire_now 用来确定性模拟"键已过期"。"""

    def setUp(self):
        self.srv = FakeRedisServer().start()
        self.url = f"redis://127.0.0.1:{self.srv.port}/0"
        self.addCleanup(self.srv.stop)

    def client(self) -> RESPClient:
        c = RESPClient(self.url).connect()
        self.addCleanup(c.close)
        return c

    def expire_now(self, key: str):
        """把键到期时间改到过去（不 sleep）：下一次命令触发惰性清扫即过期。"""
        self.srv.expires[key] = time.time() - 1


class TestDistributedLock(FakeRedisCase):
    def setUp(self):
        super().setUp()
        self.c = self.client()
        self.lock = DistributedLock(self.c, key_prefix="t:lock:")

    def test_acquire_returns_token_starting_with_owner(self):
        token = self.lock.acquire("job", "machineA")
        self.assertTrue(token.startswith("machineA:"))
        self.assertEqual(self.lock.holder("job"), token)

    def test_second_acquire_returns_none(self):
        # 两个"机器"各一个连接抢同一把锁：SET NX 原子，只有一个成功
        c2 = self.client()
        lock2 = DistributedLock(c2, key_prefix="t:lock:")
        self.assertIsNotNone(self.lock.acquire("job", "machineA"))
        self.assertIsNone(lock2.acquire("job", "machineB"))
        self.assertTrue(self.lock.holder("job").startswith("machineA:"))

    def test_renew_extends_ttl(self):
        self.lock.acquire("job", "machineA", ttl=5)
        self.assertEqual(self.c.ttl("t:lock:job"), 5)
        self.assertTrue(self.lock.renew("job", self.lock.holder("job"), ttl=50))
        self.assertEqual(self.c.ttl("t:lock:job"), 50)

    def test_renew_with_wrong_token_fails_and_keeps_lock(self):
        token = self.lock.acquire("job", "machineA", ttl=5)
        self.assertFalse(self.lock.renew("job", "machineB:forged", ttl=50))
        self.assertEqual(self.lock.holder("job"), token)
        self.assertEqual(self.c.ttl("t:lock:job"), 5)   # 假 token 没能续期

    def test_release_with_token_then_again_fails(self):
        token = self.lock.acquire("job", "machineA")
        self.assertTrue(self.lock.release("job", token))
        self.assertIsNone(self.lock.holder("job"))
        self.assertFalse(self.lock.release("job", token))   # 锁已不在

    def test_release_with_wrong_token_keeps_lock(self):
        token = self.lock.acquire("job", "machineA")
        self.assertFalse(self.lock.release("job", "machineB:forged"))
        self.assertEqual(self.lock.holder("job"), token)    # 不能删别人的锁

    def test_expired_lock_stolen_old_token_cannot_renew_or_release(self):
        """经典竞态：A 的锁过期 → B 抢到 → A 的旧 token 必须全部失效，
        且任何操作都不能动到 B 的锁（不能误续、不能误删）。"""
        old = self.lock.acquire("job", "machineA", ttl=30)
        self.expire_now("t:lock:job")                       # 模拟 30 秒过去
        c2 = self.client()
        lock2 = DistributedLock(c2, key_prefix="t:lock:")
        new = lock2.acquire("job", "machineB", ttl=30)
        self.assertIsNotNone(new)
        self.assertNotEqual(old, new)
        # A 拿着旧 token 续期/释放：双拒，B 的锁原封不动
        self.assertFalse(self.lock.renew("job", old, ttl=99))
        self.assertFalse(self.lock.release("job", old))
        self.assertEqual(lock2.holder("job"), new)
        self.assertEqual(c2.ttl("t:lock:job"), 30)
        # B 自己的 token 仍然好使
        self.assertTrue(lock2.release("job", new))

    def test_default_ttl_used_when_none(self):
        lock = DistributedLock(self.c, key_prefix="t:lock:", default_ttl=7)
        lock.acquire("job", "machineA")                     # 不传 ttl
        self.assertEqual(self.c.ttl("t:lock:job"), 7)


class TestOptimisticTransaction(FakeRedisCase):
    """WATCH/MULTI/EXEC 语义：只认"写"造成的版本变化。"""

    def setUp(self):
        super().setUp()
        self.c1 = self.client()
        self.c2 = self.client()

    def test_write_between_watch_and_exec_aborts(self):
        key = "t:lock:race"
        self.c1.execute("SET", key, "tok1")
        self.c1.watch(key)
        self.assertEqual(self.c1.get(key), "tok1")
        self.c2.execute("SET", key, "tok2")                 # 他人改动 → 版本 +1
        self.c1.multi()
        self.c1.expire(key, 30)
        self.assertIsNone(self.c1.exec())                   # 事务作废
        self.assertEqual(self.c1.get(key), "tok2")          # 值未被覆盖

    def test_read_between_watch_and_exec_does_not_abort(self):
        key = "t:lock:race"
        self.c1.execute("SET", key, "tok1")
        self.c1.watch(key)
        self.assertEqual(self.c1.get(key), "tok1")
        self.assertEqual(self.c2.get(key), "tok1")          # 只读不改版本
        self.c1.multi()
        self.c1.expire(key, 30)
        replies = self.c1.exec()                            # 照常执行
        self.assertIsInstance(replies, list)
        self.assertEqual(self.c1.ttl(key), 30)


class TestHeartbeatRegistry(FakeRedisCase):
    def setUp(self):
        super().setUp()
        self.c = self.client()
        self.hb = HeartbeatRegistry(self.c, key_prefix="t:hb:")

    def test_beat_then_alive_with_info(self):
        self.hb.beat("r1", {"bot": "bot-1", "host": "pc-a"})
        self.assertTrue(self.hb.is_alive("r1"))
        self.assertEqual(self.hb.alive_runners(), ["r1"])
        info = self.hb.runner_info("r1")
        self.assertEqual(info["runner_id"], "r1")
        self.assertEqual(info["meta"]["bot"], "bot-1")
        self.assertIn("at", info)

    def test_expired_runner_is_dead_and_registry_cleaned(self):
        self.hb.beat("r1", ttl=1)
        self.assertTrue(self.hb.is_alive("r1"))
        self.expire_now("t:hb:r1")                          # TTL 到，不 sleep
        self.assertFalse(self.hb.is_alive("r1"))
        # alive_runners 顺手把过期成员清出注册集合
        self.assertEqual(self.hb.alive_runners(), [])
        self.assertEqual(self.c.smembers("t:hb:registry"), [])

    def test_drop_removes_heartbeat_and_membership(self):
        self.hb.beat("r1")
        self.hb.drop("r1")
        self.assertFalse(self.hb.is_alive("r1"))
        self.assertEqual(self.c.smembers("t:hb:registry"), [])

    def test_info_missing_returns_none(self):
        self.assertIsNone(self.hb.runner_info("ghost"))

    def test_alive_runners_sorted_multiple(self):
        self.hb.beat("r2")
        self.hb.beat("r1")
        self.assertEqual(self.hb.alive_runners(), ["r1", "r2"])


class TestDeadLetterQueue(FakeRedisCase):
    def setUp(self):
        super().setUp()
        self.c = self.client()
        self.dlq = DeadLetterQueue(self.c, key_prefix="t:dlq:", threshold=3)

    def test_below_threshold_only_counts(self):
        self.assertFalse(self.dlq.note_failure("t1", "boom"))
        self.assertFalse(self.dlq.note_failure("t1", "boom"))
        self.assertEqual(self.dlq.failures("t1"), 2)
        self.assertEqual(self.dlq.count(), 0)
        self.assertEqual(self.dlq.list(), [])

    def test_threshold_enters_dlq(self):
        self.dlq.note_failure("t1", "boom-1")
        self.dlq.note_failure("t1", "boom-2")
        self.assertTrue(self.dlq.note_failure("t1", "boom-3", {"url": "x"}))
        entries = self.dlq.list()
        self.assertEqual(len(entries), 1)
        e = entries[0]
        self.assertEqual(e["task_id"], "t1")
        self.assertEqual(e["reason"], "boom-3")
        self.assertEqual(e["attempts"], 3)
        self.assertEqual(e["payload"], {"url": "x"})
        self.assertIn("at", e)
        self.assertEqual(self.dlq.failures("t1"), 0)        # 入队后计数清零
        self.assertTrue(self.dlq.contains("t1"))

    def test_recover_returns_entry_and_removes(self):
        self._enter("t1")
        entry = self.dlq.recover("t1")
        self.assertEqual(entry["task_id"], "t1")
        self.assertEqual(self.dlq.count(), 0)
        self.assertFalse(self.dlq.contains("t1"))
        self.assertIsNone(self.dlq.recover("t1"))           # 不在死信中

    def test_drop_true_then_false(self):
        self._enter("t1")
        self.assertTrue(self.dlq.drop("t1"))
        self.assertEqual(self.dlq.count(), 0)
        self.assertFalse(self.dlq.drop("t1"))

    def test_duplicate_entries_dedup_on_read_and_recover_all(self):
        self._enter("t1")
        self._enter("t1")                                   # 同一任务再次攒满阈值
        self.assertEqual(self.dlq.count(), 2)               # 底层两条原始记录
        self.assertEqual(len(self.dlq.list()), 1)           # 读侧按 task_id 去重
        entry = self.dlq.recover("t1")                      # 恢复一次全清
        self.assertEqual(entry["task_id"], "t1")
        self.assertEqual(self.dlq.count(), 0)
        self.assertEqual(self.dlq.list(), [])

    def test_threshold_one_enters_immediately(self):
        dlq = DeadLetterQueue(self.c, key_prefix="t:dlq:", threshold=1)
        self.assertTrue(dlq.note_failure("t1", "once"))
        self.assertEqual(dlq.count(), 1)

    def test_limit_truncates(self):
        self._enter("t1")
        self._enter("t2")
        self.assertEqual(len(self.dlq.list(limit=1)), 1)
        self.assertEqual(len(self.dlq.list(limit=10)), 2)

    def _enter(self, task_id: str):
        for _ in range(self.dlq.threshold):
            self.dlq.note_failure(task_id, "boom")


class TestClusterCoordinatorSwitch(FakeRedisCase):
    """开关纪律：未启用=明确语义；强开不可达=构造即抛（不静默降级）。"""

    def test_disabled_returns_unset_semantics(self):
        with mock.patch.dict(os.environ, {"SECTRUST_REDIS": "",
                                          "REDIS_URL": self.url}):
            cluster = ClusterCoordinator.from_env()
        self.assertFalse(cluster.enabled)
        self.assertIsNone(cluster.lock)
        self.assertIsNone(cluster.heartbeat)
        self.assertIsNone(cluster.dead_letters)
        self.assertIsNone(cluster.acquire("n", "o"))
        self.assertFalse(cluster.renew("n", "t"))
        self.assertFalse(cluster.release("n", "t"))
        self.assertIsNone(cluster.holder("n"))
        self.assertIsNone(cluster.claim_task("t1", "o"))
        self.assertFalse(cluster.release_task("t1", "t"))
        self.assertFalse(cluster.beat("r1"))
        self.assertEqual(cluster.alive_runners(), [])
        self.assertFalse(cluster.is_alive("r1"))
        self.assertFalse(cluster.note_failure("t1", "boom"))
        self.assertEqual(cluster.dead_letter_list(), [])
        self.assertIsNone(cluster.recover_dead_letter("t1"))
        self.assertEqual(cluster._all_clients, [])          # 一次连接都没开

    def test_url_without_switch_stays_disabled_and_never_connects(self):
        # REDIS_URL 指向一个必定不通的地址：未开开关时**不能**因为探活而报错
        dead_url = "redis://127.0.0.1:1/0"
        with mock.patch.dict(os.environ, {"SECTRUST_REDIS": "",
                                          "REDIS_URL": dead_url}):
            cluster = ClusterCoordinator.from_env()         # 不抛即过
        self.assertFalse(cluster.enabled)
        self.assertEqual(cluster._all_clients, [])

    def test_forced_on_unreachable_raises(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()                                           # 该端口随即无人监听
        dead_url = f"redis://127.0.0.1:{port}/0"
        with mock.patch.dict(os.environ, {"SECTRUST_REDIS": "1",
                                          "REDIS_URL": dead_url}):
            with self.assertRaises(RuntimeError) as cm:
                ClusterCoordinator.from_env()
        self.assertIn("不可达", str(cm.exception))           # 报错说人话
        self.assertIn("静默降级", str(cm.exception))         # 且点明拒绝降级


class TestClusterCoordinatorOnFake(FakeRedisCase):
    def test_explicit_enabled_true_no_env(self):
        cluster = ClusterCoordinator(url=self.url, enabled=True)
        self.addCleanup(cluster.close)
        self.assertTrue(cluster.enabled)
        token = cluster.acquire("n", "machineA")
        self.assertIsNotNone(token)
        self.assertTrue(cluster.release("n", token))

    def test_from_env_end_to_end(self):
        with mock.patch.dict(os.environ, {"SECTRUST_REDIS": "1",
                                          "REDIS_URL": self.url}):
            cluster = ClusterCoordinator.from_env()
        self.addCleanup(cluster.close)
        self.assertTrue(cluster.enabled)
        # 锁
        token = cluster.claim_task("t1", "machineA")
        self.assertTrue(cluster.holder("claim:t1").startswith("machineA:"))
        # 心跳
        self.assertTrue(cluster.beat("machineA", {"role": "worker"}))
        self.assertEqual(cluster.alive_runners(), ["machineA"])
        # 死信：三次失败后进 DLQ，可人工恢复
        self.assertFalse(cluster.note_failure("t1", "e1"))
        self.assertFalse(cluster.note_failure("t1", "e2"))
        self.assertTrue(cluster.note_failure("t1", "e3"))
        self.assertEqual(len(cluster.dead_letter_list()), 1)
        entry = cluster.recover_dead_letter("t1")
        self.assertEqual(entry["task_id"], "t1")
        self.assertEqual(cluster.dead_letter_list(), [])
        # 收尾释放任务锁
        self.assertTrue(cluster.release_task("t1", token))

    def test_two_machines_claim_same_task(self):
        """两台机器（两个 coordinator）抢同一任务：只有一台拿到；
        第一台释放后第二台才能拿到——跨机认领的最小拓扑。"""
        with mock.patch.dict(os.environ, {"SECTRUST_REDIS": "1",
                                          "REDIS_URL": self.url}):
            a = ClusterCoordinator.from_env()
            b = ClusterCoordinator.from_env()
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        token_a = a.claim_task("t1", "machineA")
        self.assertIsNotNone(token_a)
        self.assertIsNone(b.claim_task("t1", "machineB"))    # B 抢不到
        self.assertTrue(a.beat("machineA"))
        self.assertTrue(b.beat("machineB"))
        self.assertEqual(a.alive_runners(), ["machineA", "machineB"])
        self.assertTrue(a.release_task("t1", token_a))
        token_b = b.claim_task("t1", "machineB")             # A 释放后 B 拿到
        self.assertIsNotNone(token_b)
        self.assertTrue(b.release_task("t1", token_b))

    def test_thread_local_clients_and_close(self):
        cluster = ClusterCoordinator(url=self.url, enabled=True)
        self.addCleanup(cluster.close)
        main_c = cluster.client
        got = {}

        def worker():
            got["c"] = cluster.client

        t = threading.Thread(target=worker)
        t.start()
        t.join()
        self.assertIsNot(got["c"], main_c)                   # 每线程独立连接
        self.assertEqual(len(cluster._all_clients), 2)
        cluster.close()
        self.assertEqual(cluster._all_clients, [])
        # 白盒断言：close 是真关 socket（_sock 置 None），不是只摘引用
        self.assertIsNone(main_c._sock)
        self.assertIsNone(got["c"]._sock)


if __name__ == "__main__":
    unittest.main()
