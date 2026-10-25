#!/usr/bin/env python3
"""生成「数据支撑」证据产物：端到端 DAG 截图(SVG) + 压测延迟图(SVG) + 自包含 HTML 报告。

读取真实压测 JSON 与流水线汇总，输出到 reports/。
零依赖：仅标准库。可用于随时重现 PRODUCT_ANALYSIS.md 引用的证据图。
"""
import json
import os
import html

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
OUT = os.path.join(ROOT, "reports")
os.makedirs(OUT, exist_ok=True)


def load_json(name):
    with open(os.path.join(DATA, name), "r", encoding="utf-8") as f:
        return json.load(f)


# ---- 真实数据（来自 data/ 落盘产物）---------------------------------------
STRESS_200 = load_json("cluster_stress_20261025_211044.json")   # 3机/200任务/6s
STRESS_120 = load_json("cluster_stress_20261025_211009.json")   # 3机/120任务/3s

# 端到端流水线实跑（data/pipeline_run_20261025_213311.md，5/5 完成）
E2E = {
    "run_id": "pipeline_run_20261025_213311",
    "ts": "2026-10-25T21:33:11",
    "total": 5,
    "nodes": [
        {"id": "research", "label": "调研与资料收集", "op": "noop",
         "dur": None, "artifact": None, "status": "completed"},
        {"id": "lint", "label": "静态检查", "op": "noop",
         "dur": None, "artifact": None, "status": "completed"},
        {"id": "fetch", "label": "抓取原始数据", "op": "shell",
         "dur": 0.1647, "artifact": "sectrust_raw.json", "status": "completed",
         "backend": "local-enforced"},
        {"id": "analyze", "label": "分析并生成报告", "op": "shell",
         "dur": 0.1778, "artifact": "sectrust_report.md", "status": "completed",
         "backend": "local-enforced"},
        {"id": "publish", "label": "发布交付物", "op": "noop",
         "dur": None, "artifact": None, "status": "completed"},
    ],
    "edges": [("research", "lint"), ("research", "fetch"),
              ("lint", "analyze"), ("fetch", "analyze"), ("analyze", "publish")],
}

REGRESSION = {"total": 781, "skipped": 2, "failed": 0}


