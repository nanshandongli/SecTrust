"""
消息队列抽象（transport/queue.py）—— 让任务分发可跨进程/跨机（路线 1：分布式）。

默认实现 LocalQueue：把消息落进独立的 SQLite（WAL）文件，做到「进程崩溃不丢消息、
可被多个 worker 进程消费」。这样即使没有 Redis/Kafka，本产品也能以"单文件 + 轮询"
的方式实现基本的异步任务分发，且零依赖。

可选实现 RedisQueue：设置 REDIS_URL（redis://host:port/db）即启用，消息走 Redis，
适合真正跨多机。客户端是**手写的 RESP over socket**（transport/resp.py），
零第三方依赖。两个实现共用同一 MessageQueue 接口，runner / broker 无感切换。

⚠️ 选 LocalQueue 仅适合单/少量机器（同机或挂载同一磁盘）；跨机房/高吞吐请上 Redis/MQ。
"""

from __future__ import annotations

import abc
import json
import os
import sqlite3
import threading
import time
import uuid
from typing import Callable, Dict, List, Optional


class MessageQueue(abc.ABC):
    """消息队列抽象。topic 即频道（如 "tasks"、"approvals"）。"""

    @abc.abstractmethod
    def publish(self, topic: str, message: dict) -> str:
        """发布一条消息，返回消息 id。"""

    @abc.abstractmethod
    def consume(self, topic: str, batch: int = 10) -> List[dict]:
        """取出并"认领"一批待处理消息（取出即标记为处理中，handler 成功后 ack）。"""

    @abc.abstractmethod
    def ack(self, msg_id: str) -> None:
        """确认消息处理完成。"""

    @abc.abstractmethod
    def requeue(self, msg_id: str) -> None:
        """处理失败，重新入队（放回 pending）。"""

    @abc.abstractmethod
    def pending(self, topic: str) -> int:
        """某 topic 待处理消息数。"""

    def subscribe(self, topic: str, handler: Callable[[dict], None],
                  poll_interval: float = 1.0, stop_event: threading.Event = None,
                  batch: int = 10):
        """阻塞式消费循环：不断取出消息交给 handler，直到 stop_event 置位。
        在线程中运行；handler 抛异常时该消息自动重新入队（不丢）。"""
        stop_event = stop_event or threading.Event()
        while not stop_event.is_set():
            try:
                msgs = self.consume(topic, batch=batch)
                for m in msgs:
                    try:
                        handler(m["payload"])
                        self.ack(m["id"])
                    except Exception:  # noqa: BLE001
                        self.requeue(m["id"])
            except Exception:  # noqa: BLE001
                pass
            if stop_event.wait(poll_interval):
                break


