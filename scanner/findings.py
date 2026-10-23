"""
发现对象与工程化（scanner/findings.py）—— 借鉴 gitleaks 的运营化设计。

扫描器能否被真正用起来，不取决于"能不能报"，而取决于三件运营小事：
  1. **指纹稳定**：同一条凭证在两次扫描里必须得到同一个 id，否则无法去重、无法基线、
     无法判定"是否已修复"。gitleaks 用 file+rule+secret 的哈希，这里同思路。
  2. **抑制有档位**：从窄到宽三档（行内注释 → .scanignore 指纹 → 配置 allowlist），
     越宽的档位越要声明理由。这是 gitleaks 最重要的运维经验（"范围越宽越要证明"）。
  3. **基线（baseline）**：存量仓库第一次扫一定爆出成百上千条，没人会去修，于是扫描
     被摘掉。基线把"存量"与"新增"分开——CI 只拦新增，存量作为有主的项目另行清理。

另有两处安全细节（多数自研扫描器会忽略）：
  · 默认**脱敏**：报告里只出现 `abc…xyz (len=40)`，避免"扫描报告本身成为泄密源"。
    活体验证需要原文时才从内存取 secret。
  · **去重**：同一串常被多条规则命中（AIza… 既是 GCP 又是 Gemini），按指纹合并、
    保留最具体（severity 最高）的那条，避免告警翻倍。
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from dataclasses import dataclass, field, asdict
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .entropy import shannon_entropy

# ---------------------------------------------------------------- 严重级与置信

SEVERITY_ORDER = {"critical": 5, "high": 4, "medium": 3, "low": 2, "info": 1}
_SEVERITY_BASE = {"critical": 0.90, "high": 0.80, "medium": 0.60, "low": 0.40, "info": 0.20}


def confidence_of(severity: str, entropy: float) -> float:
    """置信度：严重级为基线，熵作为修正（熵越高越"像"随机凭证）。

    刻意 clamp 在 [0.05, 0.99]：置信度是排序/过滤信号，不是概率，不应出现 1.0 的确定性。
    """
    base = _SEVERITY_BASE.get(severity, 0.5)
    h = min(1.0, max(0.0, (entropy or 0.0) / 5.0))
    return round(min(0.99, max(0.05, base * (0.7 + 0.3 * h))), 4)


# ---------------------------------------------------------------- 脱敏与指纹

def redact(secret: str, keep: int = 3) -> str:
    """把凭证脱敏成可安全入报告/日志的形态：`abc…xyz (len=40)`。"""
    if not secret:
        return ""
    n = len(secret)
    if n <= keep * 2 + 1:
        return "*" * n + f" (len={n})"
    if n <= 12:
        keep = 2
    return f"{secret[:keep]}…{secret[-keep:]} (len={n})"


def fingerprint(rule_id: str, secret: str, path: Optional[str] = None) -> str:
    """稳定指纹：**跨进程、跨机器、跨平台一致**（blake2b，非内置 hash）。

    含 path 的 basename（不含目录）而非完整路径：文件被移动/重命名后仍能被基线识别，
    但同一凭证出现在两个文件里会被视为两条（这与 gitleaks 行为一致，便于逐处修复）。
    """
    base = os.path.basename(path) if path else ""
    payload = f"{rule_id}\x00{secret}\x00{base}"
    return hashlib.blake2b(payload.encode("utf-8"), digest_size=8).hexdigest()


# ---------------------------------------------------------------- Finding

@dataclass
class Finding:
    rule_id: str
    rule_name: str
    severity: str
    path: str
    line_no: int
    secret_masked: str
    fingerprint: str
    confidence: float = 0.0
    description: str = ""
    provider: str = ""
    tags: List[str] = field(default_factory=list)
    validator: Optional[str] = None
    entropy: float = 0.0
    line_masked: str = ""
    suppressed: bool = False
    suppress_reason: str = ""
    validation: Optional[Dict] = None

    # 不参与序列化：原文只在内存里供活体验证使用，绝不写进报告/基线
    secret: Optional[str] = field(default=None, repr=False, compare=False)

    def to_dict(self, include_secret: bool = False) -> Dict:
        d = asdict(self)
        if not include_secret:
            d.pop("secret", None)
        return d


def normalize(raw: Dict, path: str = "") -> Finding:
    """把 detectors.match_line 的原始 dict 规整成 Finding（补指纹/掩码/置信）。"""
    secret = raw.get("secret", "")
    fp = fingerprint(raw.get("rule_id", ""), secret, path)
    sev = raw.get("severity", "medium")
    ent = float(raw.get("entropy") or shannon_entropy(secret))
    line = raw.get("line", "")
    return Finding(
        rule_id=raw.get("rule_id", ""),
        rule_name=raw.get("rule_name", ""),
        severity=sev,
        path=path,
        line_no=int(raw.get("line_no", 0)),
        secret_masked=redact(secret),
        fingerprint=fp,
        confidence=confidence_of(sev, ent),
        description=raw.get("description", ""),
        provider=raw.get("provider", ""),
        tags=list(raw.get("tags") or []),
        validator=raw.get("validator"),
        entropy=round(ent, 3),
        line_masked=_mask_line(line, secret),
        suppressed=bool(raw.get("suppressed_by")),
        suppress_reason=(f"命中抑制词 '{raw['suppressed_by']}'"
                         if raw.get("suppressed_by") else ""),
        secret=secret,
    )


def _mask_line(line: str, secret: str) -> str:
    """证据行也要脱敏：报告里能看到上下文，但不该复现完整凭证。"""
    if not secret:
        return line[:300]
    return line.replace(secret, redact(secret))[:300]


# ---------------------------------------------------------------- 去重

def dedupe(findings: Sequence[Finding]) -> List[Finding]:
    """合并同一凭证的重复命中，保留 severity 最高者。

    ⚠️ 去重键是 **(secret, path)** 而不是 fingerprint：fingerprint 里含 rule_id，
    于是"同一串被两条规则命中"（如 `AIza…` 同时命中 gcp-api-key 与 gemini-api-key）
    会得到两个不同指纹、去重失效——告警数翻倍正是要避免的结果。
    fingerprint 仍保留 rule_id，用于基线里的精确定位。
    """
    best: Dict[Tuple[str, str], Finding] = {}
    for f in findings:
        key = ((f.secret or f.secret_masked), f.path)
        cur = best.get(key)
        if cur is None:
            best[key] = f
            continue
        if SEVERITY_ORDER.get(f.severity, 0) > SEVERITY_ORDER.get(cur.severity, 0):
            best[key] = f
    return list(best.values())


# ---------------------------------------------------------------- 抑制（三档）

# 档位 1：行内注释（最窄，优先推荐）—— 与 gitleaks:allow / nosemgrep 同思路
_INLINE_ALLOW = re.compile(r"(?:scanner:allow|gitleaks:allow|nosecret|noqa:\s*secret)", re.I)

IGNORE_FILENAMES = (".scanignore", ".gitleaksignore")


def has_inline_allow(line: str) -> bool:
    return bool(_INLINE_ALLOW.search(line or ""))


def load_ignore_file(root: str) -> Set[str]:
    """档位 2：仓库根的 .scanignore（兼容 .gitleaksignore），每行一个指纹。"""
    fps: Set[str] = set()
    for name in IGNORE_FILENAMES:
        p = os.path.join(root, name)
        if os.path.isfile(p):
            try:
                with open(p, "r", encoding="utf-8", errors="replace") as fh:
                    for ln in fh:
                        s = ln.strip()
                        if s and not s.startswith("#"):
                            fps.add(s)
            except OSError:
                continue
    return fps


# 档位 3：配置 allowlist（最宽）—— 路径 / 正则 / 抑制词
DEFAULT_PATH_ALLOWLIST = (
    r"(^|/)\.git/", r"(^|/)node_modules/", r"(^|/)vendor/", r"(^|/)dist/",
    r"(^|/)build/", r"(^|/)\.venv/", r"(^|/)venv/", r"(^|/)__pycache__/",
    r"\.(png|jpg|jpeg|gif|svg|ico|pdf|zip|gz|tar|woff2?|ttf|mp4|exe|dll|so)$",
    r"package-lock\.json$", r"yarn\.lock$", r"poetry\.lock$", r"\.min\.js$",
)


@dataclass
class Allowlist:
    paths: Tuple[str, ...] = DEFAULT_PATH_ALLOWLIST
    regexes: Tuple[str, ...] = ()
    stopwords: Tuple[str, ...] = ()

    def allows_path(self, path: str) -> bool:
        norm = (path or "").replace("\\", "/")
        return any(re.search(p, norm) for p in self.paths)

    def allows_match(self, line: str) -> bool:
        return any(re.search(r, line or "") for r in self.regexes)


def apply_suppressions(findings: Sequence[Finding],
                       root: str = "",
                       allowlist: Optional[Allowlist] = None,
                       inline_lines: Optional[Dict[str, List[str]]] = None,
                       keep_suppressed: bool = False) -> List[Finding]:
    """三档抑制依次生效。默认丢弃被抑制项（keep_suppressed=True 时保留并打标记，供审计）。"""
    allowlist = allowlist or Allowlist()
    ignore_fps = load_ignore_file(root) if root else set()
    out: List[Finding] = []
    for f in findings:
        reason = ""
        if f.suppressed:
            reason = f.suppress_reason or "抑制词"
        elif f.fingerprint in ignore_fps:
            reason = "已在 .scanignore 中登记"
        elif allowlist.allows_path(f.path):
            reason = "路径在 allowlist 中"
        elif inline_lines is not None:
            lines = inline_lines.get(f.path) or []
            idx = f.line_no - 1
            if 0 <= idx < len(lines) and has_inline_allow(lines[idx]):
                reason = "行内 scanner:allow 注释"
        if reason:
            if not keep_suppressed:
                continue
            f.suppressed = True
            f.suppress_reason = reason
        out.append(f)
    return out


# ---------------------------------------------------------------- 基线

def save_baseline(findings: Sequence[Finding], path: str) -> int:
    """把当前发现存为基线（只写指纹与元信息，**不写任何 secret 原文**）。"""
    payload = {
        "version": 1,
        "generated": _now_iso(),
        "findings": [
            {"fingerprint": f.fingerprint, "rule_id": f.rule_id, "severity": f.severity,
             "path": f.path, "line_no": f.line_no}
            for f in findings
        ],
    }
    _atomic_write_json(path, payload)
    return len(findings)


def load_baseline(path: str) -> Dict[str, Dict]:
    if not path or not os.path.isfile(path):
        return {}
    try:
        import json
        with open(path, "r", encoding="utf-8") as fh:
            data = __import__("json").load(fh)
    except (OSError, ValueError):
        return {}
    out: Dict[str, Dict] = {}
    for item in (data or {}).get("findings", []):
        fp = item.get("fingerprint")
        if fp:
            out[fp] = item
    return out


def diff_against_baseline(findings: Sequence[Finding],
                          baseline: Dict[str, Dict]) -> Tuple[List[Finding], List[Dict]]:
    """返回 (新增发现, 已修复的基线项)。CI 只对"新增"失败——这是基线模式的全部意义。"""
    new = [f for f in findings if f.fingerprint not in baseline]
    seen = {f.fingerprint for f in findings}
    resolved = [v for fp, v in baseline.items() if fp not in seen]
    return new, resolved


# ---------------------------------------------------------------- 增量（diff-aware）

def changed_files(root: str, since: str = "HEAD~1") -> Optional[List[str]]:
    """git 变更文件列表（semgrep 的 --baseline-commit 同思路）。非 git 仓库返回 None（表示全量）。

    只调用本机 git，不做任何网络操作；git 不可用/异常时降级为全量扫描（宁可多扫不可漏扫）。
    """
    try:
        proc = subprocess.run(["git", "-C", root, "diff", "--name-only", since],
                              capture_output=True, text=True, timeout=20)
        if proc.returncode != 0:
            return None
        return [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
    except (OSError, subprocess.SubprocessError):
        return None


# ---------------------------------------------------------------- 统计与工具

def summarize(findings: Sequence[Finding]) -> Dict:
    by_sev: Dict[str, int] = {}
    by_prov: Dict[str, int] = {}
    by_rule: Dict[str, int] = {}
    for f in findings:
        by_sev[f.severity] = by_sev.get(f.severity, 0) + 1
        by_prov[f.provider or "generic"] = by_prov.get(f.provider or "generic", 0) + 1
        by_rule[f.rule_id] = by_rule.get(f.rule_id, 0) + 1
    return {
        "total": len(findings),
        "by_severity": dict(sorted(by_sev.items(), key=lambda kv: -SEVERITY_ORDER.get(kv[0], 0))),
        "by_provider": dict(sorted(by_prov.items(), key=lambda kv: (-kv[1], kv[0]))),
        "by_rule": dict(sorted(by_rule.items(), key=lambda kv: (-kv[1], kv[0]))),
    }


def gate(findings: Sequence[Finding], fail_on: str = "high") -> Dict:
    """CI 闸门：给 exit code 语义。fail_on 为最低阻断级别（含）。"""
    threshold = SEVERITY_ORDER.get(fail_on, 4)
    blockers = [f for f in findings if SEVERITY_ORDER.get(f.severity, 0) >= threshold]
    return {"should_fail": bool(blockers), "blockers": len(blockers),
            "fail_on": fail_on, "reason": f"{len(blockers)} 条 >= {fail_on}"}


def _now_iso() -> str:
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _atomic_write_json(path: str, payload: Dict) -> None:
    import json
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


if __name__ == "__main__":
    from .detectors import find_in_text
    sample = 'AKIAKEY = "AKIAIOSFODNN7REALKEY"\ntoken = "ghp_012345678901234567890123456789abcdef" # scanner:allow\n'
    raws = find_in_text(sample)
    fs = [normalize(r, path="demo.py") for r in raws]
    print("归一化:", [(f.rule_id, f.severity, f.secret_masked, f.fingerprint) for f in fs])
    print("统计:", summarize(fs))
    print("闸门:", gate(fs))
