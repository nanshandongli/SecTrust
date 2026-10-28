#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""secure_dev 模板的「提交即扫描」Git hook 入口。

把协作审计台的 `secure_dev` 模板（代码审查 → 密钥扫描 → 报告）真正接到
开发者的提交动作上：

  1) 默认（pre-commit）：同步扫描**暂存文件**里的密钥/凭证，命中 critical/high
     即阻断提交（exit 1），把密钥泄露堵在入库前——这是「提交即扫描」的硬闸门。
  2) `--plan`：额外把完整 secure_dev 模板提交给本地控制台异步跑（代码审查 + 报告），
     控制台没起则在**本进程内**直接跑（无 LLM 时仅执行扫描阶段，不卡提交）。

安装：
  python tools/secure_dev_hook.py install          # 装到当前仓库 .git/hooks/pre-commit
  # 或手动：cp tools/git_hooks/pre-commit <你的仓库>/.git/hooks/pre-commit && chmod +x

退出码：0=干净 / 1=发现阻断级密钥（已阻断提交）/ 2=用法或环境错误。
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import time

# 让 tools/ 下的脚本能直接 import 项目根模块（scanner / planner / plan_service ...）
_THIS = os.path.dirname(os.path.abspath(__file__))
_PROJECT = os.path.dirname(_THIS)
if _PROJECT not in sys.path:
    sys.path.insert(0, _PROJECT)


