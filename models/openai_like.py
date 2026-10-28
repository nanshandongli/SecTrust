"""
OpenAIModelProvider —— OpenAI 及「OpenAI 兼容」端点。

覆盖范围：OpenAI 官方、通义(qwen)、智谱(glm)、DeepSeek 等所有兼容 /v1/chat/completions 的服务。
用标准库 urllib 实现，零第三方依赖（如需官方 SDK 可替换为 openai 包）。

密钥：api_key 从 config（环境变量）读取，不硬编码。
未配置 api_key 时 complete() 抛清晰错误，由路由器降级到 Mock。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Optional

from models import ChatMessage, ChatResult, ModelProvider


class OpenAIModelProvider(ModelProvider):
    name = "openai"
    # 这些前缀路由到本 provider；其余（如 ollama/、claude-）不会命中
    prefixes = ("gpt-", "o1", "o3", "qwen", "glm", "deepseek", "moonshot", "chatglm")

    def __init__(self, api_key: str = "", base_url: str = "https://api.openai.com/v1"):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.configured = bool(api_key)

    def complete(self, messages: list[ChatMessage], model: str = None, **opts) -> ChatResult:
        if not self.api_key:
            raise RuntimeError(
                "OpenAIModelProvider 缺少 api_key（请设置 OPENAI_API_KEY 或对应兼容服务的 key）"
            )
        url = f"{self.base_url}/chat/completions"
        payload = {
            "model": model or "gpt-4o-mini",
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "stream": False,
        }
        # 兼容端点对 temperature 支持不一，缺省不传；需要时经 opts 传入
        if "temperature" in opts:
            payload["temperature"] = opts["temperature"]
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=opts.get("timeout", 60)) as r:  # noqa: S310
                data = json.loads(r.read())
        except urllib.error.HTTPError as e:
            # HTTPError 的 str() 只有状态行，**不含响应体** → 上层无法分辨
            # 「模型过载(code 1305)」与「配额不足(quota)」，归因必然含糊，
            # 用户只会得到一句无用的「检查配置」。这里把响应体摘要并进消息。
            body = ""
            try:
                body = e.read().decode("utf-8", "replace").strip()
            except Exception:  # noqa: BLE001
                body = ""
            if len(body) > 300:
                body = body[:300] + "…"
            raise RuntimeError("HTTP %s %s%s" % (
                e.code, e.reason, (" | " + body) if body else "")) from e
        choice = data["choices"][0]["message"]
        return ChatResult(
            content=choice.get("content", ""),
            model=data.get("model", model),
            raw=data,
            usage=data.get("usage", {}),
        )
