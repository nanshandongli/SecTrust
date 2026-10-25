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
from trace import TraceStore, new_trace_id              # noqa: E402
from cost import CostStore                              # noqa: E402
from approval import ApprovalCenter                      # noqa: E402


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


def _emit_closure(reg: TaskRegistry, db_path: str, out_dir: str = "data",
                  title: str = "任务结案报告") -> tuple:
    """在真实跑完的 DAG 之上，记录 Trace/Cost/Approval 三路数据并落盘统一结案报告。

    用同一 trace_id 把整条流水线串起来：每个完成任务记 plan_built + task_completed；
    publish 步（高危动作）走一次审批链（request→approve）；成本记一次模型调用
    （mock 显式 $0，诚实）。最终 ResultSummarizer.write_closure 把三路并成一份
    JSON + 标准化 Markdown。返回 (md_path, json_path)。
    """
    trace = TraceStore(db_path)
    cost = CostStore(os.path.join(os.path.dirname(os.path.abspath(db_path)),
                                 "cost_closure.db"))
    appr = ApprovalCenter(db_path, tracer=trace)   # 复用同一 trace 实例
    tid = new_trace_id()
    trace.start_trace(title="pipeline_closure", kind="pipeline", trace_id=tid)

    for r in reg.list_tasks(limit=100000):
        pid = r["payload"] if isinstance(r["payload"], dict) else {}
        name = pid.get("name") or r["task_id"]
        if r["status"] == "completed":
            trace.record(tid, "plan_built", task_id=r["task_id"], summary="拆解: " + name)
            trace.record(tid, "task_completed", task_id=r["task_id"], summary="完成: " + name)
            # 成本：演示一次模型调用（mock 显式 $0——真免费，不是"不知道"）
            cost.record_model_call(task_id=r["task_id"], trace_id=tid,
                                   model="mock", input_tokens=120, output_tokens=60)
            cost.finalize_task(r["task_id"], trace_id=tid, outcome="ok")
        # publish 是高危动作：走一次审批链（request→approve）
        if r["task_id"] == "publish" and r["status"] == "completed":
            aid = appr.request("publish", "发布交付物", task_id="publish", trace_id=tid)
            appr.decide(aid, "approve", by="human")

    trace.finish(tid, status="completed")
    rs = ResultSummarizer()
    mp, jp = rs.write_closure(reg, out_dir=out_dir, trace=trace, cost=cost,
                              approval=appr, trace_id=tid, title=title)
    trace.close()
    cost.close()
    appr.close()
    return mp, jp


def run_pipeline(db_path: str, title: str = "流水线交付物",
                 with_shell: bool = False, timeout: float = 60.0,
                 poll: float = 0.05, closure: bool = False,
                 closure_out_dir: str = "data") -> tuple:
    """建图 → 真实执行 → 汇总。

    返回 (markdown, data)；当 closure=True 时额外返回第三元素 (md_path, json_path)。
    """
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
        closure_paths = None
        if closure:
            closure_paths = _emit_closure(reg, db_path, out_dir=closure_out_dir,
                                         title=title)
    finally:
        reg.close()
    if closure:
        return md, data, closure_paths
    return md, data


def main():
    ap = argparse.ArgumentParser(description="端到端流水线编排（建图→执行→汇总闭环）")
    ap.add_argument("--db-path", default=None, help="任务库（默认临时库）")
    ap.add_argument("--out", default=None, help="交付物落盘路径（默认 data/pipeline_run_<ts>.md）")
    ap.add_argument("--title", default="流水线交付物", help="交付物标题")
    ap.add_argument("--with-shell", action="store_true", help="含真实 shell 产物任务")
    ap.add_argument("--timeout", type=float, default=60.0)
    ap.add_argument("--closure", action="store_true",
                    help="额外落盘「任务结案报告」：Trace 回放+成本看板+审批链统一产物")
    ap.add_argument("--out-dir", default="data", help="结案报告落盘目录（--closure）")
    args = ap.parse_args()

    tmpdb = None
    if args.db_path:
        db_path = args.db_path
    else:
        tmpdb = tempfile.mkdtemp(prefix="pipeline_db_")
        db_path = os.path.join(tmpdb, "pipeline.db")

    result = run_pipeline(db_path, title=args.title, with_shell=args.with_shell,
                          timeout=args.timeout, closure=args.closure,
                          closure_out_dir=args.out_dir)
    md, data = result[0], result[1]
    out = args.out or os.path.join(
        "data", "pipeline_run_%s.md" % time.strftime("%Y%m%d_%H%M%S"))
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write(md)

    print(md)
    print("\n交付物已落盘：%s" % out)
    print("完成度：%s（%d/%d）" % ("✓" if data["complete"] else "⚠ 未完成",
                                  data["by_status"].get("completed", 0), data["total"]))
    if args.closure and len(result) == 3 and result[2]:
        mp, jp = result[2]
        print("\n任务结案报告（统一结构化产物）已落盘：")
        print("  markdown: %s" % mp)
        print("  json    : %s" % jp)
    return 0


if __name__ == "__main__":
    sys.exit(main())
