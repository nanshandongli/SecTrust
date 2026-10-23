"""
Semgrep 适配器（scanner/semgrep_adapter.py）

把「深度 SAST」能力接进来，又保持"零依赖、无 semgrep 也能跑"的底线：
- semgrep_available()：探测本机是否装了 semgrep CLI；
- scan_with_semgrep(target, config)：调用 semgrep 产出结构化 findings；
- 若 CLI 不可用或调用失败，调用方应回退到项目自带的正则扫描引擎（templates）。

设计取舍（对应路线 2：扫描深度）：
- 不把"深度"写死在依赖里。semgrep 是事实标准（规则量远超正则），但它是可选 extras；
- 默认引擎仍是零依赖正则；当用户装了 semgrep（或订阅规则集）时，自动升级到 AST 级扫描。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Dict, List, Optional


def semgrep_available() -> bool:
    """本机是否安装了 semgrep CLI。"""
    return shutil.which("semgrep") is not None


def build_semgrep_command(target: str, config: str = "auto",
                          rules_file: Optional[str] = None) -> List[str]:
    """构造 semgrep 命令。rules_file 优先（离线可用）；否则用 config（auto 取注册表，需联网）。"""
    cfg = rules_file or config
    return [
        "semgrep", "--config", cfg, "--json", "--quiet", "--no-error",
        target,
    ]


def scan_with_semgrep(target: str, config: str = "auto",
                      rules_file: Optional[str] = None,
                      timeout: float = 180) -> Dict:
    """用 semgrep 深度扫描 target。返回 {available, error?, results[], counts}。

    若 semgrep 不可用或调用失败，available=False 并给出 error，调用方据此回退正则引擎。"""
    if not semgrep_available():
        return {"available": False,
                "error": "semgrep 未安装（pip install semgrep 或加入 extras 后启用）",
                "results": [], "counts": {}}
    cmd = build_semgrep_command(target, config=config, rules_file=rules_file)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"available": True, "error": f"semgrep 超时(>{timeout}s)", "results": [],
                "counts": {}}
    except Exception as e:  # noqa: BLE001
        return {"available": True, "error": str(e), "results": [], "counts": {}}

    raw = (proc.stdout or "").strip()
    if not raw:
        # 可能 stderr 有信息（如规则拉取失败）
        return {"available": True, "error": (proc.stderr or "无输出").strip()[:500],
                "results": [], "counts": {}}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {"available": True, "error": "semgrep 输出非 JSON", "results": [], "counts": {}}

    results = []
    for r in data.get("results", []):
        results.append({
            "rule_id": r.get("check_id"),
            "severity": (r.get("extra", {}).get("severity") or "info").lower(),
            "message": r.get("extra", {}).get("message", ""),
            "path": r.get("path"),
            "line": (r.get("start", {}) or {}).get("line"),
        })
    counts: Dict[str, int] = {}
    for r in results:
        counts[r["severity"]] = counts.get(r["severity"], 0) + 1
    return {"available": True, "results": results, "counts": counts}


if __name__ == "__main__":
    import tempfile
    d = tempfile.mkdtemp()
    with open(os.path.join(d, "x.py"), "w", encoding="utf-8") as f:
        f.write("password = 'hunter2secret'\n")
    print(json.dumps(scan_with_semgrep(d), ensure_ascii=False, indent=2))
