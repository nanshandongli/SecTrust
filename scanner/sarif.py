"""
SARIF 2.1.0 输出（scanner/sarif.py）—— 让扫描结果进入现有安全生态。

为什么这是"新"而不仅是"多一个格式"：
    自研扫描器最常见的结局是"报告没人看"。SARIF 是 OASIS 标准、GitHub Code Scanning /
    Azure DevOps / VS Code SARIF Viewer / JetBrains 的通用输入。产出 SARIF 意味着：
      · 结果直接出现在 GitHub PR 的 Code Scanning 告警里（而不是 CI 日志里被冲掉）；
      · 开发者在 IDE 里逐条跳转、标记已处理；
      · 与 Semgrep / CodeQL 的结果在同一面板中并排，无需自建看板。
    这是"把能力接到别人已经建好的工作流上"，成本极低、收益极高。

规范要点（SARIF v2.1.0 / errata01）：
  · runs[].tool.driver.rules 是**全集**，results[].ruleIndex 指向它（也可只用 ruleId）；
  · level ∈ {error, warning, note, none}；GitHub 另读 properties["security-severity"]；
  · fingerprints 用于跨扫描关联同一问题（这里复用 findings.fingerprint，与基线一致）。
"""

from __future__ import annotations

import json
import os
from typing import Dict, List, Optional, Sequence

from .detectors import DETECTORS
from .findings import Finding, SEVERITY_ORDER

SARIF_SCHEMA = ("https://docs.oasis-open.org/sarif/sarif/v2.1.0/errata01/os/"
                "schemas/sarif-schema-2.1.0.json")
SARIF_VERSION = "2.1.0"

# SARIF level ← 本项目 severity
_LEVEL = {"critical": "error", "high": "error", "medium": "warning",
          "low": "note", "info": "note"}

# GitHub Code Scanning 使用的安全分值（0.0~10.0），决定 Qualys 风格排序
_SECURITY_SEVERITY = {"critical": 9.5, "high": 7.5, "medium": 5.0,
                      "low": 3.0, "info": 1.0}

# 精度：SARIF 推荐值，帮助消费方判断"要不要一上来就信"
_PRECISION = {"critical": "high", "high": "high", "medium": "medium",
              "low": "medium", "info": "low"}


def _rel_uri(path: str, root: Optional[str]) -> str:
    """尽量输出相对路径（GitHub 需要相对 %SRCROOT% 才能正确定位）。"""
    if not path:
        return ""
    p = path.replace("\\", "/")
    if root:
        r = os.path.abspath(root).replace("\\", "/")
        if p.startswith(r):
            p = p[len(r):].lstrip("/")
    return p


def build_rules(dets: Optional[Sequence] = None) -> List[Dict]:
    """把检测器导出为 SARIF rules 数组。"""
    dets = dets if dets is not None else DETECTORS
    rules: List[Dict] = []
    for d in dets:
        rules.append({
            "id": d.id,
            "name": d.name,
            "shortDescription": {"text": d.name},
            "fullDescription": {"text": d.description},
            "defaultConfiguration": {"level": _LEVEL.get(d.severity, "warning")},
            "help": {
                "text": (f"{d.description}\n\n"
                         f"归属厂商：{d.provider or 'generic'}；"
                         f"严重级：{d.severity}"
                         + (f"；熵下限：{d.entropy}" if d.entropy else "")
                         + (f"；可活体验证：{d.validator}" if d.validator else "")),
            },
            "properties": {
                "tags": list(d.tags),
                "precision": _PRECISION.get(d.severity, "medium"),
                "security-severity": _SECURITY_SEVERITY.get(d.severity, 5.0),
                "provider": d.provider or "generic",
                **({"entropyRequired": d.entropy} if d.entropy else {}),
            },
        })
    # 通用高熵兜底规则（engine 动态生成，不在 DETECTORS 里）
    rules.append({
        "id": "generic-high-entropy",
        "name": "High Entropy String",
        "shortDescription": {"text": "高熵字符串（疑似未知形态凭证）"},
        "fullDescription": {"text": "无已知凭证前缀，但字符分布接近随机生成，疑似私有系统凭证"},
        "defaultConfiguration": {"level": "warning"},
        "help": {"text": "请人工确认是否为凭证；确认为误报可加入 .scanignore 指纹或行内 scanner:allow"},
        "properties": {"tags": ["generic", "entropy"], "precision": "medium",
                       "security-severity": 5.0, "provider": "generic"},
    })
    return rules


