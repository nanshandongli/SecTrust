"""
结构化检测器库（scanner/detectors.py）—— 借鉴 gitleaks 的规则形态做"深度"。

为什么重写：
    原实现是 5 条散落的正则（"够用但不深"），与 gitleaks 默认规则集（200+ 形态）、
    TruffleHog（800+ 检测器）相比差 1~2 个数量级。本模块把规则升级为**结构化声明**：

        id / name / regex / keywords / entropy / secret_group / severity / tags / provider

    其中三个字段是"深度"的关键（来自 gitleaks v8 的规则设计）：
      · keywords   —— 正则前的廉价字符串预检。既是性能优化（大仓库不必逐条跑上百个
                      正则），也是正确性保证（避免纯熵规则把普通文本判成凭证）。
      · entropy    —— 对抽取出的 secret 组再做香农熵下限校验，压掉"形态对但明显是
                      填充值"的误报（如 AKIAIOSFODNN7EXAMPLE 的 secret 部分）。
      · secret_group —— 只把真正的凭证（而非整行）送去算熵、算指纹、做脱敏。

覆盖：云厂商（AWS/GCP/Azure/阿里云/腾讯云/华为云/百度云）、代码托管（GitHub/GitLab/
Bitbucket）、协作 IM（Slack/Discord/Telegram/企业微信/飞书/钉钉）、支付（Stripe/Square/
支付宝/微信支付）、AI 服务（OpenAI/Anthropic/HuggingFace/通义/文心）、CI/可观测
（npm/PyPI/Docker/JFrog/Grafana/Datadog/NewRelic）、以及私钥/JWT/数据库连接串与
PIPL/GDPR 关注的个人敏感信息（身份证/银行卡/手机号）。

⚠️ 检测器只做"形态 + 熵"判定，**不代表凭证有效**。是否仍存活由 validators.py 的
   活体验证回答（默认离线）；本模块产出的每条 finding 都带 confidence 与 reason，
   便于人/策略判断是否进一步验证。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

from .entropy import shannon_entropy, hit_stopword


# ---------------------------------------------------------------- 规则声明

_SEVERITIES = ("critical", "high", "medium", "low", "info")


@dataclass(frozen=True)
class Detector:
    """一条检测规则。字段语义对齐 gitleaks [[rules]]，并扩展 provider/tags。"""

    id: str
    name: str
    description: str
    regex: str
    keywords: Tuple[str, ...] = ()
    entropy: Optional[float] = None      # 对 secret 组的最低香农熵要求
    secret_group: int = 0                # 从匹配中抽取"真正的凭证"的分组号
    severity: str = "high"
    tags: Tuple[str, ...] = ()
    provider: str = ""                   # 归属厂商，便于按 provider 聚合/告警路由
    validator: Optional[str] = None      # 关联 validators.py 的活体验证器名
    stopwords: Tuple[str, ...] = ()      # 规则级抑制词（叠加在全局词表之上）

    def __post_init__(self):
        if self.severity not in _SEVERITIES:
            raise ValueError(f"{self.id}: severity 非法 {self.severity}")

    def compiled(self) -> "re.Pattern[str]":
        return _compile(self.regex)

    def matches_keyword(self, lower_text: str) -> bool:
        """keywords 预检：无 keywords 的规则恒过（如私钥头这类形态本身足够独特）。"""
        if not self.keywords:
            return True
        return any(k in lower_text for k in self.keywords)


_CACHE: Dict[str, "re.Pattern[str]"] = {}


def _compile(pat: str) -> "re.Pattern[str]":
    p = _CACHE.get(pat)
    if p is None:
        p = re.compile(pat)
        _CACHE[pat] = p
    return p


# ---------------------------------------------------------------- 规则集
# 说明：正则尽量使用"前缀锚定 + 字符集长度"的写法，与 gitleaks/trufflehog 一致；
#       带上下文赋值形态的规则用 secret_group 指向值。

DETECTORS: Tuple[Detector, ...] = (
    # ---------------- AWS ----------------
    Detector(
        id="aws-access-key-id", name="AWS Access Key ID",
        description="AWS 访问密钥 ID（AKIA/ASIA/ABIA/ACCA 前缀）",
        regex=r"\b((?:A3T[A-Z0-9]|AKIA|ASIA|ABIA|ACCA)[A-Z0-9]{16})\b",
        keywords=("akia", "asia", "abia", "acca", "a3t", "aws"),
        severity="high", tags=("cloud", "aws"), provider="aws",
        validator="aws",
    ),
    Detector(
        id="aws-secret-access-key", name="AWS Secret Access Key",
        description="AWS 秘密访问密钥（40 字符 base64，需上下文赋值）",
        regex=r"(?i)aws[_\-]?(?:secret[_\-]?)?access[_\-]?key[_\-]?(?:id)?[\"'`]?\s*[:=]\s*[\"'`]?"
              r"([A-Za-z0-9/+=]{40})[\"'`]?",
        keywords=("aws", "secret", "access_key", "accesskey"),
        entropy=3.0, secret_group=1,
        severity="critical", tags=("cloud", "aws"), provider="aws",
    ),
    Detector(
        id="aws-session-token", name="AWS Session Token",
        description="AWS 临时会话令牌",
        regex=r"(?i)\baws[_\-]?session[_\-]?token[\"'`]?\s*[:=]\s*[\"'`]([A-Za-z0-9/+=]{100,})[\"'`]",
        keywords=("aws", "session_token", "sessiontoken"),
        secret_group=1, severity="high", tags=("cloud", "aws"), provider="aws",
    ),

    # ---------------- GCP / Google ----------------
    Detector(
        id="gcp-api-key", name="GCP API Key",
        description="Google Cloud API 密钥（AIza 前缀 39 字符，排除 Gemini 的 AIzaSy 形态）",
        # (?!Sy) 避免与 gemini-api-key 重复命中同一串
        regex=r"\b(AIza(?!Sy)[0-9A-Za-z_\-]{35})\b",
        keywords=("aiza",), severity="high", tags=("cloud", "gcp"), provider="gcp",
    ),
    Detector(
        id="google-oauth-client-secret", name="Google OAuth Client Secret",
        description="Google OAuth 客户端密钥（GOCSPX-）",
        regex=r"\b(GOCSPX-[A-Za-z0-9_\-]{28})\b",
        keywords=("gocspx",), severity="high", tags=("cloud", "google"), provider="google",
    ),
    Detector(
        id="google-oauth-client-id", name="Google OAuth Client ID",
        description="Google OAuth 客户端 ID（*.apps.googleusercontent.com）",
        regex=r"\b([0-9]{6,}-[a-z0-9_]{32}\.apps\.googleusercontent\.com)\b",
        keywords=("apps.googleusercontent.com",), severity="low",
        tags=("cloud", "google"), provider="google",
    ),
    Detector(
        id="firebase-server-key", name="Firebase Server Key",
        description="Firebase Cloud Messaging 服务器密钥",
        regex=r"\b(AAAA[A-Za-z0-9_\-]{7}:[A-Za-z0-9_\-]{140})\b",
        keywords=("firebase", "fcm"), severity="high", tags=("cloud", "google"), provider="google",
    ),
    Detector(
        id="gcp-service-account-key", name="GCP Service Account Private Key",
        description="GCP 服务账号 JSON 中的私钥字段",
        regex=r"(?i)\"private_key\"\s*:\s*\"(-----BEGIN[^\"]{20,})",
        keywords=("private_key",), severity="critical",
        tags=("cloud", "gcp"), provider="gcp",
    ),

    # ---------------- Azure ----------------
    Detector(
        id="azure-storage-key", name="Azure Storage Account Key",
        description="Azure 存储账户访问密钥（base64 88 字符）",
        regex=r"(?i)(?:accountkey|sharedaccesskey|storage[_\-]?key)[\"'`]?\s*[:=]\s*"
              r"[\"'`]?([A-Za-z0-9+/=]{88})[\"'`]?",
        keywords=("accountkey", "sharedaccesskey", "storage_key"),
        entropy=3.5, secret_group=1, severity="critical",
        tags=("cloud", "azure"), provider="azure",
    ),
    Detector(
        id="azure-ad-client-secret", name="Azure AD Client Secret",
        description="Azure AD 应用客户端密钥",
        regex=r"(?i)client[_\-]?secret[\"'`]?\s*[:=]\s*[\"'`]?([A-Za-z0-9~\-_.]{32,40})[\"'`]?",
        keywords=("client_secret", "clientsecret", "azure"),
        entropy=3.0, secret_group=1, severity="high",
        tags=("cloud", "azure"), provider="azure",
    ),

    # ---------------- 国内云（差异化：gitleaks 主线覆盖较薄） ----------------
    Detector(
        id="alibaba-access-key-id", name="阿里云 AccessKey ID",
        description="阿里云 RAM 访问密钥 ID（LTAI 前缀）",
        regex=r"\b(LTAI[A-Za-z0-9]{12,20})\b",
        keywords=("ltai",), severity="high", tags=("cloud", "aliyun"), provider="aliyun",
    ),
    Detector(
        id="alibaba-secret-key", name="阿里云 AccessKey Secret",
        description="阿里云 RAM 访问密钥 Secret（30 字符，需上下文）",
        regex=r"(?i)(?:access[_\-]?key[_\-]?secret|aliyun[_\-]?secret)[\"'`]?\s*[:=]\s*"
              r"[\"'`]?([A-Za-z0-9]{30})[\"'`]?",
        keywords=("accesskeysecret", "access_key_secret", "aliyun"),
        entropy=3.0, secret_group=1, severity="critical",
        tags=("cloud", "aliyun"), provider="aliyun",
    ),
    Detector(
        id="tencent-secret-id", name="腾讯云 SecretId",
        description="腾讯云 CAM 密钥 SecretId（AKID 前缀 32 字符）",
        regex=r"\b(AKID[A-Za-z0-9]{32})\b",
        keywords=("akid",), severity="high", tags=("cloud", "tencent"), provider="tencent",
    ),
    Detector(
        id="huawei-access-key", name="华为云 Access Key",
        description="华为云 IAM 访问密钥 ID",
        regex=r"\b([A-Z0-9]{20})\b(?=[^\n]{0,80}huawei)" ,
        keywords=("huawei", "hwcloud"), severity="medium",
        tags=("cloud", "huawei"), provider="huawei",
    ),
    Detector(
        id="baidu-cloud-ak", name="百度云 Access Key",
        description="百度智能云安全认证 AK/SK",
        regex=r"\b(bce-v3/ALTAK-[A-Za-z0-9]{20}/[a-f0-9]{40})\b",
        keywords=("bce-v3",), severity="high", tags=("cloud", "baidu"), provider="baidu",
    ),

    # ---------------- 代码托管 ----------------
    Detector(
        id="github-pat-classic", name="GitHub Personal Access Token",
        description="GitHub 经典 PAT（ghp_ 前缀 36 字符）",
        regex=r"\b(ghp_[A-Za-z0-9]{36})\b",
        keywords=("ghp_",), severity="critical", tags=("vcs", "github"),
        provider="github", validator="github",
    ),
    Detector(
        id="github-pat-fine-grained", name="GitHub Fine-grained PAT",
        description="GitHub 细粒度 PAT（github_pat_ 前缀 82 字符）",
        regex=r"\b(github_pat_[A-Za-z0-9_]{82})\b",
        keywords=("github_pat_",), severity="critical", tags=("vcs", "github"),
        provider="github", validator="github",
    ),
    Detector(
        id="github-oauth-token", name="GitHub OAuth Token",
        description="GitHub OAuth 访问令牌（gho_）",
        regex=r"\b(gho_[A-Za-z0-9]{36})\b",
        keywords=("gho_",), severity="high", tags=("vcs", "github"), provider="github",
    ),
    Detector(
        id="github-server-token", name="GitHub App / Server Token",
        description="GitHub App 用户-服务器令牌（ghu_/ghs_）",
        regex=r"\b(gh[u s]_[A-Za-z0-9]{36}|ghu_[A-Za-z0-9]{36}|ghs_[A-Za-z0-9]{36})\b",
        keywords=("ghu_", "ghs_"), severity="high", tags=("vcs", "github"), provider="github",
    ),
    Detector(
        id="github-refresh-token", name="GitHub Refresh Token",
        description="GitHub 刷新令牌（ghr_ 前缀 76 字符）",
        regex=r"\b(ghr_[A-Za-z0-9]{76})\b",
        keywords=("ghr_",), severity="high", tags=("vcs", "github"), provider="github",
    ),
    Detector(
        id="gitlab-pat", name="GitLab Personal Access Token",
        description="GitLab PAT（glpat- 前缀）",
        regex=r"\b(glpat-[A-Za-z0-9_\-]{20,})\b",
        keywords=("glpat-",), severity="critical", tags=("vcs", "gitlab"), provider="gitlab",
    ),
    Detector(
        id="gitlab-runner-token", name="GitLab Runner Registration Token",
        description="GitLab Runner 注册令牌（GR1348941 前缀）",
        regex=r"\b(GR1348941[A-Za-z0-9_\-]{20})\b",
        keywords=("gr1348941",), severity="high", tags=("vcs", "gitlab"), provider="gitlab",
    ),
    Detector(
        id="bitbucket-app-password", name="Bitbucket App Password",
        description="Bitbucket 应用密码（ATBB 前缀）",
        regex=r"\b(ATBB[A-Za-z0-9]{32})\b",
        keywords=("atbb",), severity="high", tags=("vcs", "bitbucket"), provider="atlassian",
    ),
    Detector(
        id="gitea-token", name="Gitea / Gogs Access Token",
        description="Gitea/Gogs 访问令牌（40 位十六进制，需上下文）",
        regex=r"(?i)(?:gitea|gogs)[_\-]?token[\"'`]?\s*[:=]\s*[\"'`]?([a-f0-9]{40})[\"'`]?",
        keywords=("gitea", "gogs"), secret_group=1, severity="high",
        tags=("vcs",), provider="gitea",
    ),

    # ---------------- 协作 / IM ----------------
    Detector(
        id="slack-bot-token", name="Slack Bot Token",
        description="Slack 机器人令牌（xoxb-）",
        regex=r"\b(xoxb-[0-9]{10,13}-[0-9]{10,13}-[A-Za-z0-9]{24,})\b",
        keywords=("xoxb-",), severity="critical", tags=("im", "slack"), provider="slack",
    ),
    Detector(
        id="slack-user-token", name="Slack User Token",
        description="Slack 用户令牌（xoxp-）",
        regex=r"\b(xoxp-[0-9]{10,13}-[0-9]{10,13}-[A-Za-z0-9]{24,})\b",
        keywords=("xoxp-",), severity="critical", tags=("im", "slack"), provider="slack",
    ),
    Detector(
        id="slack-app-token", name="Slack App-level Token",
        description="Slack 应用级令牌（xapp-）",
        regex=r"\b(xapp-1-[A-Za-z0-9]{8,13}-[0-9]{10,13}-[a-f0-9]{64})\b",
        keywords=("xapp-",), severity="high", tags=("im", "slack"), provider="slack",
    ),
    Detector(
        id="slack-webhook", name="Slack Webhook URL",
        description="Slack 传入 Webhook（本身即为凭证）",
        regex=r"(https://hooks\.slack\.com/services/T[A-Za-z0-9_/+-]{20,})",
        keywords=("hooks.slack.com",), severity="high", tags=("im", "slack"), provider="slack",
    ),
    Detector(
        id="discord-bot-token", name="Discord Bot Token",
        description="Discord 机器人令牌",
        regex=r"\b([MN][A-Za-z0-9]{23}\.[A-Za-z0-9_\-]{6}\.[A-Za-z0-9_\-]{27})\b",
        keywords=("discord",), severity="high", tags=("im", "discord"), provider="discord",
    ),
    Detector(
        id="discord-webhook", name="Discord Webhook URL",
        description="Discord Webhook（含 id/token）",
        regex=r"(https://discord(?:app)?\.com/api/webhooks/[0-9]{10,}/[A-Za-z0-9_\-]{60,})",
        keywords=("discord",), severity="medium", tags=("im", "discord"), provider="discord",
    ),
    Detector(
        id="telegram-bot-token", name="Telegram Bot Token",
        description="Telegram 机器人令牌",
        regex=r"\b([0-9]{8,10}:AA[A-Za-z0-9_\-]{33})\b",
        keywords=("telegram",), severity="high", tags=("im", "telegram"), provider="telegram",
    ),
    Detector(
        id="wecom-corp-secret", name="企业微信 Corp Secret",
        description="企业微信自建应用 Secret（需上下文）",
        regex=r"(?i)(?:corpsecret| corp[_\-]?secret)[\"'`]?\s*[:=]\s*[\"'`]?([A-Za-z0-9_\-]{43})[\"'`]?",
        keywords=("corpsecret", "corp_secret", "qywx"), secret_group=1,
        severity="high", tags=("im", "wecom"), provider="wecom",
    ),
    Detector(
        id="feishu-app-secret", name="飞书 App Secret",
        description="飞书开放平台应用密钥（需上下文）",
        regex=r"(?i)app[_\-]?secret[\"'`]?\s*[:=]\s*[\"'`]?([A-Za-z0-9]{32})[\"'`]?",
        keywords=("app_secret", "appsecret", "feishu", "lark"),
        entropy=3.0, secret_group=1, severity="high", tags=("im", "feishu"), provider="feishu",
    ),
    Detector(
        id="dingtalk-app-secret", name="钉钉 App Secret",
        description="钉钉开放平台应用密钥（60 字符，需上下文）",
        regex=r"(?i)(?:dingtalk|dingding)[_\-]?(?:app)?[_\-]?secret[\"'`]?\s*[:=]\s*"
              r"[\"'`]?([A-Za-z0-9_\-]{60})[\"'`]?",
        keywords=("dingtalk", "dingding"), secret_group=1,
        severity="high", tags=("im", "dingtalk"), provider="dingtalk",
    ),

    # ---------------- 支付 ----------------
    Detector(
        id="stripe-secret-key", name="Stripe Secret Key",
        description="Stripe 秘密密钥（sk_live_/rk_live_）",
        regex=r"\b((?:sk|rk)_(?:live|test)_[0-9a-zA-Z]{24,})\b",
        keywords=("sk_live", "sk_test", "rk_live", "stripe"),
        severity="critical", tags=("payment", "stripe"), provider="stripe",
    ),
    Detector(
        id="square-access-token", name="Square Access Token",
        description="Square 访问令牌（sq0atp-/sq0csp-）",
        regex=r"\b(sq0(?:atp|csp)-[0-9A-Za-z_\-]{22,43})\b",
        keywords=("sq0atp", "sq0csp", "square"), severity="critical",
        tags=("payment", "square"), provider="square",
    ),
    Detector(
        id="paypal-client-secret", name="PayPal Client Secret",
        description="PayPal OAuth 客户端密钥",
        regex=r"(?i)paypal[_\-]?(?:client[_\-]?)?secret[\"'`]?\s*[:=]\s*[\"'`]?([A-Za-z0-9_\-]{40,})[\"'`]?",
        keywords=("paypal",), secret_group=1, severity="critical",
        tags=("payment", "paypal"), provider="paypal",
    ),
    Detector(
        id="alipay-private-key", name="支付宝商户私钥",
        description="支付宝开放平台商户应用私钥",
        regex=r"(?i)(?:alipay)[^\n]{0,40}(-----BEGIN (?:RSA )?PRIVATE KEY-----)",
        keywords=("alipay",), severity="critical", tags=("payment", "alipay"), provider="alipay",
    ),
    Detector(
        id="wxpay-api-v3-key", name="微信支付 APIv3 密钥",
        description="微信支付 API v3 密钥（32 字符，需上下文）",
        regex=r"(?i)(?:apiv3[_\-]?key|api[_\-]?v3[_\-]?(?:secret|key))[\"'`]?\s*[:=]\s*"
              r"[\"'`]?([A-Za-z0-9]{32})[\"'`]?",
        keywords=("apiv3", "apiv3key", "wxpay"), entropy=3.0, secret_group=1,
        severity="critical", tags=("payment", "wechat"), provider="wechat",
    ),

    # ---------------- AI 服务（2026 新增面） ----------------
    Detector(
        id="openai-api-key", name="OpenAI API Key",
        description="OpenAI API 密钥（sk-/sk-proj-/sk-svcacct-）",
        regex=r"\b(sk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_\-]{20,}T3BlbkFJ[A-Za-z0-9_\-]{20,}"
              r"|sk-(?:proj-|svcacct-)[A-Za-z0-9_\-]{40,})\b",
        keywords=("sk-proj", "sk-svcacct", "openai", "t3blbkfj"),
        severity="critical", tags=("ai", "openai"), provider="openai",
    ),
    Detector(
        id="anthropic-api-key", name="Anthropic API Key",
        description="Anthropic Claude API 密钥（sk-ant-）",
        regex=r"\b(sk-ant-[A-Za-z0-9_\-]{40,})\b",
        keywords=("sk-ant-", "anthropic"), severity="critical",
        tags=("ai", "anthropic"), provider="anthropic",
    ),
    Detector(
        id="huggingface-token", name="HuggingFace Access Token",
        description="HuggingFace 访问令牌（hf_ 前缀）",
        regex=r"\b(hf_[A-Za-z0-9]{34,})\b",
        keywords=("hf_",), severity="high", tags=("ai", "huggingface"), provider="huggingface",
    ),
    Detector(
        id="dashscope-api-key", name="通义千问 DashScope API Key",
        description="阿里云百炼/DashScope API Key（sk- 开头 32+，需上下文）",
        regex=r"(?i)(?:dashscope|qwen|bailian)[_\-]?api[_\-]?key[\"'`]?\s*[:=]\s*"
              r"[\"'`]?(sk-[A-Za-z0-9]{32,})[\"'`]?",
        keywords=("dashscope", "qwen", "bailian"), secret_group=1,
        severity="high", tags=("ai", "aliyun"), provider="aliyun",
    ),
    Detector(
        id="gemini-api-key", name="Google Gemini API Key",
        description="Google Generative Language API Key",
        regex=r"\b(AIzaSy[A-Za-z0-9_\-]{33})\b",
        keywords=("aizasy",), severity="high", tags=("ai", "google"), provider="google",
    ),
    Detector(
        id="replicate-token", name="Replicate API Token",
        description="Replicate 平台 API 令牌（r8_ 前缀）",
        regex=r"\b(r8_[A-Za-z0-9]{40})\b",
        keywords=("r8_",), severity="high", tags=("ai", "replicate"), provider="replicate",
    ),
    Detector(
        id="cohere-api-key", name="Cohere API Key",
        description="Cohere 平台 API 密钥",
        regex=r"\b([A-Za-z0-9]{40})\b(?=[\"'`]?[\s,]*(?:cohere))",
        keywords=("cohere",), entropy=3.5, severity="medium",
        tags=("ai", "cohere"), provider="cohere",
    ),

    # ---------------- 包管理 / 制品库 / CI ----------------
    Detector(
        id="npm-access-token", name="npm Access Token",
        description="npm registry 访问令牌（npm_ 前缀 36 字符）",
        regex=r"\b(npm_[A-Za-z0-9]{36})\b",
        keywords=("npm_",), severity="critical", tags=("registry", "npm"), provider="npm",
    ),
    Detector(
        id="pypi-upload-token", name="PyPI Upload Token",
        description="PyPI 上传令牌（pypi- 前缀）",
        regex=r"\b(pypi-[A-Za-z0-9_\-]{100,})\b",
        keywords=("pypi-",), severity="critical", tags=("registry", "pypi"), provider="pypi",
    ),
    Detector(
        id="docker-hub-pat", name="Docker Hub Personal Access Token",
        description="Docker Hub PAT（dckr_pat_ 前缀）",
        regex=r"\b(dckr_pat_[A-Za-z0-9_\-]{27,})\b",
        keywords=("dckr_pat_",), severity="high", tags=("registry", "docker"), provider="docker",
    ),
    Detector(
        id="jfrog-identity-token", name="JFrog Identity Token",
        description="JFrog Artifactory 身份令牌",
        regex=r"(?i)(?:jfrog|artifactory)[_\-]?(?:identity[_\-]?)?token[\"'`]?\s*[:=]\s*"
              r"[\"'`]?([A-Za-z0-9_\-]{64,})[\"'`]?",
        keywords=("jfrog", "artifactory"), secret_group=1, severity="high",
        tags=("registry", "jfrog"), provider="jfrog",
    ),
    Detector(
        id="github-actions-untrusted-checkout", name="Untrusted PR Checkout in Workflow",
        description="pull_request_target 下 checkout PR head 并赋予写权限（供应链风险）",
        regex=r"(?i)pull_request_target[\s\S]{0,400}?(?:ref|REF)\s*:\s*"
              r"\$\{\{\s*github\.event\.pull_request\.head\.ref\s*\}\}",
        keywords=("pull_request_target",), severity="high",
        tags=("ci", "supply-chain"), provider="github",
    ),

    # ---------------- 可观测 / 基础设施 ----------------
    Detector(
        id="datadog-api-key", name="Datadog API Key",
        description="Datadog API/APP 密钥（32 位十六进制，需上下文）",
        regex=r"(?i)(?:datadog|dd)[_\-]?(?:api|app)[_\-]?key[\"'`]?\s*[:=]\s*[\"'`]?([a-f0-9]{32})[\"'`]?",
        keywords=("datadog",), secret_group=1, severity="high",
        tags=("observability", "datadog"), provider="datadog",
    ),
    Detector(
        id="new-relic-key", name="New Relic License / User Key",
        description="New Relic 许可或用户密钥（NRAK-/NRAL- 前缀）",
        regex=r"\b(NR(?:AK|AL|AA|JS)[A-Za-z0-9_\-]{27,})\b",
        keywords=("nrak-", "nral-", "newrelic"), severity="high",
        tags=("observability", "newrelic"), provider="newrelic",
    ),
    Detector(
        id="grafana-service-account-token", name="Grafana Service Account Token",
        description="Grafana 服务账号令牌（glsa_ 前缀）",
        regex=r"\b(glsa_[A-Za-z0-9]{32}_[A-Fa-f0-9]{8})\b",
        keywords=("glsa_",), severity="high", tags=("observability", "grafana"), provider="grafana",
    ),
    Detector(
        id="grafana-api-key", name="Grafana API Key",
        description="Grafana 旧式 API Key（eyJrIjoi 前缀 JWT）",
        regex=r"\b(eyJrIjoi[A-Za-z0-9_\-]{20,})\b",
        keywords=("eyjrIjoi", "grafana"), severity="high",
        tags=("observability", "grafana"), provider="grafana",
    ),
    Detector(
        id="sentry-auth-token", name="Sentry Auth Token",
        description="Sentry 认证令牌（sntrys_ 前缀或 64 位十六进制）",
        regex=r"(?i)(?:sentry[_\-]?(?:auth[_\-]?)?token)[\"'`]?\s*[:=]\s*[\"'`]?"
              r"((?:sntrys_[A-Za-z0-9_\-]{32,})|[a-f0-9]{64})[\"'`]?",
        keywords=("sentry", "sntrys_"), secret_group=1, severity="high",
        tags=("observability", "sentry"), provider="sentry",
    ),
    Detector(
        id="kubeconfig-secret", name="Kubernetes Secret / Token",
        description="kubeconfig 或 SA token 字段",
        regex=r"(?i)(?:token|client-key-data|password)[\"'`]?\s*:\s*[\"'`]?"
              r"([A-Za-z0-9+/=_\-]{32,})[\"'`]?",
        keywords=("kubeconfig", "client-key-data", "current-context"),
        entropy=3.5, secret_group=1, severity="high",
        tags=("infra", "k8s"), provider="kubernetes",
    ),

    # ---------------- 通信 / SaaS ----------------
    Detector(
        id="sendgrid-api-key", name="SendGrid API Key",
        description="SendGrid API 密钥（SG. 两段式）",
        regex=r"\b(SG\.[A-Za-z0-9_\-]{22}\.[A-Za-z0-9_\-]{43})\b",
        keywords=("sg.",), severity="high", tags=("email", "sendgrid"), provider="sendgrid",
    ),
    Detector(
        id="mailgun-api-key", name="Mailgun API Key",
        description="Mailgun 私钥（key- 前缀 32 位十六进制）",
        regex=r"\b(key-[0-9a-zA-Z]{32})\b",
        keywords=("key-", "mailgun"), severity="high", tags=("email", "mailgun"), provider="mailgun",
    ),
    Detector(
        id="twilio-api-key", name="Twilio API Key / Account SID",
        description="Twilio API Key（SK 前缀）或 Account SID（AC 前缀）",
        regex=r"\b((?:SK[0-9a-fA-F]{32}|AC[a-fA-F0-9]{32}))\b",
        keywords=("twilio", "sk", "ac"), severity="high",
        tags=("communication", "twilio"), provider="twilio",
    ),
    Detector(
        id="shopify-access-token", name="Shopify Access Token",
        description="Shopify 管理 API 访问令牌（shpat_/shpss_/shpca_）",
        regex=r"\b(shp(?:at|ss|ca|pa)_[a-fA-F0-9]{32})\b",
        keywords=("shpat_", "shpss_", "shpca_", "shopify"),
        severity="critical", tags=("saas", "shopify"), provider="shopify",
    ),
    Detector(
        id="airtable-api-key", name="Airtable API Key",
        description="Airtable API 密钥（key 前缀 17 字符）",
        regex=r"\b(key[A-Za-z0-9]{17})\b",
        keywords=("airtable",), severity="high", tags=("saas", "airtable"), provider="airtable",
    ),
    Detector(
        id="asana-pat", name="Asana Personal Access Token",
        description="Asana PAT（32 位，需上下文）",
        regex=r"(?i)asana[_\-]?(?:pat|token)[\"'`]?\s*[:=]\s*[\"'`]?([0-9]{1,5}/[A-Za-z0-9]{32})[\"'`]?",
        keywords=("asana",), secret_group=1, severity="medium",
        tags=("saas", "asana"), provider="asana",
    ),
    Detector(
        id="cloudflare-api-key", name="Cloudflare API Key / Token",
        description="Cloudflare Global API Key 或 API Token",
        regex=r"(?i)(?:cloudflare|cf)[_\-]?api[_\-]?(?:key|token)[\"'`]?\s*[:=]\s*"
              r"[\"'`]?([A-Za-z0-9_\-]{37,40})[\"'`]?",
        keywords=("cloudflare",), secret_group=1, severity="high",
        tags=("infra", "cloudflare"), provider="cloudflare",
    ),
    Detector(
        id="digitalocean-token", name="DigitalOcean Access Token",
        description="DigitalOcean 个人访问令牌（dop_v1_ 或 64 位十六进制）",
        regex=r"\b(dop_v1_[a-f0-9]{64}|[a-f0-9]{64})\b",
        keywords=("dop_v1_", "digitalocean"), entropy=3.5, severity="high",
        tags=("cloud", "digitalocean"), provider="digitalocean",
    ),
    Detector(
        id="heroku-api-key", name="Heroku API Key",
        description="Heroku API 密钥（UUID 形态，需上下文）",
        regex=r"(?i)heroku[_\-]?api[_\-]?key[\"'`]?\s*[:=]\s*[\"'`]?"
              r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})[\"'`]?",
        keywords=("heroku",), secret_group=1, severity="high",
        tags=("cloud", "heroku"), provider="heroku",
    ),
    Detector(
        id="sonarqube-token", name="SonarQube / SonarCloud Token",
        description="Sonar 服务令牌（squ_ 前缀 40 位十六进制）",
        regex=r"\b(squ_[0-9a-f]{40})\b",
        keywords=("squ_",), severity="high", tags=("ci", "sonar"), provider="sonarsource",
    ),
    Detector(
        id="vault-token", name="HashiCorp Vault Token",
        description="Vault 服务令牌（hvs. 前缀）",
        regex=r"\b(hvs\.[A-Za-z0-9]{24,}|s\.[A-Za-z0-9]{24})\b",
        keywords=("hvs.", "vault"), severity="critical",
        tags=("infra", "vault"), provider="hashicorp",
    ),

    # ---------------- 通用凭证形态 ----------------
    Detector(
        id="private-key-block", name="Private Key Block",
        description="PEM 私钥块（RSA/DSA/EC/OPENSSH/PGP）",
        regex=r"-----BEGIN (?:RSA |DSA |EC |OPENSSH |ENCRYPTED |PGP )?PRIVATE KEY(?: BLOCK)?-----",
        keywords=("-----begin",), severity="critical", tags=("crypto", "key"), provider="generic",
    ),
    Detector(
        id="jwt-token", name="JWT Token",
        description="三段式 JWT（签发的令牌本身即凭证）",
        regex=r"\b(ey[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,})\b",
        keywords=("eyj",), severity="medium", tags=("auth", "jwt"), provider="generic",
    ),
    Detector(
        id="basic-auth-in-url", name="Credentials in URL",
        description="URL 内嵌用户名密码（scheme://user:pass@host）",
        regex=r"\b([a-zA-Z][a-zA-Z0-9+.\-]{2,10}://[^\s:/@]{1,64}:[^\s:/@]{1,64}@[^\s/]{3,})",
        keywords=("://",), severity="high", tags=("auth", "url"), provider="generic",
    ),
    Detector(
        id="db-connection-string", name="Database Connection String with Credentials",
        description="含用户名密码的数据库连接串（postgres/mysql/mongodb/redis/amqp/jdbc）",
        regex=r"\b((?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqps?|mssql|jdbc:[a-z0-9]+)"
              r"://[^\s:@/]{1,64}:[^\s:@/]{1,128}@[^\s/]{3,})",
        keywords=("postgres", "mysql", "mongodb", "redis", "amqp", "jdbc"),
        severity="critical", tags=("database",), provider="generic",
    ),
    Detector(
        id="hardcoded-password", name="Hardcoded Password",
        description="硬编码口令（低熵也算——弱口令本身就是风险，Semgrep/gitleaks 均列为必检项）",
        # 值侧**不强制要求引号**：.env 里 `DB_PASSWORD=SuperSecretDbPass123` 无引号才是常态
        regex=r"(?i)(?:password|passwd|pwd|passphrase|db[_\-]?pass|admin[_\-]?pass|"
              r"密码|口令)[\"'`]?\s*[:=]\s*[\"'`]?([^\s`\"',;)]{6,64})",
        keywords=("password", "passwd", "pwd", "passphrase", "密码", "口令"),
        # 刻意不设熵门槛：hunter2 熵只有 2.6 但确实是硬编码口令；
        # 误报由 stopwords（example/changeme/test...）与行内 scanner:allow 兜住。
        secret_group=1, severity="medium",
        tags=("generic", "credential"), provider="generic",
    ),
    Detector(
        id="generic-api-key-assignment", name="Generic API Key Assignment",
        description="api_key/secret/password 之类的明文赋值（熵过滤）",
        regex=r"(?i)(?:api[_\-]?key|apikey|secret[_\-]?key|access[_\-]?token|auth[_\-]?token|"
              r"client[_\-]?secret|password|passwd|pwd|credential)[\"'`]?\s*[:=]\s*"
              r"[\"'`]([A-Za-z0-9_\-/+.]{16,128})[\"'`]",
        keywords=("api", "key", "secret", "token", "password", "passwd", "pwd",
                  "credential", "密钥", "密码", "口令", "令牌"),
        entropy=3.5, secret_group=1, severity="medium",
        tags=("generic",), provider="generic",
    ),

    # ---------------- 个人信息（PIPL / GDPR 合规面） ----------------
    Detector(
        id="cn-id-card", name="中国身份证号",
        description="18 位居民身份证号（PIPL 敏感个人信息）",
        regex=r"\b([1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx])\b",
        keywords=(), severity="medium", tags=("pii", "cn"), provider="pii",
    ),
    Detector(
        id="cn-bank-card", name="银行卡号",
        description="16~19 位银行卡号（金融账户信息）",
        regex=r"\b([1-9]\d{15,18})\b",
        keywords=("card", "bank", "银行卡", "卡号"), severity="medium",
        tags=("pii", "finance"), provider="pii",
    ),
    Detector(
        id="cn-mobile-phone", name="中国大陆手机号",
        description="11 位手机号（个人信息，批量出现风险更高）",
        regex=r"\b(1[3-9]\d{9})\b",
        keywords=("phone", "mobile", "手机", "电话"), severity="low",
        tags=("pii", "cn"), provider="pii",
    ),
    Detector(
        id="email-address", name="邮箱地址",
        description="邮箱（常见个人信息，低危但需登记）",
        regex=r"\b([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})\b",
        keywords=("@",), severity="info", tags=("pii",), provider="pii",
    ),
)


# ---------------------------------------------------------------- 索引与统计

BY_ID: Dict[str, Detector] = {d.id: d for d in DETECTORS}


def detector_stats() -> Dict[str, int]:
    """规则集画像：总数 / 按 severity / 按 provider，供自检与文档自动取值。"""
    by_sev: Dict[str, int] = {}
    by_prov: Dict[str, int] = {}
    for d in DETECTORS:
        by_sev[d.severity] = by_sev.get(d.severity, 0) + 1
        by_prov[d.provider or "generic"] = by_prov.get(d.provider or "generic", 0) + 1
    return {
        "total": len(DETECTORS),
        "by_severity": dict(sorted(by_sev.items())),
        "by_provider": dict(sorted(by_prov.items(), key=lambda kv: (-kv[1], kv[0]))),
        "with_entropy": sum(1 for d in DETECTORS if d.entropy is not None),
        "with_keywords": sum(1 for d in DETECTORS if d.keywords),
        "with_validator": sum(1 for d in DETECTORS if d.validator),
    }


def select(categories: Optional[Iterable[str]] = None,
           severities: Optional[Iterable[str]] = None,
           providers: Optional[Iterable[str]] = None) -> List[Detector]:
    """按 provider / severity / tag 子集选择规则（用于"只扫云凭证"这类场景）。"""
    out = list(DETECTORS)
    if categories:
        cats = set(categories)
        out = [d for d in out if cats & set(d.tags)]
    if providers:
        provs = set(providers)
        out = [d for d in out if d.provider in provs]
    if severities:
        sevs = set(severities)
        out = [d for d in out if d.severity in sevs]
    return out


# ---------------------------------------------------------------- 匹配

def _extract_secret(det: Detector, m: "re.Match[str]") -> str:
    """取出"真正的凭证"：secret_group 优先，越界则退回整匹配。"""
    if det.secret_group:
        if det.secret_group <= m.re.groups:
            val = m.group(det.secret_group)
            if val:
                return val
    return m.group(0)


def _passes_entropy(det: Detector, secret: str) -> Tuple[bool, float]:
    if det.entropy is None:
        return (True, round(shannon_entropy(secret), 3))
    h = shannon_entropy(secret)
    return (h >= det.entropy, round(h, 3))


def _suppress_hit(secret: str, line: str) -> Optional[str]:
    """判定是否应被抑制词拦下。比"整行扫抑制词"精确得多——

    教训：早期实现是 `hit_stopword(secret) or hit_stopword("", line)`（整行找抑制词），
    结果 `postgres://admin:xxx@db.internal.example:5432` 因**域名**含 example 被判为
    示例值而漏报（真实凭证被抑制 = 假阴性，比误报危险得多）。因此：
      · URL / 连接串形态（含 ://）——形态本身足够独特，不做抑制词判定；
      · 其余——只看 secret 值本身；
      · 仅当整行是注释（# 或 // 开头）时，才用整行文本判定（中文项目常写
        `# 示例密钥，仅用于演示` 而 secret 本身没有抑制词）。
    """
    if "://" in (secret or ""):
        return None
    sw = hit_stopword(secret)
    if sw:
        return sw
    stripped = (line or "").lstrip()
    if stripped.startswith("#") or stripped.startswith("//") or stripped.startswith("*"):
        return hit_stopword("", line)
    return None


def match_line(det: Detector, line: str, lower_line: str) -> List[Dict]:
    """对单行跑一条规则，返回 finding 列表（可能多个匹配）。"""
    if not det.matches_keyword(lower_line):
        return []
    out: List[Dict] = []
    for m in det.compiled().finditer(line):
        secret = _extract_secret(det, m)
        ok_h, h = _passes_entropy(det, secret)
        if not ok_h:
            continue
        sw = _suppress_hit(secret, line)
        out.append({
            "rule_id": det.id,
            "rule_name": det.name,
            "description": det.description,
            "severity": det.severity,
            "tags": list(det.tags),
            "provider": det.provider,
            "validator": det.validator,
            "secret": secret,
            "match": m.group(0)[:200],
            "start": m.start(),
            "entropy": h,
            "entropy_required": det.entropy,
            "suppressed_by": sw,          # 命中抑制词 ≠ 直接丢弃，交给策略层决定
        })
    return out


def find_in_text(text: str, only: Optional[List[Detector]] = None) -> List[Dict]:
    """对整段文本跑全部（或指定）规则。返回按行号升序的 findings。

    每次调用先做一次 lowercase 全文 keywords 预检（gitleaks 的性能技巧）：
    上百条正则不必逐行跑，先按关键词粗筛出"可能相关的规则"。
    """
    dets = only if only is not None else list(DETECTORS)
    lower_all = (text or "").lower()
    active = [d for d in dets if d.matches_keyword(lower_all)]
    lines = (text or "").splitlines()
    findings: List[Dict] = []
    for i, line in enumerate(lines, start=1):
        low = line.lower()
        for det in active:
            for f in match_line(det, line, low):
                f["line_no"] = i
                f["line"] = line.strip()[:300]
                findings.append(f)
    findings.sort(key=lambda f: (f["line_no"], f["start"]))
    return findings


if __name__ == "__main__":
    import json
    print(json.dumps(detector_stats(), ensure_ascii=False, indent=2))
    sample = '\n'.join([
        'AWS_KEY = "AKIAIOSFODNN7REALKEY"',
        'aws_secret_access_key = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYzEXAMPLEKEY"',
        'github_token = "ghp_012345678901234567890123456789abcdef"',
        'password = "hunter2hunter2"',
    ])
    for f in find_in_text(sample):
        print(f["line_no"], f["rule_id"], f["severity"], f["secret"][:12] + "...",
              "entropy=", f["entropy"])
