"""
MockModelProvider —— 离线可跑的模型桩。

不调用任何外部 API，按规则生成回显/应答，用于 demo 与回归测试。
生产环境若未配置任何真实 key，也会作为安全兜底（不会让运行时崩溃）。
"""

from __future__ import annotations

from models import ChatMessage, ChatResult, ModelProvider


class MockModelProvider(ModelProvider):
    name = "mock"
    prefixes = ("mock",)

    def complete(self, messages: list[ChatMessage], model: str = None, **opts) -> ChatResult:
        last_user = next((m.content for m in reversed(messages) if m.role == "user"), "")
        reply = f"[mock:{model or 'mock'}] 已收到：{last_user}"
        return ChatResult(content=reply, model=model or "mock", usage={"mock": True})