def to_sarif(findings: Sequence[Finding],
             root: Optional[str] = None,
             tool_name: str = "sectrust-scanner",
             tool_version: str = "0.4.0",
             dets: Optional[Sequence] = None,
             include_suppressed: bool = False) -> Dict:
    """把 findings 转成 SARIF 2.1.0 文档（dict）。"""
    rules = build_rules(dets)
    rule_index = {r["id"]: i for i, r in enumerate(rules)}

    results: List[Dict] = []
    for f in findings:
        if f.suppressed and not include_suppressed:
            continue
        idx = rule_index.get(f.rule_id)
        res: Dict = {
            "ruleId": f.rule_id,
            "level": _LEVEL.get(f.severity, "warning"),
            "message": {"text": _message(f)},
            "locations": [{
                "physicalLocation": {
                    "artifactLocation": {"uri": _rel_uri(f.path, root),
                                         "uriBaseId": "%SRCROOT%"},
                    "region": {"startLine": max(1, int(f.line_no or 1))},
                }
            }],
            "fingerprints": {"aiColleague/findingId": f.fingerprint},
            "properties": {
                "severity": f.severity,
                "confidence": f.confidence,
                "provider": f.provider,
                "entropy": f.entropy,
                "secretMasked": f.secret_masked,
                **({"suppressed": True, "suppressReason": f.suppress_reason}
                   if f.suppressed else {}),
            },
        }
        if idx is not None:
            res["ruleIndex"] = idx
        if f.line_masked:
            res["locations"][0]["physicalLocation"]["region"]["snippet"] = {
                "text": f.line_masked}
        results.append(res)

    return {
        "$schema": SARIF_SCHEMA,
        "version": SARIF_VERSION,
        "runs": [{
            "tool": {
                "driver": {
                    "name": tool_name,
                    "semanticVersion": tool_version,
                    "informationUri": "https://github.com/modelcontextprotocol/servers",
                    "rules": rules,
                }
            },
            "results": results,
        }],
    }


def _message(f: Finding) -> str:
    head = f"{f.rule_name}（{f.severity}）"
    detail = f"：{f.description}" if f.description else ""
    return f"{head}{detail} | {f.secret_masked}"


def to_sarif_json(findings: Sequence[Finding], root: Optional[str] = None,
                  **kw) -> str:
    return json.dumps(to_sarif(findings, root=root, **kw), ensure_ascii=False, indent=2)


def write_sarif(findings: Sequence[Finding], out_path: str,
                root: Optional[str] = None) -> str:
    doc = to_sarif_json(findings, root=root)
    tmp = out_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(doc)
    os.replace(tmp, out_path)
    return out_path


# ---------------------------------------------------------------- 自校验

def validate_sarif(doc: Dict) -> List[str]:
    """轻量结构自检（不引入 jsonschema 依赖）：检查 SARIF 必备字段与引用完整性。

    返回问题列表；空列表表示结构合规。这是"零依赖"前提下能做到的最强保证。
    """
    problems: List[str] = []
    if doc.get("version") != SARIF_VERSION:
        problems.append(f"version 应为 {SARIF_VERSION}")
    runs = doc.get("runs")
    if not isinstance(runs, list) or not runs:
        problems.append("缺少 runs")
        return problems
    for ri, run in enumerate(runs):
        driver = (run.get("tool") or {}).get("driver") or {}
        rules = driver.get("rules")
        if not isinstance(rules, list):
            problems.append(f"runs[{ri}].tool.driver.rules 缺失")
            rules = []
        rule_ids = {r.get("id") for r in rules}
        for res in run.get("results", []):
            rid = res.get("ruleId")
            if rid not in rule_ids:
                problems.append(f"result 引用了未声明的 ruleId: {rid}")
            if res.get("level") not in ("error", "warning", "note", "none"):
                problems.append(f"result level 非法: {res.get('level')}")
            locs = res.get("locations")
            if not locs:
                problems.append(f"result {rid} 缺少 locations")
            for loc in locs or []:
                phys = (loc or {}).get("physicalLocation") or {}
                if "artifactLocation" not in phys:
                    problems.append(f"result {rid} 缺少 artifactLocation")
    return problems


if __name__ == "__main__":
    import sys
    from .engine import scan_path
    target = sys.argv[1] if len(sys.argv) > 1 else "."
    res = scan_path(target)
    fs = [Finding(**{k: v for k, v in f.items() if k != "secret"}) for f in res["findings"]]
    doc = to_sarif(fs, root=target)
    print(json.dumps(validate_sarif(doc), ensure_ascii=False) or "SARIF 结构 OK")
    print(f"rules={len(doc['runs'][0]['tool']['driver']['rules'])} "
          f"results={len(doc['runs'][0]['results'])}")
