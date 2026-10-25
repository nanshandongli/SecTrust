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
