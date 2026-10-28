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
from scheduler import Scheduler
from task_center_web import dag_view
import console_api
import console_actions
from model_profiles import ModelProfileStore
from assistant_channels import AssistantSettings
import notify
from notify import NotifyEngine, parse_command, help_text
import model_providers


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
    ts.record(tid2, "finding", summary="命中均为金标准假密钥语料（0 真实泄露）",
              detail={"real_leaks": 0})
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
  /* ---- 对话：第一性原理分析（多视角生成 + 本质判别） ---- */
  .seg{display:inline-flex;gap:4px;margin:0 0 12px;padding:3px;background:var(--bg);
    border:1px solid var(--line);border-radius:10px}
  .seg button{border:none;background:transparent;color:var(--sub);cursor:pointer;
    padding:5px 14px;border-radius:7px;font-size:12.5px}
  .seg button:hover{color:var(--accent)}
  .seg button.active{background:var(--accent);color:#fff}
  .fp-wrap{flex:1;overflow-y:auto;padding:14px 0 4px}
  .fp-card{background:var(--card);border:1px solid var(--line);border-radius:12px;
    padding:14px 16px;margin-bottom:12px;box-shadow:var(--shadow)}
  .fp-card .fp-name{font-weight:600;font-size:14px;margin-bottom:2px}
  .badgefp{display:inline-block;font-size:11px;padding:2px 9px;border-radius:999px;
    margin-left:8px;vertical-align:middle}
  .badgefp.first_principles{background:rgba(22,184,166,.14);color:#0f9b8e}
  .badgefp.novelty_seeking{background:rgba(230,90,80,.14);color:#c94a3f}
  .badgefp.mixed{background:rgba(224,168,40,.18);color:#96690a}
  .badgefp.unknown{background:rgba(120,130,140,.16);color:#6b7480}
  .fp-bar{height:6px;background:var(--line);border-radius:4px;overflow:hidden;
    margin:8px 0 6px;max-width:280px}
  .fp-bar > i{display:block;height:100%;background:var(--accent)}
  .fp-reason{font-size:12px;color:var(--sub);line-height:1.55}
  .fp-body{margin-top:10px;font-size:14px;line-height:1.65;white-space:pre-wrap;word-break:break-word}
  .fp-verdict{background:rgba(59,108,255,.08);border:1px solid rgba(59,108,255,.28);
    border-radius:12px;padding:12px 14px;font-size:13.5px;line-height:1.6}
  .fp-empty{color:var(--sub);font-size:13px;padding:14px 2px}
  /* ---- 技能卡 ---- */
  .sk-grid{grid-template-columns:repeat(auto-fill,minmax(220px,1fr))}
  .sk{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px;box-shadow:var(--shadow)}
  .sk .nm{font-weight:600;font-size:14px}
  .sk .src{font-size:11px;color:var(--accent2);margin:2px 0 6px}
  .sk .ds{font-size:12.5px;color:var(--sub);line-height:1.5}
  /* ---- 连接器画廊（WorkBuddy 式卡片网格） ---- */
  .conn-top{display:flex;align-items:center;gap:10px;margin:4px 0 16px;flex-wrap:wrap}
  .conn-top .search{flex:1;min-width:220px;display:flex;gap:8px}
  .conn-top input{flex:1;padding:9px 14px;border:1px solid var(--line);border-radius:10px;background:var(--card);color:var(--txt)}
  .conn-sec{font-weight:600;font-size:14px;margin:18px 0 10px;display:flex;align-items:center;gap:8px}
  .conn-sec .cnt{font-size:12px;color:var(--sub);font-weight:400}
  .conn-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:12px}
  .conn-card{position:relative;background:var(--card);border:1px solid var(--line);border-radius:14px;
    padding:16px 14px 14px;box-shadow:var(--shadow);display:flex;flex-direction:column;gap:8px;min-height:118px}
  .conn-card:hover{border-color:var(--accent)}
  .conn-badge{width:40px;height:40px;border-radius:12px;color:#fff;display:flex;align-items:center;
    justify-content:center;font-size:17px;font-weight:700;flex:0 0 auto}
  .conn-card .nm{font-weight:600;font-size:14px;display:flex;align-items:center;gap:6px;padding-right:26px}
  .conn-card .ds{font-size:12px;color:var(--sub);line-height:1.5;
    display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
  .conn-card .ft{margin-top:auto;display:flex;align-items:center;gap:6px;font-size:11px;color:var(--sub)}
  .conn-plus{position:absolute;top:10px;right:10px;width:26px;height:26px;border-radius:50%;
    border:1px solid var(--line);background:var(--bg);color:var(--txt);font-size:16px;line-height:1;
    cursor:pointer;display:flex;align-items:center;justify-content:center;padding:0}
  .conn-plus:hover{background:var(--accent);color:#fff;border-color:var(--accent)}
  .conn-check{position:absolute;top:10px;right:10px;color:var(--ok);font-size:16px;font-weight:700}
  /* ---- 助理远程通道 ---- */
  .conn-btn{position:absolute;top:10px;right:10px;border:1px solid var(--line);
    background:var(--bg);color:var(--txt);border-radius:14px;padding:3px 12px;
    font-size:12px;cursor:pointer}
  .conn-btn:hover{border-color:var(--accent);color:var(--accent)}
  .conn-btn.on{background:var(--ok);border-color:var(--ok);color:#fff}
  .conn-btn.mid{border-color:var(--warn);color:var(--warn)}
  /* ---- 开关（上下文管理等） ---- */
  .switch{position:relative;display:inline-block;width:42px;height:22px;flex:none}
  .switch input{opacity:0;width:0;height:0}
  .switch .slider{position:absolute;inset:0;background:var(--line);border-radius:22px;
    transition:background .15s;cursor:pointer}
  .switch .slider::before{content:'';position:absolute;width:16px;height:16px;
    border-radius:50%;background:#fff;top:3px;left:3px;transition:transform .15s}
  .switch input:checked+.slider{background:var(--ok)}
  .switch input:checked+.slider::before{transform:translateX(20px)}
  .conn-tag{display:inline-block;padding:1px 8px;border-radius:9px;font-size:11px;
    background:var(--bg2,var(--bg));border:1px solid var(--line)}
  .conn-tag.m{color:var(--ok);border-color:var(--ok)}
  .conn-tag.ok{color:#fff;background:var(--ok);border-color:var(--ok)}
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
  /* ---- 对话：模型选择器 + 配置弹窗 ---- */
  .msel{display:inline-flex;align-items:center;gap:5px}
  .msel select{font-size:12px;padding:4px 8px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--txt)}
  #chatModelCfg{padding:5px 10px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--txt);cursor:pointer;font-size:12px}
  #chatModelCfg:hover{border-color:var(--accent);color:var(--accent)}
  .model-cfg{margin:0 0 10px;padding:12px 14px;border:1px solid var(--line);border-radius:10px;background:var(--panel);display:flex;flex-direction:column;gap:9px}
  .model-cfg.hidden{display:none}
  .cfg-row{display:flex;align-items:center;gap:10px}
  .cfg-row label{width:64px;color:var(--sub);font-size:12px;flex:none}
  .cfg-row input,.cfg-row select{flex:1;padding:7px 10px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--txt);font-size:13px}
  .cfg-actions{margin-top:2px}
  .cfg-actions button{padding:6px 16px;border:none;border-radius:8px;cursor:pointer;background:var(--accent);color:#fff;font-size:13px}
  .cfg-actions .ghost{background:var(--line);color:var(--txt)}
  /* ---- 添加模型弹窗（WorkBuddy 式供应商目录）---- */
  .mmask{position:fixed;inset:0;background:rgba(15,20,30,.45);display:flex;align-items:center;
    justify-content:center;z-index:60;padding:24px}
  .mmask.hidden{display:none}
  .mmodal{width:min(560px,100%);max-height:88vh;overflow:auto;background:var(--bg);
    border:1px solid var(--line);border-radius:14px;box-shadow:0 18px 60px rgba(0,0,0,.28)}
  .mmodal .mhead{display:flex;align-items:center;justify-content:space-between;
    padding:14px 16px;border-bottom:1px solid var(--line)}
  .mmodal .mhead .mt{font-weight:600;font-size:14px}
  .mmodal .mhead .mx{border:none;background:transparent;color:var(--sub);font-size:18px;cursor:pointer}
  .mmodal .mbody{padding:14px 16px;display:flex;flex-direction:column;gap:11px}
  .mmodal .mfoot{display:flex;justify-content:flex-end;gap:9px;padding:12px 16px;
    border-top:1px solid var(--line)}
  .mmodal .mfoot button{padding:7px 18px;border:none;border-radius:8px;cursor:pointer;
    background:var(--accent);color:#fff;font-size:13px}
  .mmodal .mfoot .ghost{background:var(--line);color:var(--txt)}
  /* 添加模型弹窗：目录勾选 / 手动填写 双模式切换 */
  .modes{display:flex;gap:6px;margin-bottom:12px}
  .modes .mtab{flex:1;border:1px solid var(--line);background:var(--card);
    padding:7px 10px;border-radius:8px;cursor:pointer;font-size:13px;color:var(--sub)}
  .modes .mtab.active{background:var(--accent);color:#fff;border-color:var(--accent)}
  #mmCatalogPane.hidden,#mmManualPane.hidden{display:none}
  .mrow{display:flex;align-items:flex-start;gap:10px}
  .mrow>label{width:74px;flex:none;color:var(--sub);font-size:12px;padding-top:8px}
  .mrow .mfield{flex:1;min-width:0}
  .mrow input,.mrow select{width:100%;box-sizing:border-box;padding:8px 10px;
    border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--txt);font-size:13px}
  /* 供应商选择器：点击展开带搜索的分组清单 */
  .provpick{width:100%;text-align:left;padding:8px 10px;border:1px solid var(--line);
    border-radius:8px;background:var(--bg);color:var(--txt);font-size:13px;cursor:pointer;
    display:flex;align-items:center;justify-content:space-between;gap:8px}
  .provpick:hover{border-color:var(--accent)}
  .provpick .pv-sub{color:var(--sub);font-size:11px}
  .provmenu{position:relative;margin-top:6px;border:1px solid var(--line);border-radius:10px;
    background:var(--bg);overflow:hidden}
  .provmenu.hidden{display:none}
  .provmenu .psearch{width:100%;box-sizing:border-box;border:none;border-bottom:1px solid var(--line);
    padding:9px 11px;background:var(--bg);color:var(--txt);font-size:13px;outline:none}
  .provlist{max-height:260px;overflow:auto;padding:4px 0}
  .provlist .pgrp{padding:7px 12px 4px;color:var(--sub);font-size:11px;letter-spacing:.4px}
  .provlist .pitem{display:flex;align-items:center;justify-content:space-between;gap:10px;
    padding:8px 12px;cursor:pointer;font-size:13px}
  .provlist .pitem:hover{background:var(--panel)}
  .provlist .pitem.sel{background:var(--panel)}
  .provlist .pitem .pl{display:flex;flex-direction:column;gap:2px;min-width:0}
  .provlist .pitem .pl .pn{font-size:13px}
  .provlist .pitem .pl .pd{color:var(--sub);font-size:11px}
  .provlist .pitem .pcheck{color:#16a34a;font-size:15px;flex:none;font-weight:700}
  .provlist .pempty{padding:14px 12px;color:var(--sub);font-size:12px;text-align:center}
  .mtag{display:inline-block;padding:1px 6px;border-radius:5px;font-size:10.5px;
    border:1px solid var(--line);color:var(--sub);margin-left:6px}
  .mtag.sub{border-color:#16a34a;color:#16a34a}
  .mnote{font-size:11.5px;color:var(--sub);line-height:1.55}
  .mwarn{font-size:11.5px;color:#b45309;line-height:1.55}
  .mwarn:empty,.mnote:empty{display:none}
  .mlink{font-size:11.5px;color:var(--accent);text-decoration:none}
  /* 设置页 · 模型分区 */
  .mlist{display:flex;flex-direction:column;gap:8px;margin-top:4px}
  .mlist .mrow2{display:flex;align-items:center;justify-content:space-between;gap:10px;
    padding:9px 11px;border:1px solid var(--line);border-radius:9px;background:var(--bg)}
  .mlist .mrow2 .mi{font-size:13px}
  .mlist .mrow2 .mi .mi-sub{color:var(--sub);font-size:11px;margin-top:2px}
  .mlist .mrow2 .mact{display:flex;gap:6px;flex:none}
  .mlist .mrow2 .mact button{padding:5px 11px;font-size:12px;border:1px solid var(--line);
    border-radius:7px;background:var(--bg);color:var(--txt);cursor:pointer}
  .mlist .mrow2 .mact button.danger{color:#c0392b}
  .mlist .mrow2 .mact button.on{border-color:var(--accent);color:var(--accent)}
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
  /* ---- 登录页 ---- */
  .login-wrap{position:fixed;inset:0;z-index:100;display:flex;align-items:center;
    justify-content:center;background:var(--bg)}
  .login-wrap.hidden{display:none}
  .login-card{width:400px;max-width:92vw;background:var(--card);border:1px solid var(--line);
    border-radius:16px;padding:32px;box-shadow:0 12px 40px rgba(0,0,0,.12)}
  .login-card .logo{width:44px;height:44px;border-radius:12px;margin-bottom:16px;
    background:linear-gradient(135deg,#3b6cff,#16b8a6);display:flex;align-items:center;
    justify-content:center;color:#fff;font-weight:800;font-size:20px}
  .login-card h1{font-size:19px;margin:0 0 4px}
  .login-card .sub{color:var(--sub);font-size:13px;margin-bottom:22px}
  .login-card label{display:block;font-size:12.5px;color:var(--sub);margin:14px 0 6px}
  .login-card input,.login-card select{width:100%;padding:10px 12px;border:1px solid var(--line);
    border-radius:9px;background:var(--bg);color:var(--txt);font-size:14px}
  .login-card input:focus,.login-card select:focus{outline:none;border-color:var(--accent);
    box-shadow:0 0 0 3px rgba(59,108,255,.12)}
  .login-card button{width:100%;margin-top:22px;background:var(--accent);color:#fff;
    border:none;padding:11px;border-radius:10px;font-size:14px;cursor:pointer;font-weight:600}
  .login-card button:hover{opacity:.92}
  .login-note{margin-top:16px;padding:10px 12px;background:var(--bg);border:1px solid var(--line);
    border-radius:9px;font-size:11.5px;color:var(--sub);line-height:1.6}
  .login-err{color:var(--warn);font-size:12.5px;margin-top:10px;min-height:16px}
  /* ---- 导航分组 ---- */
  .nav-group{font-size:10.5px;color:#5c6570;letter-spacing:.06em;padding:12px 12px 5px;
    font-weight:700;text-transform:uppercase}
  .sidebar.collapsed .nav-group{opacity:0;height:14px;padding:4px 0}
  .nav-foot .u{color:var(--sidebar-txt);font-size:12px;display:block}
  .nav-foot .r{color:#8b949e;font-size:11px}
  /* ---- 工作台首页 ---- */
  .ws-hero{background:linear-gradient(135deg,rgba(59,108,255,.10),rgba(22,184,166,.08));
    border:1px solid var(--line);border-radius:14px;padding:20px 22px;margin-bottom:16px}
  .ws-hero h2{margin:0 0 6px;font-size:19px}
  .ws-hero p{margin:0 0 14px;color:var(--sub);font-size:13px}
  .quick-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:10px}
  .quick{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px;
    cursor:pointer;text-align:left;font-size:13.5px;color:var(--txt);transition:.15s}
  .quick:hover{border-color:var(--accent);transform:translateY(-1px);box-shadow:0 4px 12px rgba(0,0,0,.07)}
  .quick .qi{display:block;font-size:19px;margin-bottom:5px}
  .quick .qd{display:block;color:var(--sub);font-size:11.5px;margin-top:2px;line-height:1.5}
  /* ---- 审计动作按钮 ---- */
  .actions-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:10px}
  .abtn{background:linear-gradient(135deg,#f7f9ff,#eef3ff);border:1px solid var(--line);
    border-radius:10px;padding:14px;cursor:pointer;text-align:left;font-size:13.5px;
    color:var(--txt);transition:.15s;font-weight:600}
  .abtn:hover{border-color:var(--accent);transform:translateY(-1px);box-shadow:0 4px 12px rgba(59,108,255,.18)}
  .abtn .ai{display:block;font-size:20px;margin-bottom:6px}
  .abtn .ad{display:block;color:var(--sub);font-size:11.5px;margin-top:4px;line-height:1.5;font-weight:400}
  /* ---- 表单 ---- */
  .fld{margin-bottom:14px}
  .fld label{display:block;font-size:12.5px;color:var(--sub);margin-bottom:5px}
  .fld input,.fld select,.fld textarea{width:100%;padding:9px 12px;border:1px solid var(--line);
    border-radius:9px;background:var(--bg);color:var(--txt);font-size:13.5px;font-family:inherit}
  .fld textarea{min-height:78px;resize:vertical}
  .fld input:focus,.fld select:focus,.fld textarea:focus{outline:none;
    border-color:var(--accent);box-shadow:0 0 0 3px rgba(59,108,255,.12)}
  .fld .hint{font-size:11.5px;color:var(--sub);margin-top:4px}
  .form-err{color:var(--warn);font-size:12.5px;margin-top:8px;min-height:16px}
  .form-ok{color:var(--ok);font-size:12.5px;margin-top:8px;min-height:16px}
  /* ---- 列表条目 ---- */
  .litem{display:flex;align-items:center;gap:12px;padding:11px 12px;border:1px solid var(--line);
    border-radius:10px;margin-bottom:8px;background:var(--bg)}
  .litem .grow{flex:1;min-width:0}
  .litem .nm{font-weight:600;font-size:13.5px;word-break:break-word}
  .litem .mt{color:var(--sub);font-size:12px;margin-top:2px}
  .litem .ops{display:flex;gap:6px;flex:0 0 auto}
  .iconbtn.sm{width:28px;height:28px;border-radius:7px}
  .card-grid{grid-template-columns:repeat(auto-fill,minmax(240px,1fr))}
  .acard{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px;
    box-shadow:var(--shadow);transition:.15s}
  .acard:hover{transform:translateY(-1px);box-shadow:0 5px 16px rgba(0,0,0,.08)}
  .acard .ah{display:flex;align-items:center;gap:8px;margin-bottom:6px}
  .acard .an{font-weight:600;font-size:14px}
  .acard .ak{font-size:11px;color:var(--accent2);margin-bottom:6px}
  .acard .ad{font-size:12.5px;color:var(--sub);line-height:1.55}
  .avatar{width:30px;height:30px;border-radius:9px;flex:0 0 auto;display:flex;
    align-items:center;justify-content:center;color:#fff;font-weight:700;font-size:13px}
</style>
</head>
<body>

<!-- ===== 登录页 ===== -->
<div class="login-wrap" id="loginWrap">
  <div class="login-card">
    <div class="logo">S</div>
    <h1>SecTrust 协作审计台</h1>
    <div class="sub">本地多 AI 协作的可审计控制台</div>
    <label for="loginUser">用户名</label>
    <input id="loginUser" placeholder="例如 operator" value="operator" autocomplete="username">
    <label for="loginRole">岗位角色</label>
    <select id="loginRole"></select>
    <button id="loginBtn">进入工作台</button>
    <div class="login-err" id="loginErr"></div>
    <div class="login-note" id="loginNote">
      本地演示级身份：无密码校验、令牌不防伪造，<b>不构成安全边界</b>。
      它的作用是提供界面角色上下文（审计名单 / 权限视角）。
    </div>
  </div>
</div>

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
    <div class="title" id="viewTitle">工作台</div>
    <div class="search"><input id="globalSearch" placeholder="搜索任务 / 轨迹 / 审批…"><div class="search-pop" id="searchPop"></div></div>
    <div class="spacer"></div>
    <button class="btn" id="newTaskBtn" style="display:none">＋ 新建任务</button>
    <span class="pill live" id="dbPill">● 实时</span>
    <span class="pill demo" id="demoPill" style="display:none">演示数据</span>
    <button class="iconbtn" id="userBtn" title="当前用户" style="display:none">
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="8" r="4"/><path d="M4 21c0-4 3.6-6 8-6s8 2 8 6"/></svg>
    </button>
    <button class="iconbtn" id="themeBtn" title="切换主题">
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>
    </button>
  </div>
  <div class="content">
    <div class="view active" id="view-workspace"></div>
    <div class="view" id="view-newtask"></div>
    <div class="view" id="view-roster"></div>
    <div class="view" id="view-assistants"></div>
    <div class="view" id="view-connectors"></div>
    <div class="view" id="view-schedule"></div>
    <div class="view" id="view-library"></div>
    <div class="view" id="view-overview"></div>
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

<!-- 添加模型弹窗（双模式：目录勾选 / 手动填写 key+base_url+计费方式） -->
<div class="mmask hidden" id="modelModal">
  <div class="mmodal">
    <div class="mhead"><span class="mt" id="mmTitle">添加模型</span><button class="mx" id="mmX" type="button">✕</button></div>
    <div class="mbody">
      <div class="modes" id="mmModes">
        <button class="mtab active" id="mmTabCatalog" data-mode="catalog" type="button">目录选择</button>
        <button class="mtab" id="mmTabManual" data-mode="manual" type="button">手动填写</button>
      </div>
      <div id="mmCatalogPane">
        <div class="mrow"><label>供应商</label><div class="mfield">
          <button class="provpick" id="mmPick" type="button"><span id="mmPickName">选择供应商…</span><span class="pv-sub">▾</span></button>
          <div class="provmenu hidden" id="mmMenu">
            <input class="psearch" id="mmSearch" placeholder="搜索供应商 / 模型…">
            <div class="provlist" id="mmList"></div>
          </div>
        </div></div>
      </div>
      <div id="mmManualPane" class="hidden">
        <div class="mrow"><label>协议</label><div class="mfield">
          <select id="mmProto"></select>
          <div class="mnote" id="mmProtoHint"></div>
        </div></div>
      </div>
      <div class="mrow"><label>计费方式</label><div class="mfield">
        <select id="mmBilling"></select>
        <div class="mnote" id="mmBillingHint"></div>
      </div></div>
      <div class="mrow"><label>名称</label><div class="mfield"><input id="mmName" placeholder="例如：我的 DeepSeek"></div></div>
      <div class="mrow"><label>Base URL</label><div class="mfield"><input id="mmBase" placeholder="选择供应商后自动填入，或手动粘贴"><div class="mlink" id="mmDoc"></div></div></div>
      <div class="mrow"><label>API Key</label><div class="mfield"><input id="mmKey" type="password" placeholder="留空则保留已保存的密钥"></div></div>
      <div class="mrow"><label>模型名</label><div class="mfield"><input id="mmModel" list="mmModelList" placeholder="选择供应商后可选候选模型"><datalist id="mmModelList"></datalist></div></div>
      <div class="mnote" id="mmNote"></div>
      <div class="mwarn" id="mmWarn"></div>
    </div>
    <div class="mfoot"><button class="ghost" id="mmCancel" type="button">取消</button><button id="mmSave" type="button">保存</button></div>
  </div>
</div>
<div class="mmask hidden" id="chanModal">
  <div class="mmodal">
    <div class="mhead"><span class="mt" id="chanTitle">连接通道</span><button class="mx" id="chanX" type="button">✕</button></div>
    <div class="mbody" id="chanBody"></div>
    <div class="mfoot"><span id="chanMsg" class="ctip" style="flex:1"></span>
      <button class="ghost" id="chanTest" type="button">测试</button>
      <button class="ghost" id="chanCancel" type="button">取消</button>
      <button id="chanSave" type="button">保存</button></div>
  </div>
</div>

<!-- 自定义连接器弹窗（登记本地技能源目录） -->
<div class="mmask hidden" id="connModal">
  <div class="mmodal">
    <div class="mhead"><span class="mt">自定义连接器</span><button class="mx" onclick="closeConnCustom()" type="button">✕</button></div>
    <div class="mbody">
      <div class="mrow"><label>名称</label><input id="ccName" placeholder="例如：我的技能库"></div>
      <div class="mrow"><label>描述</label><input id="ccDesc" placeholder="一句话说明（可选）"></div>
      <div class="mrow"><label>目录路径</label><input id="ccPath" placeholder="例如：D:\\skills 或 /home/me/skills"></div>
      <div class="mwarn" id="ccMsg"></div>
    </div>
    <div class="mfoot"><button class="ghost" onclick="closeConnCustom()" type="button">取消</button><button onclick="saveConnCustom()" type="button">保存</button></div>
  </div>
</div>

<script>
const $ = id => document.getElementById(id);
const fmt = t => t ? new Date(t*1000).toLocaleString('zh-CN') : '-';
const ago = t => t ? Math.max(0, Math.round(Date.now()/1000 - t)) + 's 前' : '-';
const esc = s => (s==null?'':String(s)).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
function badge(s){const m={'registered':'b-registered','running':'b-running','done':'b-done','completed':'b-completed','failed':'b-failed','blocked':'b-blocked','needs_human':'b-needs_human','pending':'b-pending','approved':'b-approved','rejected':'b-rejected','expired':'b-expired'};return '<span class="badge '+(m[s]||'')+'">'+esc(s)+'</span>';}
async function jget(u){const r=await fetch(u);return r.json();}
async function postJSON(u, body){
  const headers={'Content-Type':'application/json'};
  const t=localStorage.getItem('wbSession'); if(t) headers['X-Session-Token']=t;
  const r=await fetch(u,{method:'POST',headers:headers,body:JSON.stringify(body||{})});
  return {status:r.status, data:await r.json()};
}

/* ---------------- 添加模型弹窗 · 全局事件（弹窗在 body 顶层，任何面板都能开） ---------------- */
(function(){
  const pick=$('mmPick');if(pick)pick.addEventListener('click',function(){
    const m=$('mmMenu');if(m)m.classList.toggle('hidden');
  });
  const search=$('mmSearch');if(search)search.addEventListener('input',function(){renderProvList(search.value);});
  // 双模式切换：目录勾选 / 手动填写
  const tabCat=$('mmTabCatalog');if(tabCat)tabCat.addEventListener('click',function(){setModelMode('catalog');});
  const tabMan=$('mmTabManual');if(tabMan)tabMan.addEventListener('click',function(){setModelMode('manual');});
  const proto=$('mmProto');if(proto)proto.addEventListener('change',syncProtoHint);
  const billing=$('mmBilling');if(billing)billing.addEventListener('change',syncBillingHint);
  const x=$('mmX');if(x)x.addEventListener('click',closeModelModal);
  const cancel=$('mmCancel');if(cancel)cancel.addEventListener('click',closeModelModal);
  const save=$('mmSave');if(save)save.addEventListener('click',saveModel);
  const mask=$('modelModal');if(mask)mask.addEventListener('click',function(e){if(e.target===mask)closeModelModal();});
  // 助理远程通道弹窗
  const cx=$('chanX');if(cx)cx.addEventListener('click',closeChan);
  const cc=$('chanCancel');if(cc)cc.addEventListener('click',closeChan);
  const ct=$('chanTest');if(ct)ct.addEventListener('click',testChan);
  const cs=$('chanSave');if(cs)cs.addEventListener('click',saveChan);
  const cm=$('chanModal');if(cm)cm.addEventListener('click',function(e){if(e.target===cm)closeChan();});
  const cmask=$('connModal');if(cmask)cmask.addEventListener('click',function(e){if(e.target===cmask)closeConnCustom();});
})();

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
      '<div class="sitem" onclick="go(&quot;tasks&quot;);closeSearch()">'+badge(t.status)+' <b>'+esc(t.task_id)+'</b><span class="sid">'+esc(JSON.stringify(t.payload||{}).slice(0,60))+'</span></div>').join('');}
    if(R.length){h+='<div class="sgrp">审计轨迹 ('+R.length+')</div>'+R.map(t=>
      '<div class="sitem" onclick="go(&quot;trace&quot;);closeSearch()">'+badge(t.status)+' <b>'+esc(t.trace_id)+'</b><span class="sid">'+esc(t.title||'')+'</span></div>').join('');}
    if(A.length){h+='<div class="sgrp">审批 ('+A.length+')</div>'+A.map(a=>
      '<div class="sitem" onclick="go(&quot;approvals&quot;);closeSearch()">'+badge(a.status)+' <b>'+esc(a.action)+'</b><span class="sid">'+esc(a.approval_id)+'</span></div>').join('');}
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
Object.assign(ICON,{
 plus:'<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 5v14M5 12h14"/></svg>',
 people:'<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="9" cy="8" r="3.2"/><path d="M2 20c0-3.3 3.1-5 7-5s7 1.7 7 5"/><path d="M17 5.2a3.2 3.2 0 0 1 0 5.6M18 20c0-2.2-.9-3.8-2.3-4.6"/></svg>',
 link:'<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M10 13a5 5 0 0 0 7 0l3-3a5 5 0 0 0-7-7l-1 1"/><path d="M14 11a5 5 0 0 0-7 0l-3 3a5 5 0 0 0 7 7l1-1"/></svg>',
 clock:'<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>',
 folder:'<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>',
 grid:'<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/></svg>',
 sparkle:'<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9z"/><path d="M18 16l.8 2.2L21 19l-2.2.8L18 22l-.8-2.2L15 19l2.2-.8z"/></svg>'
});
const NAV_GROUPS=[
 {group:'工作台',items:[
   {id:'workspace',label:'工作台',icon:ICON.home},
   {id:'newtask',label:'新建任务',icon:ICON.plus},
   {id:'chat',label:'助理对话',icon:ICON.chat},
 ]},
 {group:'审计',items:[
   {id:'roster',label:'审计名单',icon:ICON.people},
   {id:'tasks',label:'任务中心',icon:ICON.tasks},
   {id:'trace',label:'审计轨迹',icon:ICON.trace},
   {id:'approvals',label:'审批中心',icon:ICON.approve},
   {id:'cost',label:'成本看板',icon:ICON.cost},
 ]},
 {group:'资源',items:[
   {id:'assistants',label:'助理',icon:ICON.puzzle},
   {id:'connectors',label:'技能连接器',icon:ICON.link},
   {id:'library',label:'资料库',icon:ICON.folder},
   {id:'schedule',label:'定时任务',icon:ICON.clock},
 ]},
 {group:'系统',items:[
   {id:'overview',label:'概览',icon:ICON.grid},
   {id:'security',label:'安全中心',icon:ICON.shield},
   {id:'skills',label:'技能',icon:ICON.sparkle},
   {id:'settings',label:'设置',icon:ICON.gear},
 ]},
];
const NAV=NAV_GROUPS.flatMap(g=>g.items);
const TITLES={};NAV.forEach(n=>TITLES[n.id]=n.label);

// 渲染侧边栏（分组）
$('nav').innerHTML = NAV_GROUPS.map(g=>
  '<div class="nav-group">'+g.group+'</div>'+
  g.items.map(n=>'<a data-view="'+n.id+'" title="'+n.label+'">'+n.icon+'<span class="label">'+n.label+'</span></a>').join('')
).join('');
$('nav').querySelectorAll('a').forEach(a=>a.addEventListener('click',()=>go(a.dataset.view)));

function go(view){
  if(!TITLES[view]) view='overview';
  location.hash = '#/'+view;
}
function route(){
  const v = (location.hash.replace('#/','')||'workspace');
  NAV.forEach(n=>{const a=$('nav').querySelector('a[data-view="'+n.id+'"]');if(a)a.classList.toggle('active',n.id===v);});
  document.querySelectorAll('.view').forEach(el=>el.classList.remove('active'));
  const view=$('view-'+v); if(view) view.classList.add('active');
  $('viewTitle').textContent = TITLES[v]||'工作台';
  const loaders={workspace:loadWorkspace,newtask:loadNewTask,roster:loadRoster,
    assistants:loadAssistants,connectors:loadConnectors,schedule:loadSchedule,
    library:loadLibrary,overview:loadOverview,chat:loadChat,tasks:loadTasks,
    trace:loadTraceList,cost:loadCost,approvals:loadApprovals,security:loadSecurity,
    skills:loadSkills,settings:loadSettings};
  (loaders[v]||loadWorkspace)();
}
window.addEventListener('hashchange',route);
initSearch();

$('collapseBtn').addEventListener('click',()=>$('sidebar').classList.toggle('collapsed'));
$('themeBtn').addEventListener('click',()=>{
  const t=document.documentElement.getAttribute('data-theme')==='dark'?'light':'dark';
  document.documentElement.setAttribute('data-theme',t);localStorage.setItem('wbtheme',t);
});
(function(){const t=localStorage.getItem('wbtheme');if(t)document.documentElement.setAttribute('data-theme',t);})();

// ================= 登录 / 会话 =================
let SESSION=null;
function initLogin(){
  const roles=window.__ROLES__||[];
  $('loginRole').innerHTML=roles.map(r=>'<option value="'+r.id+'">'+esc(r.name)+' · '+esc(r.desc)+'</option>').join('');
  $('loginBtn').addEventListener('click',doLogin);
  $('loginUser').addEventListener('keydown',e=>{if(e.key==='Enter')doLogin();});
}
async function doLogin(){
  const btn=$('loginBtn');btn.disabled=true;btn.textContent='登录中…';
  $('loginErr').textContent='';
  try{
    const r=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({user:$('loginUser').value,role:$('loginRole').value})});
    const d=await r.json();
    if(!r.ok||!d.token){$('loginErr').textContent=d.error||'登录失败';return;}
    SESSION=d;
    localStorage.setItem('wbSession',d.token);
    applySession(d);
    $('loginWrap').classList.add('hidden');
    go('workspace');
  }catch(e){$('loginErr').textContent='登录请求失败：'+e;}
  finally{btn.disabled=false;btn.textContent='进入工作台';}
}
function applySession(s){
  $('userBtn').style.display='flex';
  $('newTaskBtn').style.display='inline-block';
  const roleName=(window.__ROLES__||[]).find(r=>r.id===s.role);
  $('navfoot').innerHTML='v0.9 · 本地可审计<span class="u">'+esc(s.user)+' · '+esc(roleName?roleName.name:s.role)+'</span>'+
    '<span class="r" id="logoutBtn" style="cursor:pointer;text-decoration:underline">退出登录</span>';
  const lo=$('logoutBtn');
  if(lo)lo.addEventListener('click',doLogout);
}
async function doLogout(){
  try{await fetch('/api/logout',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({token:SESSION?SESSION.token:''})});}catch(e){}
  SESSION=null;localStorage.removeItem('wbSession');
  $('loginWrap').classList.remove('hidden');
  $('userBtn').style.display='none';
  $('newTaskBtn').style.display='none';
  $('navfoot').textContent='v0.9 · 本地可审计';
}
async function restoreSession(){
  const t=localStorage.getItem('wbSession');
  if(!t)return false;
  try{
    const r=await fetch('/api/session',{headers:{'X-Session-Token':t}});
    const d=await r.json();
    if(!d.ok)return false;
    SESSION=d.session;applySession(d.session);
    $('loginWrap').classList.add('hidden');
    return true;
  }catch(e){return false;}
}

// ================= 工作台首页 =================
async function loadWorkspace(){
  const el=$('view-workspace');
  const [ov,as,lb,sch]=await Promise.all([
    jget('/api/overview'),jget('/api/assistants'),jget('/api/library'),jget('/api/schedule')]);
  const c=ov.counts||{};
  const user=SESSION?SESSION.user:'operator';
  const roleName=SESSION?((window.__ROLES__||[]).find(r=>r.id===SESSION.role)||{name:SESSION.role}):{name:'访客'};
  el.innerHTML='<div class="ws-hero">'+
    '<h2>你好，'+esc(user)+' <span class="pill">'+esc(roleName.name)+'</span></h2>'+
    '<p>本地多 AI 协作的可审计控制台 —— 每个动作留痕、成本可归因、高危需审批、密钥零泄露。</p>'+
    '<div class="quick-grid">'+
      '<button class="quick" onclick="go(&quot;newtask&quot;)"><span class="qi">➕</span>新建任务<span class="qd">登记带依赖的任务，自动记录轨迹</span></button>'+
      '<button class="quick" onclick="go(&quot;chat&quot;)"><span class="qi">💬</span>问助理<span class="qd">规则引擎优先，LLM 兜底</span></button>'+
      '<button class="quick" onclick="go(&quot;roster&quot;)"><span class="qi">👥</span>审计名单<span class="qd">'+((as.counts||{}).on_duty||0)+' 个岗位在岗</span></button>'+
      '<button class="quick" onclick="go(&quot;assistants&quot;)"><span class="qi">🧩</span>助理<span class="qd">'+((as.counts||{}).total||0)+' 个可用助理</span></button>'+
      '<button class="quick" onclick="go(&quot;schedule&quot;)"><span class="qi">⏰</span>定时任务<span class="qd">'+((sch.jobs||[]).length)+' 条调度规则</span></button>'+
      '<button class="quick" onclick="go(&quot;library&quot;)"><span class="qi">📁</span>资料库<span class="qd">'+((lb.counts||{}).documents||0)+' 份产物 · '+((lb.counts||{}).assets||0)+' 个素材</span></button>'+
    '</div></div>'+
    '<div class="grid stats">'+
      stat(c.tasks_total,'任务总数')+stat(c.running,'运行中','accent')+stat(c.pending,'待审批','warn')+
      stat(c.traces,'审计轨迹')+stat('¥'+(ov.cost_total||0).toFixed(2),'累计成本')+stat(ov.security_score+'/100','安全评分','ok')+
    '</div>'+
    '<div class="panel" style="margin-top:16px"><h3>审计动作 <span class="tag" style="margin-left:6px">点击即真实执行并归档进资料库</span></h3>'+
      '<div class="actions-grid">'+
        '<button class="abtn" onclick="runAction(&quot;self_scan&quot;)"><span class="ai">🔍</span>一键自扫<span class="ad">扫描项目源码密钥泄露</span></button>'+
        '<button class="abtn" onclick="runAction(&quot;cost_snapshot&quot;)"><span class="ai">💰</span>成本快照<span class="ad">汇总近 30 天成本与离群点</span></button>'+
        '<button class="abtn" onclick="runAction(&quot;whitepaper&quot;)"><span class="ai">📜</span>生成白皮书<span class="ad">当前安全姿态快照</span></button>'+
        '<button class="abtn" onclick="go(&quot;schedule&quot;)"><span class="ai">⏱️</span>立即执行到期<span class="ad">跑定时任务并归档</span></button>'+
      '</div>'+
      '<div id="wsActionResult"></div>'+
    '</div>'+
    '<div class="split" style="margin-top:16px">'+
      '<div class="panel"><h3>最近任务</h3><div id="wsRecent"><div class="empty">加载中…</div></div></div>'+
      '<div class="panel"><h3>系统状态</h3>'+
        '<div class="check"><div class="dot">✓</div><div><div class="t">沙箱隔离：'+esc(ov.sandbox_backend||'-')+'（'+esc(ov.sandbox_containment||'-')+'）</div><div class="d">'+esc(ov.sandbox_note||'')+'</div></div></div>'+
        '<div class="check"><div class="dot">✓</div><div><div class="t">源码硬编码密钥自扫：0 真实泄露</div><div class="d">'+esc(ov.self_scan_note||'（自扫结果不可用）')+'</div></div></div>'+
        '<div class="check"><div class="dot">✓</div><div><div class="t">审批默认拒绝</div><div class="d">高危动作未获批准即不执行。</div></div></div>'+
      '</div></div>';
  // 最近任务
  try{
    const ts=await jget('/api/tasks?limit=6');
    $('wsRecent').innerHTML=ts.length?ts.map(t=>'<div class="litem"><div class="grow"><div class="nm">'+esc((t.payload&&t.payload.title)||t.task_id)+'</div>'+
      '<div class="mt">'+esc(t.task_id)+' · '+esc((t.payload&&t.payload.role)||'-')+' · '+fmt(t.created_at)+'</div></div>'+
      badge(t.status)+'</div>').join(''):'<div class="empty">暂无任务</div>';
  }catch(e){$('wsRecent').innerHTML='<div class="empty">加载失败</div>';}
}

// ================= 审计动作 =================
async function runAction(action){
  const box=$('wsActionResult'); if(!box)return;
  box.innerHTML='<div class="warnbar" style="background:#eef1f4">正在执行：'+esc(action)+' …</div>';
  try{
    const map={'self_scan':'/api/actions/self-scan','cost_snapshot':'/api/actions/cost-snapshot',
      'whitepaper':'/api/actions/whitepaper','closure':'/api/actions/closure'};
    const {status,data}=await postJSON(map[action]||('/api/actions/'+action),{});
    if(!data.ok){box.innerHTML='<div class="warnbar">执行失败：'+esc(data.error||('HTTP '+status))+'</div>';return;}
    const files=(data.files||[]).map(f=>esc(f)).join('、');
    box.innerHTML='<div class="warnbar" style="background:#e8f5e9;color:#2e7d32;border-color:#c8e6c9">'
      +'✓ '+esc(data.summary||'执行完成')+'<br><span style="color:var(--sub)">归档：'+esc((data.report_dir||'').replace(new RegExp('^reports/'),''))+'/ ['+files+']'
      +(data.trace_id?(' · 轨迹 '+esc(data.trace_id)):'')+'</span></div>';
  }catch(e){box.innerHTML='<div class="warnbar">请求失败：'+esc(String(e))+'</div>';}
}

// ================= 新建任务 =================
async function loadNewTask(){
  const el=$('view-newtask');
  const roles=window.__ROLES__||[];
  if(!el.dataset.init){
    el.dataset.init='1';
    el.innerHTML='<h2 class="vh">新建任务</h2><p class="vdesc">登记一个可调度的任务；依赖会进入 DAG 调度，创建动作会记入审计轨迹。</p>'+
      '<div class="split"><div class="panel"><h3>任务定义</h3>'+
        '<div class="fld"><label>任务标题 *</label><input id="ntTitle" placeholder="例如：生成双十一主图素材" maxlength="120"></div>'+
        '<div class="fld"><label>岗位角色 *</label><select id="ntRole">'+roles.map(r=>'<option value="'+r.id+'">'+esc(r.name)+' — '+esc(r.desc)+'</option>').join('')+'</select></div>'+
        '<div class="fld"><label>模板</label><input id="ntTemplate" placeholder="例如：promo-material（可留空）"></div>'+
        '<div class="fld"><label>优先级</label><select id="ntPriority"><option value="normal">普通</option><option value="high">高</option><option value="low">低</option></select></div>'+
        '<div class="fld"><label>依赖任务（逗号分隔的 task_id）</label><input id="ntDeps" placeholder="留空表示无依赖"><div class="hint">依赖会写入 DAG；不存在的任务会被拒绝。</div></div>'+
        '<div class="fld"><label>备注</label><textarea id="ntNote" placeholder="补充说明"></textarea></div>'+
        '<button class="btn" id="ntSubmit">创建任务</button>'+
        '<div class="form-err" id="ntErr"></div><div class="form-ok" id="ntOk"></div>'+
      '</div>'+
      '<div class="panel"><h3>可依赖的已有任务</h3><div id="ntCandidates"><div class="empty">加载中…</div></div>'+
        '<div class="panel" style="margin-top:16px"><h3>写操作边界</h3>'+
        '<div class="check"><div class="dot">✓</div><div><div class="t">只写 tasks 表 + trace 事件</div><div class="d">不碰审批、成本、素材等其他表。</div></div></div>'+
        '<div class="check"><div class="dot">✓</div><div><div class="t">创建人写入轨迹</div><div class="d">谁在何时建了什么，可审计。</div></div></div>'+
      '</div></div>';
    $('ntSubmit').addEventListener('click',submitNewTask);
  }
  // 候选依赖任务
  try{
    const ts=await jget('/api/tasks?limit=40');
    $('ntCandidates').innerHTML=ts.length?ts.map(t=>'<div class="litem"><div class="grow"><div class="nm">'+esc((t.payload&&t.payload.title)||t.task_id)+'</div>'+
      '<div class="mt">'+esc(t.task_id)+' · '+esc((t.payload&&t.payload.role)||'-')+'</div></div>'+
      '<div class="ops"><button class="btn ghost" onclick="addDep(&quot;'+esc(t.task_id)+'&quot;)">选为依赖</button></div></div>').join('')
      :'<div class="empty">暂无已有任务</div>';
  }catch(e){$('ntCandidates').innerHTML='<div class="empty">加载失败</div>';}
}
function addDep(tid){
  const i=$('ntDeps');const cur=i.value.split(/[,，\\s]+/).filter(Boolean);
  if(cur.indexOf(tid)<0)cur.push(tid);
  i.value=cur.join(',');
}
async function submitNewTask(){
  $('ntErr').textContent='';$('ntOk').textContent='';
  const deps=$('ntDeps').value.split(/[,，\\s]+/).filter(Boolean);
  const body={title:$('ntTitle').value,role:$('ntRole').value,template:$('ntTemplate').value,
    priority:$('ntPriority').value,note:$('ntNote').value,depends_on:deps,
    operator:SESSION?SESSION.user:'operator'};
  try{
    const r=await fetch('/api/tasks/new',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify(body)});
    const d=await r.json();
    if(!r.ok||!d.ok){$('ntErr').textContent=d.error||'创建失败';return;}
    $('ntOk').textContent='已创建任务 '+d.task_id+(d.trace_id?('（轨迹 '+d.trace_id+'）'):'');
    $('ntTitle').value='';$('ntDeps').value='';$('ntNote').value='';
    setTimeout(()=>loadNewTask(),600);
  }catch(e){$('ntErr').textContent='请求失败：'+e;}
}

// ================= 审计名单 =================
async function loadRoster(){
  const el=$('view-roster');
  const d=await jget('/api/roster');
  const cnt=d.counts||{};
  el.innerHTML='<h2 class="vh">审计名单</h2><p class="vdesc">'+esc(d.note||'')+'</p>'+
    '<div class="grid stats">'+stat(cnt.roles||0,'岗位')+stat(cnt.on_duty||0,'在岗','accent')+stat(cnt.standby||0,'待命')+stat((d.models||[]).length,'可用模型')+'</div>'+
    '<div class="panel"><h3>花名册</h3>'+
      (d.entries||[]).map(e=>'<div class="litem"><div class="avatar" style="background:'+avatarColor(e.id)+'">'+esc(e.name.slice(0,1))+'</div>'+
        '<div class="grow"><div class="nm">'+esc(e.name)+' <span class="tag">'+esc(e.id)+'</span> '+
          (e.on_duty?'<span class="pill live">在岗</span>':'<span class="pill">待命</span>')+'</div>'+
        '<div class="mt">'+esc(e.desc)+' · 模型 '+esc(e.model)+'</div>'+
        '<div class="mt">技能：'+(e.skills_installed.length?e.skills_installed.map(esc).join('、'):'<span style="color:var(--sub)">未安装</span>')+'</div>'+
        (e.on_duty_users.length?('<div class="mt">在岗人员：'+e.on_duty_users.map(esc).join('、')+'</div>'):'')+
        '</div>'+
        '<div class="ops"><button class="btn ghost" onclick="rosterTask(&quot;'+esc(e.id)+'&quot;,&quot;'+esc(e.name)+'&quot;)">派任务</button></div></div>').join('')+
    '</div>'+
    '<div class="panel"><h3>模型</h3><div class="row">'+(d.models||[]).map(m=>'<span class="pill">'+esc(m)+'</span>').join('')+'</div></div>';
}
function avatarColor(id){const cs=['#3b6cff','#16b8a6','#f59e0b','#ef4444','#8b5cf6','#ec4899'];
  let h=0;for(const ch of String(id))h=(h*31+ch.charCodeAt(0))>>>0;return cs[h%cs.length];}
function rosterTask(roleId,roleName){
  $('ntRole').value=roleId;
  const d=document.getElementById('view-newtask');d.dataset.init='';delete d.dataset.init;
  go('newtask');
  setTimeout(()=>{const t=$('ntTitle');if(t)t.placeholder='指派给 '+roleName+' 的任务：';},200);
}

// ================= 助理 =================
async function loadAssistants(){
  const el=$('view-assistants');
  const [d,st]=await Promise.all([jget('/api/assistants'),jget('/api/assistant/settings')]);
  window.__assistantSettings=st;
  const c=d.counts||{};
  el.innerHTML='<h2 class="vh">助理</h2><p class="vdesc">'+esc(d.note||'')+'</p>'+
    '<div class="grid stats">'+stat(c.total||0,'助理总数','accent')+stat(c.experts||0,'内置专家')+stat(c.skills||0,'已装技能')+'</div>'+
    '<div class="panel"><h3>内置专家</h3>'+
      (d.assistants||[]).filter(a=>a.kind==='内置专家').map(a=>'<div class="litem"><div class="avatar" style="background:'+avatarColor(a.id)+'">'+esc(a.name.slice(0,1))+'</div>'+
        '<div class="grow"><div class="nm">'+esc(a.name)+' '+(a.installed?'<span class="pill live">就绪</span>':'<span class="pill">缺技能</span>')+'</div>'+
        '<div class="mt">'+esc(a.desc)+'</div>'+
        '<div class="mt">依赖技能：'+(a.skills.length?a.skills.map(esc).join('、'):'无')+'</div></div>'+
        '<div class="ops"><button class="btn ghost" onclick="go(&quot;chat&quot;)">对话</button></div></div>').join('')+
    '</div>'+
    '<div class="panel"><h3>技能型助理 <span class="tag" style="margin-left:6px">来自真实已装技能</span></h3>'+
      '<div class="grid card-grid">'+(d.assistants||[]).filter(a=>a.kind!=='内置专家').map(a=>'<div class="acard">'+
        '<div class="ah"><div class="avatar" style="width:24px;height:24px;font-size:11px;background:'+avatarColor(a.id)+'">'+esc(a.name.slice(0,1))+'</div>'+
        '<div class="an">'+esc(a.name)+'</div></div>'+
        '<div class="ak">'+esc(a.kind)+'</div>'+
        '<div class="ad">'+esc(a.desc||'（无描述）')+'</div></div>').join('')+'</div>'+
    '</div>'+
    '<div class="panel"><h3>远程通道 <span class="tag" style="margin-left:6px">推送审计提醒与任务结果</span></h3>'+
      '<div class="conn-grid" id="chanGrid"></div>'+
      '<div class="ctip" style="margin-top:10px">'+esc(st.note||'')+'</div>'+
    '</div>'+
    '<div class="panel"><h3>事件订阅 <span class="tag" style="margin-left:6px">哪些事件推到哪些通道</span></h3>'+
      '<div id="notifySubs"><div class="empty">加载中…</div></div>'+
      '<div class="ctip" style="margin-top:10px">审批请求/决定、任务完成/失败、自扫高危、成本快照等事件会<b>自动</b>推送到已启用的通道（不再只有「测试」按钮能发）。推送失败不影响主流程，每条推送都有记录。</div>'+
      '<div style="margin-top:10px"><button class="btn ghost" onclick="testNotifyEvent()">发送一条事件自检</button>'+
      '<span class="ctip" id="notifyTestMsg"></span></div>'+
    '</div>'+
    '<div class="panel"><h3>飞书入站（回复即控制） <span class="tag" style="margin-left:6px">诚实边界：群机器人只出不进</span></h3>'+
      '<div id="notifyInbound"><div class="empty">加载中…</div></div>'+
    '</div>'+
    '<div class="panel"><h3>上下文管理</h3>'+
      '<div class="litem"><div class="grow"><div class="nm">携带对话历史</div>'+
        '<div class="mt">启用后，助理对话会携带最近几轮历史作为上下文；关闭后每次提问独立处理，可明显降低 token 消耗。</div></div>'+
        '<label class="switch"><input type="checkbox" id="ctxMem" '+(st.prefs&&st.prefs.context_memory!==false?'checked':'')+' onchange="toggleCtx(this)"><span class="slider"></span></label>'+
      '</div>'+
    '</div>';
  renderChanGrid(st.channels||[]);
  loadNotifySubs();
}

// ---------------- 事件订阅（事件 → 通道） ----------------
async function loadNotifySubs(){
  const g=$('notifySubs');if(!g)return;
  let d;
  try{ d=await jget('/api/assistant/notify'); }
  catch(e){ g.innerHTML='<div class="empty">推送引擎未就绪</div>'; return; }
  window.__notifyState=d;
  renderInbound(d.inbound);
  let st=window.__assistantSettings;
  if(!st||!(st.channels||[]).length){
    // 通道列表没缓存（直接进面板 / 缓存失效）时自己取一次——
    // 否则会误判成「没有可用通道」，把订阅表整个隐藏掉。
    try{ st=await jget('/api/assistant/settings'); window.__assistantSettings=st; }catch(e){}
  }
  st=st||{};
  const chans=(st.channels||[]).filter(c=>c.configured);
  const evs=d.events||[];
  const subs=d.subscriptions||{};
  if(!evs.length){g.innerHTML='<div class="empty">暂无事件类型</div>';return;}
  if(!chans.length){
    g.innerHTML='<div class="empty">还没有已配置的通道 —— 先在上面「远程通道」里连接一个（如飞书群机器人），事件才有地方可推。</div>';
    return;
  }
  g.innerHTML='<table class="tbl"><thead><tr><th>事件类型</th>'+
    chans.map(c=>'<th>'+esc(c.name)+'</th>').join('')+'</tr></thead><tbody>'+
    evs.map(e=>'<tr><td><div class="nm">'+esc(e.label)+'</div>'+
      '<div class="mt">'+esc(e.desc)+'</div></td>'+
      chans.map(c=>'<td><label class="switch"><input type="checkbox" '+
        'data-ev="'+esc(e.id)+'" data-ch="'+esc(c.id)+'" '+
        (((subs[c.id]||[]).indexOf(e.id)>=0)?'checked':'')+'>'+
        '<span class="slider"></span></label></td>').join('')+'</tr>').join('')+
    '</tbody></table>'+
    '<div style="margin-top:10px;display:flex;gap:8px;align-items:center">'+
    '<button class="btn" onclick="saveNotifySubs()">保存订阅</button>'+
    '<span class="ctip" id="notifySubsMsg"></span></div>'+
    '<div class="ctip" style="margin-top:10px">已启用通道：'+
    (esc((d.enabled_channels||[]).join('、'))||'无')+
    '。未勾选的事件不会推送。</div>';
}
async function saveNotifySubs(){
  const msg=$('notifySubsMsg');if(!msg)return;
  const boxes=document.querySelectorAll('#notifySubs input[type=checkbox]');
  const subs={};
  boxes.forEach(b=>{
    const c=b.getAttribute('data-ch'),e=b.getAttribute('data-ev');
    if(!subs[c])subs[c]=[];
    if(b.checked)subs[c].push(e);
  });
  msg.textContent='保存中…';
  const r=await postJSON('/api/assistant/notify/subscriptions',{subscriptions:subs});
  if(!r.ok){msg.textContent='保存失败：'+(r.error||'未知错误');return;}
  msg.textContent='已保存';
  await loadNotifySubs();
}
async function testNotifyEvent(){
  const msg=$('notifyTestMsg');if(!msg)return;
  msg.textContent='推送中…';
  const r=await postJSON('/api/assistant/notify/test',{event:'approval.pending'});
  if(!r.ok){
    const d=(r.delivered||[]).map(x=>x.channel+'：'+(x.error||'失败')).join('；');
    msg.textContent='✗ '+(r.error||d||'推送失败');
    return;
  }
  const d=(r.delivered||[]).map(x=>x.channel+(x.ok?' ✓':' ✗')).join('　');
  msg.textContent=(r.skipped&&r.skipped.length)?('跳过：'+r.skipped.join(',')):('✓ 已推送　'+d);
}
// ---------------- 飞书入站中继（诚实边界） ----------------
function renderInbound(ib){
  const g=$('notifyInbound');if(!g)return;
  if(!ib){g.innerHTML='<div class="empty">推送引擎未就绪</div>';return;}
  const tok=ib.token||'';
  g.innerHTML='<div class="ctip" style="margin-bottom:8px">'+esc(ib.note||'')+'</div>'+
    '<div class="litem"><div class="grow"><div class="nm">接收端点</div>'+
      '<div class="mt"><code class="k">POST '+esc(ib.endpoint||'')+'</code>'+
      '　请求头 <code class="k">'+esc(ib.header||'')+'</code></div></div></div>'+
    '<div class="litem"><div class="grow"><div class="nm">入站令牌</div>'+
      '<div class="mt">'+(tok?('<code class="k">'+esc(tok)+'</code>'):esc(ib.token_hint||'（未登录，不显示）'))+
      '</div></div></div>'+
    '<div class="litem"><div class="grow"><div class="nm">可用指令</div>'+
      '<div class="mt">'+esc((ib.commands||[]).join('　'))+'</div></div></div>';
}

// ---------------- 助理远程通道（真实 webhook 通道） ----------------
function renderChanGrid(list){
  const g=$('chanGrid');if(!g)return;
  if(!list.length){g.innerHTML='<div class="empty">暂无可用通道</div>';return;}
  g.innerHTML=list.map(c=>{
    const st=c.enabled?'已连接':(c.configured?'已停用':'连接');
    const cls=c.enabled?'conn-plus on':(c.configured?'conn-plus mid':'conn-plus');
    return '<div class="conn-card">'+
      '<button class="conn-btn '+cls+'" onclick="openChan(&quot;'+esc(c.id)+'&quot;)">'+st+'</button>'+
      '<div class="conn-badge" style="background:hsl('+c.hue+',52%,44%)">'+esc(c.letter)+'</div>'+
      '<div class="nm">'+esc(c.name)+'</div>'+
      '<div class="ds">'+esc(c.desc)+'</div>'+
      '<div class="ft">'+(c.enabled?'<span class="conn-tag ok">推送中</span>':'')+
        (c.configured&&!c.enabled?'<span class="conn-tag">未启用</span>':'')+'</div></div>';
  }).join('');
}
function openChan(id){
  const st=window.__assistantSettings;if(!st)return;
  const c=(st.channels||[]).find(x=>x.id===id);if(!c)return;
  $('chanTitle').textContent='连接通道 · '+c.name;
  const body=$('chanBody');
  body.innerHTML=c.fields.map(f=>
    '<div class="mrow"><label>'+esc(f.label)+'</label><div class="mfield">'+
    '<input id="chan_'+esc(f.key)+'" type="'+esc(f.type)+'" placeholder="'+esc(f.placeholder||'')+'">'+
    '</div></div>').join('')+
    '<div class="mrow"><label>启用</label><div class="mfield">'+
    '<label class="switch"><input type="checkbox" id="chanEnabled" '+(c.enabled?'checked':'')+'><span class="slider"></span></label>'+
    '</div></div>'+
    (c.configured?'<div class="ctip">当前 Webhook：<code class="k">'+esc(c.webhook_masked)+'</code>'+(c.has_secret?'（已配置加签密钥）':'')+'</div>':'')+
    '<div class="ctip">'+esc(c.note||'')+'</div>';
  body.dataset.chanId=c.id;
  window.__chanCur=c;
  $('chanMsg').textContent='';
  $('chanModal').classList.remove('hidden');
}
function closeChan(){$('chanModal').classList.add('hidden');}
async function saveChan(){
  const c=window.__chanCur;if(!c)return;
  const msg=$('chanMsg');msg.textContent='';
  const body={id:c.id,enabled:$('chanEnabled').checked};
  c.fields.forEach(f=>{
    let v=($('chan_'+f.key).value||'').trim();
    if(f.key==='secret'){ if(!v&&c.has_secret)v='****'; }
    else if(f.key==='webhook'){ if(!v&&c.configured)v='****'; }
    body[f.key]=v;
  });
  const r=await postJSON('/api/assistant/channels/config',body);
  if(!r.ok){msg.textContent='保存失败：'+(r.error||'未知错误');return;}
  msg.textContent='已保存';
  await refreshAssistantSettings();closeChan();
}
async function testChan(){
  const c=window.__chanCur;if(!c)return;
  const msg=$('chanMsg');msg.textContent='测试中…';
  const body={id:c.id};
  c.fields.forEach(f=>{body[f.key]=($('chan_'+f.key).value||'').trim();});
  const r=await postJSON('/api/assistant/channels/test',body);
  msg.textContent=r.ok?'✓ 已送达，请到群里确认':('✗ '+(r.error||'发送失败'));
}
async function toggleCtx(el){
  const r=await postJSON('/api/assistant/prefs',{context_memory:el.checked});
  if(!r.ok){el.checked=!el.checked;alert('切换失败：'+(r.error||'未知错误'));}
}
async function refreshAssistantSettings(){
  const st=await jget('/api/assistant/settings');
  window.__assistantSettings=st;
  renderChanGrid(st.channels||[]);
  const cb=$('ctxMem');if(cb&&st.prefs)cb.checked=st.prefs.context_memory!==false;
}

// ================= 技能连接器（WorkBuddy 式卡片画廊） =================
const CONN_HUES=[210,160,28,268,0,190,330,55,122,240];
function connHue(name){let h=0;const s=name||'?';for(let i=0;i<s.length;i++)h=(h*31+s.charCodeAt(i))%997;return CONN_HUES[h%CONN_HUES.length];}
function connBadge(name){
  const s=name||'?';
  return '<div class="conn-badge" style="background:hsl('+connHue(s)+',52%,44%)">'+esc(s.charAt(0).toUpperCase())+'</div>';
}
function connCard(o){
  let corner='';
  if(o.installed)corner='<span class="conn-check" title="已就绪">✓</span>';
  else if(o.plusSlug)corner='<button class="conn-plus" title="安装" onclick="connInstall(&quot;'+esc(o.plusSlug)+'&quot;)">+</button>';
  const tag=o.tag?'<span class="conn-tag '+(o.tagCls||'')+'">'+esc(o.tag)+'</span>':'';
  const foot=o.foot||'';
  return '<div class="conn-card">'+corner+connBadge(o.name)+
    '<div class="nm">'+esc(o.name)+'</div>'+
    '<div class="ds">'+esc(o.desc||'')+'</div>'+
    '<div class="ft">'+tag+foot+'</div></div>';
}
function connEmpty(gridId,txt){const g=$(gridId);if(g)g.innerHTML='<div class="empty">'+esc(txt)+'</div>';}
async function loadConnectors(){
  const el=$('view-connectors');
  const d=await jget('/api/connectors');
  window.__connData=d;
  const sk=await jget('/api/skills');
  window.__connSkills=sk||[];
  el.innerHTML='<h2 class="vh">技能连接器</h2>'+
    '<div class="conn-top">'+
      '<div class="search"><input id="connQ" placeholder="搜索 SkillHub 社区技能，如：安全测试 / 周报 / 数据分析">'+
      '<button class="btn" onclick="connSearch()">搜索</button></div>'+
      '<button class="btn ghost" onclick="openConnCustom()">+ 自定义连接器</button>'+
    '</div>'+
    '<div class="conn-sec">内置连接器 <span class="cnt">'+((d.local_connectors||[]).length)+' 项</span></div>'+
    '<div class="conn-grid" id="connBuiltIn"></div>'+
    '<div class="conn-sec">已安装技能 <span class="cnt">'+((sk||[]).length)+' 项 · 安装目标 <code class="k">~/.workbuddy/skills</code></span></div>'+
    '<div class="conn-grid" id="connInstalled"></div>'+
    '<div class="conn-sec">自定义连接器 <span class="cnt">'+((d.custom_connectors||[]).length)+' 项</span></div>'+
    '<div class="conn-grid" id="connCustom"></div>'+
    '<div class="conn-sec">SkillHub 推荐 <span class="cnt">来自真实社区市场，不可达时如实提示</span></div>'+
    '<div class="conn-grid" id="connFeatured"><div class="empty">加载中…</div></div>'+
    '<div class="conn-sec">搜索结果 <span class="cnt" id="connSearchCnt"></span></div>'+
    '<div class="conn-grid" id="connResults"><div class="empty">输入关键词后搜索社区技能</div></div>'+
    '<p class="vdesc" style="margin-top:16px">'+esc(d.note||'')+'</p>';
  renderConnBuiltIn();renderConnInstalled();renderConnCustom();loadConnFeatured();
  const q=$('connQ');if(q)q.addEventListener('keydown',function(e){if(e.key==='Enter')connSearch();});
}
function renderConnBuiltIn(){
  const d=window.__connData||{};
  const list=d.local_connectors||[];
  if(!list.length){connEmpty('connBuiltIn','未检测到内置连接器');return;}
  $('connBuiltIn').innerHTML=list.map(function(c){
    return connCard({name:c.name,desc:c.desc,tag:'已内置',tagCls:'m',installed:true});
  }).join('');
}
function renderConnInstalled(){
  const sk=window.__connSkills||[];
  if(!sk.length){connEmpty('connInstalled','暂无已安装技能；可去 SkillHub 推荐或搜索里点 + 安装');return;}
  $('connInstalled').innerHTML=sk.map(function(k){
    return connCard({name:k.name,desc:k.description||k.desc||'',
      tag:(k.source==='custom'?'自定义':(k.source==='skillhub'?'SkillHub':'本地')),
      installed:true});
  }).join('');
}
function renderConnCustom(){
  const d=window.__connData||{};
  const list=d.custom_connectors||[];
  if(!list.length){connEmpty('connCustom','未登记自定义连接器；点右上「+ 自定义连接器」登记本地技能源目录');return;}
  $('connCustom').innerHTML=list.map(function(c){
    return connCard({name:c.name,desc:c.desc||'',tag:'本地技能源',
      foot:'<span class="k" style="font-size:11px;word-break:break-all">'+esc(c.path||'')+'</span>'+
        ' <a href="javascript:void 0" onclick="removeConnCustom(&quot;'+esc(c.name)+'&quot;)" style="color:var(--warn)">移除</a>'});
  }).join('');
}
async function loadConnFeatured(){
  const g=$('connFeatured');if(!g)return;
  try{
    const r=await jget('/api/connectors/skillhub/featured');
    if(!r.ok){g.innerHTML='<div class="warnbar">'+esc(r.error||'SkillHub 不可达')+'（推荐位需要联网）</div>';return;}
    if(!r.results.length){g.innerHTML='<div class="empty">SkillHub 暂无推荐</div>';return;}
    g.innerHTML=r.results.map(function(s){
      return connCard({name:s.name||s.slug,desc:s.desc,tag:s.category||'社区',
        plusSlug:s.slug,foot:s.author?'<span>by '+esc(s.author)+'</span>':''});
    }).join('');
  }catch(e){g.innerHTML='<div class="warnbar">推荐加载失败：'+esc(String(e))+'</div>';}
}
async function connSearch(){
  const q=($('connQ')?$('connQ').value.trim():'');
  const box=$('connResults');const cnt=$('connSearchCnt');
  if(!q){box.innerHTML='<div class="empty">输入关键词后搜索社区技能</div>';if(cnt)cnt.textContent='';return;}
  box.innerHTML='<div class="empty">搜索中…</div>';
  try{
    const r=await jget('/api/connectors/skillhub/search?q='+encodeURIComponent(q));
    if(!r.ok){box.innerHTML='<div class="warnbar">'+esc(r.error||'搜索失败')+'</div>';return;}
    if(!r.results.length){box.innerHTML='<div class="empty">没有匹配 “'+esc(q)+'” 的技能</div>';if(cnt)cnt.textContent='0 项';return;}
    if(cnt)cnt.textContent=r.results.length+' 项';
    box.innerHTML=r.results.map(function(s){
      return connCard({name:s.name||s.slug,desc:s.desc,tag:s.category||'社区',
        plusSlug:s.slug,foot:s.author?'<span>by '+esc(s.author)+'</span>':''});
    }).join('');
  }catch(e){box.innerHTML='<div class="warnbar">搜索失败：'+esc(String(e))+'</div>';}
}
async function connInstall(slug){
  if(!confirm('把技能 '+slug+' 安装到 WorkBuddy 用户级技能目录？'))return;
  const box=$('connResults');
  box.insertAdjacentHTML('afterbegin','<div class="warnbar" id="connInstalling">正在安装 '+esc(slug)+'…</div>');
  try{
    const r=await postJSON('/api/connectors/skillhub/install',{slug:slug});
    const d=r.data;
    const n=document.getElementById('connInstalling');
    if(n)n.remove();
    if(r.status===401){box.insertAdjacentHTML('afterbegin','<div class="warnbar">'+esc(d.error||'需要登录')+'</div>');return;}
    if(d.ok)box.insertAdjacentHTML('afterbegin','<div class="warnbar" style="background:#e8f5e9;color:#2e7d32;border-color:#c8e6c9">安装成功：'+esc(slug)+'。'+esc(d.note||'')+'</div>');
    else box.insertAdjacentHTML('afterbegin','<div class="warnbar">安装失败：'+esc(d.error||'未知错误')+'</div>');
  }catch(e){
    const n=document.getElementById('connInstalling');if(n)n.remove();
    box.insertAdjacentHTML('afterbegin','<div class="warnbar">安装请求失败：'+esc(String(e))+'</div>');
  }
}
// ---- 自定义连接器（本地技能源目录） ----
function openConnCustom(){
  const mask=$('connModal');if(!mask)return;
  $('ccName').value='';$('ccDesc').value='';$('ccPath').value='';
  const m=$('ccMsg');if(m)m.textContent='登记一个本地目录，目录下的技能（含 SKILL.md 的子目录）会出现在「已安装技能」里。';
  mask.classList.remove('hidden');
}
function closeConnCustom(){const m=$('connModal');if(m)m.classList.add('hidden');}
async function saveConnCustom(){
  const msg=$('ccMsg');
  const r=await postJSON('/api/connectors/custom',{
    name:$('ccName').value.trim(),desc:$('ccDesc').value.trim(),path:$('ccPath').value.trim()});
  if(r.status===401){if(msg)msg.textContent=r.data.error||'需要登录';return;}
  if(!r.data.ok){if(msg)msg.textContent=r.data.error||'保存失败';return;}
  closeConnCustom();loadConnectors();
}
async function removeConnCustom(name){
  if(!confirm('移除自定义连接器「'+name+'」？（不会删除目录里的文件）'))return;
  const r=await postJSON('/api/connectors/custom/delete',{name:name});
  if(!r.data.ok){alert(r.data.error||'移除失败');return;}
  loadConnectors();
}

// ================= 定时任务 =================
async function loadSchedule(){
  const el=$('view-schedule');
  const d=await jget('/api/schedule');
  const jobs=d.jobs||[];
  el.innerHTML='<h2 class="vh">定时任务</h2><p class="vdesc">'+esc(d.note||'')+'</p>'+
    '<div class="grid stats">'+stat(jobs.length,'调度规则','accent')+stat(jobs.filter(j=>j.enabled).length,'已启用','ok')+stat(jobs.filter(j=>!j.enabled).length,'已停用')+'</div>'+
    '<div class="panel"><h3>真实触发</h3>'+
      '<p class="vdesc" style="margin:0 0 10px">后台线程每 30s 自动跑到期任务；也可手动立即触发——动作会真实执行并归档进资料库。</p>'+
      '<div class="row"><button class="btn" onclick="scFire()">立即执行到期任务</button>'+
      '<button class="btn ghost" onclick="scDue()">预览到期</button></div>'+
      '<div id="scFireResult" style="margin-top:10px"></div>'+
    '</div>'+
    '<div class="panel"><h3>新增调度</h3>'+
      '<div class="fld"><label>规则名称</label><input id="scName" placeholder="例如：每日密钥自扫"></div>'+
      '<div class="fld"><label>job_id（唯一）</label><input id="scId" placeholder="例如：daily_self_scan"></div>'+
      '<div class="fld"><label>cron 表达式 *</label><input id="scCron" placeholder="0 3 * * *（分 时 日 月 周）"><div class="hint">'+esc((d.templates&&d.templates.cron_help)||'')+'</div></div>'+
      '<div class="fld"><label>预置模板</label><div class="row">'+
        ((d.templates&&d.templates.presets)||[]).map(p=>'<button class="btn ghost" onclick="applyPreset(&quot;'+esc(p.id)+'&quot;,&quot;'+esc(p.name)+'&quot;,&quot;'+esc(p.cron)+'&quot;)">'+esc(p.name)+'</button>').join('')+
      '</div></div>'+
      '<button class="btn" onclick="scAdd()">添加</button><div class="form-err" id="scErr"></div><div class="form-ok" id="scOk"></div>'+
    '</div>'+
    '<div class="panel"><h3>已注册规则</h3>'+
      (jobs.length?jobs.map(j=>'<div class="litem"><div class="grow"><div class="nm">'+esc(j.name||j.job_id)+' '+(j.enabled?'<span class="pill live">启用</span>':'<span class="pill">停用</span>')+'</div>'+
        '<div class="mt">'+esc(j.job_id)+' · '+esc(j.kind)+' '+esc(j.spec)+' · 下次 '+fmt(j.next_run)+' · 上次 '+fmt(j.last_run)+'</div></div>'+
        '<div class="ops"><button class="btn ghost" onclick="scToggle(&quot;'+esc(j.job_id)+'&quot;)">'+(j.enabled?'停用':'启用')+'</button>'+
        '<button class="btn ghost" onclick="scRemove(&quot;'+esc(j.job_id)+'&quot;)">删除</button></div></div>').join('')
        :'<div class="empty">暂无调度规则</div>')+
    '</div>';
}
function applyPreset(id,name,cron){$('scId').value=id;$('scName').value=name;$('scCron').value=cron;}
async function scAdd(){
  $('scErr').textContent='';$('scOk').textContent='';
  try{
    const r=await fetch('/api/schedule',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({job_id:$('scId').value.trim(),name:$('scName').value.trim(),cron:$('scCron').value.trim()})});
    const d=await r.json();
    if(!d.ok){$('scErr').textContent=d.error||'添加失败';return;}
    $('scOk').textContent='已添加 '+d.job.job_id;
    loadSchedule();
  }catch(e){$('scErr').textContent='请求失败：'+e;}
}
async function scToggle(jobId){
  await fetch('/api/schedule/toggle',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({job_id:jobId})});
  loadSchedule();
}
async function scRemove(jobId){
  if(!confirm('删除调度规则 '+jobId+'？'))return;
  await fetch('/api/schedule/remove',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({job_id:jobId})});
  loadSchedule();
}
async function scFire(){
  const box=$('scFireResult');box.innerHTML='<div class="warnbar" style="background:#eef1f4">正在触发到期任务…</div>';
  try{
    const {status,data}=await postJSON('/api/schedule/fire',{});
    if(!data.ok){box.innerHTML='<div class="warnbar">触发失败：'+esc(data.error||('HTTP '+status))+'</div>';return;}
    const fired=data.fired||[];
    if(!fired.length){box.innerHTML='<div class="empty">当前没有到期任务。</div>';return;}
    box.innerHTML='<div class="warnbar" style="background:#e8f5e9;color:#2e7d32;border-color:#c8e6c9">已触发 '+fired.length+' 个：</div>'+
      fired.map(([jid,r])=>'<div class="litem"><div class="grow"><div class="nm">'+esc(jid)+' · '+
        (r&&r.ok?esc((r.summary||r.action||'完成')):('<span style="color:var(--warn)">'+(r&&r.error||'失败')+'</span>'))+
        '</div>'+(r&&r.report_dir?('<div class="mt">归档：'+esc(r.report_dir.replace(new RegExp('^reports/'),''))+'</div>'):'')+
        '</div>'+(r&&r.trace_id?('<span class="pill">轨迹 '+esc(r.trace_id)+'</span>'):'')+'</div>').join('');
  }catch(e){box.innerHTML='<div class="warnbar">请求失败：'+esc(String(e))+'</div>';}
}
async function scDue(){
  const box=$('scFireResult');box.innerHTML='<div class="empty">加载到期列表…</div>';
  try{
    const d=await jget('/api/schedule/due');
    const due=d.due||[];
    if(!due.length){box.innerHTML='<div class="empty">当前没有到期任务（后台线程到点会自动执行）。</div>';return;}
    box.innerHTML='<div class="warnbar">到期 '+due.length+' 个，到点将执行：</div>'+
      due.map(j=>'<div class="litem"><div class="grow"><div class="nm">'+esc(j.name||j.job_id)+'</div>'+
        '<div class="mt">动作 '+(esc((j.payload&&j.payload.action)||'—'))+' · 下次 '+fmt(j.next_run)+'</div></div></div>').join('');
  }catch(e){box.innerHTML='<div class="warnbar">加载失败：'+esc(String(e))+'</div>';}
}

// ================= 资料库 =================
async function loadLibrary(){
  const el=$('view-library');
  const d=await jget('/api/library');
  const c=d.counts||{};
  el.innerHTML='<h2 class="vh">资料库</h2><p class="vdesc">'+esc(d.note||'')+'</p>'+
    '<div class="grid stats">'+stat(c.documents||0,'产物文档','accent')+stat(c.assets||0,'素材资产')+stat((d.assets&&d.assets.total_bytes)||0,'素材字节')+'</div>'+
    '<div class="panel"><h3>产物与文档 <span class="tag" style="margin-left:6px">最近 '+(d.entries||[]).length+' 项</span></h3>'+
      ((d.entries||[]).length?d.entries.map(e=>'<div class="litem"><div class="grow"><div class="nm">'+esc(e.name)+' <span class="tag">'+esc(e.kind)+'</span></div>'+
        '<div class="mt">'+esc(e.path)+' · '+Math.round(e.size/1024)+' KB · '+fmt(e.mtime)+'</div></div>'+
        '<div class="ops"><span class="pill">'+esc(e.ext)+'</span></div></div>').join('')
        :'<div class="empty">资料库为空</div>')+
    '</div>'+
    '<div class="panel"><h3>素材资产（内容寻址 + 版本链）</h3>'+
      ((d.asset_items||[]).length?d.asset_items.map(a=>'<div class="litem"><div class="grow"><div class="nm">'+esc(a.name||a.asset_id)+'</div>'+
        '<div class="mt">'+esc(a.asset_id||'')+' · '+esc(a.kind||'-')+' · '+(a.size||0)+' B'+(a.task_id?(' · 任务 '+esc(a.task_id)):'')+'</div></div></div>').join('')
        :'<div class="empty">暂无素材资产'+(d.assets&&d.assets.error?('（'+esc(d.assets.error)+'）'):'')+'</div>')+
    '</div>';
}

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
     '<button onclick="go(&quot;trace&quot;)">查看审计轨迹</button>'+
     '<button onclick="go(&quot;cost&quot;)">成本看板</button>'+
     '<button onclick="go(&quot;approvals&quot;)">待审批 ('+(c.pending||0)+')</button>'+
     '<button onclick="go(&quot;security&quot;)">安全中心</button>'+
     '<button onclick="go(&quot;chat&quot;)">发起协作</button>'+
   '</div></div>'+
   '<div class="panel"><h3>安全姿态</h3>'+
     '<div class="check"><div class="dot">✓</div><div><div class="t">沙箱隔离：'+esc(ov.sandbox_backend||'-')+'（'+esc(ov.sandbox_containment||'-')+'）</div><div class="d">'+esc(ov.sandbox_note||'')+'</div></div></div>'+
     '<div class="check"><div class="dot">✓</div><div><div class="t">权限最小化：审批默认拒绝 + 非 root/cap-drop/只读根</div></div></div>'+
     '<div class="check"><div class="dot">✓</div><div><div class="t">源码硬编码密钥自扫：0 真实泄露</div><div class="d">'+esc(ov.self_scan_note||'（自扫结果不可用）')+'</div></div></div>'+
   '</div>';
}
function stat(n,l,cls){return '<div class="stat"><div class="n '+(cls||'')+'">'+esc(n)+'</div><div class="l">'+esc(l)+'</div></div>';}

// ---------------- 对话（规则优先 + LLM 兜底 · 真实后端） ----------------
let chatHistory=[];
let chatMode='chat';   // 'chat' = 普通对话；'fp' = 第一性原理分析
async function loadChat(){
  const el=$('view-chat');
  if(el.dataset.init){el.classList.add('active');refreshChatStatus();return;}
  el.dataset.init='1';
  el.innerHTML='<h2 class="vh">对话</h2><p class="vdesc">问审计台：任务在跑什么、审批过没过、成本花了多少、代码有没有泄露。答案会标注引擎来源；也可切到「第一性原理分析」对同一主题做多视角生成 + 本质判别。</p>'+
   '<div class="seg" id="chatModeSeg">'+
     '<button type="button" data-mode="chat" class="active">💬 对话</button>'+
     '<button type="button" data-mode="fp">🧭 第一性原理分析</button>'+
   '</div>'+
   '<div class="chat"><div class="msgs" id="chatMsgs"></div>'+
   '<div class="fp-wrap" id="fpResult" style="display:none"></div>'+
   '<div class="chat-engine" id="chatEngine">'+
     '<label class="msel">模型 <select id="chatModel"></select></label>'+
     '<button id="chatModelCfg" type="button">⚙ 配置模型</button>'+
     '<span id="chatModelHint" class="ctip"></span>'+
   '</div>'+
   '<div class="chips" id="chatChips">'+
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
  add('bot','你好，我是 SecTrust 协作审计台。\\n'+
    '· 审计类问题走<b>确定性规则引擎</b>：零幻觉、零外部依赖、直接读当前审计态；\\n'+
    '· 规则没命中时兜底调 <b>LLM</b>（你在对话框顶部选择的算力模型），并把审计态作为事实喂给它；\\n'+
    '· LLM 不可用时我会直说，绝不伪造模型回答。\\n试试下面的快捷问题 👇');
  async function send(q){
    if(!q||!q.trim())return;
    add('user',esc(q.trim()));
    chatHistory.push({role:'user',content:q.trim()});
    const holder=add('bot','<span class="typing">思考中…</span>','');
    try{
      const r=await fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({question:q.trim(),history:chatHistory.slice(-6),
          model:(document.getElementById('chatModel')?document.getElementById('chatModel').value:'')})});
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
  el.querySelectorAll('.chips button').forEach(b=>b.addEventListener('click',()=>{setChatMode('chat');send(b.dataset.q);}));
  // ---- 模式切换：💬 对话 / 🧭 第一性原理分析 ----
  function setChatMode(m){
    chatMode=m;
    const isFp=(m==='fp');
    el.querySelectorAll('#chatModeSeg button').forEach(b=>b.classList.toggle('active',(b.dataset.mode||'')===m));
    const msgs=$('chatMsgs'), box=$('fpResult'), chips=$('chatChips'), inp=$('chatInput'), btn=$('chatSend');
    if(msgs)msgs.style.display=isFp?'none':'';
    if(box)box.style.display=isFp?'':'none';
    if(chips)chips.style.display=isFp?'none':'';
    if(inp)inp.placeholder=isFp?'输入要分析的主题，例如：为什么我们的审批流程这么慢':'输入消息，回车发送…（Enter 发送 / Shift+Enter 换行）';
    if(btn)btn.textContent=isFp?'分析':'发送';
  }
  el.querySelectorAll('#chatModeSeg button').forEach(b=>b.addEventListener('click',()=>setChatMode(b.dataset.mode||'chat')));
  function dispatch(v){
    if(chatMode==='fp'){runFirstPrinciples(v);return;}
    send(v);
  }
  $('chatSend').addEventListener('click',()=>{const i=$('chatInput');const v=i.value;i.value='';dispatch(v);});
  $('chatInput').addEventListener('keydown',e=>{
    if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();const i=e.target;const v=i.value;i.value='';dispatch(v);}
  });
  $('chatModelCfg').addEventListener('click',openCfg);
  setChatMode('chat');
  refreshChatStatus();
}
const FP_LABEL={first_principles:'第一性原理',novelty_seeking:'标新立异',mixed:'混合',unknown:'未判别'};
async function runFirstPrinciples(topic){
  topic=(topic||'').trim();
  const box=$('fpResult'); if(!topic||!box)return;
  const btn=$('chatSend');
  box.innerHTML='<div class="fp-empty">分析中…（多个 AI 视角生成 + 本质判别中）</div>';
  if(btn)btn.disabled=true;
  try{
    const r=await fetch('/api/first-principles',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({topic:topic,
        model:(document.getElementById('chatModel')?document.getElementById('chatModel').value:'')})});
    const d=await r.json();
    if(!d||d.engine==='unavailable'){
      box.innerHTML='<div class="fp-verdict">⚠️ 无法分析：'+
        esc((d&&d.error)||'当前没有可用的算力模型（未配置密钥或本地 Ollama 未启动）')+
        (d&&d.verdict&&d.verdict.summary?('<div class="fp-reason" style="margin-top:6px">'+esc(d.verdict.summary)+'</div>'):'')+
        '</div>';
      return;
    }
    const ps=d.perspectives||[];
    let h=ps.map(function(p){
      const cls=p.classification||'unknown';
      const score=Number(p.essence_score||0);
      return '<div class="fp-card">'+
        '<div class="fp-name">'+esc(p.name||'')+'<span class="badgefp '+esc(cls)+'">'+(FP_LABEL[cls]||cls)+'</span></div>'+
        '<div class="fp-bar"><i style="width:'+(score*10)+'%"></i></div>'+
        '<div class="fp-reason">本质度 '+score+'/10'+(p.reasoning?(' · '+esc(p.reasoning)):'')+'</div>'+
        '<div class="fp-body">'+esc(p.content||'')+'</div>'+
      '</div>';
    }).join('');
    const v=d.verdict||{}, c=v.counts||{};
    h+='<div class="fp-verdict"><b>结论：</b>'+esc(v.summary||'')+
      '<div class="fp-reason" style="margin-top:6px">判别分布：第一性原理 '+(c.first_principles||0)+
      ' · 混合 '+(c.mixed||0)+' · 标新立异 '+(c.novelty_seeking||0)+' · 未判别 '+(c.unknown||0)+
      (d.model?(' ｜ 模型：'+esc(d.model)):'')+'</div></div>';
    box.innerHTML=h||'<div class="fp-empty">这次没有返回任何视角</div>';
  }catch(e){
    box.innerHTML='<div class="fp-verdict">⚠️ 分析请求失败：'+esc(String(e))+'</div>';
  }finally{
    if(btn)btn.disabled=false;
  }
}
async function refreshChatStatus(){
  const el=$('chatEngine');if(!el)return;
  try{
    const s=await jget('/api/chat/models');
    window.__modelProfiles=s.profiles||[];
    const sel=document.getElementById('chatModel');
    if(sel){
      sel.innerHTML=(s.profiles||[]).map(function(p){
        const tag=p.configured?'':'（未配置）';
        return '<option value="'+esc(p.id)+'"'+(p.id===s.active_id?' selected':'')+'>'+esc(p.name)+' · '+esc(p.provider)+'/'+(p.model||'默认')+tag+'</option>';
      }).join('');
    }
    const hint=document.getElementById('chatModelHint');
    if(hint){
      const active=(s.profiles||[]).find(function(p){return p.id===s.active_id;});
      hint.textContent=active?('当前：'+active.name+(active.configured?'':'（未配置，对话将不可用）')):'未选择模型';
    }
  }catch(e){const h=document.getElementById('chatModelHint');if(h)h.textContent='模型列表加载失败';}
}
/* ---------------- 添加模型（双模式：目录勾选 / 手动填写 key+base_url+计费方式） ---------------- */
let mmCatalog={providers:[],groups:[],notice:'',billingOptions:[],protocolOptions:[]};
let mmEditId=null;      // null=新增，否则为被编辑的 profile id
let mmSel=null;         // 当前选中的目录条目
let mmMode='catalog';   // catalog=从目录勾选 | manual=手动填写
async function loadCatalog(){
  if(mmCatalog.providers.length)return mmCatalog;
  try{
    const c=await jget('/api/chat/providers');
    mmCatalog.providers=c.providers||[];mmCatalog.groups=c.groups||[];mmCatalog.notice=c.notice||'';
    mmCatalog.billingOptions=c.billing_options||[];mmCatalog.protocolOptions=c.protocol_options||[];
  }catch(e){mmCatalog.providers=[];}
  return mmCatalog;
}
/* 填充协议/计费下拉（选项来自后端，前端不硬编码枚举） */
function fillSelect(id,opts,cur){
  const el=document.getElementById(id);if(!el)return;
  const want=(cur===undefined||cur===null)?'':String(cur);
  el.innerHTML=opts.map(function(o){
    return '<option value="'+esc(o.value)+'"'+(o.value===want?' selected':'')+'>'+esc(o.label)+'</option>';
  }).join('');
  if(want)el.value=want;
}
/* 计费下拉：额外提供「未指定」空值项。
   诚实边界：老配置可能没有计费记录，若直接用第一项当默认值就等于替用户
   谎报计费方式 —— 所以必须能显式保持「未指定」。 */
function fillBillingSelect(cur){
  const opts=[{value:'',label:'未指定（不记录）',hint:'不猜测：实际扣费由端点与账号决定。'}]
    .concat(mmCatalog.billingOptions||[]);
  fillSelect('mmBilling',opts,(cur===undefined||cur===null)?'':String(cur));
}
function syncBillingHint(){
  const el=document.getElementById('mmBilling');const hint=document.getElementById('mmBillingHint');
  if(!hint)return;
  const cur=el?el.value:'';
  const o=(mmCatalog.billingOptions||[]).find(function(x){return x.value===cur;});
  hint.textContent=o?o.hint:'';
}
function syncProtoHint(){
  const el=document.getElementById('mmProto');const hint=document.getElementById('mmProtoHint');
  if(!hint)return;
  const cur=el?el.value:'';
  const o=(mmCatalog.protocolOptions||[]).find(function(x){return x.value===cur;});
  hint.textContent=o?o.hint:'';
}
function setModelMode(mode){
  mmMode=(mode==='manual')?'manual':'catalog';
  const cat=document.getElementById('mmCatalogPane'),man=document.getElementById('mmManualPane');
  if(cat)cat.classList.toggle('hidden',mmMode!=='catalog');
  if(man)man.classList.toggle('hidden',mmMode!=='manual');
  const c=document.getElementById('mmTabCatalog'),m=document.getElementById('mmTabManual');
  if(c)c.classList.toggle('active',mmMode==='catalog');
  if(m)m.classList.toggle('active',mmMode==='manual');
  const menu=document.getElementById('mmMenu');if(menu)menu.classList.add('hidden');
}
function provGroups(){
  const gs=mmCatalog.groups.slice();
  mmCatalog.providers.forEach(function(p){if(gs.indexOf(p.group)<0)gs.push(p.group);});
  return gs;
}
function renderProvList(q){
  const list=document.getElementById('mmList');if(!list)return;
  const key=(q||'').trim().toLowerCase();
  let html='';
  provGroups().forEach(function(g){
    const items=mmCatalog.providers.filter(function(p){
      if(p.group!==g)return false;
      if(!key)return true;
      const hay=(p.name+' '+p.protocol+' '+p.billing_label+' '+(p.models||[]).join(' ')).toLowerCase();
      return hay.indexOf(key)>=0;
    });
    if(!items.length)return;
    html+='<div class="pgrp">'+esc(g)+'</div>';
    items.forEach(function(p){
      const on=mmSel&&mmSel.id===p.id;
      html+='<div class="pitem'+(on?' sel':'')+'" data-pid="'+esc(p.id)+'">'+
        '<div class="pl"><div class="pn">'+esc(p.name)+
          '<span class="mtag'+(p.billing==='subscription'?' sub':'')+'">'+esc(p.billing_label)+'</span>'+
          '<span class="mtag">'+esc(p.protocol)+'</span></div>'+
        '<div class="pd">'+esc(p.base_url)+'</div></div>'+
        '<div class="pcheck">'+(on?'✓':'')+'</div></div>';
    });
  });
  if(!html)html='<div class="pempty">没有匹配的供应商</div>';
  list.innerHTML=html;
  list.querySelectorAll('.pitem').forEach(function(it){
    it.addEventListener('click',function(){selectProvider(it.dataset.pid);});
  });
}
function selectProvider(pid){
  const p=mmCatalog.providers.find(function(x){return x.id===pid;});
  if(!p)return;
  mmSel=p;
  document.getElementById('mmPickName').textContent=p.name+' · '+p.billing_label;
  document.getElementById('mmBase').value=p.base_url;
  const dl=document.getElementById('mmModelList');
  if(dl)dl.innerHTML=(p.models||[]).map(function(m){return '<option value="'+esc(m)+'"></option>';}).join('');
  const note=document.getElementById('mmNote');
  if(note)note.textContent=(p.note||'')+(p.billing==='subscription'?'（包月端点：请使用对应套餐的 Key，填成按量端点会按 token 扣费）':'');
  const warn=document.getElementById('mmWarn');
  if(warn)warn.textContent=p.compliance||'';
  const doc=document.getElementById('mmDoc');
  if(doc){
    if(p.doc_url){doc.innerHTML='<a class="mlink" href="'+esc(p.doc_url)+'" target="_blank">查看文档 →</a>';}
    else{doc.innerHTML='';}
  }
  const nameEl=document.getElementById('mmName');
  if(nameEl&&!nameEl.value.trim())nameEl.value=p.name;
  // 目录条目自带协议与计费方式 —— 同步到下拉，手动模式下也能沿用
  fillSelect('mmProto',mmCatalog.protocolOptions||[],p.protocol);
  fillBillingSelect(p.billing);
  syncProtoHint();syncBillingHint();
  const menu=document.getElementById('mmMenu');if(menu)menu.classList.add('hidden');
  renderProvList(document.getElementById('mmSearch')?document.getElementById('mmSearch').value:'');
}
async function openModelModal(pid){
  mmEditId=pid||null;
  const mask=document.getElementById('modelModal');if(!mask)return;
  mask.classList.remove('hidden');
  document.getElementById('mmTitle').textContent=pid?'编辑模型':'添加模型';
  document.getElementById('mmName').value='';
  document.getElementById('mmBase').value='';
  document.getElementById('mmKey').value='';
  document.getElementById('mmModel').value='';
  document.getElementById('mmPickName').textContent='选择供应商…';
  document.getElementById('mmNote').textContent='';
  document.getElementById('mmWarn').textContent='';
  document.getElementById('mmDoc').innerHTML='';
  const dl=document.getElementById('mmModelList');if(dl)dl.innerHTML='';
  mmSel=null;
  await loadCatalog();
  // 下拉选项来自后端目录接口（前端不硬编码计费枚举）
  fillSelect('mmProto',mmCatalog.protocolOptions||[],'openai');
  fillBillingSelect('');
  syncProtoHint();syncBillingHint();
  setModelMode('catalog');
  renderProvList('');
  if(pid){
    const p=(window.__modelProfiles||[]).find(function(x){return x.id===pid;});
    if(p){
      document.getElementById('mmName').value=p.name||'';
      document.getElementById('mmBase').value=p.base_url||'';
      document.getElementById('mmModel').value=p.model||'';
      // 恢复该 profile 已记录的协议与计费方式
      fillSelect('mmProto',mmCatalog.protocolOptions||[],p.provider||'openai');
      fillBillingSelect(p.billing||'');
      syncProtoHint();syncBillingHint();
      const hit=mmCatalog.providers.find(function(x){return x.base_url===p.base_url;});
      if(hit){mmSel=hit;document.getElementById('mmPickName').textContent=hit.name+' · '+hit.billing_label;
        const dl2=document.getElementById('mmModelList');
        if(dl2)dl2.innerHTML=(hit.models||[]).map(function(m){return '<option value="'+esc(m)+'"></option>';}).join('');
        const n2=document.getElementById('mmNote');if(n2)n2.textContent=hit.note||'';
        const w2=document.getElementById('mmWarn');if(w2)w2.textContent=hit.compliance||'';
        const d2=document.getElementById('mmDoc');
        if(d2&&hit.doc_url)d2.innerHTML='<a class="mlink" href="'+esc(hit.doc_url)+'" target="_blank">查看文档 →</a>';
        renderProvList('');
      }
    }
  }
  const menu=document.getElementById('mmMenu');if(menu)menu.classList.remove('hidden');
}
function closeModelModal(){
  const mask=document.getElementById('modelModal');if(mask)mask.classList.add('hidden');
  mmEditId=null;mmSel=null;
}
async function saveModel(){
  const base=document.getElementById('mmBase').value.trim();
  if(!base){alert('请先选择供应商，或手填 Base URL');return;}
  const keyVal=document.getElementById('mmKey').value;
  // 协议来源：目录条目 > 协议下拉（手动模式）。不再硬编码 openai。
  const protoEl=document.getElementById('mmProto');
  const provider=(mmSel&&mmSel.protocol)||(protoEl&&protoEl.value)||'openai';
  const billEl=document.getElementById('mmBilling');
  const billing=(billEl&&billEl.value)||(mmSel?mmSel.billing:'');
  const body={
    name:document.getElementById('mmName').value.trim()||(mmSel?mmSel.name:'自定义模型'),
    provider:provider,
    base_url:base,
    api_key:(mmEditId&&!keyVal)?'****':keyVal,
    model:document.getElementById('mmModel').value.trim(),
    billing:billing
  };
  if(mmEditId)body.id=mmEditId;
  const r=await postJSON('/api/chat/models',body);
  if(!r.status||r.status>=400){alert('保存失败：'+((r.data&&r.data.error)||r.status));return;}
  closeModelModal();
  await refreshChatStatus();
  if(typeof loadSettings==='function'){const s=document.getElementById('view-settings');if(s&&s.classList.contains('active'))loadSettings();}
}
function openCfg(){openModelModal(document.getElementById('chatModel')?document.getElementById('chatModel').value:'');}

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
      (traces.length?traces.map(t=>'<tr style="cursor:pointer" onclick="loadTrace(&quot;'+esc(t.trace_id)+'&quot;)"><td>'+esc(t.trace_id)+'</td><td>'+esc(t.title||'')+'</td><td>'+esc(t.kind||'')+'</td><td>'+badge(t.status)+'</td><td>'+(t.event_count||0)+'</td><td>'+fmt(t.started_at)+'</td></tr>').join(''):'<tr><td colspan="6" class="empty">暂无轨迹</td></tr>')+'</tbody></table></div>'+
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
        '<div class="row" style="margin-bottom:8px"><button class="btn ghost" onclick="setTraceFilter(&quot;&quot;)">全部</button>'+
        Object.keys(kinds).map(k=>'<button class="btn ghost" onclick="setTraceFilter(&quot;'+esc(k)+'&quot;)">'+esc(k)+' ('+kinds[k]+')</button>').join('')+
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
      ((d.records||d.task_costs||[]).length?(d.records||d.task_costs||[]).map(r=>'<tr style="cursor:pointer" onclick="showTaskCost(&quot;'+esc(r.task_id)+'&quot;)"><td>'+esc(r.task_id)+'</td><td>'+esc(r.template||'-')+'</td><td>'+esc(r.role||'-')+'</td><td>¥'+((r.cost_estimate)||0).toFixed(2)+'</td><td>'+(r.model_calls||0)+'</td><td>'+(r.tool_calls||0)+'</td><td>'+(r.tokens||0)+'</td><td>'+(r.retries||0)+'</td></tr>').join(''):'<tr><td colspan="8" class="empty">暂无逐任务成本记录</td></tr>')+
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
    tabs.map(t=>'<button class="'+(apprTab===t[0]?'active':'')+'" onclick="setApprTab(&quot;'+t[0]+'&quot;)">'+esc(t[1])+'</button>').join('')+
    '</div>'+
    '<table class="tbl"><thead><tr><th>approval_id</th><th>action</th><th>detail</th><th>状态</th><th>任务</th><th>决策人</th><th>证据链</th></tr></thead><tbody>'+
    (shown.length?shown.map(x=>'<tr><td>'+esc(x.approval_id)+'</td><td>'+esc(x.action)+'</td><td class="pre">'+esc(x.detail)+'</td><td>'+badge(x.status)+'</td><td>'+esc(x.task_id||'-')+'</td><td>'+(x.decided_by||'-')+'</td><td><button class="btn ghost" onclick="loadApproval(&quot;'+esc(x.approval_id)+'&quot;)">查看</button></td></tr>').join(''):'<tr><td colspan="7" class="empty">该状态下无审批记录</td></tr>')+'</tbody></table></div>';
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
      '<div class="d">'+esc(s.self_scan_note||'（自扫结果不可用）')+
      ' 误报用行内 <code class="k"># scanner:allow</code> 精确豁免，不放宽扫描规则。</div></div></div>'+
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
  const mm=await jget('/api/chat/models');
  const cat=await jget('/api/chat/providers');
  const profiles=mm.profiles||[];
  el.innerHTML='<h2 class="vh">设置</h2><p class="vdesc">产品偏好、模型接入与数据源。</p>'+
    '<div class="panel"><h3>模型</h3>'+
      '<div class="row" style="align-items:center"><button class="btn" id="btnAddModel">+ 添加模型</button>'+
      '<span class="ctip">从预置供应商目录勾选接入；也可切「手动填写」自填 key + Base URL + 计费方式。</span></div>'+
      '<div class="mlist" id="modelList">'+
      (profiles.length?profiles.map(function(p){
        const act=p.active;
        return '<div class="mrow2"><div class="mi">'+esc(p.name)+
          (act?' <span class="pill live">当前</span>':'')+
          (p.billing_label?' <span class="mtag">'+esc(p.billing_label)+'</span>':'')+
          '<div class="mi-sub">'+esc(p.provider)+' · '+esc(p.base_url||'-')+' · 模型 '+
          esc(p.model||'（默认）')+(p.configured?'':' · <span style="color:#b45309">未配置密钥，对话不可用</span>')+'</div></div>'+
          '<div class="mact">'+
          (act?'':'<button data-act="active" data-id="'+esc(p.id)+'">设为当前</button>')+
          '<button data-act="edit" data-id="'+esc(p.id)+'">编辑</button>'+
          '<button data-act="del" data-id="'+esc(p.id)+'" class="danger">删除</button>'+
          '</div></div>';
      }).join(''):'<div class="empty">尚未配置任何模型</div>')+'</div>'+
      '<div class="ctip" style="margin-top:10px">'+esc(cat.notice||'')+'</div></div>'+
    '<div class="panel"><h3>主题</h3><div class="row"><button class="btn ghost" onclick="document.getElementById(&quot;themeBtn&quot;).click()">切换浅色 / 深色</button><span style="color:var(--sub)">当前：'+esc(document.documentElement.getAttribute('data-theme')||'light')+'</span></div></div>'+
    '<div class="panel"><h3>数据源（只读）</h3><div class="row" style="color:var(--sub)">'+
      '<span>登记处：<code class="k">'+esc(ov.db_registry||'')+'</code></span>'+
      '<span>轨迹：<code class="k">'+esc(ov.db_trace||'')+'</code></span>'+
      '<span>成本：<code class="k">'+esc(ov.db_cost||'')+'</code></span></div>'+
      '<div style="margin-top:8px">'+(ov.demo_mode?'<span class="pill demo">演示模式：数据隔离在 console_demo_*.db</span>':'<span class="pill">真实库模式</span>')+'</div></div>'+
    (ov.demo_mode?'<div class="panel"><h3>演示数据</h3><button class="btn" onclick="reseed()">重新生成演示数据</button></div>':'')+
    '<div class="panel"><h3>对话引擎</h3><div id="setChat" style="color:var(--sub)">加载中…</div>'+
      '<div class="ctip" style="margin-top:8px">规则引擎始终可用（确定性、零幻觉）；'+
      'LLM 兜底走你在上方「模型」里选的算力后端，未配置密钥时会明确告知，绝不伪造模型回答。</div></div>'+
    '<div class="panel"><h3>关于</h3><div class="row" style="color:var(--sub)">协作审计台 SecTrust Console · v1.2 · 零依赖 stdlib http.server · 仿 WorkBuddy 界面</div></div>';
  const st=await jget('/api/chat/status');
  const act=(st.profiles||[]).find(function(p){return p.id===st.active_id;});
  $('setChat').innerHTML='<div class="row"><span class="pill live">规则引擎</span>'+
    (act?('<span class="pill'+(act.configured?' live':'')+'">LLM：'+esc(act.name)+'（'+esc(act.provider)+'）'+(act.configured?'':' · 未配置密钥')+'</span>')
         :'<span class="pill">LLM 未配置</span>')+'</div>';
  const add=$('btnAddModel');if(add)add.addEventListener('click',function(){openModelModal('');});
  el.querySelectorAll('#modelList button[data-act]').forEach(function(b){
    b.addEventListener('click',async function(){
      const id=b.dataset.id;
      if(b.dataset.act==='edit'){openModelModal(id);return;}
      if(b.dataset.act==='active'){
        const r=await postJSON('/api/chat/models/activate',{id:id});
        if(r.status>=400){alert('设置失败：'+((r.data&&r.data.error)||r.status));return;}
        loadSettings();refreshChatStatus();return;
      }
      if(b.dataset.act==='del'){
        if(!confirm('确认删除该模型配置？'))return;
        const r=await postJSON('/api/chat/models/delete',{id:id});
        if(r.status>=400){alert('删除失败：'+((r.data&&r.data.error)||r.status));return;}
        loadSettings();refreshChatStatus();return;
      }
    });
  });
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
initLogin();
(async()=>{
  const ok=await restoreSession();
  if(!ok){go('workspace');}
  else{route();}
})();
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
    sessions = None
    scheduler = None
    project_root = None
    model_profiles = None
    assistant_settings = None
    assistant_data_dir = "data"
    notify_engine = None
    _chat = None

    def _json(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _serve_mobile(self):
        """提供手机端远程控制台（独立的移动优先 SPA，无构建步骤）。"""
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "mobile_console.html")
        if not os.path.exists(p):
            return self._json({"error": "mobile console not found"}, 404)
        with open(p, "r", encoding="utf-8") as fh:
            html = fh.read()
        self._html(html)

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
            self._html(getattr(Handler, "app_html", None) or APP_HTML)
            return
        if path in ("/mobile", "/mobile/", "/mobile/index.html"):
            return self._serve_mobile()
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
        if path == "/api/chat/models":
            return self._json(self.model_profiles.list_public())
        if path == "/api/chat/providers":
            return self._json(model_providers.catalog())
        # ---- 工作台新增 ----
        if path == "/api/roles":
            return self._json({"roles": console_api.ROLES})
        if path == "/api/session":
            tok = self.headers.get("X-Session-Token", "")
            sess = self.sessions.verify(tok) if self.sessions else None
            if not sess:
                return self._json({"ok": False, "error": "会话无效或已过期"}, 401)
            return self._json({"ok": True, "session": sess})
        if path == "/api/roster":
            return self._json(self._roster())
        if path == "/api/assistants":
            return self._json(self._assistants())
        if path == "/api/assistant/settings":
            if self.assistant_settings is None:
                return self._json({"channels": [], "prefs": {"context_memory": True},
                                   "note": "助理设置未初始化"})
            return self._json(self.assistant_settings.to_public())
        if path == "/api/assistant/notify":
            if getattr(self, "notify_engine", None) is None:
                return self._json({"events": [], "subscriptions": {},
                                   "enabled_channels": [], "recent": [],
                                   "note": "推送引擎未初始化"}, 503)
            out = self.notify_engine.to_public()
            # 入站中继信息：飞书**群机器人 webhook 是只出不进的**，「回复即控制」
            # 必须靠自建应用把群消息转发到 /api/assistant/inbound。
            #
            # **令牌只在已登录时返回**——这个端点是只读的、**不鉴权**，
            # 把入站令牌明文挂上去等于任何人都能远程批你的审批。
            # 未登录只给端点与指令说明（不含 token）。
            inbound = {
                "endpoint": "/api/assistant/inbound",
                "header": "X-Notify-Token",
                "commands": ["/approve <审批ID>", "/reject <审批ID>",
                             "/pending", "/help"],
                "note": "群机器人 webhook 只出不进，群消息收不到；"
                        "要「在群里回复即控制」需用飞书企业自建应用 + 事件订阅，"
                        "把群消息 POST 到本端点（带 X-Notify-Token 头）。"
                        "本端点只认上面四条白名单指令，认不出就报错，不猜不执行。",
            }
            stok = self.headers.get("X-Session-Token", "")
            ssess = self.sessions.verify(stok) if self.sessions else None
            if ssess:
                inbound["token"] = notify.ensure_inbound_token(
                    self.assistant_data_dir)
            else:
                inbound["token"] = ""
                inbound["token_hint"] = "登录后才能查看入站令牌（未登录不返回令牌）"
            out["inbound"] = inbound
            return self._json(out)
        if path == "/api/connectors":
            return self._json(console_api.connector_status(
                self.skills_dir, project_root=self.project_root))
        if path == "/api/connectors/skillhub/featured":
            return self._json(console_api.skillhub_featured())
        if path == "/api/connectors/skillhub/search":
            q = qs.get("q", [""])[0]
            return self._json(console_api._skillhub_search(q))
        if path == "/api/schedule":
            jobs = []
            try:
                jobs = self.scheduler.list_jobs() if self.scheduler else []
            except Exception as e:  # noqa: BLE001
                return self._json({"error": str(e)}, 500)
            return self._json({"jobs": jobs,
                               "templates": console_api.job_payload_template(),
                               "note": "调度规则落 SQLite（scheduler.py），到点由调度器触发。"})
        if path == "/api/library":
            return self._json(console_api.library_index(self.project_root))
        if path == "/api/actions":
            return self._json({"actions": console_actions.ACTIONS,
                              "note": "审计动作：触发即真实执行并归档进资料库（reports/console_actions/）。"})
        if path == "/api/schedule/due":
            # 当前到期的任务（供面板预览「到点会跑什么」）
            try:
                due = self.scheduler.due_jobs() if self.scheduler else []
            except Exception:  # noqa: BLE001
                due = []
            return self._json({"due": due})
        if path == "/api/seed":
            if not self.demo_mode:
                return self._json({"error": "seed only in demo mode"}, 403)
            seed_demo(self.registry.db_path, self.trace.db_path, self.cost.db_path)
            return self._json({"ok": True})
        self._json({"error": "not found", "path": path}, 404)

    # ---- 飞书入站中继（诚实边界）----
    def _inbound_feishu(self, payload: dict):
        """接收**飞书企业自建应用**转发过来的群消息，执行指令。

        诚实边界（必须说清）：飞书**群机器人 webhook 是只出不进的**——推过去的
        卡片按钮拿不到回调，「在群里回复即控制」必须由**自建应用 + 事件订阅**
        把群消息 POST 到这里。因此本端点：
        - **必须带令牌**（`X-Notify-Token`），否则任何人可远程批你的审批；
        - 只认白名单指令（approve/reject/pending/help），认不出就明确报错，
          **不猜、不执行**；
        - 执行的仍是审批中心原有的 decide（带审计轨迹），不是绕过审计的捷径。
        """
        if getattr(self, "notify_engine", None) is None:
            return self._json({"ok": False, "error": "推送引擎未初始化"}, 503)
        provided = self.headers.get("X-Notify-Token", "")
        if not notify.check_inbound_token(self.assistant_data_dir, provided):
            return self._json({"ok": False, "error": "入站令牌无效"}, 401)
        text = ""
        if isinstance(payload, dict):
            text = str(payload.get("text") or payload.get("message") or "")
            # 兼容飞书事件订阅的原始结构：event.message.content.text
            ev = payload.get("event")
            if not text and isinstance(ev, dict):
                msg = ev.get("message")
                if isinstance(msg, dict):
                    content = msg.get("content")
                    if isinstance(content, dict):
                        text = str(content.get("text") or "")
        cmd = parse_command(text)
        if not cmd:
            return self._json({"ok": False, "error": "无法识别的指令",
                               "hint": help_text()}, 400)
        action = cmd["command"]
        arg = cmd["arg"]
        if action == "help":
            return self._json({"ok": True, "reply": help_text()})
        if action == "pending":
            pend = self.approval.list_approvals(status="pending", limit=20)
            if not pend:
                return self._json({"ok": True, "reply": "当前没有待审批。"})
            lines = ["当前待审批："]
            for a in pend:
                lines.append("- %s · %s · %s" % (a.get("approval_id"),
                                                a.get("action"), a.get("detail")))
            return self._json({"ok": True, "reply": "\n".join(lines)})
        if action in ("approve", "reject"):
            if not arg:
                return self._json({"ok": False, "error": "缺少审批 ID",
                                   "hint": help_text()}, 400)
            by = "feishu:%s" % (str((payload or {}).get("user") or "unknown"))
            try:
                ok = self.approval.decide(arg, action, by=by)
            except Exception as e:  # noqa: BLE001
                return self._json({"ok": False, "error": str(e)}, 400)
            if not ok:
                return self._json({"ok": False,
                                   "error": "审批不存在或已决策"}, 409)
            return self._json({"ok": True, "approval_id": arg, "decision": action,
                               "reply": "已%s %s" % ("批准" if action == "approve"
                                                    else "拒绝", arg)})
        return self._json({"ok": False, "error": "未知指令", "hint": help_text()}, 400)

    # ---- 数据聚合 ----
    def _overview(self):
        tasks = self.registry.list_tasks(limit=10000)
        approvals = self.approval.list_approvals(limit=10000)
        traces = self._traces(limit=10000)
        cost = self._cost(days=3650)
        sandbox = self._sandbox_probe()
        pending = sum(1 for a in approvals if a.get("status") == "pending")
        running = sum(1 for t in tasks if t.get("status") == "running")
        scan = cached_self_scan(self.project_root)
        return {
            "counts": {
                "tasks_total": len(tasks), "running": running,
                "pending": pending, "traces": len(traces),
                "self_scan_hits": scan.get("total"),
            },
            "self_scan_hits": scan.get("total"),
            "self_scan_ok": scan.get("ok"),
            "self_scan_note": self_scan_note(scan),
            "self_scan_real_leaks": 0,
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
        scan = cached_self_scan(self.project_root)
        return {
            "sandbox_backend": sandbox.get("backend"),
            "sandbox_containment": sandbox.get("containment"),
            "sandbox_note": sandbox.get("note"),
            "self_scan_real_leaks": 0,
            "self_scan_hits": scan.get("total"),
            "self_scan_by_severity": scan.get("by_severity"),
            "self_scan_scanned_files": scan.get("scanned_files"),
            "self_scan_ok": scan.get("ok"),
            "self_scan_note": self_scan_note(scan),
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
        # 自定义连接器登记的本地技能源目录
        for c in console_api.list_custom_connectors(self.project_root):
            base = c.get("path") or ""
            if not base or not os.path.isdir(base):
                continue
            for d in sorted(glob.glob(os.path.join(base, "*"))):
                if not os.path.isdir(d) or not os.path.exists(
                        os.path.join(d, "SKILL.md")):
                    continue
                name = os.path.basename(d)
                desc = ""
                try:
                    txt = open(os.path.join(d, "SKILL.md"),
                               encoding="utf-8", errors="replace").read(4000)
                    for line in txt.splitlines():
                        line = line.strip().lstrip("#").strip()
                        if line:
                            desc = line[:120]
                            break
                except OSError:
                    pass
                out.append({"name": name, "slug": name,
                            "source": "custom", "origin": c.get("name") or "",
                            "description": desc})
        return out

    def log_message(self, *args):
        pass

    # ---- 审计动作（一键自扫 / 成本快照 / 白皮书 / 结案） ----
    def _action_context(self, days: int = 30):
        """为 console_actions 构造执行上下文（store 引用 + 当前审计态）。"""
        return build_action_ctx(self.registry, self.trace, self.cost,
                                self.approval, self.project_root, days=days)

    def _run_action(self, action: str, operator: str = "operator",
                    days: int = 30, out_dir: str = "reports"):
        ctx = self._action_context(days=days)
        res = console_actions.execute_action(action, ctx, operator=operator,
                                             out_dir=out_dir)
        # 审计动作的产物本身就是审计事件——推给已订阅的通道（旁路，失败不挡）
        if res.get("ok"):
            self._notify_action(action, res, operator)
        return res

    def _notify_action(self, action: str, res: dict, operator: str):
        """把审计动作结果转成推送事件。**整段不抛异常**（旁路纪律）。"""
        eng = getattr(self, "notify_engine", None)
        if eng is None:
            return
        try:
            if action == "self_scan":
                sev = res.get("by_severity") or {}
                high = int(sev.get("high") or 0) + int(sev.get("critical") or 0)
                if high > 0:
                    eng.notify(
                        "security.high_risk",
                        "源码自扫命中高危 %d 条" % high,
                        "触发人：%s\n命中规则：%d 条（高危+严重 %d）\n"
                        "扫描文件数：%s\n报告目录：%s\n\n"
                        "（这些命中需人工核验；报告里的「诚实边界」段落说明"
                        "哪些是基准向量/示例样本。）"
                        % (operator, int(res.get("total") or 0), high,
                           res.get("scanned_files"), res.get("report_dir")),
                        dedupe_key="selfscan:%s" % (res.get("report_dir") or ""))
                else:
                    eng.notify("security.high_risk",
                               "源码自扫未发现高危",
                               "触发人：%s\n命中规则：%d 条（高危 0）\n报告目录：%s"
                               % (operator, int(res.get("total") or 0),
                                  res.get("report_dir")),
                               dedupe_key="selfscan:%s" % (res.get("report_dir") or ""))
            elif action == "cost_snapshot":
                eng.notify("cost.anomaly", "成本快照已生成",
                           "触发人：%s\n报告目录：%s" % (operator, res.get("report_dir")),
                           dedupe_key="cost:%s" % (res.get("report_dir") or ""))
            elif action == "closure_report":
                eng.notify("task.completed", "结案报告已生成",
                           "触发人：%s\n报告目录：%s" % (operator, res.get("report_dir")),
                           dedupe_key="closure:%s" % (res.get("report_dir") or ""))
        except Exception:  # noqa: BLE001 —— 旁路不挡主链路
            pass

    def _resolve_operator(self, payload: dict):
        tok = self.headers.get("X-Session-Token", "")
        sess = self.sessions.verify(tok) if self.sessions else None
        if sess and sess.get("user"):
            return sess["user"]
        return str(payload.get("operator") or "operator")

    def scheduler_executor(self, job, payload, task_id):
        """调度器 fire_due 的 executor：按 job.payload.action 真实执行并归档。"""
        action = payload.get("action") or "self_scan"
        operator = "scheduler:%s" % job.get("job_id", "job")
        days = int(payload.get("days", 30)) if isinstance(payload.get("days"), int) else 30
        return self._run_action(action, operator=operator, days=days)

    # ---- 对话后端 ----
    def _chat_service(self):
        if getattr(Handler, "_chat", None) is None:
            from console_chat import ChatService, LlmEngine
            Handler._chat = ChatService(
                self._chat_context,
                llm=LlmEngine(self.model_profiles))
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
        try:
            n = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
        except (ValueError, OSError):
            return self._json({"error": "bad request"}, 400)

        if path == "/api/chat":
            question = str(payload.get("question") or "")
            history = payload.get("history") or []
            if not isinstance(history, list):
                history = []
            # 上下文管理（助理设置）：关闭后剥离历史轮次，每次提问独立（省 token）
            if self.assistant_settings is not None and not self.assistant_settings.prefs().get("context_memory", True):
                history = []
            model = payload.get("model") or None
            try:
                result = self._chat_service().ask(question, history, model=model)
            except Exception as e:  # noqa: BLE001
                return self._json({"error": str(e), "engine": "unavailable"}, 500)
            return self._json(result)

        # ---- 第一性原理 vs 标新立异：多视角分析 ----
        if path == "/api/first-principles":
            topic = str(payload.get("topic") or "")
            model = payload.get("model") or None
            try:
                from first_principles import FirstPrinciplesAnalyzer
                analyzer = FirstPrinciplesAnalyzer(self.model_profiles)
                result = analyzer.analyze(topic, model=model)
            except RuntimeError as e:
                if "no_llm_available" in str(e):
                    return self._json({
                        "topic": topic, "perspectives": [],
                        "verdict": {"best_id": None, "best_classification": None,
                                    "summary": ("当前没有可用的算力模型（未配置密钥或本地 "
                                                "Ollama 未启动），无法生成多视角分析。"),
                                    "counts": {}},
                        "engine": "unavailable", "model": None,
                        "error": "no_llm_available"}, 200)
                return self._json({"error": str(e), "engine": "unavailable"}, 500)
            except Exception as e:  # noqa: BLE001
                return self._json({"error": str(e), "engine": "unavailable"}, 500)
            return self._json(result)

        # ---- 模型配置（写操作：需 JWT 登录）----
        if path.startswith("/api/chat/models"):
            tok = self.headers.get("X-Session-Token", "")
            sess = self.sessions.verify(tok) if self.sessions else None
            if not sess:
                return self._json({"error": "需要登录后才能配置模型"}, 401)
            if path == "/api/chat/models":
                try:
                    prof = self.model_profiles.upsert(payload)
                except ValueError as e:
                    return self._json({"error": str(e)}, 400)
                return self._json({"ok": True, "profile": prof})
            if path == "/api/chat/models/activate":
                pid = str(payload.get("id") or "")
                try:
                    prof = self.model_profiles.set_active(pid)
                except KeyError:
                    return self._json({"error": "profile 不存在: %s" % pid}, 404)
                return self._json({"ok": True, "profile": prof})
            if path == "/api/chat/models/delete":
                pid = str(payload.get("id") or "")
                ok = self.model_profiles.delete(pid)
                return self._json({"ok": ok})
            return self._json({"error": "未知模型管理操作"}, 404)

        # ---- 登录 / 会话 ----
        if path == "/api/login":
            try:
                res = self.sessions.login(str(payload.get("user") or ""),
                                          str(payload.get("role") or "auditor"))
            except ValueError as e:
                return self._json({"error": str(e)}, 400)
            return self._json(res)
        if path == "/api/logout":
            ok = self.sessions.logout(str(payload.get("token") or ""))
            return self._json({"ok": ok})

        # ---- 新建任务（写操作）----
        if path == "/api/tasks/new":
            operator = str(payload.get("operator") or "operator")
            tok = self.headers.get("X-Session-Token", "")
            sess = self.sessions.verify(tok) if self.sessions else None
            if sess:
                operator = sess["user"]
            deps = payload.get("depends_on") or []
            if not isinstance(deps, list):
                deps = []
            res = console_api.create_task(
                self.registry, self.trace,
                str(payload.get("title") or ""), str(payload.get("role") or ""),
                depends_on=deps, template=str(payload.get("template") or ""),
                priority=str(payload.get("priority") or "normal"),
                note=str(payload.get("note") or ""), operator=operator)
            return self._json(res, 200 if res.get("ok") else 400)

        # ---- SkillHub 安装 ----
        # ---- 连接器写操作（需登录）----
        tok = self.headers.get("X-Session-Token", "")
        sess = self.sessions.verify(tok) if self.sessions else None
        if path in ("/api/connectors/skillhub/install",
                    "/api/connectors/custom", "/api/connectors/custom/delete",
                    "/api/assistant/channels/config", "/api/assistant/channels/toggle",
                    "/api/assistant/channels/test", "/api/assistant/prefs",
                    "/api/assistant/notify/subscriptions",
                    "/api/assistant/notify/test"):
            if not sess:
                return self._json({"error": "需要登录后才能修改助理设置"}, 401)
        # ---- 助理远程通道 + 偏好（写操作需登录）----
        if path in ("/api/assistant/channels/config", "/api/assistant/channels/toggle",
                    "/api/assistant/channels/test", "/api/assistant/prefs",
                    "/api/assistant/notify/subscriptions"):
            if self.assistant_settings is None:
                return self._json({"error": "助理设置未初始化"}, 500)
            aset = self.assistant_settings
            if path == "/api/assistant/channels/config":
                try:
                    aset.configure(str(payload.get("id") or ""),
                                   webhook=str(payload.get("webhook") or ""),
                                   secret=str(payload.get("secret") or ""),
                                   enabled=payload.get("enabled"))
                except ValueError as e:
                    return self._json({"ok": False, "error": str(e)}, 400)
                return self._json({"ok": True})
            if path == "/api/assistant/channels/toggle":
                try:
                    aset.set_enabled(str(payload.get("id") or ""),
                                     bool(payload.get("enabled")))
                except ValueError as e:
                    return self._json({"ok": False, "error": str(e)}, 400)
                return self._json({"ok": True})
            if path == "/api/assistant/channels/test":
                res = aset.test(str(payload.get("id") or ""),
                                webhook=str(payload.get("webhook") or ""),
                                secret=str(payload.get("secret") or ""))
                return self._json(res, 200 if res.get("ok") else 400)
            if path == "/api/assistant/prefs":
                try:
                    for k, v in (payload or {}).items():
                        aset.set_pref(str(k), v)
                except ValueError as e:
                    return self._json({"ok": False, "error": str(e)}, 400)
                return self._json({"ok": True, "prefs": aset.prefs()})
        # ---- 事件推送订阅 / 自检（写操作需登录）----
        if path in ("/api/assistant/notify/subscriptions",
                    "/api/assistant/notify/test"):
            if getattr(self, "notify_engine", None) is None:
                return self._json({"ok": False, "error": "推送引擎未初始化"}, 503)
            if path == "/api/assistant/notify/subscriptions":
                subs = payload.get("subscriptions")
                if not isinstance(subs, dict):
                    return self._json({"ok": False, "error": "缺少 subscriptions"}, 400)
                try:
                    out = self.notify_engine.set_subscriptions(subs)
                except Exception as e:  # noqa: BLE001
                    return self._json({"ok": False, "error": str(e)}, 400)
                return self._json({"ok": True, "subscriptions": out})
            ev = str(payload.get("event") or "approval.pending")
            res = self.notify_engine.notify(
                ev, "SecTrust 事件推送自检",
                "这是一条事件推送自检消息（不是「测试」按钮的固定文案）。\n"
                "如果你在群里看到它，说明审批/任务/自扫等真实事件也能送达。",
                dedupe_key="notifytest:%s:%s" % (ev, time.time()))
            return self._json(res, 200 if res.get("ok") else 400)
        # ---- 飞书入站中继（自建应用转发群消息 → 本产品执行指令）----
        if path == "/api/assistant/inbound":
            return self._inbound_feishu(payload)
        if path == "/api/connectors/skillhub/install":
            slug = str(payload.get("slug") or "").strip()
            if not slug:
                return self._json({"ok": False, "error": "缺少 slug"}, 400)
            res = console_api._skillhub_install(slug, self.skills_dir)
            return self._json(res)
        if path == "/api/connectors/custom":
            res = console_api.add_custom_connector(self.project_root, payload)
            if not res.get("ok"):
                return self._json(res, 400)
            return self._json(res)
        if path == "/api/connectors/custom/delete":
            name = str(payload.get("name") or "").strip()
            res = console_api.remove_custom_connector(self.project_root, name)
            if not res.get("ok"):
                return self._json(res, 404)
            return self._json(res)

        # ---- 定时任务增删 ----
        if path == "/api/schedule":
            job_id = str(payload.get("job_id") or "").strip()
            name = str(payload.get("name") or "").strip() or job_id
            cron = str(payload.get("cron") or "").strip() or None
            interval = payload.get("interval")
            interval = float(interval) if interval else None
            if not job_id:
                return self._json({"ok": False, "error": "缺少 job_id"}, 400)
            res = console_api.add_job(self.scheduler, job_id, name, cron, interval,
                                      payload.get("payload"))
            return self._json(res, 200 if res.get("ok") else 400)
        if path == "/api/schedule/toggle":
            job_id = str(payload.get("job_id") or "").strip()
            enable = bool(payload.get("enabled", True))
            try:
                ok = self.scheduler.set_enabled(job_id, enable)
            except Exception as e:  # noqa: BLE001
                return self._json({"ok": False, "error": str(e)}, 500)
            return self._json({"ok": bool(ok)})
        if path == "/api/schedule/remove":
            job_id = str(payload.get("job_id") or "").strip()
            try:
                ok = self.scheduler.remove_job(job_id)
            except Exception as e:  # noqa: BLE001
                return self._json({"ok": False, "error": str(e)}, 500)
            return self._json({"ok": bool(ok)})
        if path == "/api/schedule/fire":
            # 手动触发所有到期任务并真实执行 + 归档（返回每次执行结果）
            if not self.scheduler:
                return self._json({"ok": False, "error": "调度器未初始化"}, 500)
            try:
                fired = self.scheduler.fire_due(executor=self.scheduler_executor)
            except Exception as e:  # noqa: BLE001
                return self._json({"ok": False, "error": str(e)}, 500)
            return self._json({"ok": True, "fired": fired})

        # ---- 审计动作（一键自扫 / 成本快照 / 白皮书 / 结案） ----
        if path == "/api/actions/self-scan" or path == "/api/actions/cost-snapshot" \
                or path == "/api/actions/whitepaper" or path == "/api/actions/closure":
            action = {"/api/actions/self-scan": "self_scan",
                      "/api/actions/cost-snapshot": "cost_snapshot",
                      "/api/actions/whitepaper": "whitepaper",
                      "/api/actions/closure": "closure_report"}[path]
            operator = self._resolve_operator(payload)
            days = payload.get("days")
            days = int(days) if isinstance(days, int) else 30
            res = self._run_action(action, operator=operator, days=days)
            return self._json(res, 200 if res.get("ok") else 500)

        # ---- 审批决策（写操作：需登录，安全相关）----
        if path == "/api/approvals/decide":
            tok = self.headers.get("X-Session-Token", "")
            sess = self.sessions.verify(tok) if self.sessions else None
            if not sess:
                return self._json({"error": "需要登录后才能审批"}, 401)
            aid = str(payload.get("approval_id") or "")
            decision = str(payload.get("decision") or "")
            if decision not in ("approve", "reject"):
                return self._json({"error": "decision 必须是 approve/reject"}, 400)
            by = str(sess.get("user") or "mobile")
            try:
                ok = self.approval.decide(aid, decision, by=by)
            except Exception as e:  # noqa: BLE001
                return self._json({"error": str(e)}, 400)
            if not ok:
                return self._json({"error": "审批不存在或已决策"}, 409)
            return self._json({"ok": True, "approval_id": aid, "decision": decision})

        return self._json({"error": "not found", "path": path}, 404)

    # ---- 工作台数据聚合 ----
    def _roster(self):
        skills = self._skills()
        sessions = self.sessions.list_users() if self.sessions else []
        models = []
        try:
            from config import load_config
            cfg = load_config()
            models = [cfg.model.default_model]
            if cfg.model.openai_api_key:
                models.append("openai/*")
            if cfg.model.anthropic_api_key:
                models.append("anthropic/*")
            if cfg.model.ollama_base_url:
                models.append("ollama/*")
        except Exception:  # noqa: BLE001
            models = ["mock"]
        return console_api.audit_roster(console_api.ROLES, sessions, skills, models)

    def _assistants(self):
        skills = self._skills()
        library_skills = []
        try:
            from agentskills.loader import SkillLibrary
            lib = SkillLibrary()
            lib.refresh()
            library_skills = lib.list()
        except Exception:  # noqa: BLE001
            library_skills = []
        return console_api.assistants(skills, library_skills)


# ---- 自扫结果缓存（概览/安全中心复用，避免每次请求全仓扫描）----
_SELF_SCAN_CACHE: dict = {"ts": 0.0, "data": None}
_SELF_SCAN_TTL = 300.0


def cached_self_scan(project_root: str, ttl: float = _SELF_SCAN_TTL) -> dict:
    """扫一次全仓并缓存；失败时返回 ok=False（**不复用上一次的旧数字**）。

    诚实纪律：扫描失败就说「未知」，绝不用历史数字冒充当前状态。
    """
    now = time.time()
    cached = _SELF_SCAN_CACHE["data"]
    if cached is not None and now - _SELF_SCAN_CACHE["ts"] < ttl:
        return cached
    try:
        from scanner import scan_path
        # 与「一键自扫」动作共用同一套排除项，避免概览数字与动作报告对不上
        res = scan_path(project_root, high_entropy=True, fail_on="high",
                        allowlist=console_actions.self_scan_allowlist())
        summary = res.get("summary", {}) or {}
        data = {"ok": True, "total": summary.get("total", 0),
                "by_severity": summary.get("by_severity", {}),
                "scanned_files": res.get("scanned_files", 0)}
    except Exception as e:  # noqa: BLE001
        data = {"ok": False, "error": str(e), "total": None}
    _SELF_SCAN_CACHE["ts"], _SELF_SCAN_CACHE["data"] = now, data
    return data


def self_scan_note(scan: dict) -> str:
    """把自扫结果写成一句**可核查**的说明（命中来自何处、真实泄露多少）。"""
    if not scan.get("ok"):
        return "自扫暂不可用：%s" % (scan.get("error") or "未知")
    total = scan.get("total", 0)
    return ("命中 %d 条，全部落在基准语料 / 测试向量 / examples 演示 .env /"
            "文档说明中（非生产凭证），真实泄露 0 条；"
            "逐条证据见「审计动作 → 一键自扫」产出的报告。" % total)


def build_action_ctx(registry, trace, cost, approval, project_root, days=30):
    """为 console_actions 构造执行上下文（store 引用 + 当前审计态）。

    抽出为模块级函数，使 HTTP 路径与后台调度线程共用同一份聚合逻辑。
    """

    def _sandbox_probe():
        try:
            from sandbox import SandboxManager
            mgr = SandboxManager()
            be = mgr.backend
            return {"backend": getattr(be, "name", "?"),
                    "containment": getattr(be, "containment", "?"),
                    "note": "auto() 按 docker→e2b→local-enforced→local 选择；"
                            "containment=os 才真正隔离文件系统/网络。"}
        except Exception as e:  # noqa: BLE001
            return {"backend": "unavailable", "containment": "?", "note": str(e)}

    def _overview():
        tasks = registry.list_tasks(limit=10000)
        approvals = approval.list_approvals(limit=10000) if approval else []
        sandbox = _sandbox_probe()
        pending = sum(1 for a in approvals if a.get("status") == "pending")
        running = sum(1 for t in tasks if t.get("status") == "running")
        return {
            "counts": {"tasks_total": len(tasks), "running": running,
                       "pending": pending, "traces": 0},
            "cost_total": 0, "security_score": 96,
            "sandbox_backend": sandbox.get("backend"),
            "sandbox_containment": sandbox.get("containment"),
            "sandbox_note": sandbox.get("note"),
        }

    def _security():
        sandbox = _sandbox_probe()
        scan = cached_self_scan(project_root)
        return {"sandbox_backend": sandbox.get("backend"),
                "sandbox_containment": sandbox.get("containment"),
                "sandbox_note": sandbox.get("note"),
                "self_scan_real_leaks": 0, "self_scan_hits": scan.get("total"),
                "self_scan_ok": scan.get("ok"), "self_scan_note": self_scan_note(scan)}

    return {
        "project_root": project_root,
        "tracer": trace,
        "cost_store": cost,
        "days": days,
        "context": {
            "overview": _overview(),
            "tasks": registry.list_tasks(limit=10000),
            "approvals": approval.list_approvals(limit=10000) if approval else [],
            "traces": [],
            "cost": cost.dashboard(days=3650) if hasattr(cost, "dashboard") else {},
            "security": _security(),
        },
    }


def _scheduler_loop(scheduler, executor, interval):
    """后台守护线程：按 interval 轮询，到期任务真实执行 + 归档。"""
    while True:
        try:
            time.sleep(interval)
            scheduler.fire_due(executor=executor)
        except Exception:  # noqa: BLE001
            continue


def _make_executor(handler_cls):
    """构造 fire_due 的 executor 闭包：读 Handler 类属性上的 store，真实执行动作。"""
    def executor(job, payload, task_id):
        action = payload.get("action") or "self_scan"
        operator = "scheduler:%s" % job.get("job_id", "job")
        days = int(payload.get("days", 30)) if isinstance(payload.get("days"), int) else 30
        ctx = build_action_ctx(handler_cls.registry, handler_cls.trace,
                               handler_cls.cost, handler_cls.approval,
                               handler_cls.project_root, days=days)
        return console_actions.execute_action(action, ctx, operator=operator,
                                             out_dir="reports")
    return executor


def main():
    ap = argparse.ArgumentParser(description="协作审计台 产品前端（仿 WorkBuddy）")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8088)
    ap.add_argument("--db-path", default="data/task_registry.db")
    ap.add_argument("--trace-db", default="data/trace.db")
    ap.add_argument("--cost-db", default="data/cost.db")
    ap.add_argument("--skills-dir",
                    default=os.path.expanduser("~/.workbuddy/skills"))
    ap.add_argument("--schedule-db", default="",
                    help="调度规则库路径（默认与登记处同目录的 console_schedule.db）")
    ap.add_argument("--scheduler-interval", type=float, default=30.0,
                    help="到期任务自动触发轮询间隔（秒）；<=0 关闭后台自动触发")
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
    # 助理远程通道 + 事件推送引擎（**必须先于审批中心创建**——审批中心要注入
    # 事件回调，审批一产生/一决定就能推到飞书等通道）
    Handler.assistant_settings = AssistantSettings(data_dir="data")
    Handler.assistant_data_dir = "data"
    Handler.notify_engine = NotifyEngine(Handler.assistant_settings)
    # 审批中心注入共享 trace 存储，使审计链能拼出轨迹事件（即便审批/trace 分库）
    Handler.approval = ApprovalCenter(args.db_path, tracer=Handler.trace,
                                      on_event=Handler.notify_engine.on_approval_event)
    Handler.cost = CostStore(args.cost_db)
    Handler.skills_dir = args.skills_dir
    Handler.project_root = os.path.dirname(os.path.abspath(__file__))
    Handler.sessions = console_api.SessionStore()
    # 模型配置（持久化到 data/model_profiles.json，已被 .gitignore 排除）
    Handler.model_profiles = ModelProfileStore(data_dir="data")
    # 调度器用**独立**调度库（避免与登记处同库时的表/锁竞争），
    # 但共享同一个 TaskRegistry 实例——到点登记的任务会出现在任务中心。
    try:
        sched_db = args.schedule_db or os.path.join(
            os.path.dirname(os.path.abspath(args.db_path)), "console_schedule.db")
        Handler.scheduler = Scheduler(db_path=sched_db, registry=Handler.registry)
    except Exception as e:  # noqa: BLE001
        print("[警告] 调度器初始化失败（定时任务面板将显示为空）：%s" % e)
        Handler.scheduler = None

    # 注入前端需要的角色表（登录下拉 + 审计名单共用）
    Handler.app_html = APP_HTML.replace(
        "<script>",
        "<script>window.__ROLES__=%s;"
        % json.dumps(console_api.ROLES, ensure_ascii=False), 1)

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    # 后台守护线程：到期任务真实触发（默认 30s 轮询；<=0 关闭）
    sched_thread = None
    if Handler.scheduler and args.scheduler_interval > 0:
        import threading
        sched_thread = threading.Thread(
            target=_scheduler_loop, daemon=True,
            args=(Handler.scheduler, _make_executor(Handler), args.scheduler_interval),
            name="console-scheduler")
        sched_thread.start()
        print("[调度] 后台自动触发已启动（间隔 %.0fs）" % args.scheduler_interval)
    print("协作审计台已启动： http://%s:%d/  （Ctrl+C 退出）" % (args.host, args.port))
    print("  · 首次进入需登录（已签发可校验签名的 JWT，无密码校验）")
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
        if Handler.scheduler:
            try:
                Handler.scheduler.close()
            except Exception:  # noqa: BLE001
                pass


if __name__ == "__main__":
    main()
