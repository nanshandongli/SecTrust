"""ExecutionDaemon 跨机接线测试（execution/daemon.py × transport/distributed.py）。

全程假 Redis + 临时 SQLite，无需真实服务。锁定的承诺：
1. 认领守卫：跨机锁被别的机器持有 → 任务放回 registered（status=requeued），
   不占用本机；锁释放后本机能正常拿到并执行；
2. 正常流程：执行前后锁的获取与释放都发生（finally 保证，失败也放锁）；
3. 失败记账：执行失败进死信计数，反复失败达阈值才真正入队；
4. 心跳续期：处理中的任务锁随 _cluster_beat 续期；锁已易主只告警不中断；
5. 优雅下线：close() 先摘心跳再关连接；
6. 未启用时行为与从前完全一致（不开任何连接）。
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from execution.daemon import ExecutionDaemon                              # noqa: E402
from transport.distributed import ClusterCoordinator                      # noqa: E402
from transport.resp import RESPClient                                     # noqa: E402
from tests.test_redis_queue import FakeRedisServer                        # noqa: E402


class DaemonClusterTest(unittest.TestCase):
    def setUp(self):
        self.srv = FakeRedisServer().start()
        self.url = f"redis://127.0.0.1:{self.srv.port}/0"
        self.addCleanup(self.srv.stop)
        self.tmp = tempfile.mkdtemp(prefix="sectrust-daemon-cluster-")
        self.db = os.path.join(self.tmp, "tasks.db")
        self.ws_root = os.path.join(self.tmp, "workspaces")
        self._import_shutil_cleanup()

    def _import_shutil_cleanup(self):
        import shutil
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    def _cluster(self, **kw):
        kw.setdefault("dead_letter_after", 2)
        c = ClusterCoordinator(url=self.url, enabled=True, **kw)
        self.addCleanup(c.close)
        return c

    def _daemon(self, cluster, db_path=None, **kw):
        kw.setdefault("workspace_root", self.ws_root)
        kw.setdefault("approval_wait", 1.0)
        d = ExecutionDaemon(db_path=db_path or self.db, cluster=cluster, **kw)
        self.addCleanup(d.close)
        return d

    # ---------------------------------------------------------------- 守卫
    def test_lock_held_elsewhere_requeues_and_leaves_it_alone(self):
        cluster = self._cluster()
        other = cluster.claim_task("t1", "machineB")       # 模拟另一台机器先拿到
        self.assertIsNotNone(other)
        d = self._daemon(cluster)
        d.sched.add_task("t1", payload={"op": "noop"})
        out = d.run_once()
        self.assertEqual(out, [("t1", "requeued")])
        t = d.reg.get_task("t1")
        self.assertEqual(t["status"], "registered")        # 放回重排，不 failed
        # 别人的锁原封不动
        self.assertEqual(cluster.holder("claim:t1"), other)
        # 对方释放后本机正常拿到
        self.assertTrue(cluster.release_task("t1", other))
        out = d.run_once()
        self.assertEqual(out, [("t1", "done")])
        self.assertEqual(d.reg.get_task("t1")["status"], "completed")

    def test_normal_flow_acquires_and_releases_lock(self):
        cluster = self._cluster()
        d = self._daemon(cluster)
        d.sched.add_task("t-ok", payload={"op": "noop"})
        out = d.run_once()
        self.assertEqual(out, [("t-ok", "done")])
        # 执行完后锁已释放（finally 保证，成功路径也放）
        self.assertIsNone(cluster.holder("claim:t-ok"))
        self.assertIsNone(cluster.recover_dead_letter("t-ok"))

    def test_failed_task_also_releases_lock(self):
        cluster = self._cluster()
        d = self._daemon(cluster)
        d.sched.add_task("t-bad", payload={"op": "fly"})
        out = d.run_once()
        self.assertEqual(out, [("t-bad", "failed")])
        self.assertIsNone(cluster.holder("claim:t-bad"))    # 失败也放锁

    # ---------------------------------------------------------------- 死信
    def test_repeated_failures_enter_dead_letter(self):
        cluster = self._cluster(dead_letter_after=2)
        d = self._daemon(cluster)
        d.sched.add_task("t-dlq", payload={"op": "fly"})
        self.assertEqual(d.run_once(), [("t-dlq", "failed")])
        self.assertEqual(cluster.dead_letter_list(), [])    # 一次失败只计数
        # 编排层重试：把任务放回 registered 再来一次
        d.reg.update_task_status("t-dlq", "registered")
        self.assertEqual(d.run_once(), [("t-dlq", "failed")])
        entries = cluster.dead_letter_list()
        self.assertEqual(len(entries), 1)                   # 达阈值才入队
        self.assertEqual(entries[0]["task_id"], "t-dlq")
        self.assertIn("未知 op", entries[0]["reason"])
        # 人工恢复后本机可再跑（这次换个能成的 op）
        entry = cluster.recover_dead_letter("t-dlq")
        self.assertEqual(entry["task_id"], "t-dlq")

    # ---------------------------------------------------------------- 心跳续期
    def test_cluster_beat_renews_inflight_claim(self):
        cluster = self._cluster()
        d = self._daemon(cluster)
        token = cluster.claim_task("t-long", d.reg.runner_id)
        d._current_claim = ("t-long", token)                # 白盒：模拟处理中
        ttl_before = d.cluster.client.ttl("ai-colleague:cluster:lock:claim:t-long")
        d._cluster_beat()
        ttl_after = d.cluster.client.ttl("ai-colleague:cluster:lock:claim:t-long")
        self.assertGreaterEqual(ttl_after, ttl_before)      # 续期生效
        self.assertEqual(cluster.holder("claim:t-long"), token)
        d._current_claim = None

    def test_cluster_beat_tolerates_stolen_lock(self):
        cluster = self._cluster()
        d = self._daemon(cluster)
        token = cluster.claim_task("t-long", d.reg.runner_id)
        # 锁过期后被别人抢走
        self.srv.expires["ai-colleague:cluster:lock:claim:t-long"] = time.time() - 1
        stolen = cluster.claim_task("t-long", "machineB")
        self.assertIsNotNone(stolen)
        d._current_claim = ("t-long", token)
        d._cluster_beat()                                   # 只告警，不炸不中断
        self.assertEqual(cluster.holder("claim:t-long"), stolen)
        d._current_claim = None

    # ---------------------------------------------------------------- 下线
    def test_close_drops_heartbeat_and_connections(self):
        cluster = self._cluster()
        d = self._daemon(cluster)
        d._cluster_beat()
        self.assertTrue(cluster.is_alive(d.reg.runner_id))
        d.close()
        # 注意：close 后 cluster 是"懒重连"状态——再用它会建新连接。
        # 所以验证心跳必须走**独立连接**，不能走 cluster 自身。
        raw = RESPClient(self.url).connect()
        self.addCleanup(raw.close)
        hb_key = "ai-colleague:cluster:hb:" + d.reg.runner_id
        self.assertEqual(raw.exists(hb_key), 0)             # 心跳真被摘了
        self.assertEqual(raw.smembers("ai-colleague:cluster:hb:registry"), [])
        self.assertEqual(cluster._all_clients, [])          # close 后再无连接

    # ---------------------------------------------------------------- 双机同库不同库
    def test_two_machines_local_dbs_one_requeues(self):
        """每机本地库拓扑：两个 daemon 各看各的库，同一任务只有一台真跑。

        A 的任务故意睡 1.5s 制造"正在干"的时间窗；B 在**确认 A 已持有
        任务锁之后**才去认领（轮询锁而不是猜睡眠时间——确定性同步）。
        """
        cluster = self._cluster()
        db_a = os.path.join(self.tmp, "a.db")
        db_b = os.path.join(self.tmp, "b.db")
        da = self._daemon(cluster, db_path=db_a)
        db_ = self._daemon(cluster, db_path=db_b)
        da.sched.add_task("t1", payload={
            "op": "shell",
            "cmd": [sys.executable, "-c", "import time; time.sleep(1.5)"],
        })
        db_.sched.add_task("t1", payload={"op": "noop"})

        result = {}

        def run_a():
            result["a"] = da.run_once()

        th = threading.Thread(target=run_a)
        th.start()
        deadline = time.time() + 5
        while time.time() < deadline and not cluster.holder("claim:t1"):
            time.sleep(0.02)
        self.assertIsNotNone(cluster.holder("claim:t1"), "A 未在 5s 内拿到任务锁")
        out_b = db_.run_once()
        th.join(timeout=10)
        self.assertEqual(result["a"], [("t1", "done")])
        self.assertEqual(out_b, [("t1", "requeued")])        # B 被守卫拦下
        self.assertEqual(db_.reg.get_task("t1")["status"], "registered")
        # A 干完放锁后，B 重排拿到任务正常完成
        self.assertEqual(db_.run_once(), [("t1", "done")])

    # ---------------------------------------------------------------- 未启用
    def test_disabled_cluster_same_behavior_no_connections(self):
        cluster = ClusterCoordinator(url=self.url, enabled=False)
        d = self._daemon(cluster)
        d.sched.add_task("t-off", payload={"op": "noop"})
        self.assertEqual(d.run_once(), [("t-off", "done")])
        self.assertEqual(cluster._all_clients, [])           # 一次连接都没开
        self.assertEqual(cluster.alive_runners(), [])


if __name__ == "__main__":
    unittest.main()
