"""
AnthropicProvider —— Anthropic（Claude）官方 API。

格式与 OpenAI 不同：system 单独成参，消息走 messages，需要 x-api-key 与 anthropic-version 头。
用标准库 urllib 实现，零第三方依赖。

未配置 api_key 时 complete() 抛清晰错误，由路由器降级到 Mock。
"""

from __future__ import annotations

import json
import urllib.request
from typing import Optional

from models import ChatMessage, ChatResult, ModelProvider


class AnthropicProvider(ModelProvider):
    name = "anthropic"
    prefixes = ("claude",)

    def __init__(self, api_key: str = "", base_url: str = "https://api.anthropic.com",
                 version: str = "2023-06-01"):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.version = version
        self.configured = bool(api_key)

    def complete(self, messages: list[ChatMessage], model: str = None, **opts) -> ChatResult:
        if not self.api_key:
            raise RuntimeError("AnthropicProvider 缺少 api_key（请设置 ANTHROPIC_API_KEY）")
        system = next((m.content for m in messages if m.role == "system"), None)
        conv = [{"role": m.role, "content": m.content}
                for m in messages if m.role != "system"]
        url = f"{self.base_url}/v1/messages"
        payload = {
            "model": model or "claude-3-5-sonnet-latest",
            "messages": conv,
            "max_tokens": opts.get("max_tokens", 1024),
        }
        if system:
            payload["system"] = system
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "x-api-key": self.api_key,
                "anthropic-version": self.version,
            },
        )
        with urllib.request.urlopen(req, timeout=opts.get("timeout", 60)) as r:  # noqa: S310
            data = json.loads(r.read())
        blocks = data.get("content", [])
        text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        return ChatResult(content=text, model=data.get("model", model), raw=data,
                          usage=data.get("usage", {}))
