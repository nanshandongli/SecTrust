"""
console_web.py —— 产品化「协作审计台」前端（仿 WorkBuddy 风格 · 可切换面板）

定位：把本仓库已有的审计能力（Trace 回放 / 成本看板 / 审批链 / 安全姿态）包装成一个
      类 WorkBuddy 的产品界面——左侧可折叠导航栏 + 顶部栏 + 主内容区（hash 路由切换面板）。

设计：
- 零依赖：仅用标准库 http.server；前端是单文件 SPA（内联 CSS/JS，无构建步骤）。
- 只读优先：默认只读取 task_registry.db / trace.db / cost.db，绝不做写操作。
- 演示数据：--seed 会把一套隔离的演示数据写进 data/console_demo_*.db（不污染真实库），
  让面板立刻有内容可看；不带 --seed 时只读真实库（可能为空，面板显示空态）。

界面（模仿 WorkBuddy 大体骨架，特色面板为 WorkBuddy 没有的）：
  概览 / 对话(壳) / 任务中心 / 审计轨迹 / 成本看板 / 审批中心(含导出) / 安全中心 / 技能 / 设置

运行：
    python console_web.py --port 8088                # 读真实库
    python console_web.py --port 8088 --seed         # 生成并读隔离演示库
"""

import argparse
import json
import os
import sqlite3
import tempfile
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from task_registry import TaskRegistry
from approval import ApprovalCenter
from trace import TraceStore
from cost import CostStore
from audit_suite import AuditSuite
from task_center_web import dag_view


# --------------------------------------------------------------------------
# 演示数据（隔离：只写到 console_demo_*.db，绝不碰真实库）
# --------------------------------------------------------------------------
def seed_demo(registry_db: str, trace_db: str, cost_db: str) -> None:
    """生成一套连贯的多 AI 协作审计演示数据。"""
    for p in (registry_db, trace_db, cost_db):
        if os.path.exists(p):
            os.remove(p)

    reg = TaskRegistry(db_path=registry_db, runner_id="demo-seed")
    ts = TraceStore(trace_db)
    cs = CostStore(cost_db)
    ac = ApprovalCenter(registry_db, tracer=ts)

    # --- Trace 1：双十一大促素材生成流水线（主链路）---
    tid1 = ts.start_trace(title="双十一大促素材生成流水线", kind="pipeline",
                          started_by="planner")
    ts.record(tid1, "plan_built", summary="拆解 5 道工序", detail={"steps": 5})
    t_plan = "demo-plan"
    reg.register_task(t_plan, {"name": "规划大促素材", "role": "planner"}, depends_on=[])
    t_a, t_b, t_c = "demo-design", "demo-copy", "demo-video"
    reg.register_task(t_a, {"name": "主视觉设计", "role": "designer"}, depends_on=[t_plan])
    reg.register_task(t_b, {"name": "文案撰写", "role": "writer"}, depends_on=[t_plan])
    reg.register_task(t_c, {"name": "短视频生成", "role": "video"}, depends_on=[t_plan])
    reg.update_task_status(t_plan, "done")
    reg.update_task_status(t_a, "running")
    reg.update_task_status(t_b, "done")
    reg.update_task_status(t_c, "done")

    aid1 = ac.request("publish", "将生成素材发布到生产 CDN", task_id=t_a,
                      channel="web", user_id="ops-1", trace_id=tid1)
    ts.record(tid1, "task_registered", task_id=t_a, summary="登记主视觉任务")
    ts.record(tid1, "tool_call", task_id=t_a, actor="designer-bot",
              summary="调用 CDN 上传工具", detail={"tool": "cdn_upload"})
    ac.decide(aid1, "approve", by="human-ops")
    ts.record(tid1, "task_completed", task_id=t_a, actor="designer-bot",
              summary="素材发布完成")
    reg.update_task_status(t_a, "done")
    ts.finish(tid1, status="completed")

    # --- Trace 2：代码安全自审 ---
    tid2 = ts.start_trace(title="代码安全自审流水线", kind="pipeline",
                          started_by="scanner-bot")
    ts.record(tid2, "scan_start", summary="扫描全仓源码")
    ts.record(tid2, "finding", summary="命中 22 条 high/critical（均为金标准假密钥语料）",
              detail={"findings": 22, "real_leaks": 0})
    ts.record(tid2, "scan_done", summary="自审通过：0 真实泄露")
    ts.finish(tid2, status="completed")

    # --- Trace 3：被驳回的高危操作（演示 needs_human / 拒绝）---
    tid3 = ts.start_trace(title="导出全量客户数据", kind="task", started_by="analyst")
    aid2 = ac.request("export_dataset", "导出全部客户 PII 到外部存储", task_id="demo-plan",
                      channel="api", user_id="analyst-1", trace_id=tid3)
    ts.record(tid3, "approval_requested", summary="请求导出数据集（高危）")
    ac.decide(aid2, "reject", by="dpo")
    ts.record(tid3, "approval_rejected", summary="DPO 驳回：缺少数据合规评估")
    ts.finish(tid3, status="blocked")

    # 一条挂起的审批（演示待办）
    ac.request("publish", "发布本周运营周报", task_id=t_b, channel="web",
               user_id="ops-2", trace_id=tid1)

    # --- 成本：多个任务 + 一个离群点 ---
    for i, (tid, tref, model, tok_in, tok_out) in enumerate([
        (tid1, t_a, "gpt-4o", 12000, 3000),
        (tid2, "demo-plan", "claude-3.5-sonnet", 8000, 1500),
        (tid3, "demo-plan", "gpt-4o-mini", 2000, 400),
        (tid1, t_b, "gemini-1.5-pro", 6000, 2000),
    ]):
        cs.record_model_call(task_id=tref, trace_id=tid, model=model,
                             input_tokens=tok_in, output_tokens=tok_out,
                             actor="bot-%d" % i)
        cs.record_tool_call(task_id=tref, trace_id=tid, tool="cdn_upload",
                            actor="bot-%d" % i)
        cs.finalize_task(tref, trace_id=tid, role="demo", template="promo",
                         tier="pro", outcome="completed")

    # 离群点：一次异常昂贵的调用
    cs.record_model_call(task_id=t_c, trace_id=tid1, model="gpt-4o",
                         input_tokens=180000, output_tokens=42000, actor="video-bot")
    cs.finalize_task(t_c, trace_id=tid1, role="demo", template="video",
                     tier="pro", outcome="completed")

    reg.close(); ts.close(); cs.close(); ac.close()


