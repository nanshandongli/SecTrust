"""
扫描编排（scanner/engine.py）—— 把检测器、熵引擎、抑制、基线、增量串成一次扫描。

与 templates/（合规模板：TODO/许可证/疑似密钥的"广度"扫描）分工：
    templates  = 面向审计清单的广度扫描（5 类合规项）
    engine     = 面向凭证的深度扫描（77 条结构化检测器 + 熵 + 运营化）
两者共用审批/沙箱/报告链路，可按场景单独或组合调用。

⚠️ 遍历的一个历史坑（已被自检发现并修复，此处保留注释以免回归）：
    用 os.walk('.') 时 os.path.basename('.') == '.'，会被"以点开头=隐藏目录"的判断
    命中，导致**根目录整层被静默跳过**（假阴性）。因此必须用 os.path.abspath(root) 归一。
"""

from __future__ import annotations

import os
import time
from typing import Dict, List, Optional, Sequence

from .detectors import DETECTORS, Detector, detector_stats, find_in_text, select
from .entropy import assess, extract_high_entropy_candidates
from .findings import (Allowlist, Finding, apply_suppressions, changed_files,
                       dedupe, diff_against_baseline, gate, load_baseline,
                       normalize, save_baseline, summarize)

# 超过该体积的文件跳过（二进制/生成物会让扫描既慢又噪）
DEFAULT_MAX_BYTES = 2 * 1024 * 1024

_SKIP_EXT = {
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".svg",
    ".pdf", ".zip", ".gz", ".bz2", ".xz", ".7z", ".tar", ".rar",
    ".mp3", ".mp4", ".avi", ".mov", ".woff", ".woff2", ".ttf", ".eot",
    ".exe", ".dll", ".so", ".dylib", ".bin", ".class", ".jar",
    ".sqlite", ".db", ".pyc", ".pickle", ".parquet",
}

# 只跳过"元数据存储"型隐藏目录，**不能**一刀切跳过所有点开头目录。
# ⚠️ 这是真实踩过的坑：早期实现用 `d.startswith(".")` 过滤目录，导致
#    `.github/`（工作流里常有 secrets 明文引用）被整目录漏扫；同理
#    `fn.startswith(".")` 会漏掉 `.env` —— 而 .env 是凭证泄露**最高频**的单一文件
#    （gitleaks / TruffleHog 都把它列为首要目标）。漏掉它，扫描器等于形同虚设。
_SKIP_DIRS = {
    ".git", ".hg", ".svn", ".bzr",           # VCS 内部对象（非工作树内容）
    ".tox", ".nox", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".cache",
    ".idea", ".gradle", ".terraform", ".serverless", ".next", ".nuxt",
    ".venv", "venv", "node_modules", "__pycache__", "site-packages",
}


def iter_files(root: str, max_bytes: int = DEFAULT_MAX_BYTES,
               allowlist: Optional[Allowlist] = None,
               only_files: Optional[Sequence[str]] = None) -> List[str]:
    """列出待扫描文件：跳过隐藏目录、二进制、超大文件与 allowlist 路径。

    only_files 用于 diff-aware 增量扫描（只扫 git 变更文件）。
    """
    allowlist = allowlist or Allowlist()
    root_abs = os.path.abspath(root)
    out: List[str] = []

    if only_files is not None:
        for rel in only_files:
            p = os.path.join(root_abs, rel)
            if os.path.isfile(p) and not allowlist.allows_path(p):
                out.append(p)
        return out

    if os.path.isfile(root_abs):
        return [root_abs] if not allowlist.allows_path(root_abs) else []

    for dirpath, dirnames, filenames in os.walk(root_abs):
        # 只跳"元数据存储"目录；.github / .gitlab / .circleci 等配置目录必须保留
        dirnames[:] = [d for d in dirnames
                       if d not in _SKIP_DIRS and not allowlist.allows_path(
                           os.path.join(dirpath, d) + "/")]
        for fn in filenames:
            # 点文件照扫（.env / .npmrc / .dockercfg / .netrc 全是凭证高发区）
            if fn in (".DS_Store", ".gitkeep"):
                continue
            if os.path.splitext(fn)[1].lower() in _SKIP_EXT:
                continue
            p = os.path.join(dirpath, fn)
            if allowlist.allows_path(p):
                continue
            try:
                if os.path.getsize(p) > max_bytes:
                    continue
            except OSError:
                continue
            out.append(p)
    return out


def _read_text(path: str) -> Optional[str]:
    try:
        with open(path, "rb") as fh:
            blob = fh.read()
    except OSError:
        return None
    if b"\x00" in blob[:4096]:      # 二进制特征
        return None
    try:
        return blob.decode("utf-8")
    except UnicodeDecodeError:
        return blob.decode("utf-8", errors="replace")