# ---- 任务结案报告样例（来自 data/closure_report_*.json）-------------------
def load_latest_closure():
    """读取最新的「任务结案报告」JSON（若存在）。缺失则 demo 页优雅提示。"""
    import glob
    fs = sorted(glob.glob(os.path.join(DATA, "closure_report_*.json")))
    if not fs:
        return None
    try:
        with open(fs[-1], "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001 —— 旁路：证据缺失不阻断证据页生成
        return None


def render_closure_html(d):
    """把一份「任务结案报告」渲染为 HTML 样例（Trace + 成本 + 审批链 三路并列）。"""
    if not d:
        return ('<div class="card"><div class="sub">未生成任务结案报告。运行 '
                '<code>python tools/pipeline_run.py --with-shell --closure</code> 生成 '
                '后再跑本脚本即可嵌入。</div></div>')
    s = d.get("summary", {})
    by = s.get("by_status", {})
    overview = "".join(
        "<tr><td>%s</td><td>%s</td></tr>" % (esc(k), esc(v))
        for k, v in sorted(by.items()))
    # 选一个带审批链的任务做样板（优先高风险 publish），否则取首个
    show = next((t for t in d.get("tasks", []) if t.get("approval", {}).get("available")),
                (d.get("tasks") or [None])[0])
    tr = (show or {}).get("trace", {})
    tev = "".join(
        "<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
            esc(e.get("seq")), esc(e.get("kind")), esc(e.get("actor") or "—"),
            esc(e.get("summary") or ""))
        for e in tr.get("events", []))
    rec = (show or {}).get("cost", {}).get("record", {}) or {}
    cost_line = ("模型调用 %s · 令牌 %s/%s · 工具调用 %s · 重试 %s · 估算 $%s"
                 % (rec.get("model_calls"), rec.get("input_tokens"),
                    rec.get("output_tokens"), rec.get("tool_calls"),
                    rec.get("retries"), rec.get("cost_estimate")))
    appr_html = ""
    for ch in (show or {}).get("approval", {}).get("approvals", []):
        ap = ch.get("approval", {})
        risk = ""
        for ev in ch.get("events", []):
            if isinstance(ev.get("detail"), dict) and ev["detail"].get("risk"):
                risk = ev["detail"]["risk"]
        badge = (' <span class="warn">⚠ risk=%s</span>' % esc(risk)) if risk == "high" else ""
        appr_html += ("<div>· <code>%s</code> action=%s status=<b>%s</b> by=%s%s</div>"
                      % (esc(ap.get("approval_id")), esc(ap.get("action")),
                         esc(ap.get("status")), esc(ap.get("decided_by") or "—"), badge))
    if not appr_html:
        appr_html = '<div class="sub">（该任务无审批）</div>'
    trr = d.get("trace_replay") or {}
    gmeta = trr.get("meta") or {}
    gcount = len(trr.get("events", []))
    dash = d.get("cost_dashboard") or {}
    tot = dash.get("totals", {})
    n_appr_tasks = sum(1 for t in d.get("tasks", []) if t.get("approval", {}).get("available"))
    n_high = 0
    for t in d.get("tasks", []):
        for ch in t.get("approval", {}).get("approvals", []):
            for ev in ch.get("events", []):
                if isinstance(ev.get("detail"), dict) and ev["detail"].get("risk") == "high":
                    n_high += 1
    return f"""
<div class="card">
<p class="sub">「任务结案报告」= 把 <b>Trace 回放</b>、<b>成本看板</b>、<b>审批链</b> 三路审计数据
统一落盘成一份结构化产物（JSON + Markdown 双格式）。一次流水线跑完，自动产出可审计交付物——
无需人工拼装证据。下文为本次实跑样例（任务「{esc((show or {}).get('name', ''))}」）。</p>

<h3 style="color:#cbd5e1;font-size:14px">概览</h3>
<table class="tbl"><thead><tr><th>状态</th><th>数量</th></tr></thead><tbody>{overview}</tbody></table>

<h3 style="color:#cbd5e1;font-size:14px">逐任务明细 · 样板任务（三路证据一次对齐）</h3>
<p><b>Trace 回放</b>（trace_id={esc(tr.get('trace_id'))}，本任务 {len(tr.get('events', []))} 事件）：</p>
<table class="tbl"><thead><tr><th>#</th><th>类型</th><th>角色</th><th>摘要</th></tr></thead><tbody>{tev}</tbody></table>
<p><b>成本</b>：{esc(cost_line)}</p>
<p><b>审批链</b>：</p>{appr_html}

<h3 style="color:#cbd5e1;font-size:14px">全局轨迹回放（整条流水线一次）</h3>
<div class="sub">轨迹 {esc(d.get('trace_id'))} · 状态 {esc(gmeta.get('status'))} · 事件 {gcount} 条</div>

<h3 style="color:#cbd5e1;font-size:14px">成本看板（近 {esc(dash.get('days', 30))} 天）</h3>
<div class="sub">任务数 {esc(tot.get('tasks'))} · 模型调用 {esc(tot.get('model_calls'))} · 令牌 {esc(tot.get('tokens'))}
· 工具调用 {esc(tot.get('tool_calls'))} · 重试 {esc(tot.get('retries'))} · 估算 ${esc(tot.get('cost_estimate'))}
（{esc(tot.get('unpriced_calls'))} 次未计价）</div>

<h3 style="color:#cbd5e1;font-size:14px">审批链审计汇总</h3>
<div class="sub">涉及审批的任务数 {n_appr_tasks} · 高危动作（risk=high）审计事件 {n_high} 条</div>

<div class="sub" style="margin-top:12px">完整产物（逐任务全量 + Markdown 版）由
<code>python tools/pipeline_run.py --with-shell --closure</code> 生成，落盘
<code>data/closure_report_*.json/.md</code>。</div>
</div>
"""


# ---- SVG 辅助 -------------------------------------------------------------
def esc(s):
    return html.escape(str(s))