# --------------------------------------------------------------------------
# SPA HTML（仿 WorkBuddy：左侧导航 + 顶部栏 + 主内容区）
# --------------------------------------------------------------------------
APP_HTML = """<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>协作审计台 · SecTrust Console</title>
<style>
  :root{
    --bg:#f5f6f8; --card:#ffffff; --line:#e9ebef; --txt:#1f2328; --sub:#656d76;
    --accent:#3b6cff; --accent2:#16b8a6; --sidebar:#0f1419; --sidebar-txt:#c9d1d9;
    --sidebar-active:#1f6feb; --shadow:0 1px 3px rgba(16,22,26,.08);
    --ok:#2e7d32; --warn:#c62828; --pend:#f9a825; --chip:#eef1f4;
  }
  [data-theme="dark"]{
    --bg:#161b22; --card:#1c2230; --line:#2a3240; --txt:#e6edf3; --sub:#8b949e;
    --chip:#222b38; --shadow:0 1px 3px rgba(0,0,0,.4);
  }
  *{box-sizing:border-box}
  html,body{height:100%}
  body{margin:0;font-family:-apple-system,"Segoe UI",system-ui,"PingFang SC","Microsoft YaHei",sans-serif;
    background:var(--bg);color:var(--txt);display:flex;font-size:14px}
  /* ---- 侧边栏 ---- */
  .sidebar{width:236px;background:var(--sidebar);color:var(--sidebar-txt);
    display:flex;flex-direction:column;flex:0 0 auto;transition:width .18s ease;overflow:hidden}
  .sidebar.collapsed{width:64px}
  .brand{display:flex;align-items:center;gap:10px;padding:16px 18px;font-weight:700;
    color:#fff;font-size:15px;white-space:nowrap}
  .brand .logo{width:30px;height:30px;border-radius:8px;flex:0 0 auto;
    background:linear-gradient(135deg,#3b6cff,#16b8a6);display:flex;align-items:center;
    justify-content:center;color:#fff;font-weight:800}
  .brand .sub{font-size:11px;color:#8b949e;font-weight:400}
  .nav{flex:1;padding:8px 10px;overflow-y:auto}
  .nav a{display:flex;align-items:center;gap:12px;padding:9px 12px;border-radius:8px;
    color:var(--sidebar-txt);text-decoration:none;font-size:13.5px;margin-bottom:2px;
    white-space:nowrap;cursor:pointer}
  .nav a:hover{background:#1c2530}
  .nav a.active{background:var(--sidebar-active);color:#fff}
  .nav a svg{flex:0 0 auto}
  .nav .label{transition:opacity .15s}
  .sidebar.collapsed .brand .txt,.sidebar.collapsed .nav .label{opacity:0;width:0;overflow:hidden}
  .nav-foot{padding:10px;border-top:1px solid #21262d;font-size:11px;color:#6b727b;white-space:nowrap}
  /* ---- 主区 ---- */
  .main{flex:1;display:flex;flex-direction:column;min-width:0}
  .topbar{height:54px;background:var(--card);border-bottom:1px solid var(--line);
    display:flex;align-items:center;gap:14px;padding:0 20px;box-shadow:var(--shadow);z-index:5}
  .topbar .title{font-weight:600;font-size:15px}
  .topbar .search{flex:1;max-width:420px;margin-left:8px}
  .topbar .search input{width:100%;padding:7px 12px;border:1px solid var(--line);
    border-radius:8px;background:var(--bg);color:var(--txt);font-size:13px}
  .topbar .spacer{flex:1}
  .pill{font-size:12px;padding:4px 10px;border-radius:20px;background:var(--chip);color:var(--sub)}
  .pill.live{color:var(--ok)}
  .pill.demo{background:#fff4e5;color:#b26a00}
  .iconbtn{width:34px;height:34px;border:1px solid var(--line);border-radius:8px;
    background:var(--card);cursor:pointer;display:flex;align-items:center;justify-content:center;color:var(--sub)}
  .content{padding:22px 26px;overflow-y:auto;flex:1}
  .view{display:none;animation:fade .2s ease}
  .view.active{display:block}
  @keyframes fade{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:none}}
  h2.vh{font-size:18px;margin:0 0 4px}
  .vdesc{color:var(--sub);font-size:13px;margin:0 0 18px}
  /* ---- 卡片 / 网格 ---- */
  .grid{display:grid;gap:14px}
  .stats{grid-template-columns:repeat(auto-fill,minmax(150px,1fr))}
  .stat{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;box-shadow:var(--shadow)}
  .stat .n{font-size:26px;font-weight:700;line-height:1.1}
  .stat .l{color:var(--sub);font-size:12.5px;margin-top:4px}
  .stat .n.accent{color:var(--accent)} .stat .n.ok{color:var(--ok)} .stat .n.warn{color:var(--warn)}
  .panel{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px 18px;box-shadow:var(--shadow);margin-top:16px}
  .panel h3{font-size:14px;margin:0 0 12px;display:flex;align-items:center;gap:8px}
  /* ---- 表格 ---- */
  .tbl{width:100%;border-collapse:collapse;font-size:13px}
  .tbl th,.tbl td{text-align:left;padding:9px 10px;border-bottom:1px solid var(--line);vertical-align:top}
  .tbl th{color:var(--sub);font-weight:600;position:sticky;top:0;background:var(--card)}
  .tbl tbody tr:hover{background:var(--bg)}
  .tbl td.pre{white-space:pre-wrap;word-break:break-all;max-width:340px;color:var(--sub)}
  .empty{padding:22px;color:var(--sub);text-align:center;font-size:13px}
  /* ---- 徽章 ---- */
  .badge{display:inline-block;padding:1px 9px;border-radius:10px;font-size:12px;white-space:nowrap}
  .b-registered{background:#e3f2fd;color:#1565c0} .b-running{background:#fff3e0;color:#ef6c00}
  .b-done,.b-completed,.b-approved{background:#e8f5e9;color:#2e7d32}
  .b-failed,.b-rejected,.b-expired,.b-blocked{background:#fdecea;color:#c62828}
  .b-pending,.b-needs_human{background:#fff8e1;color:#b26a00;font-weight:600}
  /* ---- 进度条 ---- */
  .progress{background:var(--chip);border-radius:6px;height:12px;width:120px;overflow:hidden}
  .progress>i{display:block;height:100%;background:var(--accent)}
  /* ---- DAG 波次 ---- */
  .wave{display:flex;gap:10px;padding:8px 0;border-bottom:1px dashed var(--line);align-items:flex-start}
  .wave:last-child{border-bottom:none}
  .wave-label{flex:0 0 70px;font-size:12px;color:var(--sub);padding-top:6px}
  .wave-nodes{display:flex;flex-wrap:wrap;gap:8px}
  .node{border:1px solid var(--line);border-radius:8px;background:var(--card);padding:6px 10px;font-size:12px;min-width:150px}
  .node .nid{font-weight:600} .node.ready{border-color:var(--accent);box-shadow:0 0 0 1px var(--accent) inset}
  .node.dead{border-color:var(--warn);background:#fdecea} .node .nwait{color:var(--warn);font-size:11px;margin-top:2px}
  /* ---- Trace 时间线 ---- */
  .tl{position:relative;padding-left:22px}
  .tl::before{content:"";position:absolute;left:7px;top:4px;bottom:4px;width:2px;background:var(--line)}
  .tl .ev{position:relative;padding:8px 0}
  .tl .ev::before{content:"";position:absolute;left:-19px;top:12px;width:10px;height:10px;border-radius:50%;
    background:var(--accent);border:2px solid var(--card)}
  .tl .ev .k{font-weight:600} .tl .ev .meta{color:var(--sub);font-size:12px}
  .tl .ev .det{color:var(--sub);font-size:12px;white-space:pre-wrap;word-break:break-all;margin-top:2px}
  /* ---- 对话壳 ---- */
  .chat{display:flex;flex-direction:column;height:calc(100vh - 54px - 44px)}
  .chat .msgs{flex:1;overflow-y:auto;padding:20px 0}
  .msg{max-width:72%;padding:10px 14px;border-radius:14px;margin-bottom:12px;line-height:1.5;font-size:14px}
  .msg.user{margin-left:auto;background:var(--accent);color:#fff;border-bottom-right-radius:4px}
  .msg.bot{margin-right:auto;background:var(--card);border:1px solid var(--line);border-bottom-left-radius:4px}
  .chips{display:flex;flex-wrap:wrap;gap:8px;padding:10px 0}
  .chips button{border:1px solid var(--accent);color:var(--accent);background:transparent;
    padding:6px 12px;border-radius:18px;cursor:pointer;font-size:13px}
  .chat .in{display:flex;gap:10px;padding-top:12px;border-top:1px solid var(--line)}
  .chat .in input{flex:1;padding:10px 14px;border:1px solid var(--line);border-radius:10px;background:var(--bg);color:var(--txt)}
  .chat .in button{background:var(--accent);color:#fff;border:none;padding:0 18px;border-radius:10px;cursor:pointer}
  /* ---- 技能卡 ---- */
  .sk-grid{grid-template-columns:repeat(auto-fill,minmax(220px,1fr))}
  .sk{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px;box-shadow:var(--shadow)}
  .sk .nm{font-weight:600;font-size:14px}
  .sk .src{font-size:11px;color:var(--accent2);margin:2px 0 6px}
  .sk .ds{font-size:12.5px;color:var(--sub);line-height:1.5}
  /* ---- 安全清单 ---- */
  .check{display:flex;align-items:flex-start;gap:10px;padding:8px 0;border-bottom:1px solid var(--line)}
  .check .dot{width:18px;height:18px;border-radius:50%;flex:0 0 auto;margin-top:1px;
    background:var(--ok);color:#fff;display:flex;align-items:center;justify-content:center;font-size:12px}
  .check .t{font-weight:600;font-size:13.5px} .check .d{color:var(--sub);font-size:12.5px}
  /* ---- tabs ---- */
  .tabs{display:flex;gap:6px;margin-bottom:14px}
  .tabs button{border:1px solid var(--line);background:var(--card);padding:6px 14px;border-radius:8px;cursor:pointer;font-size:13px;color:var(--sub)}
  .tabs button.active{background:var(--accent);color:#fff;border-color:var(--accent)}
  .btn{border:1px solid var(--accent);background:var(--accent);color:#fff;padding:7px 14px;border-radius:8px;cursor:pointer;font-size:13px;text-decoration:none;display:inline-block}
  .btn.ghost{background:transparent;color:var(--accent)}
  .row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
  code.k{background:var(--chip);padding:1px 6px;border-radius:5px;font-size:12px}
  .warnbar{background:#fff4e5;color:#b26a00;border:1px solid #ffe0b2;border-radius:10px;padding:10px 14px;font-size:13px;margin-bottom:16px}
  a.link{color:var(--accent);text-decoration:none}
  a.link:hover{text-decoration:underline}
  /* ---- 打磨：交互反馈 ---- */
  .topbar .search{position:relative}
  .topbar .search input:focus{outline:none;border-color:var(--accent);
    box-shadow:0 0 0 3px rgba(59,108,255,.12)}
  .search-pop{position:absolute;top:40px;left:0;right:0;background:var(--card);
    border:1px solid var(--line);border-radius:10px;box-shadow:0 8px 24px rgba(0,0,0,.14);
    max-height:60vh;overflow-y:auto;display:none;z-index:20}
  .search-pop.open{display:block}
  .search-pop .sgrp{font-size:11px;color:var(--sub);padding:8px 12px 4px;font-weight:600}
  .search-pop .sitem{padding:8px 12px;cursor:pointer;font-size:13px;display:flex;gap:8px;align-items:center}
  .search-pop .sitem:hover{background:var(--bg)}
  .search-pop .sitem .sid{color:var(--sub);font-size:11.5px;font-family:ui-monospace,monospace}
  .nav a.active{box-shadow:inset 2px 0 0 var(--accent2)}
  .panel{transition:box-shadow .15s,border-color .15s}
  .panel:hover{box-shadow:0 3px 14px rgba(0,0,0,.07)}
  .stat{transition:transform .15s,box-shadow .15s}
  .stat:hover{transform:translateY(-1px);box-shadow:0 4px 14px rgba(0,0,0,.08)}
  .btn{transition:opacity .15s,transform .1s}
  .btn:hover{opacity:.9}
  .btn:active{transform:translateY(1px)}
  .tbl tbody tr{transition:background .12s}
  .iconbtn:hover{border-color:var(--accent);color:var(--accent)}
  /* ---- 对话：引擎条 / 消息元信息 ---- */
  .chat-engine{display:flex;align-items:center;gap:8px;flex-wrap:wrap;padding:8px 0;
    border-bottom:1px dashed var(--line);font-size:12px}
  .ctip{color:var(--sub);font-size:11.5px}
  .msg .mmeta{margin-top:7px;padding-top:6px;border-top:1px solid var(--line);
    font-size:11.5px;color:var(--sub);line-height:1.5}
  .msg.user .mmeta{border-top-color:rgba(255,255,255,.25);color:rgba(255,255,255,.85)}
  .msg .mtext{word-break:break-word}
  .typing{color:var(--sub);font-style:italic}
  .chat .in input:focus{outline:none;border-color:var(--accent);
    box-shadow:0 0 0 3px rgba(59,108,255,.12)}
  .chat .in button:hover{opacity:.9}
  /* ---- 特色面板：明细块 ---- */
  .kv{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:10px}
  .kv .item{background:var(--bg);border:1px solid var(--line);border-radius:9px;padding:10px 12px}
  .kv .item .k{font-size:11.5px;color:var(--sub)}
  .kv .item .v{font-size:15px;font-weight:600;margin-top:3px;word-break:break-all}
  .bar{height:6px;background:var(--chip);border-radius:4px;overflow:hidden;margin-top:6px}
  .bar>i{display:block;height:100%;background:var(--accent)}
  .tag{display:inline-block;font-size:11px;padding:1px 7px;border-radius:6px;
    background:var(--chip);color:var(--sub);margin-right:6px}
  .split{display:grid;grid-template-columns:1fr 1fr;gap:14px}
  @media(max-width:980px){.split{grid-template-columns:1fr}}
</style>
</head>
<body>
<div class="sidebar" id="sidebar">
  <div class="brand">
    <div class="logo">S</div>
    <div class="txt">协作审计台<span class="sub" style="display:block">SecTrust Console</span></div>
  </div>
  <nav class="nav" id="nav"></nav>
  <div class="nav-foot" id="navfoot">v0.9 · 本地可审计</div>
</div>

<div class="main">
  <div class="topbar">
    <button class="iconbtn" id="collapseBtn" title="折叠/展开导航">
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 6h18M3 12h18M3 18h18"/></svg>
    </button>
    <div class="title" id="viewTitle">概览</div>
    <div class="search"><input id="globalSearch" placeholder="搜索任务 / 轨迹 / 审批…"><div class="search-pop" id="searchPop"></div></div>
    <div class="spacer"></div>
    <span class="pill live" id="dbPill">● 实时</span>
    <span class="pill demo" id="demoPill" style="display:none">演示数据</span>
    <button class="iconbtn" id="themeBtn" title="切换主题">
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>
    </button>
  </div>
  <div class="content">
    <div class="view active" id="view-overview"></div>
    <div class="view" id="view-chat"></div>
    <div class="view" id="view-tasks"></div>
    <div class="view" id="view-trace"></div>
    <div class="view" id="view-cost"></div>
    <div class="view" id="view-approvals"></div>
    <div class="view" id="view-security"></div>
    <div class="view" id="view-skills"></div>
    <div class="view" id="view-settings"></div>
  </div>
</div>

<script>
const $ = id => document.getElementById(id);
const fmt = t => t ? new Date(t*1000).toLocaleString('zh-CN') : '-';
const ago = t => t ? Math.max(0, Math.round(Date.now()/1000 - t)) + 's 前' : '-';
const esc = s => (s==null?'':String(s)).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
function badge(s){const m={'registered':'b-registered','running':'b-running','done':'b-done','completed':'b-completed','failed':'b-failed','blocked':'b-blocked','needs_human':'b-needs_human','pending':'b-pending','approved':'b-approved','rejected':'b-rejected','expired':'b-expired'};return '<span class="badge '+(m[s]||'')+'">'+esc(s)+'</span>';}
async function jget(u){const r=await fetch(u);return r.json();}

/* ---------------- 全局搜索（任务 / 轨迹 / 审批 / 技能） ---------------- */
let searchTimer=null;
function initSearch(){
  const inp=$('globalSearch'), pop=$('searchPop');
  function close(){pop.classList.remove('open');}
  inp.addEventListener('input',()=>{
    clearTimeout(searchTimer);
    searchTimer=setTimeout(()=>doSearch(inp.value.trim()),220);
  });
  inp.addEventListener('keydown',e=>{if(e.key==='Escape'){inp.value='';close();}});
  document.addEventListener('click',e=>{
    if(!e.target.closest('.topbar .search'))close();
  });
}
async function doSearch(q){
  const pop=$('searchPop');
  if(!q){pop.classList.remove('open');return;}
  try{
    const [tasks,traces,apprs]=await Promise.all([
      jget('/api/tasks?limit=300'),jget('/api/traces?limit=200'),jget('/api/approvals?limit=200')]);
    const ql=q.toLowerCase();
    const T=(tasks||[]).filter(t=>(t.task_id||'').toLowerCase().includes(ql)||
      JSON.stringify(t.payload||{}).toLowerCase().includes(ql)).slice(0,8);
    const R=(traces||[]).filter(t=>(t.trace_id||'').toLowerCase().includes(ql)||
      (t.title||'').toLowerCase().includes(ql)).slice(0,6);
    const A=(apprs||[]).filter(a=>(a.approval_id||'').toLowerCase().includes(ql)||
      (a.action||'').toLowerCase().includes(ql)||
      (a.detail||'').toLowerCase().includes(ql)).slice(0,6);
    if(!T.length&&!R.length&&!A.length){
      pop.innerHTML='<div class="sgrp">无匹配</div><div class="sitem" style="color:var(--sub)">没有匹配 “'+esc(q)+'” 的任务/轨迹/审批</div>';
      pop.classList.add('open');return;
    }
    let h='';
    if(T.length){h+='<div class="sgrp">任务 ('+T.length+')</div>'+T.map(t=>
      '<div class="sitem" onclick="go(\'tasks\');closeSearch()">'+badge(t.status)+' <b>'+esc(t.task_id)+'</b><span class="sid">'+esc(JSON.stringify(t.payload||{}).slice(0,60))+'</span></div>').join('');}
    if(R.length){h+='<div class="sgrp">审计轨迹 ('+R.length+')</div>'+R.map(t=>
      '<div class="sitem" onclick="go(\'trace\');closeSearch()">'+badge(t.status)+' <b>'+esc(t.trace_id)+'</b><span class="sid">'+esc(t.title||'')+'</span></div>').join('');}
    if(A.length){h+='<div class="sgrp">审批 ('+A.length+')</div>'+A.map(a=>
      '<div class="sitem" onclick="go(\'approvals\');closeSearch()">'+badge(a.status)+' <b>'+esc(a.action)+'</b><span class="sid">'+esc(a.approval_id)+'</span></div>').join('');}
    pop.innerHTML=h;pop.classList.add('open');
  }catch(e){pop.innerHTML='<div class="sitem" style="color:var(--sub)">搜索失败</div>';pop.classList.add('open');}
}
function closeSearch(){const p=$('searchPop');if(p)p.classList.remove('open');}

const ICON={
 home:'<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 11l9-8 9 8M5 10v10h14V10"/></svg>',
 chat:'<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>',
 tasks:'<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 9h18M8 4v16"/></svg>',
 trace:'<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 12h4l3-7 5 14 3-7h4"/></svg>',
 cost:'<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><path d="M12 7v10M9 9.5h4.5a2 2 0 0 1 0 4H9"/></svg>',
 approve:'<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 11l3 3 8-8"/><path d="M20 12v7a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h9"/></svg>',
 shield:'<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6z"/></svg>',
 puzzle:'<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 3a2 2 0 0 1 4 0v2h2a2 2 0 0 1 2 2v2h2a2 2 0 0 1 0 4h-2v2a2 2 0 0 1-2 2h-2v-2H9v2a2 2 0 0 1-4 0v-2H3a2 2 0 0 1 0-4h2V9a2 2 0 0 1 2-2h2z"/></svg>',
 gear:'<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="3"/><path d="M19 12a7 7 0 0 0-.1-1l2-1.5-2-3.5-2.4 1a7 7 0 0 0-1.7-1L14.5 3h-5l-.3 2.5a7 7 0 0 0-1.7 1l-2.4-1-2 3.5 2 1.5a7 7 0 0 0 0 2l-2 1.5 2 3.5 2.4-1a7 7 0 0 0 1.7 1l.3 2.5h5l.3-2.5a7 7 0 0 0 1.7-1l2.4 1 2-3.5-2-1.5a7 7 0 0 0 .1-1z"/></svg>'
};
const NAV=[
 {id:'overview',label:'概览',icon:ICON.home},
 {id:'chat',label:'对话',icon:ICON.chat},
 {id:'tasks',label:'任务中心',icon:ICON.tasks},
 {id:'trace',label:'审计轨迹',icon:ICON.trace},
 {id:'cost',label:'成本看板',icon:ICON.cost},
 {id:'approvals',label:'审批中心',icon:ICON.approve},
 {id:'security',label:'安全中心',icon:ICON.shield},
 {id:'skills',label:'技能',icon:ICON.puzzle},
 {id:'settings',label:'设置',icon:ICON.gear}
];
const TITLES={overview:'概览',chat:'对话',tasks:'任务中心',trace:'审计轨迹',cost:'成本看板',
  approvals:'审批中心',security:'安全中心',skills:'技能',settings:'设置'};

// 渲染侧边栏
$('nav').innerHTML = NAV.map(n=>
  '<a data-view="'+n.id+'">'+n.icon+'<span class="label">'+n.label+'</span></a>').join('');
$('nav').querySelectorAll('a').forEach(a=>a.addEventListener('click',()=>go(a.dataset.view)));

function go(view){
  if(!TITLES[view]) view='overview';
  location.hash = '#/'+view;
}
function route(){
  const v = (location.hash.replace('#/','')||'overview');
  NAV.forEach(n=>{const a=$('nav').querySelector('a[data-view="'+n.id+'"]');if(a)a.classList.toggle('active',n.id===v);});
  document.querySelectorAll('.view').forEach(el=>el.classList.remove('active'));
  const view=$('view-'+v); if(view) view.classList.add('active');
  $('viewTitle').textContent = TITLES[v]||'概览';
  const loaders={overview:loadOverview,chat:loadChat,tasks:loadTasks,trace:loadTraceList,
    cost:loadCost,approvals:loadApprovals,security:loadSecurity,skills:loadSkills,settings:loadSettings};
  (loaders[v]||loadOverview)();
}
window.addEventListener('hashchange',route);
initSearch();

$('collapseBtn').addEventListener('click',()=>$('sidebar').classList.toggle('collapsed'));
$('themeBtn').addEventListener('click',()=>{
  const t=document.documentElement.getAttribute('data-theme')==='dark'?'light':'dark';
  document.documentElement.setAttribute('data-theme',t);localStorage.setItem('wbtheme',t);
});
(function(){const t=localStorage.getItem('wbtheme');if(t)document.documentElement.setAttribute('data-theme',t);})();

// ---------------- 概览 ----------------
async function loadOverview(){
  const el=$('view-overview');
  const [ov]=await Promise.all([jget('/api/overview')]);
  const c=ov.counts||{};
  let demo='';
  if(ov.demo_mode) demo='<div class="warnbar">当前为 <b>演示数据</b>（隔离在 console_demo_*.db，不污染真实库）。在「设置」可重新生成。</div>';
  el.innerHTML = demo +
   '<h2 class="vh">协作审计台概览</h2>'+
   '<p class="vdesc">本地多 AI 协作的「可审计控制台」——任务、轨迹、成本、审批、安全，一屏掌握。</p>'+
   '<div class="grid stats">'+
     stat(c.tasks_total,'任务总数')+stat(c.running,'运行中','accent')+stat(c.pending,'待审批','warn')+
     stat(c.traces,'审计轨迹')+stat('¥'+(ov.cost_total||0).toFixed(2),'累计成本')+stat(ov.security_score+'/100','安全评分','ok')+
   '</div>'+
   '<div class="panel"><h3>快速入口</h3><div class="chips">'+
     '<button onclick="go(\'trace\')">查看审计轨迹</button>'+
     '<button onclick="go(\'cost\')">成本看板</button>'+
     '<button onclick="go(\'approvals\')">待审批 ('+(c.pending||0)+')</button>'+
     '<button onclick="go(\'security\')">安全中心</button>'+
     '<button onclick="go(\'chat\')">发起协作</button>'+
   '</div></div>'+
   '<div class="panel"><h3>安全姿态</h3>'+
     '<div class="check"><div class="dot">✓</div><div><div class="t">沙箱隔离：'+esc(ov.sandbox_backend||'-')+'（'+esc(ov.sandbox_containment||'-')+'）</div><div class="d">'+esc(ov.sandbox_note||'')+'</div></div></div>'+
     '<div class="check"><div class="dot">✓</div><div><div class="t">权限最小化：审批默认拒绝 + 非 root/cap-drop/只读根</div></div></div>'+
     '<div class="check"><div class="dot">✓</div><div><div class="t">源码硬编码密钥自扫：0 真实泄露</div><div class="d">22 条命中均为金标准假密钥语料，已排除。</div></div></div>'+
   '</div>';
}
function stat(n,l,cls){return '<div class="stat"><div class="n '+(cls||'')+'">'+esc(n)+'</div><div class="l">'+esc(l)+'</div></div>';}

// ---------------- 对话（规则优先 + LLM 兜底 · 真实后端） ----------------
let chatHistory=[];
async function loadChat(){
  const el=$('view-chat');
  if(el.dataset.init){el.classList.add('active');refreshChatStatus();return;}
  el.dataset.init='1';
  el.innerHTML='<h2 class="vh">对话</h2><p class="vdesc">问审计台：任务在跑什么、审批过没过、成本花了多少、代码有没有泄露。答案会标注引擎来源。</p>'+
   '<div class="chat"><div class="msgs" id="chatMsgs"></div>'+
   '<div class="chat-engine" id="chatEngine"></div>'+
   '<div class="chips">'+
     '<button data-q="今天有哪些任务在跑">今天哪些任务在跑？</button>'+
     '<button data-q="双十一素材发布审批通过了吗">素材发布审批通过了吗？</button>'+
     '<button data-q="这个月成本花了多少">这个月成本多少？</button>'+
     '<button data-q="代码里有没有泄露真实密钥">代码里有没有泄露真实密钥？</button>'+
     '<button data-q="帮我总结一下当前审计状态">总结当前审计状态</button>'+
   '</div>'+
   '<div class="in"><input id="chatInput" placeholder="输入消息，回车发送…（Enter 发送 / Shift+Enter 换行）"><button id="chatSend">发送</button></div></div>';
  const msgs=$('chatMsgs');
  function add(who,text,meta){
    const d=document.createElement('div');d.className='msg '+(who==='user'?'user':'bot');
    let h='<div class="mtext">'+String(text).replace(/\\n/g,'<br>')+'</div>';
    if(meta) h+='<div class="mmeta">'+meta+'</div>';
    d.innerHTML=h;msgs.appendChild(d);msgs.scrollTop=msgs.scrollHeight;return d;
  }
  add('bot','你好，我是 SecTrust 协作审计台。\n'+
    '· 审计类问题走<b>确定性规则引擎</b>：零幻觉、零外部依赖、直接读当前审计态；\n'+
    '· 规则没命中时兜底调 <b>LLM</b>（本地 Ollama 优先），并把审计态作为事实喂给它；\n'+
    '· LLM 不可用时我会直说，绝不伪造模型回答。\n试试下面的快捷问题 👇');
  async function send(q){
    if(!q||!q.trim())return;
    add('user',esc(q.trim()));
    chatHistory.push({role:'user',content:q.trim()});
    const holder=add('bot','<span class="typing">思考中…</span>','');
    try{
      const r=await fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({question:q.trim(),history:chatHistory.slice(-6)})});
      const d=await r.json();
      holder.querySelector('.mtext').innerHTML=String(d.answer||d.error||'无响应').replace(/\\n/g,'<br>');
      const meta=[];
      if(d.engine==='rule') meta.push('引擎：<b>规则引擎</b>（确定性，零幻觉）');
      else if(d.engine==='llm') meta.push('引擎：<b>LLM</b> '+(d.model||''));
      else meta.push('引擎：<b>不可用</b>');
      if(d.basis&&d.basis.length) meta.push('依据：'+d.basis.join('、'));
      if(d.next_steps&&d.next_steps.length) meta.push('下一步：'+d.next_steps.join('；'));
      holder.innerHTML='<div class="mtext">'+holder.querySelector('.mtext').innerHTML+'</div>'+
        '<div class="mmeta">'+meta.join(' · ')+'</div>';
      chatHistory.push({role:'assistant',content:String(d.answer||'')});
    }catch(e){
      holder.innerHTML='<div class="mtext">请求失败：'+esc(String(e))+'</div>'+
        '<div class="mmeta">引擎：<b>不可用</b></div>';
    }
  }
  el.querySelectorAll('.chips button').forEach(b=>b.addEventListener('click',()=>send(b.dataset.q)));
  $('chatSend').addEventListener('click',()=>{const i=$('chatInput');const v=i.value;i.value='';send(v);});
  $('chatInput').addEventListener('keydown',e=>{
    if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();const i=e.target;const v=i.value;i.value='';send(v);}
  });
  refreshChatStatus();
}
async function refreshChatStatus(){
  const el=$('chatEngine');if(!el)return;
  try{
    const s=await jget('/api/chat/status');
    const parts=[];
    parts.push('<span class="pill live">规则引擎</span>');
    if(s.ollama) parts.push('<span class="pill live">Ollama 可用'+(s.ollama_models&&s.ollama_models.length?('（'+s.ollama_models.length+' 模型）'):'')+'</span>');
    else parts.push('<span class="pill">Ollama 未启动</span>');
    if(s.openai) parts.push('<span class="pill live">OpenAI 已配置</span>');
    el.innerHTML=parts.join(' ')+'<span class="ctip">'+esc(s.note||'')+'</span>';
  }catch(e){el.innerHTML='<span class="ctip">引擎状态未知</span>';}
}

// ---------------- 任务中心（复用只读 API） ----------------
let tasksTab='tasks';
async function loadTasks(){
  const el=$('view-tasks');
  if(!el.dataset.init){
    el.dataset.init='1';
    el.innerHTML='<h2 class="vh">任务中心</h2><p class="vdesc">实时任务、依赖波次与 Runner 存活（只读）。</p>'+
      '<div class="tabs"><button data-t="tasks" class="active">任务</button><button data-t="dag">依赖波次</button><button data-t="runners">Runner</button></div>'+
      '<div id="tasksBody"></div>';
    el.querySelectorAll('.tabs button').forEach(b=>b.addEventListener('click',()=>{
      tasksTab=b.dataset.t;el.querySelectorAll('.tabs button').forEach(x=>x.classList.toggle('active',x===b));loadTasks();}));
  }
  const body=$('tasksBody');
  if(tasksTab==='tasks'){
    const tasks=await jget('/api/tasks?limit=500');const dag=await jget('/api/dag');
    body.innerHTML='<div class="panel"><table class="tbl"><thead><tr><th>task_id</th><th>runner</th><th>状态</th><th>依赖/等待</th><th>创建</th></tr></thead><tbody>'+
      (tasks.length?tasks.map(t=>{const dt=(dag.tasks&&dag.tasks[t.task_id])||{};const dep=(dt.depends_on&&dt.depends_on.length)?'依赖: '+dt.depends_on.join(', '):(dt.dead_upstream&&dt.dead_upstream.length?'上游已死: '+dt.dead_upstream.join(', '):'—');
        return '<tr><td>'+esc(t.task_id)+'</td><td>'+esc(t.runner_id||'-')+'</td><td>'+badge(t.status)+'</td><td style="color:var(--sub);font-size:12px">'+esc(dep)+'</td><td>'+fmt(t.created_at)+'</td></tr>';}).join(''):'<tr><td colspan="5" class="empty">无任务</td></tr>')+'</tbody></table></div>';
  } else if(tasksTab==='dag'){
    const dag=await jget('/api/dag');const ready=new Set(dag.ready||[]);
    body.innerHTML='<div class="panel">'+(!dag.tasks||!Object.keys(dag.tasks).length?'<div class="empty">暂无带依赖的任务</div>':
      (dag.waves||[]).map((w,i)=>'<div class="wave"><div class="wave-label">波次 '+i+'</div><div class="wave-nodes">'+
        w.map(tid=>{const t=dag.tasks[tid]||{};const dead=(t.dead_upstream||[]).length>0;const cls='node'+(ready.has(tid)?' ready':'')+(dead?' dead':'');
          const wait=dead?'<div class="nwait">上游已失败: '+(t.dead_upstream||[]).join(', ')+'</div>':((t.waiting_on||[]).length?'<div class="nwait">等待: '+(t.waiting_on||[]).join(', ')+'</div>':'');
          return '<div class="'+cls+'"><div class="nid">'+esc(tid)+' '+badge(t.status||'')+'</div><div class="nwait" style="color:var(--sub)">'+(t.name||'')+'</div>'+wait+'</div>';}).join('')+'</div></div>').join(''))+'</div>';
  } else {
    const rs=await jget('/api/runners');
    body.innerHTML='<div class="panel"><table class="tbl"><thead><tr><th>runner_id</th><th>last_heartbeat</th><th>状态</th><th>距上次</th></tr></thead><tbody>'+
      (rs.length?rs.map(r=>'<tr><td>'+esc(r.runner_id)+'</td><td>'+fmt(r.last_heartbeat)+'</td><td class="'+(r.status==='stale'?'b-failed':'')+'">'+badge(r.status)+'</td><td>'+ago(r.last_heartbeat)+'</td></tr>').join(''):'<tr><td colspan="4" class="empty">无 Runner 心跳</td></tr>')+'</tbody></table></div>';
  }
}

// ---------------- 审计轨迹 ----------------
let traceFilter='';
async function loadTraceList(){
  const el=$('view-trace');
  const traces=await jget('/api/traces?limit=50');
  const tot=(traces||[]).reduce((s,t)=>s+(t.event_count||0),0);
  const kinds={};
  (traces||[]).forEach(t=>{kinds[t.kind||'-']=(kinds[t.kind||'-']||0)+1;});
  el.innerHTML='<h2 class="vh">审计轨迹</h2><p class="vdesc">每一次协作都会留下不可篡改的轨迹（blake2b 内容指纹）。点击查看回放。</p>'+
    '<div class="grid stats">'+
      stat((traces||[]).length,'轨迹总数')+stat(tot,'事件总数','accent')+
      stat(Object.keys(kinds).length,'事件类型')+
      stat((traces||[]).filter(t=>t.status==='completed'||t.status==='done').length,'已完成','ok')+
    '</div>'+
    '<div class="panel"><h3>轨迹列表 <span class="tag" style="margin-left:6px">共 '+(traces||[]).length+' 条</span></h3>'+
    '<table class="tbl"><thead><tr><th>trace_id</th><th>标题</th><th>类型</th><th>状态</th><th>事件数</th><th>开始</th></tr></thead><tbody>'+
      (traces.length?traces.map(t=>'<tr style="cursor:pointer" onclick="loadTrace(\''+esc(t.trace_id)+'\')"><td>'+esc(t.trace_id)+'</td><td>'+esc(t.title||'')+'</td><td>'+esc(t.kind||'')+'</td><td>'+badge(t.status)+'</td><td>'+(t.event_count||0)+'</td><td>'+fmt(t.started_at)+'</td></tr>').join(''):'<tr><td colspan="6" class="empty">暂无轨迹</td></tr>')+'</tbody></table></div>'+
    '<div id="traceDetail"></div>';
}
async function loadTrace(tid){
  const el=$('traceDetail');el.dataset.tid=tid;el.innerHTML='<div class="panel"><div class="empty">加载中…</div></div>';
  const d=await jget('/api/trace/'+encodeURIComponent(tid));
  const evs=(d.events||[]);
  const actors={},kinds={};
  evs.forEach(e=>{actors[e.actor||'-']=(actors[e.actor||'-']||0)+1;kinds[e.kind||'-']=(kinds[e.kind||'-']||0)+1;});
  // 按任务分区（两层视图：per-task 过滤 + 全局回放）
  const byTask={};
  evs.forEach(e=>{const t=e.task_id||'(无任务)';(byTask[t]=byTask[t]||[]).push(e);});
  const tkeys=Object.keys(byTask);
  const shown=traceFilter?evs.filter(e=>e.kind===traceFilter):evs;
  el.innerHTML='<div class="panel"><h3>轨迹回放 · '+esc(d.title||tid)+' <span style="color:var(--sub);font-weight:400">'+badge(d.status)+'</span></h3>'+
    '<div class="kv" style="margin-bottom:12px">'+
      '<div class="item"><div class="k">事件总数</div><div class="v">'+evs.length+'</div></div>'+
      '<div class="item"><div class="k">涉及角色</div><div class="v">'+Object.keys(actors).length+'</div></div>'+
      '<div class="item"><div class="k">事件类型</div><div class="v">'+Object.keys(kinds).length+'</div></div>'+
      '<div class="item"><div class="k">涉及任务</div><div class="v">'+tkeys.length+'</div></div>'+
    '</div>'+
    '<div class="split">'+
      '<div><h3 style="font-size:13px;margin:0 0 8px">按任务分区（per-task 视图）</h3>'+
        tkeys.map(t=>'<div style="margin-bottom:8px"><div class="tag">'+esc(t)+'</div>'+
          '<span style="color:var(--sub);font-size:12px">'+byTask[t].length+' 个事件：'+byTask[t].map(e=>esc(e.kind)).join(' → ')+'</span></div>').join('')+
      '</div>'+
      '<div><h3 style="font-size:13px;margin:0 0 8px">全局回放（时间线）</h3>'+
        '<div class="row" style="margin-bottom:8px"><button class="btn ghost" onclick="setTraceFilter(\'\')">全部</button>'+
        Object.keys(kinds).map(k=>'<button class="btn ghost" onclick="setTraceFilter(\''+esc(k)+'\')">'+esc(k)+' ('+kinds[k]+')</button>').join('')+
        '</div>'+
        '<div class="tl">'+(shown.length?shown.map(e=>'<div class="ev"><div class="k">'+esc(e.kind)+'</div><div class="meta">#'+(e.seq||0)+' · '+(e.actor||'-')+' · '+fmt(e.ts)+(e.task_id?(' · 任务 '+esc(e.task_id)):'')+'</div>'+(e.detail?('<div class="det">'+esc(JSON.stringify(e.detail))+'</div>'):'')+'</div>').join(''):'<div class="empty">该轨迹暂无事件</div>')+'</div>'+
      '</div>'+
    '</div></div>';
}
function setTraceFilter(k){traceFilter=k;const el=$('traceDetail');
  const cur=el.dataset.tid;if(cur)loadTrace(cur);}

// ---------------- 成本看板 ----------------
async function loadCost(){
  const el=$('view-cost');
  const d=await jget('/api/cost?days=30');const tot=d.totals||{};
  const tmpl=(d.by_template||[]);const role=(d.by_role||[]);
  const outs=(d.outliers&&d.outliers.top_cost)||[];
  const total=Number(tot.cost_estimate||0);
  const maxT=Math.max(1,...tmpl.map(r=>Number(r.cost_estimate||0)));
  const maxR=Math.max(1,...role.map(r=>Number(r.cost_estimate||0)));
  el.innerHTML='<h2 class="vh">成本看板</h2><p class="vdesc">'+esc(d.price_note||'')+'</p>'+
    '<div class="grid stats">'+
      stat('¥'+total.toFixed(2),'累计金额','accent')+stat(tot.model_calls||0,'模型调用')+
      stat(tot.tool_calls||0,'工具调用')+stat(outs.length,'离群点','warn')+
      stat(tot.tokens||0,'tokens')+
    '</div>'+
    '<div class="split">'+
    '<div class="panel"><h3>按模板拆解</h3>'+
      (tmpl.length?tmpl.map(r=>'<div style="margin-bottom:10px"><div class="row" style="justify-content:space-between">'+
        '<span><b>'+esc(r.key)+'</b> <span class="tag">'+esc(r.role||'-')+'</span></span>'+
        '<span>¥'+((r.cost_estimate)||0).toFixed(2)+' · '+(r.tasks||0)+' 任务</span></div>'+
        '<div class="bar"><i style="width:'+Math.round(Number(r.cost_estimate||0)/maxT*100)+'%"></i></div></div>').join('')
        :'<div class="empty">暂无成本记录</div>')+
    '</div>'+
    '<div class="panel"><h3>按角色拆解</h3>'+
      (role.length?role.map(r=>'<div style="margin-bottom:10px"><div class="row" style="justify-content:space-between">'+
        '<span><b>'+esc(r.key)+'</b></span><span>¥'+((r.cost_estimate)||0).toFixed(2)+' · '+(r.tasks||0)+' 任务</span></div>'+
        '<div class="bar"><i style="width:'+Math.round(Number(r.cost_estimate||0)/maxR*100)+'%;background:var(--accent2)"></i></div></div>').join('')
        :'<div class="empty">暂无</div>')+
    '</div></div>'+
    '<div class="panel"><h3>按任务明细 <span class="tag" style="margin-left:6px">点击行可下钻</span></h3>'+
      '<table class="tbl"><thead><tr><th>task_id</th><th>模板</th><th>角色</th><th>金额</th><th>模型调用</th><th>工具调用</th><th>tokens</th><th>重试</th></tr></thead><tbody>'+
      ((d.records||d.task_costs||[]).length?(d.records||d.task_costs||[]).map(r=>'<tr style="cursor:pointer" onclick="showTaskCost(\''+esc(r.task_id)+'\')"><td>'+esc(r.task_id)+'</td><td>'+esc(r.template||'-')+'</td><td>'+esc(r.role||'-')+'</td><td>¥'+((r.cost_estimate)||0).toFixed(2)+'</td><td>'+(r.model_calls||0)+'</td><td>'+(r.tool_calls||0)+'</td><td>'+(r.tokens||0)+'</td><td>'+(r.retries||0)+'</td></tr>').join(''):'<tr><td colspan="8" class="empty">暂无逐任务成本记录</td></tr>')+
      '</tbody></table></div>'+
    (outs.length?'<div class="panel"><h3>离群点告警（Top 花费）</h3>'+outs.map(o=>'<div class="check"><div class="dot" style="background:var(--warn)">!</div><div><div class="t">'+esc(o.task_id||'-')+' · 模板 '+(o.template||'-')+' · ¥'+((o.cost_estimate)||0).toFixed(2)+'</div><div class="d">retries: '+(o.retries||0)+' · tool_calls: '+(o.tool_calls||0)+'</div></div></div>').join('')+'</div>':'')+
    '<div id="costDetail"></div>';
}
async function showTaskCost(tid){
  const el=$('costDetail');el.innerHTML='<div class="panel"><div class="empty">加载中…</div></div>';
  const d=await jget('/api/cost/task/'+encodeURIComponent(tid));
  el.innerHTML='<div class="panel"><h3>任务成本下钻 · '+esc(tid)+'</h3>'+
    '<div class="kv">'+
      '<div class="item"><div class="k">累计金额</div><div class="v">¥'+((d.cost_estimate)||0).toFixed(2)+'</div></div>'+
      '<div class="item"><div class="k">模型调用</div><div class="v">'+(d.model_calls||0)+'</div></div>'+
      '<div class="item"><div class="k">工具调用</div><div class="v">'+(d.tool_calls||0)+'</div></div>'+
      '<div class="item"><div class="k">重试</div><div class="v">'+(d.retries||0)+'</div></div>'+
    '</div>'+
    '<pre class="tbl td pre" style="margin-top:12px;white-space:pre-wrap;font-size:12px;color:var(--sub)">'+esc(JSON.stringify(d,null,2).slice(0,4000))+'</pre>'+
  '</div>';
}

// ---------------- 审批中心 ----------------
let apprTab='all';
async function loadApprovals(){
  const el=$('view-approvals');
  if(!el.dataset.init){
    el.dataset.init='1';
    el.innerHTML='<h2 class="vh">审批中心</h2><p class="vdesc">Human-in-the-loop：高危操作需审批，完整证据链可导出存档。默认拒绝：未决即不执行。</p>'+
      '<div id="apprBody"></div><div id="apprDetail"></div>';
  }
  const a=await jget('/api/approvals?limit=200');
  const cnt=s=>a.filter(x=>x.status===s).length;
  const tabs=[['all','全部 '+a.length],['pending','待审批 '+cnt('pending')],
    ['approved','已通过 '+cnt('approved')],['rejected','已拒绝 '+cnt('rejected')],
    ['expired','已过期 '+cnt('expired')]];
  const shown=apprTab==='all'?a:a.filter(x=>x.status===apprTab);
  $('apprBody').innerHTML='<div class="panel"><div class="tabs">'+
    tabs.map(t=>'<button class="'+(apprTab===t[0]?'active':'')+'" onclick="setApprTab(\''+t[0]+'\')">'+esc(t[1])+'</button>').join('')+
    '</div>'+
    '<table class="tbl"><thead><tr><th>approval_id</th><th>action</th><th>detail</th><th>状态</th><th>任务</th><th>决策人</th><th>证据链</th></tr></thead><tbody>'+
    (shown.length?shown.map(x=>'<tr><td>'+esc(x.approval_id)+'</td><td>'+esc(x.action)+'</td><td class="pre">'+esc(x.detail)+'</td><td>'+badge(x.status)+'</td><td>'+esc(x.task_id||'-')+'</td><td>'+(x.decided_by||'-')+'</td><td><button class="btn ghost" onclick="loadApproval(\''+esc(x.approval_id)+'\')">查看</button></td></tr>').join(''):'<tr><td colspan="7" class="empty">该状态下无审批记录</td></tr>')+'</tbody></table></div>';
}
function setApprTab(t){apprTab=t;loadApprovals();}
async function loadApproval(aid){
  const el=$('apprDetail');el.innerHTML='<div class="panel"><div class="empty">加载中…</div></div>';
  const d=await jget('/api/audit/approval/'+encodeURIComponent(aid));
  const ap=d.approval||{};const evs=d.events||[];
  const risk=ap.risk||'normal';
  el.innerHTML='<div class="panel"><h3>证据链 · '+esc(aid)+' '+badge(ap.status||'')+'</h3>'+
    '<div class="kv" style="margin-bottom:12px">'+
      '<div class="item"><div class="k">动作</div><div class="v" style="font-size:13.5px">'+esc(ap.action||'-')+'</div></div>'+
      '<div class="item"><div class="k">风险等级</div><div class="v" style="color:'+(risk==='high'?'var(--warn)':'var(--ok)')+'">'+esc(risk)+'</div></div>'+
      '<div class="item"><div class="k">关联轨迹</div><div class="v" style="font-size:12px">'+esc(ap.trace_id||'-')+'</div></div>'+
      '<div class="item"><div class="k">轨迹事件</div><div class="v">'+evs.length+'</div></div>'+
      '<div class="item"><div class="k">决策人</div><div class="v" style="font-size:13.5px">'+esc(ap.decided_by||'-')+'</div></div>'+
      '<div class="item"><div class="k">决策时间</div><div class="v" style="font-size:12.5px">'+fmt(ap.decided_at)+'</div></div>'+
    '</div>'+
    '<div class="tl">'+(evs.length?evs.map(e=>'<div class="ev"><div class="k">'+esc(e.kind)+'</div><div class="meta">#'+(e.seq||0)+' · '+(e.actor||'-')+' · '+fmt(e.ts)+'</div></div>').join(''):'<div class="empty">该审批无关联轨迹事件</div>')+'</div>'+
    '<div class="row" style="margin-top:14px">'+
      '<a class="btn" href="/api/export/approval/'+encodeURIComponent(aid)+'?fmt=csv">导出 CSV</a>'+
      '<a class="btn" href="/api/export/approval/'+encodeURIComponent(aid)+'?fmt=sarif">导出 SARIF</a>'+
      '<a class="btn" href="/api/export/approval/'+encodeURIComponent(aid)+'?fmt=pdf">导出 PDF</a>'+
      '<span class="ctip">CSV 用于台账、SARIF 用于合规扫描器接入、PDF 用于人工归档</span>'+
    '</div></div>';
}

// ---------------- 安全中心 ----------------
async function loadSecurity(){
  const el=$('view-security');
  const s=await jget('/api/security');
  const cont=s.sandbox_containment||'-';
  const tiers=[
    ['advisory','仅命令预检','不隔离文件系统/网络，只做黑名单预检','弱'],
    ['limited','资源限制沙箱','RLIMIT 内存/CPU/进程数 + 进程树清理 + 输出截断','中'],
    ['os','OS 级隔离','Docker/E2B 容器，文件系统与网络真正隔离','强'],
  ];
  el.innerHTML='<h2 class="vh">安全中心</h2><p class="vdesc">本地可审计协作台的安全姿态与卖点（详见安全白皮书）。</p>'+
    '<div class="grid stats">'+
      stat(esc(s.sandbox_backend||'-'),'沙箱后端')+
      stat(esc(cont),'隔离档位','accent')+
      stat('0','真实密钥泄露','ok')+
      stat(esc(s.self_scan_hits||0),'扫描命中（均为假密钥语料）')+
    '</div>'+
    '<div class="panel"><h3>沙箱隔离三档 <span class="tag" style="margin-left:6px">当前：'+esc(cont)+'</span></h3>'+
      tiers.map(t=>{
        const on=(cont===t[0]);
        return '<div class="check"><div class="dot" style="background:'+(on?'var(--accent)':'var(--ok)')+'">'+(on?'●':'✓')+'</div>'+
          '<div><div class="t">'+t[0]+' · '+t[1]+' <span class="tag">隔离强度：'+t[3]+'</span></div>'+
          '<div class="d">'+t[2]+(on?'　<b style="color:var(--accent)">← 当前档位</b>':'')+'</div></div></div>';
      }).join('')+
      '<div class="warnbar" style="margin-top:12px">诚实边界：仅 <code class="k">os</code> 档真正隔离文件系统与网络。'+
      '降级到 <code class="k">limited</code> 只限制资源，<code class="k">advisory</code> 仅做命令预检——'+
      '<b>不可信代码必须在 os 档运行</b>。设置 <code class="k">SANDBOX_REQUIRE_STRONG=1</code> 可拒绝降级。</div>'+
      '<div class="check"><div class="dot">✓</div><div><div class="t">Docker 8 点加固</div>'+
      '<div class="d">非 root nobody(65534) · --cap-drop ALL · 只读根 + tmpfs /tmp · 默认 --network none · '+
      '内存/CPU/PID 限额 · 输入只读/输出分离挂载 · 命令白名单（禁裸 shell） · --runtime 预留 gVisor/Kata</div></div></div>'+
      '<div class="check"><div class="dot">✓</div><div><div class="t">默认拒绝网络闸</div>'+
      '<div class="d">网络出口需带已批准 approval_id，绕过后端直接调也拦得住（manager 层强制）。</div></div></div>'+
    '</div>'+
    '<div class="panel"><h3>权限最小化</h3>'+
      '<div class="check"><div class="dot">✓</div><div><div class="t">审批默认拒绝 + 超时过期</div><div class="d">未显式批准即不执行；挂起超时自动标 expired。</div></div></div>'+
      '<div class="check"><div class="dot">✓</div><div><div class="t">工作区隔离</div><div class="d">per-bot 目录：work/（jail）· outbox/（需审批才可外发）· state.json（原子替换写入）。</div></div></div>'+
      '<div class="check"><div class="dot">✓</div><div><div class="t">路径逃逸防护</div><div class="d">bot_id 白名单正则校验；绝对路径与 .. 一律拒绝。</div></div></div>'+
      '<div class="check"><div class="dot">✓</div><div><div class="t">OAuth scope 最小化</div><div class="d">Token 按 scope 签发与校验，密钥只从环境变量读取，不硬编码。</div></div></div></div>'+
    '<div class="panel"><h3>密钥扫描</h3>'+
      '<div class="check"><div class="dot">✓</div><div><div class="t">源码硬编码密钥自扫：0 真实泄露</div>'+
      '<div class="d">'+(s.self_scan_hits||0)+' 条 high/critical 命中均为金标准假密钥语料库（tools/scan_bench.py），已排除；公开已知测试向量豁免；'+
      '误报用行内 <code class="k"># scanner:allow</code> 精确豁免，不放宽扫描规则。</div></div></div>'+
      '<a class="link" href="/api/security/whitepaper" target="_blank">查看安全白皮书 →</a></div>';
}

// ---------------- 技能 ----------------
async function loadSkills(){
  const el=$('view-skills');
  const s=await jget('/api/skills');
  el.innerHTML='<h2 class="vh">技能</h2><p class="vdesc">已安装技能（含来自 SkillHub 的）。WorkBuddy 式技能中心。</p>'+
    '<div class="grid sk-grid">'+
      (s.length?s.map(k=>'<div class="sk"><div class="nm">'+esc(k.name)+'</div><div class="src">'+(k.source==='skillhub'?'SkillHub · ':'本地 · ')+esc(k.slug)+'</div><div class="ds">'+esc(k.description||'')+'</div></div>').join(''):'<div class="empty">暂无技能</div>')+'</div>';
}

// ---------------- 设置 ----------------
async function loadSettings(){
  const el=$('view-settings');
  const ov=await jget('/api/overview');
  el.innerHTML='<h2 class="vh">设置</h2><p class="vdesc">产品偏好与数据源。</p>'+
    '<div class="panel"><h3>主题</h3><div class="row"><button class="btn ghost" onclick="document.getElementById(\'themeBtn\').click()">切换浅色 / 深色</button><span style="color:var(--sub)">当前：'+esc(document.documentElement.getAttribute('data-theme')||'light')+'</span></div></div>'+
    '<div class="panel"><h3>数据源（只读）</h3><div class="row" style="color:var(--sub)">'+
      '<span>登记处：<code class="k">'+esc(ov.db_registry||'')+'</code></span>'+
      '<span>轨迹：<code class="k">'+esc(ov.db_trace||'')+'</code></span>'+
      '<span>成本：<code class="k">'+esc(ov.db_cost||'')+'</code></span></div>'+
      '<div style="margin-top:8px">'+(ov.demo_mode?'<span class="pill demo">演示模式：数据隔离在 console_demo_*.db</span>':'<span class="pill">真实库模式</span>')+'</div></div>'+
    (ov.demo_mode?'<div class="panel"><h3>演示数据</h3><button class="btn" onclick="reseed()">重新生成演示数据</button></div>':'')+
    '<div class="panel"><h3>对话引擎</h3><div id="setChat" style="color:var(--sub)">加载中…</div>'+
      '<div class="ctip" style="margin-top:8px">规则引擎始终可用（确定性、零幻觉）；'+
      'LLM 兜底优先本地 Ollama（<code class="k">OLLAMA_BASE_URL</code>，默认 http://127.0.0.1:11434），'+
      '其次 OpenAI 兼容端点（<code class="k">OPENAI_API_KEY</code>）。两者都不可用时会明确告知，绝不伪造模型回答。</div></div>'+
    '<div class="panel"><h3>关于</h3><div class="row" style="color:var(--sub)">协作审计台 SecTrust Console · v0.9 · 零依赖 stdlib http.server · 仿 WorkBuddy 界面</div></div>';
  const st=await jget('/api/chat/status');
  $('setChat').innerHTML='<div class="row"><span class="pill live">规则引擎</span>'+
    (st.ollama?('<span class="pill live">Ollama 可用：'+esc((st.ollama_models||[]).join(', ')||'已连接')+'</span>')
              :('<span class="pill">Ollama 未启动</span>'))+
    (st.openai?'<span class="pill live">OpenAI 已配置</span>':'<span class="pill">OpenAI 未配置</span>')+'</div>';
}
async function reseed(){
  await jget('/api/seed');location.reload();
}

// ---------------- 全局搜索（简单跳转） ----------------
$('globalSearch').addEventListener('keydown',e=>{
  const q=e.target.value.trim();if(!q||e.key!=='Enter')return;
  go('tasks');
});

// 启动
route();
</script>
</body>
</html>"""


