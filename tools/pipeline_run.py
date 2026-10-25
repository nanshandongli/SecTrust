"""
端到端流水线编排（tools/pipeline_run.py）—— 把「拆解→调度→执行→质量」真正闭合成
一份交付物。

此前编排层有 Planner（拆解）/ DagScheduler（依赖与并行波次）/ ExecutionDaemon
（真执行）/ QualityGate（质量回流），但跑完没有一个「收口」：分散在各任务
payload.result 里的产出没人汇总。本工具演示端到端闭环：

  1. 建图：DagScheduler.add_task 搭一个有并行分支的 DAG；
  2. 执行：真实 ExecutionDaemon（单进程内 run_once 循环）认领并真跑每个任务；
  3. 汇总：ResultSummarizer 收集全部任务产出，生成 markdown 交付物并落盘。

默认 DAG 全部用 noop 任务（瞬时完成、确定性），保证任何环境都能跑通闭环；
`--with-shell` 会加一个真实 shell 任务（写临时文件）演示「产物清单」抽取。

用法：
    python tools/pipeline_run.py                       # 默认 noop DAG，落到 data/
    python tools/pipeline_run.py --with-shell           # 含真实 shell 产物
    python tools/pipeline_run.py --db-path /tmp/p.db --out data/run.md --title "双十一巡检"
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from orchestration import DagScheduler                  # noqa: E402
from task_registry import TaskRegistry                  # noqa: E402
from execution.daemon import ExecutionDaemon            # noqa: E402
from summarizer import ResultSummarizer                 # noqa: E402


def build_dag(sched: DagScheduler, with_shell: bool = False) -> None:
    """搭一个有并行分支的 DAG（演示依赖 + 波次）。

        research
       /        \
    fetch      lint   （并行）
       |
    analyze
       |
    publish  （依赖 analyze 与 lint）
    """
    sched.add_task("research", payload={"op": "noop", "name": "调研与资料收集"})
    sched.add_task("lint", payload={"op": "noop", "name": "静态检查（与抓取并行）"})
    if with_shell:
        raw = os.path.join(tempfile.gettempdir(), "sectrust_raw.json")
        sched.add_task(
            "fetch", payload={
                "op": "shell", "name": "抓取原始数据（写文件）", "timeout": 30,
                "cmd": [sys.executable, "-c",
                        "import json,os,tempfile;"
                        "p=os.path.join(tempfile.gettempdir(),'sectrust_raw.json');"
                        "json.dump({'rows':[1,2,3,4,5]}, open(p,'w'));"
                        "print('wrote', p)"],
            }, depends_on=["research"])
        sched.add_task(
            "analyze", payload={
                "op": "shell", "name": "分析并生成报告", "timeout": 30,
                "cmd": [sys.executable, "-c",
                        "import json,os,tempfile;"
                        "p=os.path.join(tempfile.gettempdir(),'sectrust_report.md');"
                        "d=json.load(open(os.path.join(tempfile.gettempdir(),"
                        "'sectrust_raw.json')));"
                        "open(p,'w').write('# Report\\nrows=%d\\n' % len(d['rows']));"
                        "print('wrote', p)"],
            }, depends_on=["fetch"])
    else:
        sched.add_task("fetch", payload={"op": "noop", "name": "抓取原始数据"},
                       depends_on=["research"])
        sched.add_task("analyze", payload={"op": "noop", "name": "分析并生成报告"},
                       depends_on=["fetch"])
    sched.add_task("publish", payload={"op": "noop", "name": "发布交付物"},
                   depends_on=["analyze", "lint"])


def run_pipeline(db_path: str, title: str = "流水线交付物",
                 with_shell: bool = False, timeout: float = 60.0,
                 poll: float = 0.05) -> tuple:
    """建图 → 真实执行 → 汇总。返回 (markdown, data)。"""
    sched = DagScheduler(db_path)
    build_dag(sched, with_shell=with_shell)

    daemon = ExecutionDaemon(
        db_path=db_path, bot_id="pipeline-runner",
        heartbeat_interval=30.0, poll_interval=poll, cluster=None)
    deadline = time.time() + timeout
    while time.time() < deadline:
        out = daemon.run_once(max_tasks=10)
        st = sched.stats()
        if not out and st["by_status"].get("registered", 0) == 0 \
                and st["by_status"].get("running", 0) == 0:
            break
        time.sleep(poll)
    daemon.close()
    sched.close()

    reg = TaskRegistry(db_path=db_path)
    try:
        md, data = ResultSummarizer().summarize(reg, title=title)
    finally:
        reg.close()
    return md, data


def main():
    ap = argparse.ArgumentParser(description="端到端流水线编排（建图→执行→汇总闭环）")
    ap.add_argument("--db-path", default=None, help="任务库（默认临时库）")
    ap.add_argument("--out", default=None, help="交付物落盘路径（默认 data/pipeline_run_<ts>.md）")
    ap.add_argument("--title", default="流水线交付物", help="交付物标题")
    ap.add_argument("--with-shell", action="store_true", help="含真实 shell 产物任务")
    ap.add_argument("--timeout", type=float, default=60.0)
    args = ap.parse_args()

    tmpdb = None
    if args.db_path:
        db_path = args.db_path
    else:
        tmpdb = tempfile.mkdtemp(prefix="pipeline_db_")
        db_path = os.path.join(tmpdb, "pipeline.db")

    md, data = run_pipeline(db_path, title=args.title,
                            with_shell=args.with_shell, timeout=args.timeout)
    out = args.out or os.path.join(
        "data", "pipeline_run_%s.md" % time.strftime("%Y%m%d_%H%M%S"))
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write(md)

    print(md)
    print("\n交付物已落盘：%s" % out)
    print("完成度：%s（%d/%d）" % ("✓" if data["complete"] else "⚠ 未完成",
                                  data["by_status"].get("completed", 0), data["total"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
