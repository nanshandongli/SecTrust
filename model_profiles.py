"""
model_profiles.py —— 可持久化的「算力模型配置」（对话框内选择/配置其他模型的根基）。

为什么需要它：
    仓库既有 models/ 包（Ollama / OpenAI 兼容 / Anthropic / Mock）是「能力层」，
    但此前只在环境变量里读一个默认 key，且 console_chat 的 LlmEngine 自己写死了
    Ollama 优先 + OpenAI 兜底。这无法满足「像 WorkBuddy 那样在对话框里选/配多个
    第三方算力」——需要一个**用户可在界面增删、可持久化、可一键切换**的配置表。

每个 profile 描述一个可用算力后端：
    - provider: ollama | openai | anthropic
    - base_url: 端点（本地 Ollama 默认 http://127.0.0.1:11434；云端可自定义兼容网关）
    - api_key:  仅云端需要；本地 Ollama 留空（configured 恒为 True）
    - model:    模型名（llama3 / gpt-4o-mini / claude-3-5-sonnet-latest …）
    - active:   是否为当前对话默认使用的后端

安全边界（对齐仓库「诚实边界」范式）：
    - 落盘到 data/model_profiles.json（已被 .gitignore 排除，**绝不进仓库**）；
    - API 返回时 API Key 只暴露后 4 位（mask），明文只在磁盘与调用 provider 时出现；
    - 云端 profile 缺 key 时 configured=False，对话引擎会如实报「不可用」而非伪造回答。
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass
from typing import Dict, List, Optional

import model_providers

ALLOWED_PROVIDERS = ("ollama", "openai", "anthropic")
# 计费方式取值来自供应商目录模块（单一事实来源，避免两处枚举漂移）
ALLOWED_BILLINGS = (
    model_providers.BILLING_FREE,
    model_providers.BILLING_SUBSCRIPTION,
    model_providers.BILLING_PAYG,
    model_providers.BILLING_LOCAL,
)
DEFAULT_DATA_DIR = "data"


@dataclass
class ModelProfile:
    id: str
    name: str
    provider: str
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    active: bool = False
    # 计费方式：用户对自己所购方案的**记录**（free/subscription/payg/local）。
    # 诚实边界：它不改变请求怎么发、也不改变实际扣费——实际扣费由
    # 「端点(base_url) + 账号」决定；这里只用于设置页展示与提示。
    billing: str = ""
    # 用途类型：chat（对话/文本，默认）| image（文生图）。
    # 同一个兼容网关若同时支持 chat 与 /images/generations，用户可设两个 profile
    # 分别标 chat / image；image 算子优先挑 kind=image 的已配置 profile，
    # 否则回退当前 active（很多网关 chat/绘图共用同一 key）。
    kind: str = "chat"

    @property
    def configured(self) -> bool:
        # 本地 Ollama 无需密钥；云端必须有 key 才算可用
        if self.provider == "ollama":
            return True
        return bool(self.api_key)

    @property
    def billing_label(self) -> str:
        if not self.billing:
            return ""
        return model_providers.billing_label(self.billing)

    def to_public(self) -> Dict[str, object]:
        d = asdict(self)
        d["configured"] = self.configured
        d["billing_label"] = self.billing_label
        d["api_key_masked"] = ("****" + self.api_key[-4:]) if self.api_key else ""
        d.pop("api_key", None)
        return d


class ModelProfileStore:
    """profiles 的加载 / 保存 / 增删改查。磁盘是单一事实来源。"""

    def __init__(self, data_dir: str = DEFAULT_DATA_DIR, path: Optional[str] = None):
        self.path = path or os.path.join(data_dir, "model_profiles.json")
        self._profiles: Dict[str, ModelProfile] = {}
        self._load()
        self._seed_defaults()

    # ---------------- 持久化 ----------------
    def _load(self) -> None:
        if not os.path.isfile(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for p in (data.get("profiles") or []):
                prof = ModelProfile(
                    id=str(p.get("id") or ""),
                    name=str(p.get("name") or ""),
                    provider=str(p.get("provider") or ""),
                    base_url=str(p.get("base_url") or ""),
                    api_key=str(p.get("api_key") or ""),
                    model=str(p.get("model") or ""),
                    billing=str(p.get("billing") or ""),
                    kind=str(p.get("kind") or "chat"),
                )
                prof.active = bool(p.get("active"))
                if prof.id:
                    self._profiles[prof.id] = prof
        except Exception:  # noqa: BLE001
            self._profiles = {}

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(
                {"profiles": [asdict(p) for p in self._profiles.values()]},
                f, ensure_ascii=False, indent=2,
            )

    def _seed_defaults(self) -> None:
        """首次运行时注入默认 profile：本地 Ollama 必给；环境变量里的云端也自动建。"""
        changed = False
        if not self._profiles:
            self._profiles["ollama-local"] = ModelProfile(
                id="ollama-local", name="本地 Ollama",
                provider="ollama", base_url="http://127.0.0.1:11434",
                model="", active=True,
                billing=model_providers.BILLING_LOCAL)
            changed = True

        env_openai = os.environ.get("OPENAI_API_KEY")
        if env_openai and "openai-env" not in self._profiles:
            self._profiles["openai-env"] = ModelProfile(
                id="openai-env", name="OpenAI（环境变量）",
                provider="openai",
                base_url=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
                api_key=env_openai,
                model=os.environ.get("OPENAI_CHAT_MODEL", "gpt-4o-mini"),
                active=False)
            changed = True

        env_anthropic = os.environ.get("ANTHROPIC_API_KEY")
        if env_anthropic and "anthropic-env" not in self._profiles:
            self._profiles["anthropic-env"] = ModelProfile(
                id="anthropic-env", name="Claude（环境变量）",
                provider="anthropic",
                base_url=os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com"),
                api_key=env_anthropic,
                model=os.environ.get("ANTHROPIC_CHAT_MODEL", "claude-3-5-sonnet-latest"),
                active=False)
            changed = True

        # 保证有且仅有一个 active
        if not any(p.active for p in self._profiles.values()):
            first = next(iter(self._profiles.values()), None)
            if first:
                first.active = True
        if changed:
            self._save()

    # ---------------- 查询 ----------------
    def iter_profiles(self) -> List[ModelProfile]:
        return list(self._profiles.values())

    def list_public(self) -> Dict[str, object]:
        return {
            "profiles": [p.to_public() for p in self._profiles.values()],
            "active_id": self.get_active_id(),
        }

    def get(self, pid: str) -> Optional[ModelProfile]:
        return self._profiles.get(pid)

    def get_active_id(self) -> Optional[str]:
        for p in self._profiles.values():
            if p.active:
                return p.id
        return None

    def get_active(self) -> Optional[ModelProfile]:
        pid = self.get_active_id()
        return self._profiles.get(pid) if pid else None

    def get_image(self) -> Optional[ModelProfile]:
        """挑一个用于文生图的 profile（诚实边界：未配置就返回 None 让调用方报错）。

        优先级：
          1. 显式标 kind=image 且已配置（有 key）的 profile；
          2. 否则回退当前 active（前提是云端已配置）——很多 OpenAI 兼容网关
             的 /images/generations 与 /chat/completions 共用同一 key 与 base_url。
        本地 Ollama 没有绘图端点，不参与回退（configured=True 但会调不通，
        由调用方如实报「不支持文生图」而非静默伪造）。
        """
        for p in self._profiles.values():
            if p.kind == "image" and p.configured:
                return p
        active = self.get_active()
        if active and active.configured and active.provider != "ollama":
            return active
        return None

    # ---------------- 写操作 ----------------
    def upsert(self, data: Dict[str, object]) -> Dict[str, object]:
        pid = str(data.get("id") or "").strip()
        if not pid:
            pid = "custom-" + uuid.uuid4().hex[:8]
        provider = str(data.get("provider") or "")
        if provider not in ALLOWED_PROVIDERS:
            raise ValueError("provider 必须是 %s 之一" % (ALLOWED_PROVIDERS,))
        name = str(data.get("name") or "").strip() or pid
        base_url = str(data.get("base_url") or "").strip()
        model = str(data.get("model") or "").strip()
        api_key = str(data.get("api_key") or "")
        billing = str(data.get("billing") or "").strip()
        if billing and billing not in ALLOWED_BILLINGS:
            raise ValueError("billing 必须是 %s 之一" % (ALLOWED_BILLINGS,))
        # 用途类型：chat（默认）/ image（文生图）。前端未带时保留原值或默认 chat。
        kind = str(data.get("kind") or "").strip()
        if kind and kind not in ("chat", "image"):
            raise ValueError("kind 必须是 chat 或 image")

        existing = self._profiles.get(pid)
        # 若前端回传的是掩码（****xxxx），说明用户没改 key，保留磁盘原值
        if api_key.startswith("****") and existing:
            api_key = existing.api_key
        # 编辑时前端若没带 billing（老前端/老数据），保留已记录的值
        if not billing and existing:
            billing = existing.billing
        # 编辑时前端若没带 kind（老前端/老数据），保留已记录的值
        if not kind and existing:
            kind = existing.kind

        prof = ModelProfile(
            id=pid, name=name, provider=provider,
            base_url=base_url, api_key=api_key, model=model,
            active=bool(data.get("active",
                                 existing.active if existing else False)),
            billing=billing,
            kind=kind or "chat")
        self._profiles[pid] = prof
        self._save()
        return prof.to_public()

    def delete(self, pid: str) -> bool:
        if pid not in self._profiles:
            return False
        was_active = self._profiles[pid].active
        del self._profiles[pid]
        if was_active:
            nxt = next(iter(self._profiles.values()), None)
            if nxt:
                nxt.active = True
        self._save()
        return True

    def set_active(self, pid: str) -> Dict[str, object]:
        if pid not in self._profiles:
            raise KeyError("no such profile: %s" % pid)
        for p in self._profiles.values():
            p.active = (p.id == pid)
        self._save()
        return self._profiles[pid].to_public()