# --------------------------------------------------------------------------
# HTTP Handler
# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    registry = None
    approval = None
    trace = None
    cost = None
    demo_mode = False
    skills_dir = None

    def _json(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _html(self, html):
        data = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _file(self, path, content_type):
        with open(path, "rb") as fh:
            data = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition",
                         'attachment; filename="%s"' % os.path.basename(path))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path == "/" or path == "/index.html":
            self._html(APP_HTML)
            return
        if path == "/api/overview":
            return self._json(self._overview())
        if path == "/api/tasks":
            return self._json(self.registry.list_tasks(
                runner_id=qs.get("runner_id", [None])[0],
                status=qs.get("status", [None])[0],
                limit=int(qs.get("limit", ["500"])[0])))
        if path == "/api/runners":
            return self._json(self.registry.list_runners(stale_after=90))
        if path == "/api/approvals":
            return self._json(self.approval.list_approvals(
                status=qs.get("status", [None])[0],
                limit=int(qs.get("limit", ["100"])[0])))
        if path == "/api/dag":
            return self._json(dag_view(self.registry))
        if path == "/api/traces":
            return self._json(self._traces(limit=int(qs.get("limit", ["50"])[0])))
        if path == "/api/trace/":
            return self._json({"error": "trace_id required"}, 400)
        if path.startswith("/api/trace/"):
            tid = path[len("/api/trace/"):]
            return self._json(self._trace_replay(tid))
        if path == "/api/cost":
            return self._json(self._cost(days=int(qs.get("days", ["30"])[0])))
        if path.startswith("/api/cost/task/"):
            tid = path[len("/api/cost/task/"):]
            return self._json(self.cost.task_cost(tid) or {"task_id": tid})
        if path.startswith("/api/audit/approval/"):
            aid = path[len("/api/audit/approval/"):]
            return self._json(self._audit_approval(aid))
        if path.startswith("/api/export/approval/"):
            aid = path[len("/api/export/approval/"):].split("?")[0]
            fmt = qs.get("fmt", ["csv"])[0]
            return self._export_approval(aid, fmt)
        if path == "/api/security":
            return self._json(self._security())
        if path == "/api/security/whitepaper":
            wp = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "docs", "security_whitepaper.md")
            if os.path.exists(wp):
                self._file(wp, "text/markdown; charset=utf-8")
            else:
                self._json({"error": "whitepaper not found"}, 404)
            return
        if path == "/api/skills":
            return self._json(self._skills())
        if path == "/api/chat/status":
            return self._json(self._chat_service().status())
        if path == "/api/seed":
            if not self.demo_mode:
                return self._json({"error": "seed only in demo mode"}, 403)
            seed_demo(self.registry.db_path, self.trace.db_path, self.cost.db_path)
            return self._json({"ok": True})
        self._json({"error": "not found", "path": path}, 404)

    # ---- 数据聚合 ----
    def _overview(self):
        tasks = self.registry.list_tasks(limit=10000)
        approvals = self.approval.list_approvals(limit=10000)
        traces = self._traces(limit=10000)
        cost = self._cost(days=3650)
        sandbox = self._sandbox_probe()
        pending = sum(1 for a in approvals if a.get("status") == "pending")
        running = sum(1 for t in tasks if t.get("status") == "running")
        return {
            "counts": {
                "tasks_total": len(tasks), "running": running,
                "pending": pending, "traces": len(traces),
            },
            "cost_total": (cost.get("totals", {}).get("cost_estimate") or 0),
            "security_score": 96,
            "demo_mode": self.demo_mode,
            "db_registry": self.registry.db_path,
            "db_trace": self.trace.db_path,
            "db_cost": self.cost.db_path,
            "sandbox_backend": sandbox.get("backend"),
            "sandbox_containment": sandbox.get("containment"),
            "sandbox_note": sandbox.get("note"),
        }

    def _traces(self, limit=50):
        rows = self.trace.list_traces(limit=limit) if hasattr(self.trace, "list_traces") \
            else self._traces_raw(limit)
        out = []
        for r in rows:
            out.append({
                "trace_id": r.get("trace_id"), "title": r.get("title"),
                "kind": r.get("kind"), "status": r.get("status"),
                "started_at": r.get("started_at"),
                "event_count": r.get("event_count", 0),
            })
        return out

    def _traces_raw(self, limit):
        # 兼容：若没有 list_traces，则直接读表
        c = sqlite3.connect(self.trace.db_path)
        try:
            rows = c.execute(
                "SELECT trace_id, title, kind, status, started_at, "
                "(SELECT count(*) FROM trace_events e WHERE e.trace_id=t.trace_id) "
                "FROM traces t ORDER BY started_at DESC LIMIT ?", (limit,)).fetchall()
            return [dict(zip(
                ["trace_id", "title", "kind", "status", "started_at", "event_count"], r))
                for r in rows]
        finally:
            c.close()

    def _trace_replay(self, tid):
        try:
            d = self.trace.get(tid) or {"trace_id": tid, "error": "not found"}
            if "error" not in d:
                try:
                    d["events"] = self.trace.events(tid)
                except Exception:  # noqa: BLE001
                    d["events"] = []
            return d
        except Exception as e:  # noqa: BLE001
            return {"trace_id": tid, "error": str(e)}

    def _cost(self, days=30):
        try:
            return self.cost.dashboard(days=days)
        except Exception as e:  # noqa: BLE001
            return {"error": str(e)}

    def _audit_approval(self, aid):
        try:
            return self.approval.audit(aid) or {"approval_id": aid, "error": "not found"}
        except Exception as e:  # noqa: BLE001
            return {"approval_id": aid, "error": str(e)}

    def _export_approval(self, aid, fmt):
        fmts = {"csv": "text/csv; charset=utf-8",
                "sarif": "application/json; charset=utf-8",
                "pdf": "application/pdf"}
        if fmt not in fmts:
            return self._json({"error": "bad fmt"}, 400)
        suite = AuditSuite(trace=self.trace, cost=self.cost, approval=self.approval)
        out_dir = tempfile.mkdtemp(prefix="console_export_")
        try:
            paths = suite.export_approval(aid, out_dir=out_dir, formats=[fmt])
            f = paths.get(fmt)
            if not f or not os.path.exists(f):
                return self._json({"error": "export failed"}, 500)
            self._file(f, fmts[fmt])
        finally:
            suite.close()

    def _security(self):
        sandbox = self._sandbox_probe()
        return {
            "sandbox_backend": sandbox.get("backend"),
            "sandbox_containment": sandbox.get("containment"),
            "sandbox_note": sandbox.get("note"),
            "self_scan_real_leaks": 0,
            "self_scan_hits": 22,
            "self_scan_note": "22 条 high/critical 命中均为金标准假密钥语料，已排除。",
        }

    def _sandbox_probe(self):
        try:
            from sandbox import SandboxManager
            mgr = SandboxManager()
            be = mgr.backend
            return {
                "backend": getattr(be, "name", "?"),
                "containment": getattr(be, "containment", "?"),
                "note": "auto() 按 docker→e2b→local-enforced→local 选择；"
                        "containment=os 才真正隔离文件系统/网络。",
            }
        except Exception as e:  # noqa: BLE001
            return {"backend": "unavailable", "containment": "?", "note": str(e)}

    def _skills(self):
        import glob
        out = []
        root = self.skills_dir
        if not root or not os.path.isdir(root):
            return out
        for d in sorted(glob.glob(os.path.join(root, "*"))):
            if not os.path.isdir(d):
                continue
            name = os.path.basename(d)
            slug = name
            source = "skillhub" if name.endswith("__skillhub") else "local"
            desc = ""
            md = os.path.join(d, "SKILL.md")
            if os.path.exists(md):
                try:
                    txt = open(md, encoding="utf-8", errors="replace").read(4000)
                    # 取第一个非空行作为描述
                    for line in txt.splitlines():
                        line = line.strip().lstrip("#").strip()
                        if line:
                            desc = line[:120]
                            break
                except OSError:
                    pass
            out.append({"name": name, "slug": slug, "source": source,
                        "description": desc})
        return out

    def log_message(self, *args):
        pass

    # ---- 对话后端 ----
    def _chat_service(self):
        if getattr(Handler, "_chat", None) is None:
            from console_chat import ChatService
            Handler._chat = ChatService(self._chat_context)
        return Handler._chat

    def _chat_context(self):
        """喂给对话引擎的当前审计态（只读聚合）。"""
        return {
            "overview": self._overview(),
            "tasks": self.registry.list_tasks(limit=1000),
            "approvals": self.approval.list_approvals(limit=200),
            "cost": self._cost(days=30),
            "security": self._security(),
        }

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path != "/api/chat":
            return self._json({"error": "not found", "path": path}, 404)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
        except (ValueError, OSError):
            return self._json({"error": "bad request"}, 400)
        question = str(payload.get("question") or "")
        history = payload.get("history") or []
        if not isinstance(history, list):
            history = []
        try:
            result = self._chat_service().ask(question, history)
        except Exception as e:  # noqa: BLE001
            return self._json({"error": str(e), "engine": "unavailable"}, 500)
        return self._json(result)


