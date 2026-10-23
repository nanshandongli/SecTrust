"""
配置加载 —— 所有密钥只从环境变量读，默认值留空，绝不硬编码。

优先级：默认值 < 环境变量 < 可选 JSON 配置文件（--config）。

环境变量清单（仓库另提供 .env.example）：
  # 模型
  OPENAI_API_KEY / OPENAI_BASE_URL
  ANTHROPIC_API_KEY
  OLLAMA_BASE_URL
  DEFAULT_MODEL            # 例如 gpt-4o-mini / claude-3-5-sonnet-latest / ollama/llama3 / mock
  # 企业微信
  WECHAT_TOKEN / WECHAT_AES_KEY / WECHAT_CORP_ID / WECHAT_AGENT_ID / WECHAT_SECRET
  WECHAT_CALLBACK_PORT
  # 飞书
  FEISHU_APP_ID / FEISHU_APP_SECRET / FEISHU_VERIFICATION_TOKEN
  FEISHU_CALLBACK_PORT
  # 技能（GitHub 拉取）
  GITHUB_SKILLS_REPO / GITHUB_SKILLS_REF   # ref 建议 pin 到 commit/tag
  GITHUB_SKILLS_CACHE / SKILLS_ALLOWLIST   # 逗号分隔
  LOCAL_SKILLS_DIR
  # 运行时
  DB_PATH / CHANNEL / HEARTBEAT_INTERVAL / MOCK_INTERVAL
"""

from __future__ import annotations

import dataclasses
import json
import os
from typing import Optional


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default)


@dataclasses.dataclass
class ChannelConfig:
    name: str = "mock"
    mock_interval: float = 3.0
    # 企业微信
    wecom_token: str = ""
    wecom_aes_key: str = ""
    wecom_corp_id: str = ""
    wecom_agent_id: str = ""
    wecom_secret: str = ""
    wecom_callback_port: int = 8081
    # 飞书
    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    feishu_verification_token: str = ""
    feishu_encrypt_key: str = ""   # 可选：开启事件加密（Encrypt Key）时填写
    feishu_callback_port: int = 8082


@dataclasses.dataclass
class ModelConfig:
    default_model: str = "mock"
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    anthropic_api_key: str = ""
    ollama_base_url: str = "http://127.0.0.1:11434"


@dataclasses.dataclass
class SkillConfig:
    local_dir: str = "skills/builtin"
    github_repo: str = ""
    github_ref: str = "main"
    github_cache_dir: str = ".skill_cache"
    allowlist: list = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class RunnerConfig:
    db_path: str = "data/task_registry.db"
    channel: ChannelConfig = dataclasses.field(default_factory=ChannelConfig)
    model: ModelConfig = dataclasses.field(default_factory=ModelConfig)
    skill: SkillConfig = dataclasses.field(default_factory=SkillConfig)
    heartbeat_interval: float = 30.0
    runner_id: Optional[str] = None


def load_config(path: Optional[str] = None) -> RunnerConfig:
    cfg = RunnerConfig()

    # 1) 环境变量覆盖
    ch, md, sk = cfg.channel, cfg.model, cfg.skill
    ch.name = _env("CHANNEL", ch.name)
    ch.mock_interval = float(_env("MOCK_INTERVAL", ch.mock_interval))
    ch.wecom_token = _env("WECHAT_TOKEN", ch.wecom_token)
    ch.wecom_aes_key = _env("WECHAT_AES_KEY", ch.wecom_aes_key)
    ch.wecom_corp_id = _env("WECHAT_CORP_ID", ch.wecom_corp_id)
    ch.wecom_agent_id = _env("WECHAT_AGENT_ID", ch.wecom_agent_id)
    ch.wecom_secret = _env("WECHAT_SECRET", ch.wecom_secret)
    ch.wecom_callback_port = int(_env("WECHAT_CALLBACK_PORT", ch.wecom_callback_port))
    ch.feishu_app_id = _env("FEISHU_APP_ID", ch.feishu_app_id)
    ch.feishu_app_secret = _env("FEISHU_APP_SECRET", ch.feishu_app_secret)
    ch.feishu_verification_token = _env("FEISHU_VERIFICATION_TOKEN", ch.feishu_verification_token)
    ch.feishu_encrypt_key = _env("FEISHU_ENCRYPT_KEY", ch.feishu_encrypt_key)
    ch.feishu_callback_port = int(_env("FEISHU_CALLBACK_PORT", ch.feishu_callback_port))

    md.default_model = _env("DEFAULT_MODEL", md.default_model)
    md.openai_api_key = _env("OPENAI_API_KEY", md.openai_api_key)
    md.openai_base_url = _env("OPENAI_BASE_URL", md.openai_base_url)
    md.anthropic_api_key = _env("ANTHROPIC_API_KEY", md.anthropic_api_key)
    md.ollama_base_url = _env("OLLAMA_BASE_URL", md.ollama_base_url)

    sk.local_dir = _env("LOCAL_SKILLS_DIR", sk.local_dir)
    sk.github_repo = _env("GITHUB_SKILLS_REPO", sk.github_repo)
    sk.github_ref = _env("GITHUB_SKILLS_REF", sk.github_ref)
    sk.github_cache_dir = _env("GITHUB_SKILLS_CACHE", sk.github_cache_dir)
    if _env("SKILLS_ALLOWLIST"):
        sk.allowlist = [x.strip() for x in _env("SKILLS_ALLOWLIST").split(",") if x.strip()]

    cfg.db_path = _env("DB_PATH", cfg.db_path)
    cfg.heartbeat_interval = float(_env("HEARTBEAT_INTERVAL", cfg.heartbeat_interval))

    # 2) 可选 JSON 文件再覆盖（最高优先级）
    if path and os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        # 简单逐层合并（保持 dataclass 结构）
        for key in ("db_path", "heartbeat_interval", "runner_id"):
            if key in data:
                setattr(cfg, key, data[key])
        for sub, subcls in (("channel", ChannelConfig), ("model", ModelConfig), ("skill", SkillConfig)):
            if sub in data and isinstance(data[sub], dict):
                cur = getattr(cfg, sub)
                for k, v in data[sub].items():
                    if hasattr(cur, k):
                        setattr(cur, k, v)

    return cfg
