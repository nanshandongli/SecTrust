"""
合规扫描模板库 + 执行编排（Scan Template Library & Orchestration）

职责：
- 维护「扫描模板」注册表（templates/builtin.py 内置 5 个）；
- 提供编排函数，把「模板 + 审批中心 + 沙箱 + 任务登记处」串成一条可审计的执行链：
      request_scan  ->  进入审批中心(pending, 默认拒绝)
      approve       ->  真人/自动批准
      execute_scan  ->  沙箱隔离执行模板脚本，结果写回登记处
  （也可用 run_scan(auto_approve=True) 一步到位，便于可信自动化）

所有对外暴露的执行都**强制经过审批 + 沙箱**两道闸门，与产品的安全叙事一致。

信任边界：
- 模板脚本只在 sandbox 内运行（Docker/E2B 强隔离，或本地 jail 降级），不联网、不写外部文件；
- 模板脚本以「文件」形式落到 templates/_scripts/ 后由 python 执行，因此脚本源码里的
  /etc、C:\\Windows 等正则字面量不会进入命令行参数，不会误触 sandbox 的越权预检。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Callable, Optional

from approval import ApprovalCenter
from config import load_config
from sandbox import DockerSandbox, LocalSandbox, SandboxManager
from task_registry import TaskRegistry


_SCRIPTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_scripts")

TEMPLATES: dict = {}


@dataclass
class ScanTemplate:
    """一个合规扫描模板：id / 名称 / 描述 / 严重级别 / 沙箱内脚本或自定义命令。"""

    id: str
    name: str
    description: str
    severity: str  # low / medium / high
    script: str = ""                                   # 沙箱内执行的 Python 脚本（为空则用 command）
    command: Optional[Callable[[str], list]] = None     # 自定义命令构造（如复用通用脚本）
    requires_approval: bool = True

    def build_command(self, workdir: str) -> list:
        if self.command is not None:
            return self.command(workdir)
        return _build_python_cmd(self.id, self.script, workdir)


def register(template: ScanTemplate) -> None:
    TEMPLATES[template.id] = template


def list_templates() -> list:
    return [
        {
            "id": t.id,
            "name": t.name,
            "description": t.description,
            "severity": t.severity,
            "requires_approval": t.requires_approval,
        }
        for t in TEMPLATES.values()
    ]


def get_template(template_id: str) -> Optional[ScanTemplate]:
    return TEMPLATES.get(template_id)


# ---- 脚本文件化（避免脚本源码出现在命令行触发越权预检）----
def _materialize(name: str, script: str) -> str:
    os.makedirs(_SCRIPTS_DIR, exist_ok=True)
    path = os.path.join(_SCRIPTS_DIR, f"{name}.py")
    # 始终覆盖：模板脚本内嵌于 builtin.py，源码更新后不应残留旧版本
    with open(path, "w", encoding="utf-8") as f:
        f.write(script)
    return path


def _build_python_cmd(name: str, script: str, workdir: str) -> list:
    return [sys.executable, _materialize(name, script), workdir]


# ---- 编排层 ----
def _db_path(db_path: Optional[str] = None) -> str:
    if db_path:
        return db_path
    try:
        return load_config().db_path
    except Exception:
        return TaskRegistry.DEFAULT_DB_PATH


def _resolve_backend(name: str):
    if name == "docker":
        return DockerSandbox()
    if name == "local":
        return LocalSandbox()
    if name in ("local-enforced", "enforced"):
        from sandbox import EnforcedLocalSandbox
        return EnforcedLocalSandbox()
    return SandboxManager.auto()


def parse_report(content: str) -> dict:
    """从沙箱 stdout 中提取最后一行 JSON 报告。"""
    if not content or not content.strip():
        return {}
    for line in reversed(content.strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except Exception:
                pass
    return {}


def request_scan(
    template_id: str,
    repo_dir: str,
    db_path: Optional[str] = None,
    channel: str = "api",
    user_id: str = "api",
    repo_label: Optional[str] = None,
) -> dict:
    """发起一次扫描请求：登记任务 + 进入审批中心(pending)。默认拒绝，需批准后才执行。
    返回 {task_id, approval_id, status:'pending', ...}。"""
    tmpl = get_template(template_id)
    if tmpl is None:
        raise ValueError(f"未知扫描模板: {template_id}")
    db = _db_path(db_path)
    reg = TaskRegistry(db_path=db)
    ac = ApprovalCenter(db_path=db)
    tid = f"scan-{template_id}-{uuid.uuid4().hex[:8]}"
    label = repo_label or repo_dir
    reg.register_task(
        tid,
        payload={"channel": channel, "user_id": user_id,
                 "action": f"scan:{template_id}", "repo": label, "template": template_id},
    )
    reg.update_task_status(tid, "running")
    aid = ac.request(
        "scan_repo", f"{template_id}::{label}", task_id=tid,
        channel=channel, user_id=user_id,
    )
    reg.set_progress(tid, "pending_approval", f"进入审批中心 {aid}，等待批准（Human-in-the-loop）")
    reg.close()
    ac.close()
    return {"task_id": tid, "approval_id": aid, "status": "pending",
            "template": template_id, "repo": label}


def execute_scan(
    task_id: str,
    approval_id: str,
    repo_dir: str,
    db_path: Optional[str] = None,
    sandbox_backend: str = "auto",
    repo_label: Optional[str] = None,
    timeout: float = 120,
    template_id: Optional[str] = None,
    engine: str = "regex",
) -> dict:
    """执行一次已批准的扫描：沙箱隔离运行模板脚本，结果写回登记处。
    若审批未批准，直接返回 blocked（绝不执行）。

    engine：
      - "regex"（默认）：用内嵌正则模板，零依赖、离线可用；
      - "semgrep"：若本机装有 semgrep，额外跑一次 AST 级深度扫描并合并发现；
        若 semgrep 不可用，自动回退到 regex 并在报告中标注。"""
    db = _db_path(db_path)
    reg = TaskRegistry(db_path=db)
    ac = ApprovalCenter(db_path=db)
    if not ac.is_approved(approval_id):
        st = ac.status(approval_id)
        reg.close()
        ac.close()
        return {"task_id": task_id, "approval_id": approval_id,
                "status": "blocked", "reason": f"审批状态={st}，未批准不执行"}

    label = repo_label or repo_dir
    # 把目标复制到 jail 工作目录（生产环境可替换为 git clone 到沙箱）
    workdir = os.path.abspath(os.path.join(".scan_jail", task_id))
    shutil.rmtree(workdir, ignore_errors=True)
    shutil.copytree(repo_dir, workdir)

    tmpl_id = template_id or "generic_sast"
    tmpl = get_template(tmpl_id) or get_template("generic_sast")

    reg.set_progress(task_id, "approved", "已批准，进入沙箱")
    sb = _resolve_backend(sandbox_backend)
    res = sb.run(tmpl.build_command(workdir), workdir, timeout=timeout)
    if not res.ok:
        reg.set_progress(task_id, "sandbox_error", f"沙箱执行失败: {res.error}")
        reg.update_task_status(task_id, "failed")
        reg.close()
        ac.close()
        shutil.rmtree(workdir, ignore_errors=True)
        return {"task_id": task_id, "approval_id": approval_id, "status": "failed",
                "error": res.error, "backend": res.backend}

    report = parse_report(res.content)
    engine_used = "regex"

    # 可选：semgrep 深度扫描（装了才跑；否则回退）
    if engine == "semgrep":
        try:
            from scanner.semgrep_adapter import semgrep_available, scan_with_semgrep
            if semgrep_available():
                sg = scan_with_semgrep(workdir, timeout=timeout)
                if sg.get("available") and not sg.get("error"):
                    report = _merge_semgrep(report, sg)
                    engine_used = "semgrep"
                else:
                    report.setdefault("notes", []).append(
                        f"semgrep 不可用，已回退正则引擎：{sg.get('error')}")
            else:
                report.setdefault("notes", []).append(
                    "semgrep 未安装，使用正则引擎（pip install semgrep 可升级）")
        except Exception as e:  # noqa: BLE001
            report.setdefault("notes", []).append(f"semgrep 调用异常，已回退正则：{e}")

    reg.set_progress(task_id, 100, "扫描完成，结果已生成")
    reg.update_task_status(task_id, "done")
    reg.close()
    ac.close()
    shutil.rmtree(workdir, ignore_errors=True)
    return {"task_id": task_id, "approval_id": approval_id, "status": "done",
            "backend": res.backend, "duration": round(res.duration, 2),
            "engine_used": engine_used, "report": report}


def _merge_semgrep(regex_report: dict, semgrep_report: dict) -> dict:
    """把 semgrep 的结构化发现合并进正则报告，统一到 findings 列表。"""
    out = dict(regex_report)
    findings = out.get("findings") or []
    for r in semgrep_report.get("results", []):
        findings.append({
            "source": "semgrep",
            "rule_id": r.get("rule_id"),
            "severity": r.get("severity"),
            "message": r.get("message"),
            "path": r.get("path"),
            "line": r.get("line"),
        })
    out["findings"] = findings
    # 用 semgrep 的 severity 计数补充
    sg_counts = semgrep_report.get("counts", {})
    if sg_counts:
        out["semgrep_counts"] = sg_counts
    out["scanners"] = ["regex", "semgrep"]
    return out


def run_scan(
    template_id: str,
    repo_dir: str,
    db_path: Optional[str] = None,
    sandbox_backend: str = "auto",
    auto_approve: bool = False,
    channel: str = "api",
    user_id: str = "api",
    repo_label: Optional[str] = None,
    timeout: float = 120,
    engine: str = "regex",
) -> dict:
    """便捷入口：
    - auto_approve=False：仅发起审批（pending），由人工/外部批准后再 execute_scan；
    - auto_approve=True：自动批准并立即执行，返回完整报告（用于可信自动化 / CLI 演示）。"""
    req = request_scan(
        template_id, repo_dir, db_path=db_path,
        channel=channel, user_id=user_id, repo_label=repo_label,
    )
    if auto_approve:
        ac = ApprovalCenter(db_path=_db_path(db_path))
        ac.decide(req["approval_id"], "approve", by="auto")
        ac.close()
        return execute_scan(
            req["task_id"], req["approval_id"], repo_dir,
            db_path=db_path, sandbox_backend=sandbox_backend,
            repo_label=repo_label, timeout=timeout, template_id=req["template"],
            engine=engine,
        )
    return req


# 注册内置模板（必须在 register / ScanTemplate / 编排函数定义之后）
from . import builtin  # noqa: F401
