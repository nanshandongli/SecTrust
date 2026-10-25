"""
通用结果汇总（summarizer.py）—— 让流水线「端到端真正闭环」的最后一块砖。

编排层已经有：Planner（拆解）→ DagScheduler（依赖/就绪/并行波次）→
ExecutionDaemon（执行）→ QualityGate（质量回流）。但缺一个**收口**阶段：
所有任务跑完后，把分散在各任务 payload.result 里的产出，聚合成一份人能直接
看的**交付物**。

ResultSummarizer 做这件事，且刻意做成「通用」：
- 不关心任务是什么 op、属于哪个模板；它只读 tasks 表：status + payload。
- 产出 = payload["result"]（执行层 complete 时写入）；失败原因 = payload["error"]。
- 产物清单 = 约定键 result["artifacts"]（路径列表）；shell 任务的 stdout 也算产出。
- 未完成标记：任何 failed / blocked / needs_human / running / registered 都视为
  「流水线尚未干净结束」，汇总里明确列出，绝不假装完成。

用法：
    from summarizer import ResultSummarizer
    md, data = ResultSummarizer().summarize(task_registry)
    # md 是 markdown 交付物；data 是结构化结果（可二次处理/落库）

或 CLI：
    python summarizer.py --db-path data/task_registry.db
"""

from __future__ import annotations

import json
import os
import time
from typing import List, Optional


# 未结束（流水线不干净）的状态集合
_INCOMPLETE = {"failed", "blocked", "needs_human", "running", "registered"}


def _fmt_ts(ts):
    """把 unix 时间戳渲染成可读时间；None/异常 → 占位。"""
    if ts is None:
        return "—"
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
    except Exception:
        return str(ts)


def _render_trace_events(events, limit: int = 60) -> str:
    """把轨迹事件渲染成时序表（折叠超长）。"""
    if not events:
        return "（无轨迹事件）"
    lines = ["| # | 时间 | 类型 | 角色 | 摘要 |",
             "|----|------|------|------|------|"]
    for e in events[:limit]:
        lines.append("| %s | %s | %s | %s | %s |" % (
            e.get("seq"), _fmt_ts(e.get("ts")), e.get("kind", ""),
            e.get("actor") or "—", (e.get("summary") or "").replace("|", "\\|")[:60]))
    if len(events) > limit:
        lines.append("| … | | | | 另有 %d 条（已折叠）|" % (len(events) - limit))
    return "\n".join(lines)


def _render_cost(section) -> str:
    """渲染单任务成本视图（快照优先，未 finalize 用实时聚合）。"""
    if not section.get("available"):
        return "（未接入成本账本）"
    src = section.get("record") or section.get("aggregate")
    if not src:
        return "（该任务暂无成本事件）"
    return ("模型调用 %s · 输入/输出 token %s/%s · 工具调用 %s · 重试 %s · "
            "估算 $%s%s" % (
                src.get("model_calls", 0), src.get("input_tokens", 0),
                src.get("output_tokens", 0), src.get("tool_calls", 0),
                src.get("retries", 0), round(float(src.get("cost_estimate", 0) or 0), 6),
                "（%s 次未计价调用）" % src.get("unpriced_calls", 0)
                if src.get("unpriced_calls") else ""))


def _render_approval(section) -> str:
    """渲染单任务审批链（含高危标记）。"""
    chains = section.get("approvals") or []
    if not chains:
        return "（该任务无审批 / 未接入审批中心）"
    lines = []
    for ch in chains:
        ap = (ch or {}).get("approval") or {}
        evs = (ch or {}).get("events") or []
        risk = None
        for e in evs:
            d = e.get("detail")
            if isinstance(d, dict) and d.get("risk") == "high":
                risk = "high"
                break
        lines.append("- `%s` action=%s status=**%s** by=%s 审计事件 %d 条%s" % (
            ap.get("approval_id"), ap.get("action"), ap.get("status"),
            ap.get("decided_by") or "—", len(evs),
            "  ⚠️risk=high" if risk == "high" else ""))
    return "\n".join(lines)


