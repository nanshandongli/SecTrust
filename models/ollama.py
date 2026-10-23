"""
OllamaProvider —— 本地模型（Ollama）。

打本地 Ollama 的 /api/chat 端点，零依赖、可离线（前提是本机已跑 Ollama 并 pull 了模型）。
模型名约定用前缀 `ollama/`，例如 `ollama/llama3`。
"""

from __future__ import annotations

import json
import urllib.request

from models import ChatMessage, ChatResult, ModelProvider


class OllamaProvider(ModelProvider):
    name = "ollama"
    prefixes = ("ollama/",)

    def __init__(self, base_url: str = "http://127.0.0.1:11434"):
        self.base_url = base_url.rstrip("/")

    def complete(self, messages: list[ChatMessage], model: str = None, **opts) -> ChatResult:
        # 模型名去掉前缀：ollama/llama3 -> llama3
        model_name = (model or "llama3").split("/", 1)[-1]
        url = f"{self.base_url}/api/chat"
        payload = {
            "model": model_name,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "stream": False,
        }
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=opts.get("timeout", 120)) as r:  # noqa: S310 (本地)
            data = json.loads(r.read())
        return ChatResult(
            content=data.get("message", {}).get("content", ""),
            model=model_name,
            raw=data,
            usage=data.get("prompt_eval_count", 0) and {
                "prompt_tokens": data.get("prompt_eval_count", 0),
                "completion_tokens": data.get("eval_count", 0),
            },
        )
