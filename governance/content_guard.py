"""
content_guard.py —— 多模态内容防护 + 大小模型组合（规则引擎做前后置合规校验）

对应要求："小模型/规则引擎负责前端脱敏和后端合规校验，大模型只做中段语义
理解与生成"。本模块即那个"规则引擎"：在请求大模型之前做脱敏与注入检测
（前端），在大模型产出之后做合规校验（后端）。纯规则、零依赖、可审计
（命中哪条规则都返回，契合"可验证优先"）。

覆盖：
  · 前端脱敏 desensitize()：识别 PII（手机/身份证/邮箱/银行卡/地址等）并掩码，
    避免敏感明文进入大模型上下文（数据管控 + 多模态文本防护）。
  · 后端输入校验 check_input()：提示注入 / 越狱 / 角色扮演覆盖等启发式，
    对应"对抗攻击"防护。
  · 后端输出校验 check_output()：检测模型是否泄露已被掩码的 PII、是否夹带
    注入指令回显，对应"多模态内容防护"与"幻觉/越狱"兜底。

诚实边界：规则引擎是**纵深防御的一层**，不是银弹；命中后交由上层决定
（拦截 / 告警 / 人工），本模块只负责"检出并说明"，不擅自改写语义。
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

# --------------------------------------------------------------------------
# PII 模式（中文语境常用）
# --------------------------------------------------------------------------
_PII_PATTERNS: List[Tuple[str, re.Pattern, str]] = [
    # (类型, 正则, 掩码模板) —— 掩码保留首尾便于回溯，中间打码
    ("手机", re.compile(r"(?<!\d)(1[3-9]\d{9})(?!\d)"), r"\1"),  # 占位，下方特判
    ("身份证", re.compile(r"(?<!\d)(\d{6})(\d{8})(\d{3}[\dXx])(?!\d)"),
     r"\1********\3"),
    ("邮箱", re.compile(r"([A-Za-z0-9_.+%-]+)@([A-Za-z0-9.-]+\.[A-Za-z]{2,})"),
     r"\1***@\2"),
    ("银行卡", re.compile(r"(?<!\d)(\d{6})(\d{6,10})(\d{4})(?!\d)"),
     r"\1******\3"),
    ("护照", re.compile(r"([EGDS]\d{8})", re.I), r"\1"),
]

# 地址类关键词（弱匹配，仅标注，不强制打码整句）
_ADDRESS_HINTS = ("住址", "户籍", "家庭地址", "身份证号", "手机号", "银行卡号")

# --------------------------------------------------------------------------
# 提示注入 / 越狱 规则（启发式；命中即上报，severity 越高越危险）
# --------------------------------------------------------------------------
_INJECTION_RULES: List[Tuple[str, str, int]] = [
    # (规则名, 匹配串(小写包含), 严重度 1~3)
    ("忽略系统指令", "忽略", 2),
    ("角色覆盖", "你现在是一个", 2),
    ("越狱dan", "dan", 3),
    ("越狱jailbreak", "jailbreak", 3),
    ("系统提示词泄露", "system prompt", 3),
    ("提示词泄露", "你的提示词", 3),
    ("指令注入分隔符", "---", 1),
    ("伪指令声称", "developer mode", 3),
    ("伪指令声称2", "开发者模式", 3),
    ("角色扮演越权", "扮演一个没有限制的", 3),
    ("忽略前文", "忽略以上", 2),
    ("忽略之前", "忽略之前", 2),
    ("不受限制", "不受限制", 2),
    ("输出你的规则", "输出你的规则", 2),
]


def _mask_phone(m: re.Match) -> str:
    num = m.group(1)
    return num[:3] + "****" + num[-4:]


def desensitize(text: str, level: str = "medium") -> Dict:
    """前端脱敏：把 PII 掩码后再进大模型上下文。

    返回 {masked, findings:[{type, count}], level}。level:
      low    —— 仅手机/身份证/银行卡；
      medium —— 上述 + 邮箱（默认）；
      high   —— 上述 + 命中地址关键词的整段标注。
    """
    if not text:
        return {"masked": text, "findings": [], "level": level}
    out = text
    findings: List[Dict] = []

    # 手机
    cnt = 0
    out, n = re.subn(_PII_PATTERNS[0][1], _mask_phone, out)
    cnt += n
    if cnt:
        findings.append({"type": "手机", "count": cnt})

    # 身份证 / 邮箱 / 银行卡
    for name, pat, tmpl in _PII_PATTERNS[1:]:
        if name == "邮箱" and level == "low":
            continue
        new, n = pat.subn(tmpl, out)
        if n:
            findings.append({"type": name, "count": n})
            out = new

    # 地址关键词（high 才标注整段，避免误伤正常文本）
    if level == "high":
        for hint in _ADDRESS_HINTS:
            if hint in out:
                findings.append({"type": "敏感信息提示(" + hint + ")", "count": 1})

    return {"masked": out, "findings": findings, "level": level}


def check_input(text: str) -> Dict:
    """后端输入合规校验：提示注入 / 越狱启发式。

    返回 {risk: 'low'|'medium'|'high', score, hits:[{rule, severity}]}；
    risk 由最高 severity 决定（>=3 高危）。
    """
    if not text:
        return {"risk": "low", "score": 0, "hits": []}
    low = text.lower()
    hits: List[Dict] = []
    for name, needle, sev in _INJECTION_RULES:
        if needle in low:
            hits.append({"rule": name, "severity": sev})
    max_sev = max((h["severity"] for h in hits), default=0)
    risk = "high" if max_sev >= 3 else ("medium" if max_sev >= 2 else "low")
    return {"risk": risk, "score": max_sev, "hits": hits}


def check_output(text: str, findings: Optional[List[Dict]] = None) -> Dict:
    """后端输出合规校验：检测是否泄露已被掩码的 PII、是否回显注入指令。

    返回 {issues:[{type, detail}], safe: bool}。safe=False 表示需要人工/拦截。
    """
    issues: List[Dict] = []
    if not text:
        return {"issues": [], "safe": True}

    # 1) 明文 PII 回潮（未经脱敏的 11 位手机号 / 身份证 18 位）
    if re.search(r"(?<!\d)1[3-9]\d{9}(?!\d)", text):
        issues.append({"type": "PII泄露", "detail": "输出含未脱敏手机号"})
    if re.search(r"(?<!\d)\d{17}[\dXx](?!\d)", text):
        issues.append({"type": "PII泄露", "detail": "输出含未脱敏身份证号"})

    # 2) 注入指令回显（模型把攻击 prompt 当内容吐出来）
    low = text.lower()
    for name, needle, sev in _INJECTION_RULES:
        if sev >= 3 and needle in low:
            issues.append({"type": "注入指令回显", "detail": name})
            break

    return {"issues": issues, "safe": len(issues) == 0}


def guard_pipeline(text: str, level: str = "medium") -> Dict:
    """一键前后置：脱敏 + 输入校验。返回合并结果供上层决策。

    上层据此决定：risk=high 直接拦截；否则用 masked 文本喂大模型。
    """
    de = desensitize(text, level=level)
    inj = check_input(text)
    return {
        "masked": de["masked"],
        "desensitize_findings": de["findings"],
        "input_risk": inj["risk"],
        "input_hits": inj["hits"],
        "block": inj["risk"] == "high",
    }


# ==========================================================================
# 非文本模态（图像 / 音频 / 视频）内容防护 —— A2
#
# 诚实边界（必须遵守，不得跨越）：
#   · 纯规则引擎只能校验**魔数 / 类型 / 大小 / 生成 prompt 注入**，
#     做不了"这张图是否涉黄/涉政""这段音频说了什么"的语义审核。
#   · 未接入视觉/音频审核模型时，一律如实标注 semantic_check=false，
#     绝不谎称"已做语义审核"。真语义审核由上层注入 semantic_hook。
#   · 魔数校验拦"改后缀的可执行文件/脚本"（把 .exe 改名 .png 混进来）。
# 零第三方依赖，离线可测。
# ==========================================================================

MAX_MEDIA_BYTES = 20 * 1024 * 1024   # 20MB 上限，防巨型载荷打爆内存

# (真实 kind, 标准 mime, 魔数, 偏移, 复合校验(偏移,魔数)或 None)
_MEDIA_SIGNATURES: Tuple = (
    ("image", "image/png",     b"\x89PNG\r\n\x1a\n", 0, None),
    ("image", "image/jpeg",    b"\xff\xd8\xff",      0, None),
    ("image", "image/gif",     b"GIF8",              0, None),
    ("image", "image/webp",    b"RIFF",              0, (8, b"WEBP")),
    ("image", "image/bmp",     b"BM",                0, None),
    ("audio", "audio/mpeg",    b"ID3",               0, None),
    ("audio", "audio/mpeg",    b"\xff\xfb",          0, None),  # MP3 帧同步
    ("audio", "audio/wav",     b"RIFF",              0, (8, b"WAVE")),
    ("audio", "audio/ogg",     b"OggS",              0, None),
    ("video", "video/mp4",     b"ftyp",              4, None),  # bytes[4:8]=ftyp
)

# 改后缀的可执行文件 / 脚本（优先于媒体魔数判定，命中即拒）
_DANGEROUS_SIGNATURES: Tuple[Tuple[bytes, str], ...] = (
    (b"MZ", "Windows PE 可执行文件"),
    (b"\x7fELF", "ELF 可执行文件"),
    (b"\xca\xfe\xba\xbe", "Mach-O / Java class"),
    (b"#!", "脚本 shebang"),
    (b"<?php", "PHP 脚本"),
    (b"<script", "HTML/JS 脚本"),
)


def validate_media(data: bytes, kind: str = "", declared_mime: str = "",
                   max_bytes: int = MAX_MEDIA_BYTES) -> Dict:
    """校验媒体字节：魔数 / 真实类型 / 声明 MIME / 类型一致性 / 大小上限。

    返回 {ok, detected_kind, detected_mime, declared_mime, size, reason}。
    不匹配一律 ok=False + 明确 reason，绝不抛异常、绝不猜测放行。
    """
    out = {"ok": False, "detected_kind": None, "detected_mime": None,
           "declared_mime": declared_mime or "", "size": 0, "reason": ""}
    if not isinstance(data, (bytes, bytearray)):
        out["reason"] = "媒体数据非字节类型（%s）" % type(data).__name__
        return out
    blob = bytes(data)
    out["size"] = len(blob)
    if not blob:
        out["reason"] = "媒体数据为空"
        return out
    if len(blob) > max_bytes:
        out["reason"] = "媒体超过大小上限（%d > %d 字节）" % (len(blob), max_bytes)
        return out
    # 1) 危险签名优先：改后缀的可执行文件/脚本，即使碰巧带媒体魔数也拒
    head = blob[:16]
    for sig, desc in _DANGEROUS_SIGNATURES:
        if head.startswith(sig):
            out["reason"] = "检测到非媒体内容（%s），疑似改后缀伪装，拒绝" % desc
            return out
    # 2) 魔数识别真实类型
    detected = None
    for dk, dm, sig, off, tail in _MEDIA_SIGNATURES:
        if blob[off:off + len(sig)] != sig:
            continue
        if tail is not None and blob[tail[0]:tail[0] + len(tail[1])] != tail[1]:
            continue
        detected = (dk, dm)
        break
    if detected is None:
        out["reason"] = "魔数校验失败：无法识别为受支持的媒体类型（image/audio/video）"
        return out
    dk, dm = detected
    out["detected_kind"] = dk
    out["detected_mime"] = dm
    # 3) 声明的 kind 与实际是否一致
    if kind and dk != kind:
        out["reason"] = "媒体类型不一致：声明 %s，魔数判定 %s（%s）" % (kind, dk, dm)
        return out
    # 4) 声明的 MIME 与实际是否一致（octet-stream / 空 视为未声明，不作冲突）
    if declared_mime:
        dmime = declared_mime.split(";")[0].strip().lower()
        if dmime and dmime != "application/octet-stream" and dmime != dm:
            out["reason"] = "MIME 不一致：声明 %s，魔数判定 %s" % (dmime, dm)
            return out
    out["ok"] = True
    out["reason"] = "魔数与声明一致（%s，%d 字节）" % (dm, len(blob))
    return out


def check_media_prompt(prompt: str) -> Dict:
    """媒体**生成 prompt** 的注入/越狱检测（复用文本 check_input 规则）。

    返回 {risk, score, hits, block}；block=True 时上层应拒绝生成。
    """
    inj = check_input(prompt or "")
    return {"risk": inj["risk"], "score": inj["score"],
            "hits": inj["hits"], "block": inj["risk"] == "high"}


def moderate_media(data: bytes, kind: str = "", declared_mime: str = "",
                   semantic_hook=None, max_bytes: int = MAX_MEDIA_BYTES) -> Dict:
    """媒体内容审核入口：魔数/大小/类型校验 + 可选真语义审核。

    semantic_hook 未注入时 **semantic_check=false 如实标注**，不谎称已做
    语义审核；注入后 hook 抛错/不可用也如实标注并保守拒绝（强合规场景）。
    hook 返回 {"safe": bool, "reason": str} 时 safe=False 判不安全。
    """
    v = validate_media(data, kind, declared_mime, max_bytes=max_bytes)
    out = {"ok": bool(v["ok"]), "validate": v, "semantic_check": False,
           "semantic": None, "reason": v["reason"]}
    if not v["ok"]:
        return out
    if semantic_hook is None:
        out["reason"] = (v["reason"] + "；未配置视觉/音频语义审核模型，"
                         "仅完成魔数/大小/类型校验，semantic_check=false"
                         "（不宣称已做语义审核）")
        return out
    try:
        s = semantic_hook(bytes(data), kind)
        out["semantic"] = s
        out["semantic_check"] = True
        if isinstance(s, dict) and s.get("safe") is False:
            out["ok"] = False
            out["reason"] = ("语义审核模型判定不安全：%s"
                             % (s.get("reason") or "未提供原因"))
    except Exception as e:  # noqa: BLE001 - 审核不可用不能谎称通过
        out["semantic_error"] = str(e)
        out["ok"] = False
        out["reason"] = "语义审核不可用（semantic_check=false），保守拒绝：%s" % e
    return out
