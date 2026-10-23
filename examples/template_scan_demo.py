"""
合规扫描模板库演示（离线、无需密钥、约 1 分钟可复现）

依次用「审批中心 + 沙箱隔离」跑全部内置模板，展示每个模板的发现：
  - generic_sast          代码卫生基线
  - secret_leak           密钥/Token 泄露
  - hardcoded_credential  硬编码凭证
  - dependency_vuln       依赖漏洞（离线启发式）
  - cicd_config           CI/CD 配置安全

用法：
  python examples/template_scan_demo.py
  # 可选：指定被扫描目录
  python examples/template_scan_demo.py --repo-dir /path/to/repo

说明：
- 默认扫描 examples/sample_repo（已内置一个故意「有问题」的样例：含漏洞依赖、
  假密钥 .env、危险的 GitHub Actions 工作流），便于直观看到发现。
- 沙箱后端默认 auto（Docker 可达则强隔离，否则本地 jail 降级）。
- 所有扫描都经过「审批中心 + 沙箱」双闸门——此处用 auto_approve 演示，生产请改为人工批准。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 强制 UTF-8 输出，避免 Windows 控制台代码页导致的非 ASCII（中文）乱码
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

from templates import list_templates, run_scan


HERE = os.path.dirname(os.path.abspath(__file__))
SAMPLE_REPO = os.path.join(HERE, "sample_repo")


def _print_report(template_id: str, res: dict):
    if res.get("status") != "done":
        print(f"  ⚠️ 状态={res.get('status')} 错误={res.get('error')}（后端={res.get('backend')}）")
        return
    report = res.get("report") or {}
    findings = report.get("findings", [])
    count_keys = [k for k in ("scanned_files", "manifests_scanned", "configs_scanned") if k in report]
    counted = "  ".join(f"{k}={report[k]}" for k in count_keys)
    print(f"  ✅ 后端={res.get('backend')}  耗时={res.get('duration')}s  {counted}")
    if findings:
        for f in findings[:12]:
            sev = f.get("severity", "?")
            print(f"     [{sev.upper():>6}] {f.get('rule')}  {f.get('file')}:{f.get('line')}  {f.get('match')}")
        if len(findings) > 12:
            print(f"     … 共 {len(findings)} 条发现")
    else:
        print("     未发现风险项")


def main():
    ap = argparse.ArgumentParser(description="合规扫描模板库演示")
    ap.add_argument("--repo-dir", default=SAMPLE_REPO)
    ap.add_argument("--backend", default="auto", choices=["auto", "docker", "local"])
    ap.add_argument("--db-path", default=None, help="可选，指定临时/复用的 SQLite")
    args = ap.parse_args()

    repo_dir = os.path.abspath(args.repo_dir)
    if not os.path.isdir(repo_dir):
        print(f"仓库目录不存在: {repo_dir}")
        sys.exit(1)

    db_path = args.db_path or os.path.join(tempfile.mkdtemp(), "demo.db")

    print("=" * 64)
    print(" 合规扫描模板库演示 —— 审批中心 + 沙箱隔离 + 任务登记处")
    print(f" 目标目录: {repo_dir}")
    print(f" 沙箱后端: {args.backend}   DB: {db_path}")
    print("=" * 64)

    templates = list_templates()
    print(f"内置模板（{len(templates)} 个）:")
    for t in templates:
        print(f"  - {t['id']:<22} [{t['severity']:<6}] {t['name']}")

    for t in templates:
        print(f"\n>>> 运行模板: {t['id']}（{t['name']}）")
        try:
            res = run_scan(
                t["id"], repo_dir, db_path=db_path,
                sandbox_backend=args.backend, auto_approve=True,
                channel="demo", user_id="demo",
            )
            _print_report(t["id"], res)
        except Exception as e:  # noqa: BLE001
            print(f"  ⚠️ 执行异常: {e}")

    print("\n" + "=" * 64)
    print("演示结束。查看实时进度/审批：")
    print(f"  python task_center_web.py --db-path {db_path} --port 8090")
    print("=" * 64)


if __name__ == "__main__":
    main()