def _render_result(result, limit: int = 600) -> str:
    """把任意 result 渲染成可读文本（dict→紧凑 JSON，超长截断）。"""
    if result is None:
        return "（无产出）"
    if isinstance(result, str):
        txt = result
    elif isinstance(result, dict):
        try:
            txt = json.dumps(result, ensure_ascii=False, indent=2, default=str)
        except (TypeError, ValueError):
            txt = str(result)
    else:
        txt = str(result)
    if len(txt) > limit:
        txt = txt[:limit] + "\n…（已截断 %d 字符）" % (len(txt) - limit)
    return txt


def _extract_artifacts(task_id: str, result) -> List[dict]:
    """从 task 的 result 里抽出产物清单（通用约定）。

    识别三类：
      1. result["artifacts"] 是路径列表 → 直接列；
      2. result 是含 "path" 键的 dict → 列为单产物；
      3. shell 结果含 "stdout" 且像文件路径 → 作为产出引用。
    返回 [{"task_id", "path"/"ref", "kind"}]。
    """
    out: List[dict] = []
    if not isinstance(result, dict):
        return out
    arts = result.get("artifacts")
    if isinstance(arts, list):
        for a in arts:
            if isinstance(a, str):
                out.append({"task_id": task_id, "path": a, "kind": "artifact"})
            elif isinstance(a, dict) and a.get("path"):
                out.append({"task_id": task_id, "path": a["path"], "kind": "artifact"})
    # 含 path 键的 dict（如 {"path": "...", "bytes": N}）
    if isinstance(result.get("path"), str):
        out.append({"task_id": task_id, "path": result["path"], "kind": "output"})
    # shell 结果：stdout 里若出现绝对/相对路径行，作为产出引用
    so = result.get("stdout")
    if isinstance(so, str):
        for line in so.splitlines():
            ls = line.strip()
            if (("/" in ls or "\\" in ls or ls.startswith("."))
                    and (ls.endswith((".json", ".md", ".txt", ".csv", ".html",
                                      ".png", ".pdf")))):
                out.append({"task_id": task_id, "path": ls, "kind": "ref"})
                break
    return out


