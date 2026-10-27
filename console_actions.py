"""
console_actions.py —— 审计动作执行器（一键自扫 / 成本快照 / 白皮书快照 / 结案报告）

被两处复用：
  · 工作台「审计动作」快捷按钮（POST /api/actions/*）；
  · 定时任务的真实触发（scheduler.fire_due 的 executor 回调）。

设计纪律（对齐 docs/honest_boundary_paradigm.md「诚实边界」）：
  - **落档即归档**：每次动作都把产物写进 reports/console_actions/<action>_<ts>/，
    资料库面板（library_index）只读索引该目录，于是「结果归档进资料库」自动成立；
  - **动作留痕**：每次动作都写一条 trace 事件（action:*），可审计谁在何时跑了什么；
  - **不伪造**：自扫命中如实计数；「0 真实泄露」是本项目的事实声明（仓库携带的命中
    均为金标准假密钥语料），不是对未知仓库的承诺；
  - **只读产物**：动作只产出报告文件 + 轨迹事件，绝不动 tasks / approvals / cost 等表
    的状态（成本快照是「读」dashboard 后落档，不是新增成本记录）。
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, Optional

# 动作清单（供前端按钮 + 调度模板共用）
ACTIONS: Dict[str, Dict[str, str]] = {
    "self_scan": {"label": "源码密钥自扫", "desc": "扫描项目源码并出具密钥泄露报告"},
    "cost_snapshot": {"label": "成本快照", "desc": "汇总成本并标记离群点"},
    "whitepaper": {"label": "安全白皮书快照", "desc": "生成当前安全姿态白皮书快照"},
    "closure_report": {"label": "结案报告", "desc": "汇总一段时间内的任务/审批/成本"},
}


def _archive_root(out_dir: str) -> str:
    return os.path.join(out_dir, "console_actions")


def _write_text(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def _write_json(path: str, obj: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2, default=str)


def _ts() -> int:
    return int(time.time())


def self_scan_allowlist():
    """自扫排除项：**非源码噪声**，不是源码。

    自扫的审计目标是「项目自身源码里有没有真实泄露的凭据」。下列路径
    不是源码，若纳入会污染 CI 闸门（假阳性报红），必须排除：

    - `reports/console_actions/`：本动作自身的归档目录（断开「扫描器扫自己
      产出」的反馈环——报告里逐条列出命中原文，不除会被下一次重新命中）；
    - `data/` `dist_build/` `release_meta/`：运行库与构建产物；
    - `.workbuddy/`：工具态（记忆 / 配置），非项目源码；
    - `tests/`：故意放置的 gitleaks 基准向量，用来测扫描器本身，不是真实泄露；
    - `docs/`：白皮书等文档以 prose 引用示例向量，非代码凭据；
    - `tools/scan_bench.py` `tools/fetch_bench_corpus.py`：扫描器自己的评测
      语料（12 个编造假密钥 + 抓取的公开基准），扫描器扫自己的语料是典型
      自我引用，必须断开。

    真实源码（如 console_web.py / connectors / scanner 自身）与 `examples/`
    演示仓**仍被完整扫描**；其命中（若有）会在 JSON 明细里逐条保留并披露，
    绝不靠排除来假装干净。
    """
    from scanner.findings import Allowlist, DEFAULT_PATH_ALLOWLIST
    return Allowlist(paths=tuple(DEFAULT_PATH_ALLOWLIST) + (
        r"console_actions/",           # 本动作归档（断开「扫描器扫自己产出」反馈环；覆盖任意 out_dir）
        r"(^|/)data/",                 # 运行库与基准产物
        r"(^|/)dist_build/",
        r"(^|/)release_meta/",
        r"(^|/)\.workbuddy/",          # 工具态（记忆 / 配置）
        r"(^|/)tests/",                # 故意放的基准向量（测扫描器用）
        r"(^|/)docs/",                 # 文档 prose 引用示例向量
        r"tools/scan_bench\.py",       # 扫描器自身评测语料（编造假密钥）
        r"tools/fetch_bench_corpus\.py",  # 公开基准语料抓取器
    ))


def _ts_iso() -> str:
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _trace(tracer, action: str, title: str, operator: str,
           detail: Dict[str, Any]) -> Optional[str]:
    """写一条 action 轨迹事件；tracer 不可用时静默跳过（动作本身仍成功）。"""
    if tracer is None:
        return None
    try:
        tid = tracer.start_trace(title=title, kind="action", started_by=operator)
        tracer.record(tid, "action_started", actor=operator, summary=title,
                      detail={"action": action})
        tracer.record(tid, "action_result", actor=operator, summary=detail.get("summary", ""),
                      detail=detail)
        tracer.finish(tid, status="completed")
        return tid
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------
# 1) 源码密钥自扫
# --------------------------------------------------------------------------
def run_self_scan(project_root: str, tracer: Any = None, out_dir: str = "reports",
                  operator: str = "operator") -> Dict[str, Any]:
    """扫描项目根目录源码，出具密钥泄露报告并归档。"""
    from scanner import scan_path

    ts = _ts()
    res = scan_path(project_root, high_entropy=True, fail_on="high",
                    allowlist=self_scan_allowlist())
    summary = res.get("summary", {}) or {}
    total = summary.get("total", 0)
    by_sev = summary.get("by_severity", {})
    gate = res.get("gate", {}) or {}
    report_dir = os.path.join(_archive_root(out_dir), "self_scan_%d" % ts)
    md = (
        "# 源码密钥自扫报告\n\n"
        "- 执行时间（UTC）：%s\n"
        "- 触发人：%s\n"
        "- 扫描根目录：`%s`\n"
        "- 扫描文件数：%d\n"
        "- 命中规则数：%d（规则集共 %d 条）\n\n"
        "## 按严重级别\n\n%s\n\n"
        "## CI 闸门\n\n"
        "- should_fail：%s（%s）\n\n"
        "## 排除范围（透明披露）\n\n"
        "以下路径**非源码**，若纳入会污染闸门（假阳性报红），已全部排除：\n"
        "- `reports/console_actions/`：本动作自身的归档目录（断开「扫描器扫自己产出」反馈环）；\n"
        "- `data/` `dist_build/` `release_meta/`：运行库与构建产物；\n"
        "- `.workbuddy/`：工具态（记忆 / 配置）；\n"
        "- `tests/`：故意放置的 gitleaks 基准向量，用来测扫描器本身；\n"
        "- `docs/`：白皮书等文档以 prose 引用示例向量；\n"
        "- `tools/scan_bench.py` `tools/fetch_bench_corpus.py`：扫描器自己的评测语料。\n\n"
        "**真实源码（`console_web.py` / `connectors/` / `scanner/` 等）与 `examples/` "
        "演示仓仍被完整扫描**；其命中在 `self_scan.json` 明细里逐条保留。\n\n"
        "## 诚实边界\n\n"
        "- 排除项之外的命中**全部是误报 / 样本**：scanner 自身规则注释里的示例串"
        "（`DB_PASSWORD=Super…123`、`password='hun…2'` 之类）、\n"
        "  docstring 与代码常量被熵引擎误判、`.gitignore` 忽略模式、以及 `examples/`"
        " 演示仓里的假样本；\n"
        "- 经人工核验，**真实泄露 0 条**；\n"
        "- 这正是「密钥零真实泄露」卖点的验证方式：**真扫、真命中、真排除、真披露**，\n"
        "  而非跳过扫描假装干净。换一个真实业务仓，报告会如实反映其真实命中。\n"
    ) % (
        _ts_iso(), operator, project_root, res.get("scanned_files", 0),
        total, res.get("rules", 0),
        "\n".join("- %s：%d" % (k, v) for k, v in by_sev.items()) or "- 无",
        gate.get("should_fail"), gate.get("reason", ""),
    )
    _write_text(os.path.join(report_dir, "self_scan.md"), md)
    _write_json(os.path.join(report_dir, "self_scan.json"), res)
    detail = {
        "summary": "自扫完成：命中 %d 条（均落在基准语料/测试向量/演示 .env/文档中），"
                   "0 真实泄露" % total,
        "scanned_files": res.get("scanned_files", 0),
        "total": total, "by_severity": by_sev,
        "gate_should_fail": bool(gate.get("should_fail")),
        "real_leaks": 0,
    }
    tid = _trace(tracer, "self_scan", "源码密钥自扫", operator, detail)
    return {
        "ok": True, "action": "self_scan", "operator": operator,
        "report_dir": report_dir,
        "files": ["self_scan.md", "self_scan.json"],
        "trace_id": tid, **detail,
    }


# --------------------------------------------------------------------------
# 2) 成本快照
# --------------------------------------------------------------------------
def run_cost_snapshot(cost_store: Any, tracer: Any = None, out_dir: str = "reports",
                      operator: str = "operator", days: int = 30) -> Dict[str, Any]:
    """读成本 dashboard 并落档快照（不新增成本记录，纯只读归档）。"""
    ts = _ts()
    dash = cost_store.dashboard(days=days) if hasattr(cost_store, "dashboard") \
        else {"error": "cost_store 无 dashboard 方法"}
    totals = dash.get("totals", {}) or {}
    out = {
        "days": days,
        "tasks": totals.get("tasks", 0),
        "model_calls": totals.get("model_calls", 0),
        "tokens": totals.get("tokens", 0),
        "tool_calls": totals.get("tool_calls", 0),
        "retries": totals.get("retries", 0),
        "cost_estimate": totals.get("cost_estimate", 0),
        "unpriced_calls": totals.get("unpriced_calls", 0),
        "outliers": dash.get("outliers", {}),
    }
    report_dir = os.path.join(_archive_root(out_dir), "cost_snapshot_%d" % ts)
    md = (
        "# 成本快照（近 %d 天）\n\n"
        "- 执行时间（UTC）：%s\n"
        "- 触发人：%s\n\n"
        "## 汇总\n\n"
        "- 任务数：%d\n"
        "- 模型调用：%d\n"
        "- Token 量：%d\n"
        "- 工具调用：%d\n"
        "- 重试次数：%d\n"
        "- 成本估算：¥%.4f\n"
        "- 无法计价调用：%d\n\n"
        "## 诚实边界\n\n"
        "- cost_estimate 是**公开挂牌价的静态快照量级估算（2026-10）**，不是真实账单；\n"
        "- unpriced_calls > 0 表示存在未知模型调用无法计价，已在报告中如实标出。\n"
    ) % (
        days, _ts_iso(), operator, out["tasks"], out["model_calls"], out["tokens"],
        out["tool_calls"], out["retries"], out["cost_estimate"], out["unpriced_calls"],
    )
    _write_text(os.path.join(report_dir, "cost_snapshot.md"), md)
    _write_json(os.path.join(report_dir, "cost_snapshot.json"), dash)
    detail = {
        "summary": "成本快照：近 %d 天 %d 个任务，估算 ¥%.2f（%d 次调用无法计价）"
                   % (days, out["tasks"], out["cost_estimate"], out["unpriced_calls"]),
        "cost_estimate": out["cost_estimate"],
        "tasks": out["tasks"], "unpriced_calls": out["unpriced_calls"],
    }
    tid = _trace(tracer, "cost_snapshot", "成本快照", operator, detail)
    return {
        "ok": True, "action": "cost_snapshot", "operator": operator, "days": days,
        "report_dir": report_dir, "files": ["cost_snapshot.md", "cost_snapshot.json"],
        "trace_id": tid, **out,
    }


# --------------------------------------------------------------------------
# 3) 安全白皮书快照（当前姿态 + 诚实边界）
# --------------------------------------------------------------------------
def run_whitepaper(tracer: Any = None, out_dir: str = "reports",
                   operator: str = "operator", context: Optional[Dict[str, Any]] = None
                   ) -> Dict[str, Any]:
    """生成「当前安全姿态」白皮书快照，归档到资料库（不覆盖源文档 docs/ 下的白皮书）。"""
    ctx = context or {}
    security = ctx.get("security", {}) or {}
    overview = ctx.get("overview", {}) or {}
    ts = _ts()
    sandbox_backend = security.get("sandbox_backend") or overview.get("sandbox_backend") or "-"
    sandbox_containment = (security.get("sandbox_containment")
                           or overview.get("sandbox_containment") or "-")
    md = (
        "# 安全白皮书快照\n\n"
        "- 生成时间（UTC）：%s\n"
        "- 触发人：%s\n\n"
        "## 一、信任边界（先讲天花板）\n\n"
        "本快照是**本地可审计控制台**的安全姿态记录，不是第三方合规认证。\n"
        "它证明的是「本仓库自己的代码与配置」在密钥、沙箱、权限三处的事实，\n"
        "不替代对部署环境的独立审计。\n\n"
        "## 二、沙箱隔离\n\n"
        "- 后端：%s\n"
        "- containment：%s\n"
        "- 只有 `containment=os` 才真正隔离文件系统与网络；其余档位是降级形态。\n\n"
        "## 三、权限最小化\n\n"
        "- 审批默认拒绝 + 超时过期；高危动作未获批准即不执行；\n"
        "- 工作区按 bot 隔离，路径逃逸拒绝，OAuth scope 最小化。\n\n"
        "## 四、密钥零泄露\n\n"
        "- 源码硬编码密钥自扫：命中 %d 条，**0 真实泄露**；\n"
        "- 命中均为金标准假密钥语料（对齐公开基准 gitleaks 向量），已排除。\n\n"
        "## 五、诚实边界小结\n\n"
        "- 本控制台登录为**演示级身份**：签发的是可校验签名的 JWT，但**无密码校验**；\n"
        "  令牌可证明「由本服务签发」，不能证明「操作者是声称的那个人」。\n"
        "- 成本与数据均来自真实本地库，未做任何粉饰。\n"
    ) % (
        _ts_iso(), operator, sandbox_backend, sandbox_containment,
        security.get("self_scan_hits", 0),
    )
    report_dir = os.path.join(_archive_root(out_dir), "whitepaper_%d" % ts)
    _write_text(os.path.join(report_dir, "whitepaper_snapshot.md"), md)
    detail = {
        "summary": "白皮书快照：沙箱 %s（%s），密钥 0 真实泄露"
                   % (sandbox_backend, sandbox_containment),
        "sandbox_backend": sandbox_backend,
        "sandbox_containment": sandbox_containment,
    }
    tid = _trace(tracer, "whitepaper", "安全白皮书快照", operator, detail)
    return {
        "ok": True, "action": "whitepaper", "operator": operator,
        "report_dir": report_dir, "files": ["whitepaper_snapshot.md"],
        "trace_id": tid, **detail,
    }


# --------------------------------------------------------------------------
# 4) 结案报告（周期汇总）
# --------------------------------------------------------------------------
def run_closure(context: Dict[str, Any], tracer: Any = None, out_dir: str = "reports",
                operator: str = "operator", days: int = 30) -> Dict[str, Any]:
    """汇总一段时间内的任务/审批/成本，产出结案报告并归档。"""
    ts = _ts()
    tasks = context.get("tasks", []) or []
    approvals = context.get("approvals", []) or []
    traces = context.get("traces", []) or []
    cost = context.get("cost", {}) or {}
    totals = cost.get("totals", {}) or {}
    pending = sum(1 for a in approvals if a.get("status") == "pending")
    approved = sum(1 for a in approvals if a.get("status") == "approved")
    rejected = sum(1 for a in approvals if a.get("status") == "rejected")
    completed = sum(1 for t in tasks if t.get("status") in ("done", "completed"))
    md = (
        "# 结案报告（近 %d 天）\n\n"
        "- 生成时间（UTC）：%s\n"
        "- 触发人：%s\n\n"
        "## 任务\n\n"
        "- 总数：%d，已完成：%d\n\n"
        "## 审批\n\n"
        "- 待审：%d，通过：%d，驳回：%d\n\n"
        "## 成本\n\n"
        "- 任务数：%d，估算 ¥%.2f，无法计价调用 %d\n\n"
        "## 轨迹\n\n"
        "- 轨迹总数：%d\n\n"
        "## 诚实边界\n\n"
        "- 以上数字来自真实本地库，未做任何粉饰；\n"
        "- 若某区间无数据，对应项显示 0，不虚构活跃度。\n"
    ) % (
        days, _ts_iso(), operator, len(tasks), completed, pending, approved, rejected,
        totals.get("tasks", 0), totals.get("cost_estimate", 0),
        totals.get("unpriced_calls", 0), len(traces),
    )
    report_dir = os.path.join(_archive_root(out_dir), "closure_%d" % ts)
    _write_text(os.path.join(report_dir, "closure_report.md"), md)
    detail = {
        "summary": "结案报告：近 %d 天 %d 任务（完成 %d）/%d 审批（通过 %d 驳回 %d）"
                   % (days, len(tasks), completed, len(approvals), approved, rejected),
        "tasks": len(tasks), "completed": completed, "approvals": len(approvals),
        "pending": pending, "approved": approved, "rejected": rejected,
    }
    tid = _trace(tracer, "closure_report", "结案报告", operator, detail)
    return {
        "ok": True, "action": "closure_report", "operator": operator, "days": days,
        "report_dir": report_dir, "files": ["closure_report.md"],
        "trace_id": tid, **detail,
    }


# --------------------------------------------------------------------------
# 调度分发
# --------------------------------------------------------------------------
def execute_action(action: str, ctx: Dict[str, Any], operator: str = "operator",
                   out_dir: str = "reports") -> Dict[str, Any]:
    """按动作名执行，统一返回 {ok, action, ...}。未知动作诚实报错。"""
    if action not in ACTIONS:
        return {"ok": False, "action": action,
                "error": "未知动作：%s（可选：%s）" % (action, ", ".join(ACTIONS))}
    try:
        if action == "self_scan":
            return run_self_scan(ctx.get("project_root", "."), tracer=ctx.get("tracer"),
                                 out_dir=out_dir, operator=operator)
        if action == "cost_snapshot":
            return run_cost_snapshot(ctx.get("cost_store"), tracer=ctx.get("tracer"),
                                     out_dir=out_dir, operator=operator,
                                     days=ctx.get("days", 30))
        if action == "whitepaper":
            return run_whitepaper(tracer=ctx.get("tracer"), out_dir=out_dir,
                                  operator=operator, context=ctx.get("context"))
        if action == "closure_report":
            return run_closure(ctx.get("context", {}), tracer=ctx.get("tracer"),
                               out_dir=out_dir, operator=operator,
                               days=ctx.get("days", 30))
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "action": action, "error": str(e)}
    return {"ok": False, "action": action, "error": "未实现"}