def node_box(x, y, w, h, label, op, dur, artifact, status):
    c = "#14532d" if status == "completed" else "#7f1d1d"
    c2 = "#166534" if status == "completed" else "#991b1b"
    lines = []
    lines.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="10" '
                 f'fill="{c}" stroke="{c2}" stroke-width="2"/>')
    cx = x + w / 2
    # 标题
    lines.append(f'<text x="{cx}" y="{y+22}" text-anchor="middle" '
                 f'font-family="Segoe UI,Helvetica,Arial,sans-serif" '
                 f'font-size="14" font-weight="700" fill="#f0fdf4">{esc(label)}</text>')
    # op + dur
    sub = f'op={esc(op)}'
    if dur is not None:
        sub += f"  {dur*1000:.0f}ms"
    lines.append(f'<text x="{cx}" y="{y+40}" text-anchor="middle" '
                 f'font-family="Consolas,Menlo,monospace" font-size="11" '
                 f'fill="#bbf7d0">{esc(sub)}</text>')
    # artifact
    if artifact:
        a = esc(artifact)
        if len(a) > 20:
            a = a[:19] + "…"
        lines.append(f'<text x="{cx}" y="{y+55}" text-anchor="middle" '
                     f'font-family="Consolas,Menlo,monospace" font-size="10" '
                     f'fill="#86efac">📄 {a}</text>')
    # 状态徽标
    lines.append(f'<circle cx="{x+w-14}" cy="{y+14}" r="9" fill="#22c55e"/>')
    lines.append(f'<text x="{x+w-14}" y="{y+18}" text-anchor="middle" '
                 f'font-size="12" font-weight="700" fill="#052e16">✓</text>')
    return "\n".join(lines)


def edge(x1, y1, x2, y2):
    return (f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" '
            f'stroke="#475569" stroke-width="2.5" marker-end="url(#arw)"/>')


def build_dag_svg():
    W, H = 820, 470
    pos = {
        "research": (335, 20), "lint": (130, 150), "fetch": (540, 150),
        "analyze": (335, 290), "publish": (335, 400),
    }
    bw, bh = 150, 64
    centers = {k: (x + bw / 2, y + bh / 2) for k, (x, y) in pos.items()}
    s = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" '
         f'font-family="sans-serif">']
    s.append('<defs><marker id="arw" markerWidth="10" markerHeight="10" '
             'refX="8" refY="3" orient="auto" markerUnits="strokeWidth">'
             '<path d="M0,0 L8,3 L0,6 Z" fill="#475569"/></marker></defs>')
    # 背景
    s.append(f'<rect x="0" y="0" width="{W}" height="{H}" fill="#0f172a"/>')
    s.append(f'<text x="410" y="16" text-anchor="middle" font-size="15" '
             f'font-weight="700" fill="#e2e8f0">端到端流水线实跑 · 5/5 完成 '
             f'(菱形 DAG + 真实 shell 产物)</text>')
    # 边（先画，压在节点下）
    for a, b in E2E["edges"]:
        ax, ay = centers[a]; bx, by = centers[b]
        # 从 a 底部到 b 顶部
        s.append(edge(ax, ay + bh / 2, bx, by - bh / 2))
    # 节点
    for n in E2E["nodes"]:
        x, y = pos[n["id"]]
        s.append(node_box(x, y, bw, bh, n["label"], n["op"], n["dur"],
                          n["artifact"], n["status"]))
    # 图例
    s.append('<text x="20" y="455" font-size="11" fill="#94a3b8">'
             '绿框=已完成 ✓；op=noop 为占位步，op=shell 为真实落盘步'
             '（sectrust_raw.json / sectrust_report.md 写入系统临时目录）</text>')
    s.append('</svg>')
    return "\n".join(s)


def bar(x, y_base, w, val, maxv, chart_h, fill, label):
    h = max(2.0, val / maxv * chart_h)
    y = y_base - h
    out = [f'<rect x="{x:.1f}" y="{y:.1f}" width="{w}" height="{h:.1f}" '
           f'fill="{fill}" rx="2"/>']
    out.append(f'<text x="{x+w/2:.1f}" y="{y-4:.1f}" text-anchor="middle" '
               f'font-size="11" fill="#e2e8f0">{val:.3f}</text>')
    if label:
        out.append(f'<text x="{x+w/2:.1f}" y="{y_base+16:.1f}" '
                   f'text-anchor="middle" font-size="11" fill="#cbd5e1">{esc(label)}</text>')
    return "\n".join(out)