# ----------------------------------------------------------------- 路径/暂存
def git_top_level(repo: str) -> str:
    """返回 git 仓库根目录；非 git 仓库则抛 RuntimeError。"""
    try:
        out = subprocess.run(["git", "-C", repo, "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, check=True)
        return out.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        raise RuntimeError("不是在 git 仓库中运行：%s" % e)


def staged_files(repo: str) -> list:
    """返回本次提交暂存（已 add）的文件绝对路径列表。"""
    out = subprocess.run(
        ["git", "-C", repo, "diff", "--cached", "--name-only",
         "--diff-filter=ACMR"],
        capture_output=True, text=True, check=True)
    files = []
    for rel in out.stdout.splitlines():
        rel = rel.strip()
        if not rel:
            continue
        ap = os.path.join(repo, rel)
        if os.path.isfile(ap):
            files.append(ap)
    return files


# ----------------------------------------------------------------- 扫描闸门
def scan_paths(paths: list, root: str, fail_on: str = "high") -> dict:
    """扫描一组文件，返回 {should_fail, findings, summary}。"""
    from scanner import scan_file
    from scanner.findings import (dedupe, apply_suppressions, summarize, gate)

    lines_by_path: dict = {}
    findings = []
    for p in paths:
        try:
            findings.extend(scan_file(p, lines_out=lines_by_path))
        except Exception:  # noqa: BLE001 —— 单个文件异常不拖垮整次扫描
            continue
    findings = dedupe(findings)
    findings = apply_suppressions(findings, root=root, inline_lines=lines_by_path)
    return {
        "should_fail": gate(findings, fail_on=fail_on)["should_fail"],
        "findings": findings,
        "summary": summarize(findings),
        "gate": gate(findings, fail_on=fail_on),
    }


def scan_repo(repo: str, scope: str = "staged", fail_on: str = "high") -> dict:
    """scope=staged 只扫暂存文件；scope=all 扫整个仓库。"""
    if scope == "staged":
        paths = staged_files(repo)
        if not paths:
            return {"should_fail": False, "findings": [], "summary": {"total": 0},
                    "gate": {"should_fail": False, "blockers": 0}, "scanned": 0}
        res = scan_paths(paths, root=repo, fail_on=fail_on)
        res["scanned"] = len(paths)
        return res
    # all：直接走 scanner 的目录扫描（带仓库级抑制 + 基线）
    from scanner import scan_path
    r = scan_path(repo, fail_on=fail_on)
    r["scanned"] = r.get("scanned_files", 0)
    return r


def _fmt_findings(res: dict, limit: int = 40) -> str:
    from scanner.findings import redact
    lines = []
    sev = res.get("summary", {}).get("by_severity", {})
    if sev:
        lines.append("  按严重度： " + "  ".join(
            "%s=%d" % (k, v) for k, v in sev.items()))
    for f in (res.get("findings") or [])[:limit]:
        red = ""
        try:
            red = redact(getattr(f, "secret", "") or "")
        except Exception:  # noqa: BLE001
            red = getattr(f, "match", "") or ""
        lines.append("  · [%s] %s  %s:%s  %s" % (
            getattr(f, "severity", "?"), getattr(f, "rule_name",
                                                 getattr(f, "rule_id", "?")),
            getattr(f, "path", "?"), getattr(f, "line_no", "?"), red))
    if len(res.get("findings") or []) > limit:
        lines.append("  … 其余 %d 条见完整报告"
                     % (len(res["findings"]) - limit))
    return "\n".join(lines)


# ----------------------------------------------------------------- 模板接线
def _submit_to_console(repo: str, console_url: str, fail_on: str) -> dict:
    """把 secure_dev 模板提交给本地控制台（异步跑）。best-effort。"""
    import urllib.request
    url = console_url.rstrip("/") + "/api/plan/build"
    body = json.dumps({
        "requirement": "对代码仓库做安全审计",
        "params": {"repo_path": repo},
        "tier": "complex",
    }).encode("utf-8")
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return {"ok": True, "mode": "console", "plan_id": data.get("plan_id"),
                "created": data.get("created"), "detail": data}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "mode": "console", "error": str(e)}


def _run_plan_in_process(repo: str, fail_on: str, timeout: float) -> dict:
    """本进程内直接跑 secure_dev 模板（无 LLM 时仅扫描阶段有效）。"""
    tmp = tempfile.mkdtemp(prefix="sectrust-hook-")
    db = os.path.join(tmp, "hook.db")
    ws = os.path.join(tmp, "ws")
    os.makedirs(ws, exist_ok=True)
    from task_registry import TaskRegistry
    from trace import TraceStore
    from orchestration import DagScheduler
    from plan_service import PlanService
    from execution.daemon import ExecutionDaemon

    reg = TaskRegistry(db_path=db)
    tracer = TraceStore(db_path=db)
    sched = DagScheduler(registry=reg, tracer=tracer)
    svc = PlanService(registry=reg, tracer=tracer, scheduler=sched)
    built = svc.build("对代码仓库做安全审计", params={"repo_path": repo},
                      tier="complex")
    task_ids = built.get("created", [])

    d = ExecutionDaemon(db_path=db, workspace_root=ws, profiles=None)
    # 驱动到全部任务终态（或超时）。cap 迭代防死循环。
    cap = 400
    while cap > 0:
        cap -= 1
        tasks = reg.list_tasks()
        if tasks and all(t["status"] in ("done", "failed", "needs_human")
                         for t in tasks):
            break
        batch = d.run_once(max_tasks=4)
        if not batch:
            time.sleep(0.2)
    d.close()

    rows = []
    scan_gate = {"should_fail": False}
    for t in reg.list_tasks():
        payload = t.get("payload") or {}
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:  # noqa: BLE001
                payload = {}
        rows.append({"task_id": t["task_id"], "status": t["status"],
                     "op": payload.get("op")})
        if payload.get("op") == "scan":
            result = t.get("result") or {}
            if isinstance(result, str):
                try:
                    result = json.loads(result)
                except Exception:  # noqa: BLE001
                    result = {}
            g = (result.get("scan") or {}).get("gate") or {}
            scan_gate = {"should_fail": bool(g.get("should_fail"))}
    return {"ok": True, "mode": "in-process", "plan_id": built.get("plan_id"),
            "tasks": rows, "scan_gate": scan_gate, "tmp": tmp}


def run_plan(repo: str, console_url: str = None, fail_on: str = "high",
             timeout: float = 120.0) -> dict:
    """提交/运行 secure_dev 模板。控制台可达走 API，否则本进程跑。"""
    if console_url:
        return _submit_to_console(repo, console_url, fail_on)
    # 探测默认控制台端口（仅在未显式给 URL 时）
    detected = _detect_console()
    if detected:
        r = _submit_to_console(repo, detected, fail_on)
        if r.get("ok"):
            return r
    return _run_plan_in_process(repo, fail_on, timeout)


def _detect_console() -> str:
    """best-effort 探测本地控制台（默认 8000）。"""
    import socket
    for port in (8000, 8080, 5000):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.3)
        try:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return "http://127.0.0.1:%d" % port
        except OSError:
            pass
        finally:
            s.close()
    return ""


# ----------------------------------------------------------------- 安装
_HOOK_SHELL_TMPL = """#!/bin/sh
# 由 secure_dev_hook.py 自动安装：提交即扫描（密钥闸门）
# 仅当 SECTRUST_HOOK_DISABLE=1 时跳过（紧急绕过用，慎用）
if [ "$SECTRUST_HOOK_DISABLE" = "1" ]; then
  echo "[sectrust] hook 已禁用 (SECTRUST_HOOK_DISABLE=1)"
  exit 0
fi
PY="${SECTRUST_HOOK_PYTHON:-python3}"
SCRIPT="{SCRIPT}"
if [ -n "$SCRIPT" ] && [ -f "$SCRIPT" ]; then
  "$PY" "$SCRIPT" --scope staged
  exit $?
fi
echo "[sectrust] 未找到扫描脚本（$SCRIPT），跳过扫描"
exit 0
"""


