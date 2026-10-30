"""
models/multimodal.py —— 图像 / 音频 / 视频生成 provider（多 AI 协同的"设计 AI / 音频 AI / 剪辑 AI"）。

与 chat 层（models/__init__.py）同风格：
  - 统一抽象 MediaProvider.generate(kind, prompt) -> MediaResult
  - OpenAICompatibleMediaProvider：/images/generations、/audio/speech，
    视频走可配置路径（OpenAI 兼容生态里各家不一，默认 /videos/generations，
    不通则明确报错，不静默造假）。
  - MockMediaProvider：离线确定性假字节——没有密钥时演示链路照样跑得通，
    且测试不需要网络。
  - MediaRouter：按 model 名前缀路由；未配置/未命中 -> Mock（与 chat 路由同哲学）。

诚实边界：**无密钥时不可用**（configured=False，路由器降级 Mock，产出是假字节）。
真机联调需要用户自备 API key——本模块只保证"接口就绪、错误清晰、测试可离线"。
"""

from __future__ import annotations

import abc
import base64
import json
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Optional

# 支持的媒体类型
MEDIA_KINDS = ("image", "audio", "video")


@dataclass
class MediaResult:
    kind: str                       # image / audio / video
    provider: str = "none"
    model: str = ""
    mime: str = "application/octet-stream"
    data: Optional[bytes] = None    # 二进制内容（b64_json 或二进制响应体）
    url: Optional[str] = None       # 远端 URL（服务端只回链接时）
    prompt: str = ""
    raw: Any = None
    meta: dict = field(default_factory=dict)


class MediaProvider(abc.ABC):
    """媒体生成 provider 抽象。configured=False 时路由器降级到 Mock。"""

    name: str = "base"
    prefixes: tuple = ()
    kinds: tuple = MEDIA_KINDS
    configured: bool = True

    @abc.abstractmethod
    def generate(self, kind: str, prompt: str, **opts) -> MediaResult:
        raise NotImplementedError

    def supports(self, model_name: str) -> bool:
        if "*" in self.prefixes:
            return True
        return any(model_name.startswith(p) for p in self.prefixes)


class MockMediaProvider(MediaProvider):
    """离线假字节：按 kind 返回确定性的占位内容。

    用途：① 无密钥时跑通演示链路；② 测试不依赖网络。
    产出**不是真图/真音频**——bytes 只是带魔数的占位数据，meta.fake=True。"""

    name = "mock"
    prefixes = ("mock",)
    configured = True

    _MAGIC = {
        "image": (b"\x89PNG\r\n\x1a\n" + b"FAKE-IMAGE-BYTES", "image/png"),
        "audio": (b"ID3\x03\x00\x00\x00" + b"FAKE-AUDIO-BYTES", "audio/mpeg"),
        "video": (b"\x00\x00\x00\x18ftypmp42" + b"FAKE-VIDEO-BYTES", "video/mp4"),
    }

    def generate(self, kind: str, prompt: str, **opts) -> MediaResult:
        if kind not in MEDIA_KINDS:
            raise ValueError(f"不支持的媒体类型: {kind}（支持 {MEDIA_KINDS}）")
        magic, mime = self._MAGIC[kind]
        payload = magic + f"|prompt={prompt}".encode()
        return MediaResult(kind=kind, provider=self.name,
                           model=opts.get("model", "mock-media"),
                           mime=mime, data=payload, prompt=prompt,
                           meta={"fake": True})