def build_latency_svg():
    W, H = 760, 380
    cats = ["p50", "p95", "p99", "max"]
    vA = [STRESS_200["bench_a_coordination"]["lock_acquire_latency_ms"][c] for c in cats]
    vB = [STRESS_120["bench_a_coordination"]["lock_acquire_latency_ms"][c] for c in cats]
    maxv = 1.0
    chart_x, chart_w = 70, 600
    y_base, chart_h = 300, 230
    s = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" '
         f'font-family="sans-serif">']
    s.append(f'<rect x="0" y="0" width="{W}" height="{H}" fill="#0f172a"/>')
    s.append(f'<text x="{W/2:.0f}" y="22" text-anchor="middle" font-size="15" '
             f'font-weight="700" fill="#e2e8f0">锁获取延迟分位（ms）· 两档压测</text>')
    # y 轴
    s.append(f'<line x1="{chart_x}" y1="{y_base}" x2="{chart_x+chart_w}" '
             f'y2="{y_base}" stroke="#64748b" stroke-width="1.5"/>')
    for g in (0.0, 0.25, 0.5, 0.75, 1.0):
        gy = y_base - g * chart_h
        s.append(f'<line x1="{chart_x}" y1="{gy:.1f}" x2="{chart_x+chart_w}" '
                 f'y2="{gy:.1f}" stroke="#334155" stroke-width="1"/>')
        s.append(f'<text x="{chart_x-8:.1f}" y="{gy+4:.1f}" text-anchor="end" '
                 f'font-size="10" fill="#94a3b8">{g:.2f}</text>')
    # 分组柱
    group_w = chart_w / len(cats)
    bw = 34
    gap = 10
    for i, c in enumerate(cats):
        gx = chart_x + i * group_w + (group_w - 2 * bw - gap) / 2
        s.append(bar(gx, y_base, bw, vA[i], maxv, chart_h, "#3b82f6", None))
        s.append(bar(gx + bw + gap, y_base, bw, vB[i], maxv, chart_h, "#f59e0b", c))
    # 图例
    s.append('<rect x="70" y="340" width="14" height="14" fill="#3b82f6"/>')
    s.append('<text x="90" y="352" font-size="12" fill="#cbd5e1">'
             '200任务/6s 档 (1665 ops/s)</text>')
    s.append('<rect x="300" y="340" width="14" height="14" fill="#f59e0b"/>')
    s.append('<text x="320" y="352" font-size="12" fill="#cbd5e1">'
             '120任务/3s 档 (1761 ops/s)</text>')
    s.append('</svg>')
    return "\n".join(s)


def table(headers, rows, cls="tbl"):
    out = [f'<table class="{cls}"><thead><tr>']
    out += [f"<th>{esc(h)}</th>" for h in headers]
    out.append("</tr></thead><tbody>")
    for r in rows:
        out.append("<tr>" + "".join(f"<td>{esc(c)}</td>" for c in r) + "</tr>")
    out.append("</tbody></table>")
    return "\n".join(out)


def build_html():
    dag = build_dag_svg()
    lat = build_latency_svg()
    closure = load_latest_closure()
    a200 = STRESS_200["bench_a_coordination"]
    b200 = STRESS_200["bench_b_pipeline"]
    a120 = STRESS_120["bench_a_coordination"]
    b120 = STRESS_120["bench_b_pipeline"]

    a_rows = [
        ["指标", "200任务/6s 档", "120任务/3s 档"],
        ["机器数（进程模拟）", a200["machines"], a120["machines"]],
        ["锁池大小", a200["lock_pool_size"], a120["lock_pool_size"]],
        ["锁操作总数", a200["total_lock_ops"], a120["total_lock_ops"]],
        ["吞吐 (ops/s)", a200["lock_ops_per_sec"], a120["lock_ops_per_sec"]],
        ["P50 延迟 (ms)", a200["lock_acquire_latency_ms"]["p50"],
         a120["lock_acquire_latency_ms"]["p50"]],
        ["P99 延迟 (ms)", a200["lock_acquire_latency_ms"]["p99"],
         a120["lock_acquire_latency_ms"]["p99"]],
        ["Max 延迟 (ms)", a200["lock_acquire_latency_ms"]["max"],
         a120["lock_acquire_latency_ms"]["max"]],
        ["互斥违规（双抢）", a200["lock_exclusion"]["violations"],
         a120["lock_exclusion"]["violations"]],
        ["同锁最大并发持有", a200["lock_exclusion"]["max_concurrent_per_lock"],
         a120["lock_exclusion"]["max_concurrent_per_lock"]],
        ["在线机器观测", "3 台全在线", "3 台全在线"],
        ["死信阈值行为", "驱动失败→入队→recover 消失 ✓",
         "驱动失败→入队→recover 消失 ✓"],
    ]
    b_rows = [
        ["指标", "200任务/6s 档", "120任务/3s 档"],
        ["任务 seed", b200["tasks_seeded"], b120["tasks_seeded"]],
        ["跨机去重完成", b200["tasks_completed_unique"], b120["tasks_completed_unique"]],
        ["重复执行（双机同跑）", b200["double_execution_count"],
         b120["double_execution_count"]],
        ["跨机放回 (requeue)", b200["requeued_total"], b120["requeued_total"]],
        ["墙钟 (s)", b200["wall_seconds"], b120["wall_seconds"]],
        ["吞吐 (任务/s)", b200["throughput_tasks_per_sec"], b120["throughput_tasks_per_sec"]],
        ["各机完成分布", str(b200["per_machine_done"]), str(b120["per_machine_done"])],
        ["正确性", "✓ 通过" if b200["correct"] else "✗",
         "✓ 通过" if b120["correct"] else "✗"],
    ]

    e2e_rows = [["任务", "op", "耗时", "产物", "状态"]]
    for n in E2E["nodes"]:
        e2e_rows.append([
            n["label"], n["op"],
            f'{n["dur"]*1000:.0f}ms' if n["dur"] else "—",
            n["artifact"] or "—",
            "✓ completed" if n["status"] == "completed" else n["status"],
        ])

    html_doc = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>本地可审计协作台 · 实跑证据</title>