class LocalQueue(MessageQueue):
    """基于 SQLite（WAL）的持久化队列。零依赖、崩溃可恢复、可被多进程消费。"""

    def __init__(self, db_path: str = None):
        self.db_path = db_path or os.getenv(
            "MQ_DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                      "..", "data", "mq.db")
        )
        os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)
        self._local = threading.local()
        self._init_schema()

    def _connect(self):
        if not hasattr(self._local, "conn") or self._local.conn is None:
            conn = sqlite3.connect(self.db_path, timeout=30, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("PRAGMA synchronous=FULL")
            self._local.conn = conn
        return self._local.conn

    def _init_schema(self):
        conn = self._connect()
        conn.execute(
            """CREATE TABLE IF NOT EXISTS mq_messages (
                id TEXT PRIMARY KEY,
                topic TEXT NOT NULL,
                payload TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )"""
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_mq_topic_status "
                     "ON mq_messages(topic, status)")
        conn.commit()

    def publish(self, topic: str, message: dict) -> str:
        mid = f"mq-{uuid.uuid4().hex[:12]}"
        conn = self._connect()
        conn.execute(
            "INSERT INTO mq_messages(id, topic, payload, status, created_at, updated_at) "
            "VALUES (?,?,?, 'pending', ?, ?)",
            (mid, topic, json.dumps(message, ensure_ascii=False),
             time.time(), time.time()),
        )
        conn.commit()
        return mid

    def consume(self, topic: str, batch: int = 10) -> List[dict]:
        conn = self._connect()
        cur = conn.execute(
            "SELECT id, payload FROM mq_messages "
            "WHERE topic=? AND status='pending' ORDER BY created_at ASC LIMIT ?",
            (topic, batch),
        )
        rows = cur.fetchall()
        ids = [r[0] for r in rows]
        if ids:
            conn.execute(
                "UPDATE mq_messages SET status='processing', attempts=attempts+1, "
                f"updated_at=? WHERE id IN ({','.join('?' * len(ids))})",
                [time.time(), *ids],
            )
            conn.commit()
        return [{"id": r[0], "payload": json.loads(r[1])} for r in rows]

    def ack(self, msg_id: str) -> None:
        conn = self._connect()
        conn.execute("UPDATE mq_messages SET status='done', updated_at=? WHERE id=?",
                     (time.time(), msg_id))
        conn.commit()

    def requeue(self, msg_id: str) -> None:
        conn = self._connect()
        conn.execute("UPDATE mq_messages SET status='pending', updated_at=? WHERE id=?",
                     (time.time(), msg_id))
        conn.commit()

    def pending(self, topic: str) -> int:
        conn = self._connect()
        return conn.execute(
            "SELECT COUNT(*) FROM mq_messages WHERE topic=? AND status='pending'",
            (topic,),
        ).fetchone()[0]

    def close(self):
        if hasattr(self._local, "conn") and self._local.conn:
            self._local.conn.close()
            self._local.conn = None


class RedisQueue(MessageQueue):
    """Redis 后端（跨机）。用**手写 RESP 客户端**（transport/resp.py），零第三方依赖。

    语义与 LocalQueue 对齐（不再是"弹出即消失"）：
      publish  -> LPUSH  {prefix}{topic}:queue
      consume  -> RPOP 取出并 HSET 进 {prefix}{topic}:processing（处理中）
      ack      -> HDEL processing（确认完成）
      requeue  -> HDEL processing + LPUSH queue（失败重回待处理）
      pending  -> LLEN queue（不含 processing 中的）

    崩溃恢复：processing 里的消息不会丢（仍在 hash 里），
    但需要人工/运维决定重放——本类不自动恢复，避免"重启后重复消费"的静默风险。
    """

    def __init__(self, url: str = None, key_prefix: str = "ai-colleague:mq:"):
        self.url = url or os.getenv("REDIS_URL", "redis://localhost:6379/0")
        self.key_prefix = key_prefix
        self._client = None
        self._last_topic = None  # subscribe 基类只传 msg_id 给 ack/requeue，靠它兜底

    def available(self) -> bool:
        """Redis 可达则 True（socket 探测，不 import 任何第三方包）。"""
        try:
            from transport.resp import RESPClient
            c = RESPClient(self.url)
            try:
                return c.ping()
            finally:
                c.close()
        except Exception:  # noqa: BLE001
            return False

    @property
    def client(self):
        if self._client is None:
            from transport.resp import RESPClient
            self._client = RESPClient(self.url)
        return self._client

    def _queue_key(self, topic: str) -> str:
        return self.key_prefix + topic + ":queue"

    def _processing_key(self, topic: str) -> str:
        return self.key_prefix + topic + ":processing"

    def publish(self, topic: str, message: dict) -> str:
        mid = f"mq-{uuid.uuid4().hex[:12]}"
        self.client.lpush(self._queue_key(topic),
                          json.dumps({"id": mid, "payload": message},
                                     ensure_ascii=False))
        return mid

    def consume(self, topic: str, batch: int = 10) -> List[dict]:
        self._last_topic = topic
        out = []
        for _ in range(batch):
            raw = self.client.rpop(self._queue_key(topic))
            if raw is None:
                break
            item = json.loads(raw)
            # 取出即登记到 processing：ack 才删除，崩溃后仍可找回（不静默丢失）
            self.client.hset(self._processing_key(topic), item["id"], raw)
            out.append(item)
        return out

    def ack(self, msg_id: str, topic: str = None) -> None:
        # topic 缺省时无法定位 processing key；MessageQueue.subscribe 的调用方
        # 只传 msg_id，因此用最近一次 consume 的 topic 兜底。
        topic = topic or self._last_topic
        self.client.hdel(self._processing_key(topic), msg_id)

    def requeue(self, msg_id: str, topic: str = None) -> None:
        topic = topic or self._last_topic
        raw = self.client.hget(self._processing_key(topic), msg_id)
        if raw:
            self.client.lpush(self._queue_key(topic), raw)
            self.client.hdel(self._processing_key(topic), msg_id)

    def pending(self, topic: str) -> int:
        return self.client.llen(self._queue_key(topic))

    def processing_count(self, topic: str) -> int:
        """处理中（已取出未 ack）的消息数——崩溃恢复时的待人工清单。"""
        return len(self.client.hkeys(self._processing_key(topic)))

    def close(self):
        if self._client is not None:
            try:
                self._client.close()
            finally:
                self._client = None


def build_queue(backend: str = "auto", db_path: str = None,
                redis_url: str = None) -> MessageQueue:
    """按优先级选择队列后端：redis(可用) -> local。

    backend="redis" 时强制作 Redis（不可用会在 publish/consume 时报错，不静默降级——
    跨机场景静默降级回单机队列是最危险的一类 bug）。
    """
    if backend in ("redis", "auto"):
        rq = RedisQueue(url=redis_url)
        if backend == "redis" or rq.available():
            return rq
    return LocalQueue(db_path=db_path)


if __name__ == "__main__":
    # 自检落临时目录，避免污染 data/
    import tempfile
    q = LocalQueue(
        db_path=os.path.join(tempfile.mkdtemp(prefix="mq_selftest_"), "mq.db"))
    q.publish("demo", {"hello": "world"})
    q.publish("demo", {"n": 2})
    print("pending:", q.pending("demo"))
    msgs = q.consume("demo", batch=10)
    for m in msgs:
        print("got:", m["payload"])
        q.ack(m["id"])
    print("pending after ack:", q.pending("demo"))
