"""
model_providers.py —— 预置「供应商目录」（provider catalog）。

为什么需要它：
    用户在对话框里加模型时，真正费劲的不是填 API Key，而是**填对 Base URL**：
    同一家厂商的「包月（Coding Plan）」和「按量计费」是**两个不同的端点**，
    Key 与端点必须配套，填错轻则 401，重则**按 token 扣费**（钱直接烧掉）。
    让用户凭记忆手填 base_url 是不负责任的。

    所以本模块把已核实的真实端点预置成一份目录：用户从清单里**勾选**，
    Base URL 自动填好、协议自动定、计费属性与合规提示一并呈现。

设计要点（改动时不可破坏）：
    - **只写已核实的端点**。没核实过的 base_url 一律不进目录，宁可少列也不编造。
    - **包月与按量分行**：billing 字段区分 "subscription" / "payg"，
      同厂商分两条，因为 base_url 完全不同。
    - **协议字段 protocol** 映射到 models/ 包的能力层：
      "openai"（OpenAI 兼容） / "anthropic" / "ollama"（本地）。
    - **合规提示**：腾讯云 Token Plan 订阅条款明令禁止用于自动化脚本/自定义应用后端，
      本产品是自动化审计流水线 —— 该提示必须随目录一起送到用户眼前，不能藏。
    - **免费额度分组**：各厂商免费层是"有上限的 $0"，不是"无限免费"。额度数字与
      「是否需信用卡」「数据是否用于训练」必须写进 note / compliance，不得含糊。

数据来源：本仓库 v1.2 期间的联网核实结果（见 README「模型接入」章节）。
免费额度分组的端点与限额核对于 2026-10-27（厂商文档口径，额度按月变动，以官网为准）。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional

# 目录分组（顺序即前端展示顺序；免费额度排最前，方便无密钥用户起步）
GROUP_FREE = "免费额度"
GROUP_SUBSCRIPTION = "Coding Plan（包月）"
GROUP_PAYG = "按量计费"
GROUP_INTL = "国际版"
GROUP_LOCAL = "本地"

BILLING_FREE = "free"
BILLING_SUBSCRIPTION = "subscription"
BILLING_PAYG = "payg"
BILLING_LOCAL = "local"

GROUP_ORDER = [
    GROUP_FREE,
    GROUP_SUBSCRIPTION,
    GROUP_PAYG,
    GROUP_INTL,
    GROUP_LOCAL,
]


@dataclass
class ProviderEntry:
    id: str
    name: str
    group: str
    protocol: str            # openai | anthropic | ollama
    base_url: str
    billing: str             # subscription | payg | local
    models: List[str] = field(default_factory=list)
    doc_url: str = ""
    key_url: str = ""
    note: str = ""
    compliance: str = ""
    featured: bool = False

    def to_public(self) -> Dict[str, object]:
        d = asdict(self)
        d["billing_label"] = self.billing_label
        d["label"] = "%s · %s" % (self.name, self.billing_label)
        return d

    @property
    def billing_label(self) -> str:
        return {
            BILLING_FREE: "免费额度",
            BILLING_SUBSCRIPTION: "包月",
            BILLING_PAYG: "按量计费",
            BILLING_LOCAL: "本地",
        }.get(self.billing, self.billing)

    @property
    def is_free_tier(self) -> bool:
        """免费层＝额度内 $0，但**有硬上限**，UI 需据此提示。"""
        return self.billing == BILLING_FREE


def _e(*args, **kwargs) -> ProviderEntry:
    return ProviderEntry(*args, **kwargs)


# --------------------------------------------------------------------------
# 目录正文（端点均已联网核实；未核实的一律不写）
# --------------------------------------------------------------------------
PROVIDERS: List[ProviderEntry] = [
    # ---------------- 免费额度（有硬上限的 $0）----------------
    _e(
        id="groq-free",
        name="Groq（免费层）",
        group=GROUP_FREE,
        protocol="openai",
        base_url="https://api.groq.com/openai/v1",
        billing=BILLING_FREE,
        models=["openai/gpt-oss-120b", "openai/gpt-oss-20b", "qwen/qwen3.8-27b"],
        doc_url="https://console.groq.com/docs/rate-limits",
        key_url="https://console.groq.com/keys",
        note="免费层：30 次/分钟、1000 次/天、8K token/分钟、20 万 token/天"
             "（以 gpt-oss-120b 计）。无需信用卡，注册即得 Key。仅开放权重模型。",
        compliance="免费层面向开发验证，非生产 SLA；8K token/分钟 是最先撞到的天花板。",
        featured=True,
    ),
    _e(
        id="gemini-free",
        name="Google Gemini API（AI Studio 免费层）",
        group=GROUP_FREE,
        protocol="openai",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        billing=BILLING_FREE,
        models=["gemini-3.8-flash", "gemini-3.6-flash", "gemini-3.5-flash-lite",
                "gemini-2.5-flash"],
        doc_url="https://ai.google.dev/gemini-api/docs/pricing",
        key_url="https://aistudio.google.com/apikey",
        note="Flash 与 Flash-Lite 系列免费；2.5 Pro 有少量免费额度。"
             "Gemini 4 Argon、图像与 Veo 模型不在免费层。端点走 OpenAI 兼容路径。",
        compliance="Google 条款明示：免费层提交的内容可能用于改进其产品，并可能被人工"
                    "审阅。涉密/客户数据请勿走免费层。",
        featured=True,
    ),
    _e(
        id="openrouter-free",
        name="OpenRouter（:free 免费模型）",
        group=GROUP_FREE,
        protocol="openai",
        base_url="https://openrouter.ai/api/v1",
        billing=BILLING_FREE,
        models=["openrouter/free", "google/gemma-4-31b-it:free",
                "nvidia/nemotron-3-super-120b-a12b:free"],
        doc_url="https://openrouter.ai/docs/api-reference/limits",
        key_url="https://openrouter.ai/keys",
        note="模型 ID 以 :free 结尾即 $0/token。限额 20 次/分钟、50 次/天；账号累计充值"
             "满 10 credits 后升到 1000 次/天。一个 Key 试遍多家开源模型，适合选型对比。",
        compliance="免费模型清单每周轮换；部分免费变体由会留存/训练数据的供应商托管。"
                    "账号余额为负时，连免费模型也会返回 402。",
        featured=True,
    ),
    _e(
        id="mistral-free",
        name="Mistral（Free mode）",
        group=GROUP_FREE,
        protocol="openai",
        base_url="https://api.mistral.ai/v1",
        billing=BILLING_FREE,
        models=["mistral-small-latest", "mistral-large-latest", "codestral-latest"],
        doc_url="https://docs.mistral.ai/deployment/laplateforme/overview/",
        key_url="https://console.mistral.ai/api-keys/",
        note="La Plateforme 的 Free mode：默认开放 API，无需信用卡。",
        compliance="Mistral 不公开具体限额数值，需在 Admin Panel → API → Limits 查看；"
                    "部分模型或用量可能需要绑定支付方式（文档列了 402 错误）。",
    ),
    _e(
        id="nvidia-free",
        name="NVIDIA NIM（build.nvidia.com）",
        group=GROUP_FREE,
        protocol="openai",
        base_url="https://integrate.api.nvidia.com/v1",
        billing=BILLING_FREE,
        models=["nvidia/nemotron-3-super-120b-a12b", "nvidia/nemotron-3-ultra-550b-a55b",
                "meta/llama-3.3-70b-instruct"],
        doc_url="https://build.nvidia.com/",
        key_url="https://build.nvidia.com/",
        note="加入 NVIDIA 开发者计划（免费）后，NIM 端点可免费用于原型验证，速率按模型不同。",
        compliance="官方定位为原型验证；生产使用需 NVIDIA AI Enterprise 授权。",
    ),
    _e(
        id="cohere-trial",
        name="Cohere（trial key）",
        group=GROUP_FREE,
        protocol="openai",
        base_url="https://api.cohere.ai/compatibility/v1",
        billing=BILLING_FREE,
        models=["command-a-plus-05-2026"],
        doc_url="https://docs.cohere.com/docs/rate-limits",
        key_url="https://dashboard.cohere.com/api-keys",
        note="注册即自动生成 trial key：每月 1000 次调用、20 次聊天/分钟，兼容 OpenAI SDK。",
        compliance="Cohere 明确禁止 trial key 用于生产或商业用途。",
    ),
    _e(
        id="sambanova-free",
        name="SambaNova Cloud（免费层）",
        group=GROUP_FREE,
        protocol="openai",
        base_url="https://api.sambanova.ai/v1",
        billing=BILLING_FREE,
        models=["Meta-Llama-3.3-70B-Instruct", "gpt-oss-120b", "DeepSeek-V3.2"],
        doc_url="https://docs.sambanova.ai/cloud/docs/get-started/overview",
        key_url="https://cloud.sambanova.ai/",
        note="账号未绑定支付方式即为免费层：每模型 20 次/分钟、20 次/天、20 万 token/天。",
        compliance="20 次/天只够跑通链路，不适合迭代开发或批量任务。",
    ),

    # ---------------- Coding Plan（包月）----------------
    _e(
        id="tencent-token-plan",
        name="腾讯云 Token Plan（通用）",
        group=GROUP_SUBSCRIPTION,
        protocol="openai",
        base_url="https://tokenhub.tencentmaas.com/plan/v3",
        billing=BILLING_SUBSCRIPTION,
        models=[
            "tc-code-latest", "hunyuan-2.0-instruct", "hunyuan-2.0-thinking",
            "hunyuan-t1", "hunyuan-turbos", "minimax-m2.5", "minimax-m2.7",
            "kimi-k2.5", "glm-5", "glm-5.1",
        ],
        doc_url="https://cloud.tencent.com/document/product/1772",
        key_url="https://cloud.tencent.com/product/tokenplan",
        note="腾讯云 Token Plan 通用/个人版，OpenAI 兼容协议。密钥形如 sk-tp-…",
        compliance="腾讯云 Token Plan 订阅条款明令禁止将额度用于自动化脚本、"
                    "自定义应用程序后端或非交互式批量调用。本产品是自动化审计流水线，"
                    "启用前请自行确认你的使用方式是否在授权范围内。",
        featured=True,
    ),
    _e(
        id="tencent-token-plan-anthropic",
        name="腾讯云 Token Plan（Anthropic 协议）",
        group=GROUP_SUBSCRIPTION,
        protocol="anthropic",
        base_url="https://tokenhub.tencentmaas.com/plan/anthropic",
        billing=BILLING_SUBSCRIPTION,
        models=["tc-code-latest", "hunyuan-2.0-thinking", "minimax-m2.5", "kimi-k2.5"],
        doc_url="https://cloud.tencent.com/document/product/1772",
        key_url="https://cloud.tencent.com/product/tokenplan",
        note="同一份 Token Plan 额度，Anthropic 协议端点（适合 Claude 风格调用）。",
        compliance="同「腾讯云 Token Plan（通用）」：禁止用于自动化脚本/自定义应用后端批量调用。",
    ),
    _e(
        id="tencent-token-plan-lkeap",
        name="腾讯云 Token Plan（LKEAP 接入点）",
        group=GROUP_SUBSCRIPTION,
        protocol="openai",
        base_url="https://api.lkeap.cloud.tencent.com/plan/v3",
        billing=BILLING_SUBSCRIPTION,
        models=["tc-code-latest", "hunyuan-2.0-instruct", "hunyuan-2.0-thinking"],
        doc_url="https://cloud.tencent.com/document/product/1772",
        key_url="https://cloud.tencent.com/product/tokenplan",
        note="经知识引擎 LKEAP 网关接入的 Token Plan 端点（企业内常见）。",
        compliance="同「腾讯云 Token Plan（通用）」：禁止用于自动化脚本/自定义应用后端批量调用。",
    ),
    _e(
        id="aliyun-coding-plan",
        name="阿里云百炼 Coding Plan",
        group=GROUP_SUBSCRIPTION,
        protocol="openai",
        base_url="https://coding.dashscope.aliyuncs.com/v1",
        billing=BILLING_SUBSCRIPTION,
        models=["qwen3-coder-plus", "qwen3-max", "qwen-plus", "qwen-turbo", "glm-4.7"],
        doc_url="https://help.aliyun.com/zh/model-studio/coding-plan",
        key_url="https://bailian.console.aliyun.com/",
        note="百炼「Coding Plan」包月专用端点。填成按量端点会按 token 计费。",
        compliance="包月额度仅可用于编码场景的交互式使用；自动化批处理请确认套餐条款。",
        featured=True,
    ),
    _e(
        id="zhipu-coding-plan",
        name="智谱 GLM Coding Plan",
        group=GROUP_SUBSCRIPTION,
        protocol="openai",
        base_url="https://open.bigmodel.cn/api/coding/paas/v4",
        billing=BILLING_SUBSCRIPTION,
        models=["GLM-4.7", "GLM-5", "glm-5.1", "GLM-5-Air"],
        doc_url="https://docs.bigmodel.cn/cn/coding-plan/overview",
        key_url="https://open.bigmodel.cn/",
        note="智谱 Coding Plan 包月端点（/api/coding/paas/v4）。",
        compliance="包月套餐不可用于按量端点；跨端点使用会按 token 扣费。",
    ),
    _e(
        id="kimi-coding-plan",
        name="Kimi Coding Plan",
        group=GROUP_SUBSCRIPTION,
        protocol="openai",
        base_url="https://api.kimi.com/coding/v1",
        billing=BILLING_SUBSCRIPTION,
        models=["kimi-k2.5", "kimi-k2-turbo-preview", "kimi-k2-thinking"],
        doc_url="https://platform.moonshot.cn/docs/guide/kimi-k2-quickstart",
        key_url="https://platform.moonshot.cn/",
        note="月之暗面 Kimi Coding Plan 包月端点。",
        compliance="包月套餐不可用于按量端点；跨端点使用会按 token 扣费。",
    ),
    _e(
        id="minimax-coding-plan",
        name="MiniMax 开放平台",
        group=GROUP_SUBSCRIPTION,
        protocol="openai",
        base_url="https://api.minimaxi.com/v1",
        billing=BILLING_SUBSCRIPTION,
        models=["MiniMax-M2.7", "MiniMax-M2.5", "minimax-m2.7", "minimax-m2.5"],
        doc_url="https://platform.minimaxi.com/document",
        key_url="https://platform.minimaxi.com/",
        note="MiniMax 国内站端点，OpenAI 兼容。",
        compliance="请确认所购套餐覆盖编程场景用量。",
    ),
    _e(
        id="volcengine-coding-plan",
        name="火山方舟 Coding Plan",
        group=GROUP_SUBSCRIPTION,
        protocol="openai",
        base_url="https://ark.cn-beijing.volces.com/api/coding/v3",
        billing=BILLING_SUBSCRIPTION,
        models=["doubao-seed-code", "doubao-seed-1-6", "kimi-k2.5", "glm-5"],
        doc_url="https://www.volcengine.com/docs/82379",
        key_url="https://console.volcengine.com/ark/region:ark+cn-beijing/apiKey",
        note="字节火山方舟编程套餐端点。",
        compliance="请确认所购套餐覆盖编程场景用量。",
    ),

    # ---------------- 按量计费 ----------------
    _e(
        id="aliyun-payg",
        name="阿里云百炼（按量）",
        group=GROUP_PAYG,
        protocol="openai",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        billing=BILLING_PAYG,
        models=["qwen3-max", "qwen-plus", "qwen-turbo", "qwen3-coder-plus"],
        doc_url="https://help.aliyun.com/zh/model-studio/compatibility-of-openai-with-dashscope",
        key_url="https://bailian.console.aliyun.com/",
        note="百炼按量（compatible-mode）端点。",
        compliance="按 token 计费，请自行设置额度告警。",
        featured=True,
    ),
    _e(
        id="deepseek",
        name="DeepSeek",
        group=GROUP_PAYG,
        protocol="openai",
        base_url="https://api.deepseek.com/v1",
        billing=BILLING_PAYG,
        models=["deepseek-chat", "deepseek-reasoner"],
        doc_url="https://api-docs.deepseek.com/",
        key_url="https://platform.deepseek.com/",
        note="DeepSeek 官方 OpenAI 兼容端点。",
        compliance="按 token 计费（推理模型更贵），请设置额度告警。",
        featured=True,
    ),
    _e(
        id="zhipu-payg",
        name="智谱 GLM（按量）",
        group=GROUP_PAYG,
        protocol="openai",
        base_url="https://open.bigmodel.cn/api/paas/v4",
        billing=BILLING_PAYG,
        models=["GLM-4.7", "GLM-5", "glm-5.1", "GLM-5-Air",
                "GLM-4.7-Flash", "GLM-4.6V-Flash", "GLM-4.5-Flash", "GLM-4-Flash"],
        doc_url="https://docs.bigmodel.cn/cn/guide/develop/openai/introduction",
        key_url="https://open.bigmodel.cn/",
        note="智谱按量端点（/api/paas/v4），与包月端点路径不同。"
             "Flash 系列（含 GLM-4.7-Flash 200K 上下文、GLM-4.6V-Flash 视觉）永久免费，"
             "手机号注册即可，无需信用卡 —— 想零成本起步优先选 Flash 模型名。",
        compliance="按 token 计费，请设置额度告警。",
    ),
    _e(
        id="moonshot",
        name="月之暗面 Kimi（按量）",
        group=GROUP_PAYG,
        protocol="openai",
        base_url="https://api.moonshot.cn/v1",
        billing=BILLING_PAYG,
        models=["kimi-k2.5", "moonshot-v1-128k", "kimi-k2-thinking"],
        doc_url="https://platform.moonshot.cn/docs/api/chat",
        key_url="https://platform.moonshot.cn/",
        note="Kimi 按量端点。",
        compliance="按 token 计费，请设置额度告警。",
    ),
    _e(
        id="siliconflow",
        name="硅基流动 SiliconFlow",
        group=GROUP_PAYG,
        protocol="openai",
        base_url="https://api.siliconflow.cn/v1",
        billing=BILLING_PAYG,
        models=["deepseek-ai/DeepSeek-V3", "Qwen/Qwen2.5-72B-Instruct", "moonshotai/Kimi-K2",
                "Qwen/Qwen3-8B", "deepseek-ai/DeepSeek-R1-Distill-Qwen-7B"],
        doc_url="https://docs.siliconflow.cn/",
        key_url="https://cloud.siliconflow.cn/",
        note="硅基流动聚合平台，一个 Key 调多家开源模型。"
             "Qwen/Qwen3-8B、DeepSeek-R1-Distill-Qwen-7B 等小模型永久免费（约 30 RPM / 60K TPM）；"
             "注册另送代金券。国内直连，零成本起步的稳妥选项。",
        compliance="按 token 计费，请设置额度告警。",
    ),
    _e(
        id="tencent-hunyuan",
        name="腾讯混元",
        group=GROUP_PAYG,
        protocol="openai",
        base_url="https://api.hunyuan.cloud.tencent.com/v1",
        billing=BILLING_PAYG,
        models=["hunyuan-turbos", "hunyuan-t1", "hunyuan-2.0-instruct", "hunyuan-2.0-thinking"],
        doc_url="https://cloud.tencent.com/document/product/1729",
        key_url="https://console.cloud.tencent.com/hunyuan",
        note="腾讯混元按量端点。",
        compliance="按 token 计费，请设置额度告警。",
    ),
    _e(
        id="openai",
        name="OpenAI",
        group=GROUP_PAYG,
        protocol="openai",
        base_url="https://api.openai.com/v1",
        billing=BILLING_PAYG,
        models=["gpt-4o-mini", "gpt-4o", "gpt-4.1", "o4-mini"],
        doc_url="https://platform.openai.com/docs/",
        key_url="https://platform.openai.com/api-keys",
        note="OpenAI 官方端点。",
        compliance="按 token 计费，请设置额度告警。",
        featured=True,
    ),
    _e(
        id="anthropic",
        name="Anthropic（Claude）",
        group=GROUP_PAYG,
        protocol="anthropic",
        base_url="https://api.anthropic.com",
        billing=BILLING_PAYG,
        models=["claude-3-5-sonnet-latest", "claude-3-5-haiku-latest", "claude-sonnet-4-20250514"],
        doc_url="https://docs.anthropic.com/",
        key_url="https://console.anthropic.com/settings/keys",
        note="Anthropic 原生协议端点。",
        compliance="按 token 计费，请设置额度告警。",
        featured=True,
    ),

    # ---------------- 国际版 ----------------
    _e(
        id="tencent-token-plan-intl",
        name="腾讯云 Token Plan（国际站）",
        group=GROUP_INTL,
        protocol="openai",
        base_url="https://tokenhub-intl.tencentcloudmaas.com/v1",
        billing=BILLING_SUBSCRIPTION,
        models=["tc-code-latest", "hunyuan-2.0-instruct", "minimax-m2.5", "kimi-k2.5"],
        doc_url="https://www.tencentcloud.com/document/product/1772",
        key_url="https://www.tencentcloud.com/product/tokenplan",
        note="国际站 Token Plan 端点，密钥与国际站账号绑定。",
        compliance="同「腾讯云 Token Plan（通用）」：禁止用于自动化脚本/自定义应用后端批量调用。",
    ),
    _e(
        id="zhipu-coding-plan-intl",
        name="智谱 GLM Coding Plan（国际站 z.ai）",
        group=GROUP_INTL,
        protocol="openai",
        base_url="https://open.z.ai/api/coding/paas/v4",
        billing=BILLING_SUBSCRIPTION,
        models=["GLM-4.7", "GLM-5", "glm-5.1"],
        doc_url="https://docs.z.ai/",
        key_url="https://z.ai/",
        note="智谱国际站 Coding Plan 端点。",
        compliance="包月套餐不可用于按量端点；跨端点使用会按 token 扣费。",
    ),
    _e(
        id="minimax-intl",
        name="MiniMax（国际站）",
        group=GROUP_INTL,
        protocol="openai",
        base_url="https://api.minimax.io/v1",
        billing=BILLING_PAYG,
        models=["MiniMax-M2.7", "minimax-m2.5"],
        doc_url="https://platform.minimax.io/document",
        key_url="https://platform.minimax.io/",
        note="MiniMax 国际站端点。",
        compliance="按 token 计费，请设置额度告警。",
    ),
    _e(
        id="moonshot-intl",
        name="月之暗面 Kimi（国际站）",
        group=GROUP_INTL,
        protocol="openai",
        base_url="https://api.moonshot.ai/v1",
        billing=BILLING_PAYG,
        models=["kimi-k2.5", "kimi-k2-thinking"],
        doc_url="https://platform.moonshot.ai/docs",
        key_url="https://platform.moonshot.ai/",
        note="Kimi 国际站端点。",
        compliance="按 token 计费，请设置额度告警。",
    ),

    # ---------------- 本地 ----------------
    _e(
        id="ollama-local",
        name="本地 Ollama",
        group=GROUP_LOCAL,
        protocol="ollama",
        base_url="http://127.0.0.1:11434",
        billing=BILLING_LOCAL,
        models=["llama3", "qwen2.5:7b", "gemma3", "deepseek-r1:7b"],
        doc_url="https://ollama.com/library",
        key_url="",
        note="本地推理，无需 API Key，也不产生任何费用。",
        compliance="数据不出本机。",
        featured=True,
    ),
]


# --------------------------------------------------------------------------
# 查询接口
# --------------------------------------------------------------------------
def list_providers() -> List[Dict[str, object]]:
    """返回目录（按分组顺序），供前端渲染供应商清单。"""
    out = [p.to_public() for p in PROVIDERS]
    order = {g: i for i, g in enumerate(GROUP_ORDER)}
    out.sort(key=lambda d: (order.get(d.get("group", ""), 99), d.get("name", "")))
    return out


def catalog() -> Dict[str, object]:
    """完整目录：分组 + 条目 + 全局提示。"""
    return {
        "groups": list(GROUP_ORDER),
        "providers": list_providers(),
        "notice": "Base URL 必须与所购计费方案配套：填成「按量计费」端点会按 token 扣费，"
                  "填成「包月」端点则用不了包月额度。目录中的端点均已核实。",
    }


def get(pid: str) -> Optional[ProviderEntry]:
    for p in PROVIDERS:
        if p.id == pid:
            return p
    return None


def by_protocol(protocol: str) -> List[ProviderEntry]:
    return [p for p in PROVIDERS if p.protocol == protocol]


def base_url_for(pid: str) -> str:
    p = get(pid)
    return p.base_url if p else ""


def validate_entry(entry: Dict[str, object]) -> Dict[str, object]:
    """校验前端从目录提交的配置，失败抛 ValueError（消息面向用户）。"""
    provider = str(entry.get("provider") or "")
    if provider not in ("ollama", "openai", "anthropic"):
        raise ValueError("未知的协议类型：%s" % provider)
    pid = str(entry.get("provider_id") or "")
    if pid:
        p = get(pid)
        if not p:
            raise ValueError("目录中不存在该供应商：%s" % pid)
        if p.protocol != provider:
            raise ValueError("协议与目录不一致：目录为 %s" % p.protocol)
    base_url = str(entry.get("base_url") or "").strip()
    if not base_url:
        raise ValueError("Base URL 不能为空")
    if not (base_url.startswith("http://") or base_url.startswith("https://")):
        raise ValueError("Base URL 必须以 http:// 或 https:// 开头")
    return {"provider": provider, "base_url": base_url}