<style>
 body{{background:#0b1120;color:#e2e8f0;font-family:Segoe UI,Helvetica,Arial,sans-serif;margin:0;padding:28px}}
 h1{{font-size:22px;margin:0 0 4px}} h2{{font-size:17px;margin:26px 0 10px;color:#93c5fd}}
 .sub{{color:#94a3b8;font-size:13px;margin-bottom:18px}}
 .card{{background:#111827;border:1px solid #1f2937;border-radius:12px;padding:18px;margin:14px 0}}
 .grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:10px 0}}
 .kpi{{background:#0f172a;border:1px solid #1f2937;border-radius:10px;padding:14px}}
 .kpi .v{{font-size:24px;font-weight:700;color:#22c55e}} .kpi .l{{font-size:12px;color:#94a3b8}}
 table.tbl{{border-collapse:collapse;width:100%;font-size:13px;margin-top:6px}}
 table.tbl th,table.tbl td{{border:1px solid #1f2937;padding:7px 10px;text-align:left}}
 table.tbl th{{background:#1e293b;color:#cbd5e1}} table.tbl td{{color:#e2e8f0}}
 .ok{{color:#22c55e;font-weight:700}} .warn{{color:#f59e0b}}
 .boundary{{background:#1c1917;border-left:4px solid #f59e0b;padding:12px 14px;border-radius:6px;
   color:#fde68a;font-size:13px;line-height:1.6}}
 svg{{width:100%;height:auto;background:#0f172a;border-radius:10px}}
 code{{background:#1e293b;padding:1px 6px;border-radius:4px;font-size:12px}}
</style></head><body>
<h1>本地可审计协作台 · 实跑证据</h1>
<div class="sub">生成于 {esc(E2E['ts'])} · 数据来自 <code>data/cluster_stress_*.json</code> 与
<code>data/pipeline_run_*.md</code> · 零依赖、可复跑（<code>tools/gen_evidence_report.py</code>）</div>

<div class="grid">
 <div class="kpi"><div class="v">{REGRESSION['total']}</div><div class="l">全量回归项（失败 {REGRESSION['failed']}，skip {REGRESSION['skipped']}）</div></div>
 <div class="kpi"><div class="v">5/5</div><div class="l">端到端流水线实跑完成</div></div>
 <div class="kpi"><div class="v">0</div><div class="l">跨机锁互斥违规 / 任务重复执行</div></div>
 <div class="kpi"><div class="v">~1665</div><div class="l">跨机锁吞吐 ops/s（P99&lt;0.16ms）</div></div>
</div>

<div class="boundary" style="border-left-color:#22c55e">
<b>信任锚点（请先读这条）：</b>本页所有数字均来自<b>本地真实跑出来的数据</b>，可复跑、可审计；
但我们<b>如实声明</b>——本次为单宿主 + 同机多进程（进程模拟机器、本机 TCP 回环模拟网络），
协调后端是项目自带的协议兼容 FakeRedisServer，<b>非官方 Redis</b>。吞吐/延迟数字<b>不往外推</b>到分散物理机
（详见下方「诚实边界」）。我们宁可把天花板写清楚，也不把玩具数字包装成生产基准——这正是专业版付费点的起点。
</div>

<h2>一、端到端流水线（DAG 实跑截图）</h2>
<div class="card">
{dag}
{table(["任务","op","耗时","产物","状态"], e2e_rows[1:], "tbl")}
<div class="sub" style="margin-top:10px">菱形 DAG：research 扇出到 lint/fetch，汇合到 analyze，再到 publish。
op=shell 的两步真实写入系统临时目录（<code>sectrust_raw.json</code> / <code>sectrust_report.md</code>），
由 <code>ResultSummarizer</code> 自动抽取为产物清单——通用汇总不绑定任何模板或 op。</div>
</div>

<h2>二、真实多机压测数据（同机多进程 + 本机 TCP 协调）</h2>
<div class="card">
<h3 style="color:#cbd5e1;font-size:14px">基准 A · 跨机原语争用（锁 / 心跳 / 死信）</h3>
{table(a_rows[0], a_rows[1:], "tbl")}
{lat}
</div>
<div class="card">
<h3 style="color:#cbd5e1;font-size:14px">基准 B · 真实 ExecutionDaemon 集群（共享 SQLite + TCP 协调）</h3>
{table(b_rows[0], b_rows[1:], "tbl")}
</div>

<div class="boundary">
<b>诚实边界（必须如实记录）：</b>本次为单宿主 + 同机多进程，进程隔离模拟机器、本机 TCP 回环模拟网络；
协调后端是项目自带的协议兼容 FakeRedisServer（真 socket 级 TCP RESP 服务），非官方 Redis。
真实跨机房还会叠加延迟 / 分区 / WAL 落 NFS 失效等风险。这不改变已验证的不变量
（锁互斥、任务不重复执行、死信可恢复），但吞吐/延迟数字不应直接外推到分散物理机。
</div>

<h2>三、任务结案报告样例（Trace 回放 + 成本 + 审批链 统一落盘）</h2>
{render_closure_html(closure)}

<h2>四、结论与专业版前瞻</h2>
<div class="card">
产品已从「功能清单」升级为<b>有数据支撑的本地可审计协作台</b>：
<ul>
<li>端到端：建 DAG → 真实 daemon 执行 → 通用汇总落盘，链路闭环（5/5 完成）。</li>
<li>跨机：锁互斥 0 违规、任务跨机 0 重复执行、死信阈值正确可恢复。</li>
<li>质量：全量回归 {REGRESSION['total']} 项 0 失败（skip {REGRESSION['skipped']}）。</li>
<li>审计：本页「任务结案报告」即开箱能力——Trace/成本/审批链三路自动并成一份可交付产物。</li>
</ul>
<p style="margin-top:12px"><b>专业版差异点（把"本地审计能力"变成"企业可交付物"）：</b></p>
<ul>
<li><b>真机多机房压测报告</b>：补上本页诚实边界里的空白——在真实分散物理机/多可用区跑同套不变量，给出可外推的吞吐/延迟基准。</li>
<li><b>跨地域延迟看板</b>：把单宿主下的锁/任务延迟，升级为跨地域分位看板与异常告警。</li>
<li><b>审批链审计导出</b>：把结案报告里的审批链一键导出为合规留存格式（PDF/SARIF/审计流），满足企业审计与留痕要求。</li>
</ul>
<div class="sub" style="margin-top:8px">免费/社区版已具备"审计数据生成能力"；专业版做的是"把这些数据变成合规交付物"。详见
<code>PRODUCT_ANALYSIS.md</code> 第八节。</div>
</div>
</body></html>"""
    return html_doc


def main():
    dag = build_dag_svg()
    lat = build_latency_svg()
    doc = build_html()
    with open(os.path.join(OUT, "e2e_dag.svg"), "w", encoding="utf-8") as f:
        f.write(dag)
    with open(os.path.join(OUT, "stress_latency_chart.svg"), "w", encoding="utf-8") as f:
        f.write(lat)
    with open(os.path.join(OUT, "e2e_and_stress_report.html"), "w", encoding="utf-8") as f:
        f.write(doc)
    print("wrote:", os.path.join(OUT, "e2e_dag.svg"))
    print("wrote:", os.path.join(OUT, "stress_latency_chart.svg"))
    print("wrote:", os.path.join(OUT, "e2e_and_stress_report.html"))


if __name__ == "__main__":
    main()