class ResultSummarizer:
    """把 tasks 表聚合成一份交付物（markdown）+ 结构化数据。

    通用性：只依赖 tasks 表的 status / payload 字段，不绑定任何模板或 op。
    """

    def collect(self, registry, task_ids: Optional[List[str]] = None) -> dict:
        """收集所有（或指定）任务的状态/产出，返回结构化 dict。

        字段：generated_at, total, by_status, tasks[{task_id,name,status,
        result,error,artifacts}], artifacts[], incomplete[{task_id,status,error}],
        complete(bool)。
        """
        rows = registry.list_tasks(limit=100000)
        if task_ids is not None:
            want = set(task_ids)
            rows = [r for r in rows if r["task_id"] in want]
        rows.sort(key=lambda r: (r["created_at"], r["task_id"]))

        by_status: dict[str, int] = {}
        tasks = []
        artifacts = []
        incomplete = []
        for r in rows:
            payload = r["payload"] if isinstance(r["payload"], dict) else {}
            status = r["status"]
            by_status[status] = by_status.get(status, 0) + 1
            name = payload.get("name") or r["task_id"]
            result = payload.get("result")
            error = payload.get("error")
            arts = _extract_artifacts(r["task_id"], result)
            artifacts.extend(arts)
            tasks.append({
                "task_id": r["task_id"], "name": name, "status": status,
                "result": result, "error": error, "artifacts": arts,
            })
            if status in _INCOMPLETE:
                incomplete.append({"task_id": r["task_id"], "status": status,
                                   "error": error})

        return {
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "total": len(rows),
            "by_status": by_status,
            "tasks": tasks,
            "artifacts": artifacts,
            "incomplete": incomplete,
            "complete": len(incomplete) == 0 and by_status.get("completed", 0) > 0,
        }

    def to_markdown(self, data: dict, title: str = "流水线交付物") -> str:
        """把 collect 的结果渲染成 markdown 交付物。"""
        lines: List[str] = []
        lines.append("# %s\n" % title)
        lines.append("- 生成时间：%s" % data["generated_at"])
        done = data["by_status"].get("completed", 0)
        if data["complete"]:
            lines.append("- 完成度：**%d / %d**（全部完成 ✓）" % (done, data["total"]))
        else:
            lines.append("- 完成度：**%d / %d**  ⚠️ 流水线尚有未结束项"
                         % (done, data["total"]))
        lines.append("")

        # 状态汇总表
        lines.append("## 状态汇总\n")
        lines.append("| 状态 | 数量 |")
        lines.append("|------|------|")
        for st in sorted(data["by_status"]):
            lines.append("| %s | %d |" % (st, data["by_status"][st]))
        lines.append("")

        # 产物清单
        lines.append("## 产物清单\n")
        if data["artifacts"]:
            for a in data["artifacts"]:
                lines.append("- `%s`（来自任务 `%s`，%s）"
                             % (a["path"], a["task_id"], a["kind"]))
        else:
            lines.append("- 无（本次运行未产生文件类产物）")
        lines.append("")

        # 逐任务结果
        lines.append("## 逐任务结果\n")
        for t in data["tasks"]:
            mark = "✓" if t["status"] == "completed" else "…"
            lines.append("### %s `%s` — %s %s" % (t["name"], t["task_id"],
                                                 t["status"], mark))
            if t["status"] == "completed":
                lines.append(_render_result(t["result"]))
            elif t["error"]:
                lines.append("失败原因：%s" % t["error"])
            else:
                lines.append("（尚未完成）")
            lines.append("")

        # 未完成项
        if data["incomplete"]:
            lines.append("## 未完成项（需人工处理）\n")
            for it in data["incomplete"]:
                lines.append("- `%s`：状态=%s%s"
                             % (it["task_id"], it["status"],
                                "，原因=%s" % it["error"] if it["error"] else ""))
            lines.append("")

        return "\n".join(lines).rstrip() + "\n"

    def summarize(self, registry, task_ids: Optional[List[str]] = None,
                  title: str = "流水线交付物") -> tuple:
        """一站式：收集 + 渲染。返回 (markdown, data)。"""
        data = self.collect(registry, task_ids=task_ids)
        return self.to_markdown(data, title=title), data

    # ------------------------------------------------------------------
    # 「任务结案报告」：把 Trace 回放 / 成本看板 / 审批链统一落盘一份结构化产物
    # ------------------------------------------------------------------
    def closure_collect(self, registry, task_ids: Optional[List[str]] = None,
                       trace=None, cost=None, approval=None,
                       trace_id: Optional[str] = None,
                       top_days: int = 30) -> dict:
        """收集任务 + 三路审计数据（Trace / Cost / Approval），返回结构化 dict。

        三路存储均为**可选 duck-typed**：传入即并入报告，不传则该章节标注
        "未接入"——保持 ResultSummarizer 零硬耦合、可单独测。
        - trace    : 需有 by_task(task_id)→{trace_id,events}、events(trace_id)、get(trace_id)
        - cost     : 需有 task_cost(task_id)、dashboard(days, top)
        - approval : 需有 approvals_for_task(task_id)、audit(approval_id)
        """
        base = self.collect(registry, task_ids=task_ids)
        tasks_out = []
        for t in base["tasks"]:
            trace_sec = {"available": False, "trace_id": None, "events": []}
            if trace is not None:
                try:
                    bt = trace.by_task(t["task_id"])
                    if bt.get("trace_id"):
                        evs = [e for e in bt.get("events", [])
                               if e.get("task_id") == t["task_id"]]
                        trace_sec = {"available": True, "trace_id": bt["trace_id"],
                                     "events": evs}
                except Exception:  # noqa: BLE001 —— 旁路：任一存储坏了不阻断结案
                    pass
            cost_sec = {"available": False}
            if cost is not None:
                try:
                    cc = cost.task_cost(t["task_id"])
                    if cc is not None:
                        d = dict(cc)
                        d["available"] = True
                        cost_sec = d
                except Exception:  # noqa: BLE001
                    pass
            appr_sec = {"available": False, "approvals": []}
            if approval is not None:
                try:
                    aids = approval.approvals_for_task(t["task_id"])
                    chains = []
                    for a in aids:
                        try:
                            chains.append(approval.audit(a["approval_id"]))
                        except Exception:  # noqa: BLE001
                            chains.append({"approval": a, "events": []})
                    appr_sec = {"available": bool(aids), "approvals": chains}
                except Exception:  # noqa: BLE001
                    pass
            tasks_out.append({"task_id": t["task_id"], "name": t["name"],
                              "status": t["status"], "result": t["result"],
                              "error": t["error"], "artifacts": t["artifacts"],
                              "trace": trace_sec, "cost": cost_sec,
                              "approval": appr_sec})

        cost_dash = None
        if cost is not None:
            try:
                cost_dash = cost.dashboard(days=int(top_days))
            except Exception:  # noqa: BLE001
                pass
        trace_replay = None
        if trace is not None and trace_id:
            try:
                trace_replay = {"meta": trace.get(trace_id),
                                "events": trace.events(trace_id)}
            except Exception:  # noqa: BLE001
                pass
        return {
            "report_type": "task_closure_report",
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "trace_id": trace_id,
            "summary": {"total": base["total"], "by_status": base["by_status"],
                        "complete": base["complete"]},
            "tasks": tasks_out,
            "cost_dashboard": cost_dash,
            "trace_replay": trace_replay,
            "incomplete": base["incomplete"],
        }

    def closure_markdown(self, data: dict,
                         title: str = "任务结案报告") -> str:
        """把 closure_collect 的结果渲染成标准化「任务结案报告」模板。

        章节：一、概览 / 二、逐任务明细（每任务含 Trace 回放·成本·审批链）/
        三、全局轨迹回放（整条流水线一次）/ 四、成本看板 / 五、审批链审计汇总 /
        六、未完成项。
        """
        L: List[str] = []
        L.append("# %s\n" % title)
        L.append("- 生成时间：%s" % data["generated_at"])
        if data.get("trace_id"):
            L.append("- 关联轨迹：%s" % data["trace_id"])
        s = data["summary"]
        done = s["by_status"].get("completed", 0)
        if s["complete"]:
            L.append("- 完成度：**%d / %d**（全部完成 ✓）" % (done, s["total"]))
        else:
            L.append("- 完成度：**%d / %d**  ⚠️ 尚有未结束项" % (done, s["total"]))
        L.append("")

        L.append("## 一、概览\n")
        L.append("| 状态 | 数量 |")
        L.append("|------|------|")
        for st in sorted(s["by_status"]):
            L.append("| %s | %d |" % (st, s["by_status"][st]))
        L.append("")

        L.append("## 二、逐任务明细\n")
        for t in data["tasks"]:
            mark = "✓" if t["status"] == "completed" else "…"
            L.append("### %s `%s` — %s %s" % (t["name"], t["task_id"],
                                              t["status"], mark))
            if t["status"] == "completed":
                L.append("\n**产出**：")
                L.append(_render_result(t["result"]))
            elif t.get("error"):
                L.append("\n失败原因：%s" % t["error"])
            else:
                L.append("\n（尚未完成）")
            if t["artifacts"]:
                L.append("\n**产物**：")
                for a in t["artifacts"]:
                    L.append("- `%s`（%s）" % (a["path"], a["kind"]))
            tr = t["trace"]
            L.append("\n**Trace 回放**（%s）：" % (
                "trace_id=%s，本任务 %d 事件" % (tr["trace_id"], len(tr["events"]))
                if tr["available"] else "未接入轨迹层"))
            L.append(_render_trace_events(tr["events"]) if tr["available"]
                     else "（未接入轨迹层）")
            L.append("\n**成本**：%s" % _render_cost(t["cost"]))
            L.append("\n**审批链**：")
            L.append(_render_approval(t["approval"]))
            L.append("")

        tr = data.get("trace_replay")
        if tr and tr.get("events"):
            L.append("## 三、全局轨迹回放（整条流水线）\n")
            meta = tr.get("meta") or {}
            L.append("- 轨迹 %s · 状态 %s · 事件 %d · 起止 %s ~ %s" % (
                data.get("trace_id"), meta.get("status"), len(tr["events"]),
                _fmt_ts(meta.get("first_ts")), _fmt_ts(meta.get("last_ts"))))
            L.append("")
            L.append(_render_trace_events(tr["events"]))
            L.append("")

        dash = data.get("cost_dashboard")
        if dash:
            L.append("## 四、成本看板（近 %d 天）\n" % dash.get("days", 30))
            tot = dash.get("totals", {})
            L.append("- 任务数 %s · 模型调用 %s · 令牌 %s · 工具调用 %s · 重试 %s · "
                     "估算 $%s（%s 次未计价）" % (
                         tot.get("tasks", 0), tot.get("model_calls", 0),
                         tot.get("tokens", 0), tot.get("tool_calls", 0),
                         tot.get("retries", 0),
                         round(float(tot.get("cost_estimate", 0) or 0), 6),
                         tot.get("unpriced_calls", 0)))
            topc = (dash.get("outliers") or {}).get("top_cost") or []
            if topc:
                L.append("\n吞金榜 Top：")
                for r in topc[:5]:
                    L.append("- `%s`（%s/%s）$%s 重试 %s" % (
                        r.get("task_id"), r.get("role") or "—",
                        r.get("template") or "—",
                        round(float(r.get("cost_estimate", 0) or 0), 6),
                        r.get("retries", 0)))
            L.append("\n> %s" % dash.get("price_note", ""))
            L.append("")

        all_appr = [t["approval"] for t in data["tasks"]
                    if t["approval"]["approvals"]]
        if all_appr:
            L.append("## 五、审批链审计汇总\n")
            tasks_with_appr = {t["task_id"] for t in data["tasks"]
                               if t["approval"]["approvals"]}
            n = sum(len(a["approvals"]) for a in all_appr)
            L.append("- 涉及审批的任务数：%d，审批条目 %d" % (len(tasks_with_appr), n))
            high = 0
            for a in all_appr:
                for ch in a["approvals"]:
                    for e in (ch or {}).get("events") or []:
                        d = e.get("detail")
                        if isinstance(d, dict) and d.get("risk") == "high":
                            high += 1
            if high:
                L.append("- 高危动作（risk=high）审计事件：%d 条" % high)
            L.append("")

        if data["incomplete"]:
            L.append("## 六、未完成项（需人工处理）\n")
            for it in data["incomplete"]:
                L.append("- `%s`：状态=%s%s" % (
                    it["task_id"], it["status"],
                    "，原因=%s" % it["error"] if it["error"] else ""))
            L.append("")

        return "\n".join(L).rstrip() + "\n"

    def write_closure(self, registry, out_dir: str = "data",
                      task_ids: Optional[List[str]] = None, trace=None,
                      cost=None, approval=None,
                      trace_id: Optional[str] = None,
                      title: str = "任务结案报告",
                      top_days: int = 30) -> tuple:
        """一站式：收集 + 渲染 + 落盘。写 <out_dir>/closure_report_<ts>.json+.md。
        返回 (md_path, json_path)。"""
        os.makedirs(out_dir, exist_ok=True)
        data = self.closure_collect(
            registry, task_ids=task_ids, trace=trace, cost=cost,
            approval=approval, trace_id=trace_id, top_days=top_days)
        md = self.closure_markdown(data, title=title)
        ts = time.strftime("%Y%m%d_%H%M%S")
        jp = os.path.join(out_dir, "closure_report_%s.json" % ts)
        mp = os.path.join(out_dir, "closure_report_%s.md" % ts)
        with open(jp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, default=str)
        with open(mp, "w", encoding="utf-8") as f:
            f.write(md)
        return mp, jp