def scan_file(path: str, dets: Optional[List[Detector]] = None,
              high_entropy: bool = True, he_threshold: float = 4.0,
              lines_out: Optional[Dict[str, List[str]]] = None) -> List[Finding]:
    """扫单个文件：结构化规则 +（可选）通用高熵兜底。

    lines_out：传入 dict 时把原文按行留下，供行内 `scanner:allow` 抑制判定使用
    （抑制需要看原始行，而 Finding 里只保留脱敏后的证据行）。
    """
    text = _read_text(path)
    if not text:
        return []
    if lines_out is not None:
        lines_out[path] = text.splitlines()
    findings = [normalize(r, path=path) for r in find_in_text(text, only=dets)]

    if high_entropy:
        # 兜底：捕获"没有任何已知前缀"的私有/内部凭证（TruffleHog 的熵思路）
        known_spans = {(f.line_no, f.secret) for f in findings}
        for i, line in enumerate(text.splitlines(), start=1):
            for cand, h, score, reason in extract_high_entropy_candidates(line, he_threshold):
                if (i, cand) in known_spans:
                    continue
                findings.append(normalize({
                    "rule_id": "generic-high-entropy",
                    "rule_name": "High Entropy String",
                    "description": f"高熵字符串（无已知凭证前缀）：{reason}",
                    "severity": "medium",
                    "tags": ["generic", "entropy"],
                    "provider": "generic",
                    "validator": None,
                    "secret": cand,
                    "match": cand,
                    "line_no": i,
                    "line": line,
                    "start": 0,
                    "entropy": h,
                    "entropy_required": he_threshold,
                    "suppressed_by": None,
                }, path=path))
    return findings


def scan_path(root: str,
              dets: Optional[List[Detector]] = None,
              allowlist: Optional[Allowlist] = None,
              categories: Optional[Sequence[str]] = None,
              providers: Optional[Sequence[str]] = None,
              severities: Optional[Sequence[str]] = None,
              high_entropy: bool = True,
              he_threshold: float = 4.0,
              only_changed: bool = False,
              since: str = "HEAD~1",
              baseline_path: Optional[str] = None,
              write_baseline: bool = False,
              keep_suppressed: bool = False,
              max_bytes: int = DEFAULT_MAX_BYTES,
              fail_on: str = "high") -> Dict:
    """扫一个目录/文件，返回完整结果（含统计与 CI 闸门建议）。

    参数要点：
      · only_changed / since —— diff-aware 增量扫描（CI PR 场景，快一个数量级）；
      · baseline_path —— 提供时只把"新增"算作阻断项；write_baseline=True 则写入基线；
      · keep_suppressed —— 审计模式：保留被抑制项并标注原因（默认丢弃）。
    """
    t0 = time.time()
    if dets is None and (categories or providers or severities):
        dets = select(categories=categories, providers=providers, severities=severities)

    only_files = None
    if only_changed:
        only_files = changed_files(root, since=since)
        if only_files == []:       # git 说"没有变更文件"
            return {"findings": [], "summary": {"total": 0}, "gate": {"should_fail": False},
                    "scanned_files": 0, "elapsed_ms": round((time.time() - t0) * 1000, 1),
                    "mode": "diff-aware", "note": "git 报告无变更文件"}

    files = iter_files(root, max_bytes=max_bytes, allowlist=allowlist, only_files=only_files)

    findings: List[Finding] = []
    lines_by_path: Dict[str, List[str]] = {}
    for p in files:
        findings.extend(scan_file(p, dets=dets, high_entropy=high_entropy,
                                  he_threshold=he_threshold, lines_out=lines_by_path))

    findings = dedupe(findings)
    findings = apply_suppressions(findings, root=root if os.path.isdir(root) else
                                  os.path.dirname(root), allowlist=allowlist,
                                  inline_lines=lines_by_path,
                                  keep_suppressed=keep_suppressed)

    new_findings, resolved = findings, []
    baseline_existed = bool(baseline_path) and os.path.isfile(baseline_path)
    if baseline_path:
        base = load_baseline(baseline_path)
        # 首次建基线＝把存量全部登记，此时"新增"应为 0（否则第一次跑就红灯，
        # 团队第一反应就是关掉扫描——基线模式的意义正在于避免这种结局）
        new_findings, resolved = (([], []) if not baseline_existed
                                  else diff_against_baseline(findings, base))
        if write_baseline:
            save_baseline(findings, baseline_path)

    return {
        "findings": [f.to_dict() for f in findings],
        "new_findings": [f.to_dict() for f in new_findings],
        "resolved_since_baseline": resolved,
        "summary": summarize(findings),
        "new_summary": summarize(new_findings),
        "gate": gate(new_findings if baseline_path else findings, fail_on=fail_on),
        "scanned_files": len(files),
        "rules": detector_stats()["total"],
        "mode": "diff-aware" if only_files is not None else "full",
        "elapsed_ms": round((time.time() - t0) * 1000, 1),
    }


if __name__ == "__main__":
    import json
    import sys
    target = sys.argv[1] if len(sys.argv) > 1 else "."
    res = scan_path(target, high_entropy=True)
    res.pop("findings", None)
    res.pop("new_findings", None)
    print(json.dumps(res, ensure_ascii=False, indent=2))
