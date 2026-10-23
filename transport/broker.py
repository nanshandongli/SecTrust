"""
事件总线 / worker 协调（transport/broker.py）

把"消息队列"封装成业务友好的事件总线，用于：
- 中心 API 在任务创建/审批通过时发布事件；
- 远程 runner / web 面板订阅事件，实时驱动（无需轮询登记处）。

默认后端是 LocalQueue（SQLite 持久化）；配置 REDIS_URL 后自动切到 Redis（跨机）。
"""

from __future__ import annotations

import threading
from typing import Callable, Optional

try:
    from transport.queue import MessageQueue, build_queue
except ImportError:  # 直接 `python transport/broker.py` 运行时，包不在路径上
    import os
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from transport.queue import MessageQueue, build_queue


class Broker:
    """事件总线：发布事件 + 启动工作线程消费事件。"""

    def __init__(self, queue: MessageQueue = None, backend: str = "auto"):
        self.queue = queue or build_queue(backend)

    def publish_event(self, topic: str, event_type: str, data: dict) -> str:
        """发布一个事件。data 会被包成 {event_type, data, ts}。"""
        return self.queue.publish(topic, {"event_type": event_type, "data": data})

    def pending(self, topic: str) -> int:
        return self.queue.pending(topic)

    def spawn_worker(self, topic: str, handler: Callable[[dict], None],
                     poll_interval: float = 1.0) -> threading.Thread:
        """启动一个后台 worker 线程，持续消费 topic 事件交给 handler。
        handler 收到的是 {event_type, data} 字典。"""
        stop = threading.Event()

        def _loop():
            self.queue.subscribe(topic, handler, poll_interval=poll_interval,
                                 stop_event=stop)

        th = threading.Thread(target=_loop, daemon=True)
        th.start()
        th._stop_event = stop  # type: ignore
        return th

    def stop_worker(self, worker: threading.Thread) -> None:
        stop = getattr(worker, "_stop_event", None)
        if stop:
            stop.set()


if __name__ == "__main__":
    import time
    b = Broker()
    received = []

    def h(ev):
        received.append(ev)

    w = b.spawn_worker("tasks", h, poll_interval=0.1)
    b.publish_event("tasks", "task_created", {"task_id": "t1"})
    b.publish_event("tasks", "approval_resolved", {"approval_id": "a1", "decision": "approve"})
    time.sleep(0.5)
    b.stop_worker(w)
    print("received:", received)
    assert len(received) == 2, received
    print("broker OK")
