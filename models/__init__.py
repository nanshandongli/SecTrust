"""
模型层（Model）—— 多模型统一接口与路由。

统一接口：ChatMessage(role, content) -> ModelProvider.complete() -> ChatResult(content, model, usage)。

已提供：
  - MockModelProvider   ：离线回显，零依赖，作为缺 key 时的安全兜底。
  - OpenAIModelProvider ：OpenAI 及兼容端点（通义/GLM/DeepSeek 等），标准库 urllib 实现，零依赖。
  - AnthropicProvider   ：Anthropic 官方格式，标准库 urllib 实现。
  - OllamaProvider      ：本地模型，打 http://127.0.0.1:11434，零依赖。

所有密钥只从 config（环境变量）读取，不硬编码。缺 key 时自动降级到 Mock，避免运行时崩溃。
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class ChatMessage:
    role: str          # "system" / "user" / "assistant"
    content: str


@dataclass
class ChatResult:
    content: str
    model: str
    raw: Any = None
    usage: dict = field(default_factory=dict)


class ModelProvider(abc.ABC):
    """模型 provider 抽象。prefixes 用于路由器按模型名前缀匹配。"""
    name: str = "base"
    prefixes: tuple = ()
    configured: bool = True  # 未正确配置（如缺 key）时为 False，路由器会跳过它降级到 Mock

    @abc.abstractmethod
    def complete(self, messages: list[ChatMessage], model: str = None, **opts) -> ChatResult:
        raise NotImplementedError

    def supports(self, model_name: str) -> bool:
        if "*" in self.prefixes:
            return True
        return any(model_name.startswith(p) for p in self.prefixes)


class ModelRouter:
    """按模型名路由到对应 provider；都不匹配时用 fallback（默认 Mock）。"""

    def __init__(self, providers: list[ModelProvider], default_model: str = "mock",
                 fallback: ModelProvider = None):
        self.providers = providers
        self.default_model = default_model
        self.fallback = fallback or MockModelProvider()

    def route(self, model_name: str) -> ModelProvider:
        if model_name is None:
            model_name = self.default_model
        for p in self.providers:
            if p.configured and p.supports(model_name):
                return p
        return self.fallback

    def complete(self, messages: list[ChatMessage], model: str = None, **opts) -> ChatResult:
        model = model or self.default_model
        provider = self.route(model)
        return provider.complete(messages, model=model, **opts)


# 延迟导入避免循环；放在文件末尾
from models.mock import MockModelProvider  # noqa: E402
