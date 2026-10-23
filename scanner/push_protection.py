"""
推送前保护（scanner/push_protection.py）—— 把扫描前移到"提交/推送之前"。

对应路线 2 的"CI / push protection"：Gitleaks/TruffleHog 的价值一半在"阻断泄露于发生前"。
本模块生成：
- 本地 git pre-push 钩子：推送前跑一次轻量正则扫描，命中 high 直接阻断（非零退出）；
- GitHub Actions workflow：push/PR 时跑扫描，失败则阻断合并。

两样都是"默认离线、零依赖"，与既有正则引擎同款；若装了 semgrep 可一键替换为深度扫描。
"""

from __future__ import annotations

import os
from typing import List


_HOOK_SCRIPT = """#!/bin/sh
# AI 同事 · 推送前密钥/敏感信息扫描（自动生成，可编辑）
# 命中 high 严重级别即阻断推送；如需临时绕过：git push --no-verify
python - <<'PY'
import os, re, sys
ROOT = os.environ.get("PUSH_PROTECT_ROOT", ".")
SECRET = re.compile(r"(?i)(api[_-]?key|secret|password|token|private[_-]?key)\\s*[:=]\\s*['\\"][^'\\"]{4,}")
high = 0
for dp, _, fnames in os.walk(ROOT):
    if os.path.basename(dp).startswith('.'):
        continue
    for fn in fnames:
        if not fn.endswith(('.py','.js','.ts','.go','.java','.yml','.yaml','.env','.json','.txt','.md')):
            continue
        fp = os.path.join(dp, fn)
        try:
            with open(fp, encoding='utf-8', errors='ignore') as fh:
                for i, line in enumerate(fh, 1):
                    if SECRET.search(line) and 'example' not in line.lower():
                        print(f"[HIGH] {fp}:{i}: {line.strip()[:120]}")
                        high += 1
        except Exception:
            pass
if high:
    print(f"\\n阻断推送：发现 {high} 处疑似密钥/敏感信息。若确认无误，使用 git push --no-verify 跳过。")
    sys.exit(1)
print("推送前扫描通过。")
PY
"""


def generate_pre_push_hook(repo_dir: str, severity_threshold: str = "high") -> str:
    """在 repo_dir/.git/hooks/pre-push 写入扫描钩子，返回路径。"""
    hook_dir = os.path.join(repo_dir, ".git", "hooks")
    if not os.path.isdir(hook_dir):
        raise NotADirectoryError(f"未在 {repo_dir} 找到 .git/hooks（非 git 仓库？）")
    # severity_threshold 仅作提示，正则引擎当前统一按"疑似密钥"阻断
    path = os.path.join(hook_dir, "pre-push")
    with open(path, "w", encoding="utf-8") as f:
        f.write(_HOOK_SCRIPT)
    os.chmod(path, 0o755)
    return path


def github_actions_yaml(severity_threshold: str = "high", use_semgrep: bool = False) -> str:
    """生成 GitHub Actions workflow YAML 文本（push/PR 触发扫描）。"""
    if use_semgrep:
        scan_step = (
            "      - name: Semgrep 深度扫描\n"
            "        run: pip install semgrep && semgrep --config auto --json -o semgrep.json || true\n"
            "      - name: 检查扫描结果\n"
            "        run: python -c \"import json,sys; d=json.load(open('semgrep.json')); "
            "sys.exit(1 if d.get('results') else 0)\"\n"
        )
    else:
        scan_step = (
            "      - name: 轻量正则扫描\n"
            "        run: python - <<'PY'\n"
            "import os, re, sys\n"
            "SECRET = re.compile(r\"(?i)(api[_-]?key|secret|password|token)\\\\s*[:=]\\\\s*['\\\"][^'\\\"]{4,}\")\n"
            "hit=0\n"
            "for dp,_,fs in os.walk('.'):\n"
            "    if os.path.basename(dp).startswith('.'): continue\n"
            "    for fn in fs:\n"
            "        if not fn.endswith(('.py','.js','.ts','.go','.yml','.env')): continue\n"
            "        try:\n"
            "            for line in open(os.path.join(dp,fn), encoding='utf-8', errors='ignore'):\n"
            "                if SECRET.search(line) and 'example' not in line.lower():\n"
            "                    print('HIGH', os.path.join(dp,fn), line.strip()[:100]); hit+=1\n"
            "        except Exception: pass\n"
            "sys.exit(1 if hit else 0)\n"
            "PY\n"
        )
    return (
        "name: Secret Scan\n"
        "on:\n"
        "  push:\n"
        "  pull_request:\n"
        "jobs:\n"
        "  scan:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - uses: actions/checkout@v4\n"
        + scan_step
    )


def available_hooks() -> List[str]:
    return ["pre-push", "github-actions"]


if __name__ == "__main__":
    print("=== GitHub Actions (regex) ===")
    print(github_actions_yaml())
