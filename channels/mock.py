"""
MockChannel —— 离线可跑的渠道，用于 demo 与回归测试。

后台线程按 mock_interval 周期吐一条 InboundMessage 给 handler；
send() 只把回显打到日志（不真正发网络）。零依赖。
"""

from __future__ import annotations

import threading
import time

from channels import ChannelAdapter, InboundMessage, OutboundMessage, _new_id


class MockChannel(ChannelAdapter):
    name = "mock"

    def __init__(self, config, interval: float = None, max_messages: int = 0):
        super().__init__(config)
        self.interval = interval if interval is not None else getattr(config, "mock_interval", 3.0)
        self.max_messages = max_messages  # 0 = 不限
        self._stop = threading.Event()
        self._thread = None
        self._sent = 0
        self._received = 0

    def _emit_loop(self, handler):
        seq = 0
        while not self._stop.is_set():
            seq += 1
            self._received += 1
            handler(
                InboundMessage(
                    channel=self.name,
                    message_id=_new_id("mock"),
                    user_id=f"mock-user-{seq % 3}",
                    text=f"你好，这是第 {seq} 条模拟消息",
                )
            )
            if self.max_messages and seq >= self.max_messages:
                break
            self._stop.wait(self.interval)

    def start(self, handler):
        self._stop.clear()
        self._thread = threading.Thread(target=self._emit_loop, args=(handler,), daemon=True)
        self._thread.start()
        # MockChannel 用线程模拟"阻塞接收"，这里保持进程存活交给调用方；
        # 若单独运行，可调用 join()。
        self._thread.join()

    def join(self, timeout=None):
        if self._thread:
            self._thread.join(timeout=timeout)

    def send(self, msg: OutboundMessage) -> None:
        self._sent += 1
        print(f"[mock.channel] -> {msg.user_id}: {msg.text}")

    def stop(self) -> None:
        self._stop.set()