class OpenAICompatibleMediaProvider(MediaProvider):
    """OpenAI 兼容端点的媒体生成（图像/音频/视频）。

    端点约定（可用 endpoint_overrides 覆盖）：
      image -> {base_url}/images/generations   （返回 b64_json 或 url）
      audio -> {base_url}/audio/speech         （二进制响应体）
      video -> {base_url}/videos/generations   （生态不一；不通会明确报错）
    """

    name = "openai-media"
    prefixes = ("gpt-image", "dall-e", "dalle", "tts", "whisper", "*media*")

    def __init__(self, api_key: str = "", base_url: str = "https://api.openai.com/v1",
                 endpoint_overrides: dict = None):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.endpoint_overrides = endpoint_overrides or {}
        self.configured = bool(api_key)

    def _post_json(self, path: str, payload: dict, timeout: float = 120):
        req = urllib.request.Request(
            f"{self.base_url}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.api_key}"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
            return json.loads(r.read())

    def _post_binary(self, path: str, payload: dict, timeout: float = 120) -> bytes:
        req = urllib.request.Request(
            f"{self.base_url}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.api_key}"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
            return r.read()

    def generate(self, kind: str, prompt: str, **opts) -> MediaResult:
        if not self.api_key:
            raise RuntimeError(
                f"{self.name} 缺少 api_key（媒体生成不可用；"
                f"未配置时路由器会降级到 Mock 假字节）")
        if kind not in MEDIA_KINDS:
            raise ValueError(f"不支持的媒体类型: {kind}")
        model = opts.get("model") or {
            "image": "gpt-image-1", "audio": "tts-1", "video": "sora-2"}[kind]
        timeout = float(opts.get("timeout", 120))

        if kind == "image":
            data = self._post_json(
                self.endpoint_overrides.get("image", "/images/generations"),
                {"model": model, "prompt": prompt,
                 "n": int(opts.get("n", 1)),
                 "size": opts.get("size", "1024x1024")}, timeout=timeout)
            item = data["data"][0]
            if item.get("b64_json"):
                return MediaResult(kind=kind, provider=self.name, model=model,
                                   mime="image/png",
                                   data=base64.b64decode(item["b64_json"]),
                                   prompt=prompt, raw=data)
            return MediaResult(kind=kind, provider=self.name, model=model,
                               url=item.get("url"), prompt=prompt, raw=data)

        if kind == "audio":
            blob = self._post_binary(
                self.endpoint_overrides.get("audio", "/audio/speech"),
                {"model": model, "input": prompt,
                 "voice": opts.get("voice", "alloy"),
                 "response_format": opts.get("format", "mp3")}, timeout=timeout)
            return MediaResult(kind=kind, provider=self.name, model=model,
                               mime="audio/mpeg", data=blob, prompt=prompt)

        # video：OpenAI 兼容生态不统一——没有官方标准路径，
        # 用 endpoint_overrides={"video": "/your/path"} 显式指定后才可用。
        path = self.endpoint_overrides.get("video")
        if not path:
            raise RuntimeError(
                "视频生成端点未配置：OpenAI 兼容生态无统一 video 路径，"
                "请传 endpoint_overrides={'video': '/videos/generations'} "
                "对接具体服务商；不配置时不会静默造假")
        data = self._post_json(path, {"model": model, "prompt": prompt},
                               timeout=timeout)
        item = (data.get("data") or [{}])[0]
        return MediaResult(kind=kind, provider=self.name, model=model,
                           url=item.get("url"),
                           data=base64.b64decode(item["b64_json"])
                           if item.get("b64_json") else None,
                           prompt=prompt, raw=data)


class MediaRouter:
    """按 model 名路由到媒体 provider；未配置/未命中 -> Mock（假字节，meta.fake=True）。"""

    def __init__(self, providers: list[MediaProvider] = None,
                 fallback: MediaProvider = None):
        self.providers = providers if providers is not None else []
        self.fallback = fallback or MockMediaProvider()

    def route(self, model_name: str = None) -> MediaProvider:
        if model_name is None:
            return self.fallback
        for p in self.providers:
            if p.configured and p.supports(model_name):
                return p
        return self.fallback

    def generate(self, kind: str, prompt: str, model: str = None, **opts) -> MediaResult:
        """生成媒体，前后接内容防护（A2）：

        1. 前置：生成 prompt 过注入/越狱检测，高危直接拒绝（不把攻击送进 provider）；
        2. 产出：有字节数据时过魔数/类型/大小校验，不一致明确报错；
        3. meta.content_guard 记录守卫决策，**semantic_check=false 如实标注**——
           未接入视觉/音频审核模型，绝不谎称已做语义审核。
        provider 只回 URL 未回字节时无法做魔数校验，如实标注 validate.ok=None。
        """
        from governance import content_guard as _cg
        pc = _cg.check_media_prompt(prompt)
        if pc["block"]:
            raise ValueError(
                "内容防护拦截：媒体生成 prompt 检测到提示注入/越狱意图（命中 %s）"
                % "、".join(h["rule"] for h in pc["hits"]))
        res = self.route(model).generate(kind, prompt, model=model, **opts)
        if res.data:
            v = _cg.validate_media(res.data, kind, res.mime or "")
            if not v["ok"]:
                raise RuntimeError("媒体内容防护拦截：%s" % v["reason"])
            guard = {"prompt_risk": pc["risk"], "prompt_hits": pc["hits"],
                     "validate": v, "semantic_check": False,
                     "note": "仅魔数/大小/类型校验；未配置视觉/音频语义审核模型，"
                             "不宣称已做语义审核"}
        else:
            guard = {"prompt_risk": pc["risk"], "prompt_hits": pc["hits"],
                     "validate": {"ok": None,
                                  "reason": "provider 只回 URL 未回字节，无法做魔数校验"},
                     "semantic_check": False,
                     "note": "无字节数据，未做魔数校验；未配置语义审核模型"}
        res.meta = dict(res.meta or {})
        res.meta["content_guard"] = guard
        return res
