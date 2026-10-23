"""
熵引擎（scanner/entropy.py）—— 借鉴 TruffleHog 的香农熵判据，让"未知的密钥"也能被抓到。

为什么需要它：
    纯正则只能命中"已知形态"的密钥（AKIA...、ghp_...），对私有系统、内部令牌、新服务
    ——那些没有公开前缀的凭证——完全无能为力。TruffleHog 的核心洞察是：**密钥是高熵串**
    （随机生成、字符分布均匀，H ≈ 4~5 bits/char），而自然语言/普通标识符熵很低
    （H ≈ 1~2 bits/char）。因此"熵 > 阈值"可以捕获形态未知的凭证。

    但高熵不等于密钥：content-hash、UUID、base64 图片、minified JS 同样高熵。
    所以本模块提供三层收敛（与 TruffleHog / gitleaks 一致）：
      1) 上下文关键词命中（附近有 password/token/secret/密钥 等）→ 强证据；
      2) 形态先验（字符集混合度、长度、是否 base64/hex 单一字母表）→ 弱证据；
      3) 抑制词表 stopwords（checksum/integrity/uuid/sha256/example/...）→ 降噪。

零依赖：只用 math / collections，无第三方包。
"""

from __future__ import annotations

import math
import re
from typing import Iterable, List, Optional, Tuple

# ---------------------------------------------------------------- 香农熵

def shannon_entropy(data: str) -> float:
    """字符串的香农熵，单位 bits/char：H = -Σ p(x)·log2 p(x)。

    与 TruffleHog pkg/detectors/falsepositives.go:StringShannonEntropy 同一公式。
    空串返回 0.0。"""
    if not data:
        return 0.0
    counts = {}
    for ch in data:
        counts[ch] = counts.get(ch, 0) + 1
    total = len(data)
    h = 0.0
    for c in counts.values():
        p = c / total
        h -= p * math.log2(p)
    return h


