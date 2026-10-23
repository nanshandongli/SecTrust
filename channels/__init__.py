"""
渠道适配层（Channel）

职责：把各 IM 平台的入站消息标准化为 InboundMessage，并把回复发回原平台（OutboundMessage）。
所有渠道实现都必须继承 ChannelAdapter 并实现 start / send / stop。

设计约束：
- 只做「消息收发 + 标准化」，不碰业务处理、模型调用、技能执行（那是 AgentRunner 的事）。
- 不硬编码任何密钥；需要的凭据从 config 读取（config 又从环境变量加载）。
- 真实渠道（企业微信 / 飞书）的回调校验与加解密在对应桩中给出结构，接手时按官方文档补全网络部分。
"""

from __future__ import annotations

import abc
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from config import ChannelConfig


@dataclass
class InboundMessage:
    """平台无关的入站消息。各渠道收到原生消息后转换成它再交给 handler。"""
    channel: str                 # "wecom" / "feishu" / "telegram" / "discord" / "mock"
    message_id: str
    user_id: str
    text: str
    timestamp: float = field(default_factory=time.time)
    raw: Any = None              # 平台原始消息体，便于调试与平台专属能力


@dataclass
class OutboundMessage:
    """平台无关的回消息。AgentRunner 生成后交给 channel.send() 发回。"""
    channel: str
    user_id: str
    text: str
    raw: Any = None


class ChannelAdapter(abc.ABC):
    """所有渠道的抽象基类。name 用于登记处 payload 标记来源渠道。"""

    name: str = "base"

    def __init__(self, config: "ChannelConfig"):
        self.config = config

    @abc.abstractmethod
    def start(self, handler: Callable[[InboundMessage], None]) -> None:
        """阻塞运行：收到消息后调用 handler(inbound)。实现里应支持被 stop() 唤醒退出。"""
        raise NotImplementedError

    @abc.abstractmethod
    def send(self, msg: OutboundMessage) -> None:
        """把回复发回原渠道。"""
        raise NotImplementedError

    def stop(self) -> None:
        """优雅停止；默认空实现，子类按需覆盖（如关停 HTTP 服务器）。"""
        pass


def _new_id(prefix: str = "msg") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"