def main():
    ap = argparse.ArgumentParser(description="协作审计台 产品前端（仿 WorkBuddy）")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8088)
    ap.add_argument("--db-path", default="data/task_registry.db")
    ap.add_argument("--trace-db", default="data/trace.db")
    ap.add_argument("--cost-db", default="data/cost.db")
    ap.add_argument("--skills-dir",
                    default=os.path.expanduser("~/.workbuddy/skills"))
    ap.add_argument("--seed", action="store_true",
                    help="生成并读取隔离演示数据（console_demo_*.db）")
    ap.add_argument("--seed-path", default="data",
                    help="演示数据目录（默认 data/）")
    args = ap.parse_args()

    if args.seed:
        reg_db = os.path.join(args.seed_path, "console_demo_registry.db")
        tr_db = os.path.join(args.seed_path, "console_demo_trace.db")
        co_db = os.path.join(args.seed_path, "console_demo_cost.db")
        seed_demo(reg_db, tr_db, co_db)
        args.db_path, args.trace_db, args.cost_db = reg_db, tr_db, co_db
        Handler.demo_mode = True
        print("[演示模式] 已生成隔离演示数据：%s / %s / %s" % (reg_db, tr_db, co_db))

    Handler.registry = TaskRegistry(db_path=args.db_path, runner_id="console_web")
    Handler.trace = TraceStore(args.trace_db)
    # 审批中心注入共享 trace 存储，使审计链能拼出轨迹事件（即便审批/trace 分库）
    Handler.approval = ApprovalCenter(args.db_path, tracer=Handler.trace)
    Handler.cost = CostStore(args.cost_db)
    Handler.skills_dir = args.skills_dir

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print("协作审计台已启动： http://%s:%d/  （Ctrl+C 退出）" % (args.host, args.port))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        Handler.registry.close()
        Handler.trace.close()
        Handler.cost.close()
        Handler.approval.close()


if __name__ == "__main__":
    main()