def sliding_window_entropy(data: str, window: int = 64) -> float:
    """滑动窗口平均熵。长串整体熵会被低频尾段稀释，窗口均值更稳健。"""
    if not data:
        return 0.0
    if len(data) <= window:
        return shannon_entropy(data)
    vals = [shannon_entropy(data[i:i + window]) for i in range(0, len(data) - window + 1, window // 2)]
    return sum(vals) / len(vals) if vals else 0.0


def entropy_score(value: str, low: float = 3.0, high: float = 4.5) -> float:
    """把熵归一化到 [0,1]：min(1, (H-low)/(high-low))，低于 low 记 0。

    θ_low=3.0 / θ_high=4.5 是 TruffleHog 社区常用取值（--filter-entropy 3.5 即在此区间）。
    """
    if not value:
        return 0.0
    h = shannon_entropy(value)
    if h <= low:
        return 0.0
    return min(1.0, (h - low) / (high - low))


# ---------------------------------------------------------------- 形态先验

_BASE64 = re.compile(r"^[A-Za-z0-9+/=_-]+$")
_HEX = re.compile(r"^[a-fA-F0-9]+$")
_HEX_UPPER = re.compile(r"^[A-F0-9]+$")
_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_DIGITS = re.compile(r"^\d+$")


def charset_classes(value: str) -> int:
    """命中几类字符：小写 / 大写 / 数字 / 特殊。真密钥通常 >= 2~3 类。"""
    n = 0
    if re.search(r"[a-z]", value):
        n += 1
    if re.search(r"[A-Z]", value):
        n += 1
    if re.search(r"[0-9]", value):
        n += 1
    if re.search(r"[^A-Za-z0-9]", value):
        n += 1
    return n


def looks_like_hash(value: str) -> bool:
    """是否为内容寻址散列（git sha / md5 / sha1 / sha256）——高熵但不是凭证。"""
    if len(value) in (32, 40, 56, 64, 128) and (_HEX.match(value) or _HEX_UPPER.match(value)):
        return True
    return False


def looks_like_uuid(value: str) -> bool:
    return bool(_UUID.match(value))


def looks_like_base64_blob(value: str) -> bool:
    """超长单一 base64 blob，通常是图片/压缩包内联，而非密钥。"""
    return len(value) > 200 and bool(_BASE64.match(value))


# ---------------------------------------------------------------- 上下文关键词

# 中英双语：覆盖国内项目里常见的中文命名（密钥/口令/私钥/凭据）
CONTEXT_KEYWORDS = (
    "secret", "token", "apikey", "api_key", "api-key", "password", "passwd", "pwd",
    "credential", "private_key", "privatekey", "access_key", "accesskey",
    "auth", "authorization", "bearer", "session", "cookie", "signature", "signkey",
    "salt", "certificate", "keystore", "passphrase", "client_secret", "refresh_token",
    "密钥", "秘钥", "口令", "密码", "私钥", "凭据", "凭证", "证书", "令牌", "鉴权",
)

_NEIGHBOR = re.compile(r"[\"'`]?\s*[:=]\s*[\"'`]?")


def has_secret_context(line: str, keywords: Iterable[str] = CONTEXT_KEYWORDS) -> bool:
    """该行是否出现密钥上下文词（用于把"高熵串"升级为"疑似凭证"）。"""
    low = (line or "").lower()
    return any(k in low for k in keywords)


# ---------------------------------------------------------------- 抑制词

# 借鉴 gitleaks 的 stopwords：命中即判定为非凭证（示例值、占位符、校验和术语）
STOPWORDS = (
    "example", "sample", "dummy", "placeholder", "changeme", "your_", "your-",
    "xxxx", "xxxxx", "00000", "11111", "aaaaaa", "abc123", "test", "testing",
    "fake", "mock", "redacted", "redact", "todo", "fixme", "none", "null",
    "checksum", "integrity", "sha256", "sha1", "sha512", "md5", "digest",
    "uuid", "guid", "etag", "version", "build", "commit", "revision",
    "localhost", "127.0.0.1", "0.0.0.0", "noreply", "no-reply",
    "示例", "占位", "测试", "待填", "请填写",
)


def hit_stopword(value: str, line: str = "", stopwords: Iterable[str] = STOPWORDS) -> Optional[str]:
    """命中抑制词则返回该词，否则 None。

    gitleaks 的 stopwords 只作用于"抽取出的 secret"；这里额外允许看整行，
    因为中文项目里常写成 `# 示例密钥，仅用于测试`，secret 本身不含抑制词。
    """
    low_v = (value or "").lower()
    for w in stopwords:
        if w in low_v:
            return w
    if line:
        low_l = line.lower()
        for w in stopwords:
            if w in low_l:
                return w
    return None


# ---------------------------------------------------------------- 综合评分

def assess(value: str, line: str = "", entropy_threshold: float = 3.5) -> Tuple[bool, float, str]:
    """综合判定一个候选串是否为"疑似凭证"。

    返回 (是否疑似, 置信分[0,1], 理由)。判定链：
      1. 抑制词命中     → False（降噪，最高优先级）
      2. 形态明显非密钥 → False（纯数字 / UUID / 内容散列 / 超长 base64 blob）
      3. 熵低于阈值      → False（自然语言或规则字符串）
      4. 否则按 熵分 0.6 + 上下文 0.3 + 字符类 0.1 加权

    权重说明：熵是主判据（TruffleHog 思路），但**上下文能显著提权**——
    一个熵只有 3.6 但写在 `password = "..."` 后面的串，比一个孤立的高熵串更可疑。
    """
    if not value or len(value) < 8:
        return (False, 0.0, "过短")

    sw = hit_stopword(value, line)
    if sw:
        return (False, 0.0, f"命中抑制词 '{sw}'")

    if _DIGITS.match(value):
        return (False, 0.0, "纯数字")
    if looks_like_uuid(value):
        return (False, 0.0, "UUID")
    if looks_like_hash(value):
        return (False, 0.0, "内容寻址散列")
    if looks_like_base64_blob(value):
        return (False, 0.0, "超长 base64 负载")

    h = shannon_entropy(value)
    if h < entropy_threshold:
        return (False, 0.0, f"熵 {h:.2f} < 阈值 {entropy_threshold}")

    e = entropy_score(value)
    ctx = 1.0 if has_secret_context(line) else 0.0
    cls = min(1.0, charset_classes(value) / 3.0)
    score = 0.6 * e + 0.3 * ctx + 0.1 * cls
    reason = f"熵 {h:.2f}" + (" + 密钥上下文" if ctx else "")
    return (True, round(score, 4), reason)


# ---------------------------------------------------------------- 高熵候选抽取

# 刻意不含 '='：否则 `DB_PASSWORD=SuperSecretDbPass123` 这类整行赋值会被当成候选串，
# 产生大量误报（实测样例仓库因此多报 2 条）。base64 padding 的 '=' 被截掉不影响熵判定。
_CANDIDATE = re.compile(r"[A-Za-z0-9+/_\-.]{20,}")

# CI 表达式片段（${{ secrets.X }}）结构化但熵不低，先剥离再抽候选
_CI_EXPR = re.compile(r"\$\{\{[^}]*\}\}")


def extract_high_entropy_candidates(line: str, threshold: float = 4.0,
                                    min_len: int = 20) -> List[Tuple[str, float, float, str]]:
    """从一行里抽出高熵候选串。返回 [(串, 熵, 置信分, 理由)]。

    仅用于"通用高熵"规则（无正则可依的未知凭证），已知形态的检测器不走这里。
    """
    out: List[Tuple[str, float, float, str]] = []
    line = _CI_EXPR.sub(" ", line or "")
    for m in _CANDIDATE.finditer(line):
        s = m.group(0)
        if len(s) < min_len:
            continue
        # 路径/URL/包名这类"结构化串"熵可能也偏高，直接排除
        if "/" in s or "\\" in s or s.count(".") >= 3:
            continue
        ok, score, reason = assess(s, line, entropy_threshold=threshold)
        if ok:
            out.append((s, round(shannon_entropy(s), 3), score, reason))
    return out


if __name__ == "__main__":
    demo = [
        "password = \"hunter2\"",
        "api_key = \"wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY\"",
        "token = \"aaaaaaaaaaaaaaaaaaaaaaaaaaaa\"",
        "sha256 = \"9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08\"",
        "id = \"3f2504e0-4f89-11d3-9a0c-0305e82c3301\"",
        "key = \"123456789012345678901234\"",
    ]
    for d in demo:
        print(d, "→", extract_high_entropy_candidates(d))
