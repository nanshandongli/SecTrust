"""Redis 队列测试：假 RESP 服务器上的协议单测 + 队列语义 roundtrip。

- FakeRedisServer：线程内 socket 服务器，只实现本项目用到的命令
  （PING/AUTH/SELECT/LPUSH/RPOP/LLEN/HSET/HGET/HDEL/HKEYS/DEL），
  不需要真 Redis 也能验证编解码与队列语义。
- 真 Redis 集成测试：仅当 REDIS_URL 可达时运行（否则 skip 并如实标注）。
"""

import json
import math
import os
import socket
import sys
import threading
import time
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from transport.resp import RESPClient, RESPError  # noqa: E402
from transport.queue import RedisQueue, build_queue  # noqa: E402


from transport.fake_redis import FakeRedisServer  # noqa: E402 -- protocol infra shared by tests/stress

class TestRESPClient(unittest.TestCase):
    def setUp(self):
        self.srv = FakeRedisServer().start()

    def tearDown(self):
        self.srv.stop()

    def _client(self):
        return RESPClient(f"redis://127.0.0.1:{self.srv.port}/0").connect()

    def test_ping(self):
        c = self._client()
        try:
            self.assertTrue(c.ping())
        finally:
            c.close()

    def test_encode_format(self):
        out = RESPClient.encode(["SET", "k", "v"])
        self.assertEqual(out, b"*3\r\n$3\r\nSET\r\n$1\r\nk\r\n$1\r\nv\r\n")

    def test_encode_int_arg_not_zero_bytes(self):
        """回归：int 参数必须编码为 ASCII（bytes(3) 会是三个零字节的坑）。"""
        out = RESPClient.encode(["SELECT", 3])
        self.assertEqual(out, b"*2\r\n$6\r\nSELECT\r\n$1\r\n3\r\n")

    def test_all_reply_types(self):
        c = self._client()
        try:
            self.assertEqual(c.execute("PING"), "PONG")
            self.assertEqual(c.execute("LPUSH", "l", "a"), 1)
            self.assertEqual(c.execute("LLEN", "l"), 1)
            self.assertEqual(c.execute("HSET", "h", "f", "v"), 1)
            self.assertEqual(c.hget("h", "f"), "v")
            self.assertIsNone(c.hget("h", "nope"))     # nil bulk
            self.assertIsNone(c.rpop("empty"))         # nil bulk
            self.assertEqual(c.hkeys("h"), ["f"])           # array
            with self.assertRaises(RESPError):
                c.execute("BOGUS")                     # error reply
        finally:
            c.close()

    def test_auth_and_select_handshake(self):
        srv = FakeRedisServer(password="s3cret", db=3).start()  # scanner:allow 测试假服务器的假口令，非真实凭据
        try:
            c = RESPClient(f"redis://:s3cret@127.0.0.1:{srv.port}/3").connect()
            c.close()
            # 错密码 -> 连接即失败
            with self.assertRaises(Exception):
                RESPClient(f"redis://:wrong@127.0.0.1:{srv.port}/3").connect()
        finally:
            srv.stop()

    def test_connection_error(self):
        # 占用一个端口再关掉，制造拒连
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        from transport.resp import RESPConnectionError
        with self.assertRaises(RESPConnectionError):
            RESPClient(f"redis://127.0.0.1:{port}/0", timeout=1).connect()


class TestRedisQueueOnFake(unittest.TestCase):
    def setUp(self):
        self.srv = FakeRedisServer().start()
        self.q = RedisQueue(url=f"redis://127.0.0.1:{self.srv.port}/0",
                            key_prefix="test:mq:")

    def tearDown(self):
        self.q.close()
        self.srv.stop()

    def test_publish_consume_ack_roundtrip(self):
        m1 = self.q.publish("t", {"n": 1})
        m2 = self.q.publish("t", {"n": 2})
        self.assertEqual(self.q.pending("t"), 2)
        msgs = self.q.consume("t", batch=10)
        self.assertEqual(len(msgs), 2)
        self.assertEqual({m["payload"]["n"] for m in msgs}, {1, 2})
        self.assertEqual(self.q.pending("t"), 0)
        self.assertEqual(self.q.processing_count("t"), 2)  # 取出未 ack
        for m in msgs:
            self.q.ack(m["id"])
        self.assertEqual(self.q.processing_count("t"), 0)

    def test_requeue_returns_to_pending(self):
        mid = self.q.publish("t", {"n": 1})
        (msg,) = self.q.consume("t")
        self.assertEqual(self.q.pending("t"), 0)
        self.q.requeue(msg["id"])
        self.assertEqual(self.q.pending("t"), 1)
        (msg2,) = self.q.consume("t")
        self.assertEqual(msg2["payload"], {"n": 1})
        self.assertEqual(msg2["id"], mid)

    def test_subscribe_handles_and_acks(self):
        self.q.publish("t", {"a": 1})
        self.q.publish("t", {"a": 2})
        got = []
        stop = threading.Event()

        def handler(payload):
            got.append(payload)

        th = threading.Thread(
            target=self.q.subscribe,
            kwargs=dict(topic="t", handler=handler, poll_interval=0.05,
                        stop_event=stop))
        th.start()
        time.sleep(0.5)
        stop.set()
        th.join(timeout=5)
        self.assertEqual(len(got), 2)
        self.assertEqual(self.q.processing_count("t"), 0)  # 全部 ack
        self.assertEqual(self.q.pending("t"), 0)

    def test_available_on_fake(self):
        self.assertTrue(self.q.available())


class TestBuildQueueRouting(unittest.TestCase):
    def test_auto_falls_back_to_local_when_no_redis(self):
        os.environ.pop("REDIS_URL", None)
        q = build_queue("auto", db_path=os.path.join(
            tempfile_dir(), "mq.db"))
        self.assertEqual(type(q).__name__, "LocalQueue")

    def test_redis_explicit_does_not_silently_degrade(self):
        # backend="redis" 且不可达 -> 仍然是 RedisQueue（发布时才报错），
        # 绝不静默降级回 local（跨机场景最危险的 bug）
        os.environ.pop("REDIS_URL", None)
        q = build_queue("redis")
        self.assertIsInstance(q, RedisQueue)


def tempfile_dir():
    import tempfile
    d = tempfile.mkdtemp(prefix="rq_test_")
    return d


@unittest.skipUnless(os.getenv("REDIS_URL"), "未设置 REDIS_URL，跳过真 Redis 集成测试")
class TestRealRedisIntegration(unittest.TestCase):
    """真 Redis 集成（如实标注：需要 REDIS_URL，CI 未配置时跳过）。"""

    def test_roundtrip_on_real_redis(self):
        q = RedisQueue()
        if not q.available():
            self.skipTest("REDIS_URL 不可达")
        topic = f"it-{int(time.time()*1000)}"
        q.publish(topic, {"hello": "world"})
        (msg,) = q.consume(topic)
        self.assertEqual(msg["payload"], {"hello": "world"})
        q.ack(msg["id"])
        q.close()


if __name__ == "__main__":
    unittest.main()