def install_hook(repo: str) -> str:
    top = git_top_level(repo)
    hooks_dir = os.path.join(top, ".git", "hooks")
    os.makedirs(hooks_dir, exist_ok=True)
    target = os.path.join(hooks_dir, "pre-commit")
    # 不覆盖已有的、非本工具安装的 hook
    if os.path.exists(target) and "secure_dev_hook" not in _read_safe(target):
        bak = target + ".bak-" + str(int(time.time()))
        os.rename(target, bak)
        sys.stderr.write("[sectrust] 已备份原 pre-commit 到 %s\n" % bak)
    script_abs = os.path.abspath(__file__)
    with open(target, "w", encoding="utf-8") as f:
        f.write(_HOOK_SHELL_TMPL.replace("{SCRIPT}", script_abs))
    os.chmod(target, 0o755)
    return target


def _read_safe(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()
    except OSError:
        return ""


# ----------------------------------------------------------------- CLI
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="secure_dev 模板：提交即扫描 Git hook")
    ap.add_argument("cmd", nargs="?", default="scan",
                   choices=["scan", "install"],
                   help="scan=扫描(默认) / install=安装 pre-commit 钩子")
    ap.add_argument("--repo", default=".", help="仓库路径（默认当前目录）")
    ap.add_argument("--scope", default="staged", choices=["staged", "all"])
    ap.add_argument("--fail-on", default="high",
                   choices=["critical", "high", "medium", "low", "info"])
    ap.add_argument("--plan", action="store_true",
                   help="额外把 secure_dev 模板提交给控制台/本进程跑")
    ap.add_argument("--console-url", default="",
                   help="控制台地址（如 http://127.0.0.1:8000），不填则自动探测")
    ap.add_argument("--timeout", type=float, default=120.0,
                   help="本进程跑模板的超时（秒）")
    args = ap.parse_args(argv)

    if args.cmd == "install":
        try:
            target = install_hook(args.repo)
        except RuntimeError as e:
            sys.stderr.write("安装失败：%s\n" % e)
            return 2
        print("[sectrust] 已安装 pre-commit 钩子：%s" % target)
        print("[sectrust] 提交时将扫描暂存文件中的密钥；紧急绕过："
              "SECTRUST_HOOK_DISABLE=1 git commit ...")
        return 0

    try:
        repo = git_top_level(args.repo)
    except RuntimeError as e:
        sys.stderr.write("错误：%s\n" % e)
        return 2

    print("[sectrust] 扫描 %s 中的密钥（fail-on=%s）…"
          % (args.scope, args.fail_on))
    res = scan_repo(repo, scope=args.scope, fail_on=args.fail_on)
    scanned = res.get("scanned", 0)
    print("[sectrust] 已扫描 %s 个文件，发现 %d 条"
          % (scanned, len(res.get("findings") or [])))
    if res.get("findings"):
        print(_fmt_findings(res))

    blocked = res.get("should_fail")
    if args.plan:
        print("[sectrust] 提交 secure_dev 模板（代码审查→密钥扫描→报告）…")
        pr = run_plan(repo, console_url=args.console_url or None,
                      fail_on=args.fail_on, timeout=args.timeout)
        if pr.get("mode") == "console":
            if pr.get("ok"):
                print("[sectrust] 已提交控制台异步执行：plan_id=%s（%d 道工序）"
                      % (pr.get("plan_id"), len(pr.get("created") or [])))
            else:
                print("[sectrust] 控制台提交失败（%s），回退本进程扫描已完成"
                      % pr.get("error"))
        else:
            print("[sectrust] 本进程执行：%s"
                  % ", ".join("%s=%s" % (t["task_id"].split("/")[-1], t["status"])
                              for t in pr.get("tasks", [])))
            # 本进程扫描阶段的闸门结果覆盖同步闸门判定
            blocked = blocked or pr.get("scan_gate", {}).get("should_fail")

    if blocked:
        sys.stderr.write(
            "\n[sectrust] ✗ 发现阻断级密钥（%s 及以上），提交已被拦截。\n"
            "[sectrust]   请移除硬编码密钥或改用环境变量/密钥管理；"
            "紧急绕过：SECTRUST_HOOK_DISABLE=1 git commit（不推荐）。\n"
            % args.fail_on)
        return 1
    print("[sectrust] ✓ 未发现阻断级密钥。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
