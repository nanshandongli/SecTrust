"""
task_center Web 查看页面（只读轮询）

职责：
- 不写入登记处：仅通过 TaskRegistry.list_tasks / list_runners 做只读查询；
- 提供 REST 风格 API：/api/tasks、/api/runners、/api/approvals（审批中心只读）、
  /api/dag（依赖视图：波次分层/就绪集/全图/阻塞传播，复用 DagScheduler 调度语义）；
- 提供仪表盘页面 /：任务表格（含依赖/等待列）+ 状态筛选 + runner 模糊过滤 + 实时进度条(live_view) +
  审批中心待办卡片 + DAG 波次图 + 3s 自动刷新，stale(>90s 无心跳) 的 runner 高亮标红。

说明：
- 暴露的端口只用于"查看"，登记处 SQLite 文件本身并未被网络暴露；
- 审批中心与登记处共用同一 SQLite 文件（独立表），这里也只做只读读取。
- 保持纯标准库，零依赖。

运行：
    python task_center_web.py --db-path data/task_registry.db --port 8090
"""

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from task_registry import TaskRegistry
from approval import ApprovalCenter


HTML = """<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>task_center 仪表盘</title>
<style>
  :root { --bg:#f6f7f9; --card:#fff; --line:#eaecef; --txt:#1f2328; --sub:#6a737d; }
  * { box-sizing: border-box; }
  body { font-family: -apple-system, "Segoe UI", system-ui, sans-serif; margin:0; background:var(--bg); color:var(--txt); }
  header { padding:18px 24px 8px; }
  h1 { font-size:20px; margin:0 0 4px; }
  .meta { color:var(--sub); font-size:13px; }
  .toolbar { display:flex; flex-wrap:wrap; gap:12px; align-items:center; padding:12px 24px; }
  .toolbar label { font-size:13px; color:var(--sub); display:flex; align-items:center; gap:6px; }
  select, input[type=text] { padding:5px 8px; border:1px solid #cfd4da; border-radius:6px; font-size:13px; }
  input[type=text] { width:180px; }
  button { padding:5px 12px; border:1px solid #cfd4da; border-radius:6px; background:#fff; cursor:pointer; font-size:13px; }
  button:hover { background:#f0f2f5; }
  .auto { user-select:none; }
  .chips { display:flex; flex-wrap:wrap; gap:8px; padding:0 24px 12px; }
  .chip { font-size:12px; padding:3px 10px; border-radius:12px; background:#eef1f4; color:#384047; }
  .chip b { color:#1f2328; }
  .chip.warn { background:#fdecea; color:#c62828; }
  section { padding:0 24px 24px; }
  h2 { font-size:15px; margin:0 0 8px; color:#384047; }
  .tbl-wrap { background:var(--card); border:1px solid var(--line); border-radius:8px; overflow:auto; box-shadow:0 1px 3px rgba(0,0,0,.06); }
  table { border-collapse:collapse; width:100%; font-size:13px; }
  th, td { text-align:left; padding:8px 10px; border-bottom:1px solid var(--line); vertical-align:top; white-space:nowrap; }
  th { background:#f0f2f5; font-weight:600; position:sticky; top:0; }
  tbody tr:hover { background:#fafbfc; }
  td.pre { white-space:pre-wrap; word-break:break-all; max-width:360px; }
  .stale { color:#c0392b; font-weight:600; }
  .ok { color:#2e7d32; }
  .badge { display:inline-block; padding:1px 9px; border-radius:10px; font-size:12px; }
  .b-registered { background:#e3f2fd; color:#1565c0; }
  .b-running { background:#fff3e0; color:#ef6c00; }
  .b-done { background:#e8f5e9; color:#2e7d32; }
  .b-failed { background:#fdecea; color:#c62828; }
  .b-pending { background:#fff8e1; color:#f9a825; font-weight:600; }
  .b-approved,.b-rejected,.b-expired { background:#eceff1; color:#546e7a; }
  .empty { padding:16px; color:var(--sub); font-size:13px; }
  .progress { background:#eef1f4; border-radius:6px; height:14px; width:140px; overflow:hidden; position:relative; }
  .progress > i { display:block; height:100%; background:#42a5f5; }
  .progress.txt > i { background:#90a4ae; }
  .plog { color:var(--sub); font-size:11px; }
  /* ---- DAG 依赖视图 ---- */
  .wave { display:flex; align-items:flex-start; gap:10px; padding:8px 0; border-bottom:1px dashed var(--line); }
  .wave:last-child { border-bottom:none; }
  .wave-label { flex:0 0 92px; font-size:12px; color:var(--sub); padding-top:6px; }
  .wave-nodes { display:flex; flex-wrap:wrap; gap:8px; }
  .node { border:1px solid var(--line); border-radius:8px; background:#fff; padding:6px 10px; font-size:12px; min-width:150px; max-width:280px; }
  .node .nid { font-weight:600; word-break:break-all; }
  .node .nname { color:var(--sub); }
  .node .nwait { color:#c62828; font-size:11px; margin-top:2px; }
  .node.ready-node { border-color:#42a5f5; box-shadow:0 0 0 1px #42a5f5 inset; }
  .node.dead { border-color:#c62828; background:#fdecea; }
  .b-completed { background:#e8f5e9; color:#2e7d32; }
  .b-blocked { background:#fdecea; color:#c62828; }
  .b-needs_human { background:#f3e5f5; color:#6a1b9a; font-weight:600; }
  .dep-cell { font-size:11px; color:var(--sub); max-width:200px; white-space:pre-wrap; word-break:break-all; }
</style>
</head>
<body>
<header>
  <h1>task_center 仪表盘 <span style="font-size:12px;color:#6a737d;">（只读）</span></h1>
  <div class="meta" id="meta">加载中…</div>
</header>

<div class="toolbar">
  <label>状态筛选
    <select id="statusFilter">
      <option value="">全部</option>
      <option value="registered">registered</option>
      <option value="running">running</option>
      <option value="done">done</option>
      <option value="failed">failed</option>
    </select>
  </label>
  <label>runner 模糊
    <input id="runnerFilter" type="text" placeholder="runner_id 含…（可选）">
  </label>
  <button id="refreshBtn">立即刷新</button>
  <label class="auto"><input type="checkbox" id="autoChk" checked> 自动刷新(3s)</label>
</div>

<div class="chips" id="chips"></div>

<section>
  <h2>审批中心 (Approvals) · Human-in-the-loop</h2>
  <div class="tbl-wrap">
    <table id="approvals"><thead><tr>
      <th>approval_id</th><th>action</th><th>detail</th><th>status</th><th>task_id</th><th>created_at</th><th>decided_by</th>
    </tr></thead><tbody></tbody></table>
  </div>
</section>

<section>
  <h2>依赖视图 (DAG) · 并行波次 / 就绪 / 阻塞传播</h2>
  <div class="tbl-wrap" style="padding:10px 14px;">
    <div id="dagwaves"></div>
  </div>
</section>

<section>
  <h2>任务 (tasks)</h2>
  <div class="tbl-wrap">
    <table id="tasks"><thead><tr>
      <th>task_id</th><th>runner_id</th><th>status</th><th>依赖 / 等待</th><th>进度(live)</th><th>created_at</th><th>updated_at</th><th>payload</th>
    </tr></thead><tbody></tbody></table>
  </div>
</section>

<section>
  <h2>Runner 存活 (runners)</h2>
  <div class="tbl-wrap">
    <table id="runners"><thead><tr>
      <th>runner_id</th><th>last_heartbeat</th><th>status</th><th>距上次心跳</th>
    </tr></thead><tbody></tbody></table>
  </div>
</section>

<script>
const $ = (id) => document.getElementById(id);
const fmt = (t) => t ? new Date(t*1000).toLocaleString('zh-CN') : '-';
const ago = (t) => t ? Math.max(0, Math.round(Date.now()/1000 - t)) + 's 前' : '-';
function badge(s){
  const m = {'registered':'b-registered','running':'b-running','done':'b-done','completed':'b-completed',
             'failed':'b-failed','blocked':'b-blocked','needs_human':'b-needs_human',
             'pending':'b-pending','approved':'b-approved','rejected':'b-rejected','expired':'b-expired'};
  return '<span class="badge '+(m[s]||'')+'">'+s+'</span>';
}
function progressCell(p){
  if (p == null) return '-';
  if (typeof p === 'number'){
    const pct = Math.max(0, Math.min(100, p));
    return '<div class="progress"><i style="width:'+pct+'%"></i></div>';
  }
  return '<div class="progress txt"><i style="width:100%"></i></div> <span class="plog">'+p+'</span>';
}

let currentStatus = "";
let autoTimer = null;

function buildTaskUrl(){
  let u = '/api/tasks?limit=500';
  if (currentStatus) u += '&status=' + encodeURIComponent(currentStatus);
  return u;
}

async function refresh(){
  try{
    const [tasksRaw, runners, approvals, dag] = await Promise.all([
      fetch(buildTaskUrl()).then(r => r.json()),
      fetch('/api/runners').then(r => r.json()),
      fetch('/api/approvals?limit=50').then(r => r.json()),
      fetch('/api/dag').then(r => r.json())
    ]);
    const rf = $('runnerFilter').value.trim();
    const tasks = rf ? tasksRaw.filter(t => t.runner_id.includes(rf)) : tasksRaw;

    const c = {registered:0, running:0, done:0, failed:0};
    tasksRaw.forEach(t => { if (c[t.status] != null) c[t.status]++; });
    const pendingN = approvals.filter(a => a.status === 'pending').length;
    const staleN = runners.filter(r => r.status === 'stale').length;
    $('chips').innerHTML =
      '<span class="chip">任务总数 <b>'+tasksRaw.length+'</b></span>' +
      '<span class="chip">registered <b>'+c.registered+'</b></span>' +
      '<span class="chip">running <b>'+c.running+'</b></span>' +
      '<span class="chip">done <b>'+c.done+'</b></span>' +
      '<span class="chip">failed <b>'+c.failed+'</b></span>' +
      (pendingN ? '<span class="chip warn">待审批 <b>'+pendingN+'</b></span>' : '') +
      '<span class="chip">runner <b>'+runners.length+'</b>（stale <b>'+staleN+'</b>）</span>';

    $('meta').textContent = '刷新于 ' + new Date().toLocaleTimeString('zh-CN') +
      ' · 显示 ' + tasks.length + ' / ' + tasksRaw.length + ' 条';

    // 审批中心
    const ab = $('approvals').querySelector('tbody');
    ab.innerHTML = approvals.length ? approvals.map(a =>
      '<tr><td>'+a.approval_id+'</td><td>'+a.action+'</td><td class="pre">'+a.detail+'</td><td class="'+
      (a.status==='pending'?'stale':'')+'">'+badge(a.status)+'</td><td>'+a.task_id+'</td><td>'+
      fmt(a.created_at)+'</td><td>'+(a.decided_by||'-')+'</td></tr>'
    ).join('') : '<tr><td colspan="7" class="empty">无审批记录</td></tr>';

    // 依赖视图：按波次分层渲染，蓝框=可认领，红=上游已死（阻塞传播）
    const dg = $('dagwaves');
    if (!dag.tasks || !Object.keys(dag.tasks).length){
      dg.innerHTML = '<div class="empty">暂无带依赖的任务（用 dag_add / planner.build 建图后这里会出现波次）</div>';
    } else {
      const readySet = new Set(dag.ready || []);
      dg.innerHTML = (dag.waves || []).map((wave, i) =>
        '<div class="wave"><div class="wave-label">波次 '+i+'</div><div class="wave-nodes">' +
        wave.map(tid => {
          const t = dag.tasks[tid] || {};
          const dead = (t.dead_upstream || []).length > 0;
          const cls = 'node' + (readySet.has(tid) ? ' ready-node' : '') + (dead ? ' dead' : '');
          const wait = dead ? ('<div class="nwait">上游已失败/被阻塞: '+(t.dead_upstream||[]).join(', ')+'</div>')
                    : ((t.waiting_on || []).length ? '<div class="nwait">等待: '+(t.waiting_on||[]).join(', ')+'</div>' : '');
          return '<div class="'+cls+'"><div class="nid">'+tid+' '+badge(t.status||'')+'</div>' +
                 '<div class="nname">'+(t.name||'')+'</div>'+wait+'</div>';
        }).join('') + '</div></div>'
      ).join('');
    }

    // 任务（含实时进度与依赖列）
    const tb = $('tasks').querySelector('tbody');
    tb.innerHTML = tasks.length ? tasks.map(t => {
      const p = t.payload || {};
      const lastLog = (p.log && p.log.length) ? p.log[p.log.length-1] : '';
      const dt = (dag.tasks && dag.tasks[t.task_id]) || {};
      const depTxt = (dt.depends_on && dt.depends_on.length)
        ? '依赖: ' + dt.depends_on.join(', ') + ((dt.waiting_on && dt.waiting_on.length) ? '\n等待: ' + dt.waiting_on.join(', ') : '')
        : (dt.dead_upstream && dt.dead_upstream.length ? '上游已死: ' + dt.dead_upstream.join(', ') : '—');
      return '<tr><td>'+t.task_id+'</td><td>'+t.runner_id+'</td><td>'+badge(t.status)+'</td><td class="dep-cell">'+
        depTxt+'</td><td>'+
        progressCell(p.progress) + (lastLog ? '<div class="plog">'+lastLog+'</div>' : '') +
        '</td><td>'+fmt(t.created_at)+'</td><td>'+fmt(t.updated_at)+'</td><td class="pre">'+
        (t.payload ? JSON.stringify(t.payload) : '')+'</td></tr>';
    }).join('') : '<tr><td colspan="8" class="empty">无匹配任务</td></tr>';

    const rb = $('runners').querySelector('tbody');
    rb.innerHTML = runners.length ? runners.map(r =>
      '<tr><td>'+r.runner_id+'</td><td>'+fmt(r.last_heartbeat)+'</td><td class="'+
      (r.status==='stale'?'stale':'ok')+'">'+r.status+'</td><td>'+ago(r.last_heartbeat)+'</td></tr>'
    ).join('') : '<tr><td colspan="4" class="empty">无 runner 心跳记录</td></tr>';
  }catch(e){
    $('meta').textContent = '刷新失败：' + e;
  }
}

function toggleAuto(){
  if (autoTimer){ clearInterval(autoTimer); autoTimer = null; }
  else { autoTimer = setInterval(refresh, 3000); }
}

$('statusFilter').addEventListener('change', e => { currentStatus = e.target.value; refresh(); });
$('runnerFilter').addEventListener('input', refresh);
$('refreshBtn').addEventListener('click', refresh);
$('autoChk').addEventListener('change', toggleAuto);

refresh();
toggleAuto();
</script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    registry = None    # 只读：tasks / runners
    approval = None    # 只读：approvals

    def _send_json(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_html(self, html):
        data = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        if parsed.path == "/":
            self._send_html(HTML)
        elif parsed.path == "/api/tasks":
            runner_id = qs.get("runner_id", [None])[0]
            status = qs.get("status", [None])[0]
            limit = int(qs.get("limit", ["100"])[0])
            self._send_json(
                self.registry.list_tasks(runner_id=runner_id, status=status, limit=limit)
            )
        elif parsed.path == "/api/runners":
            self._send_json(self.registry.list_runners(stale_after=90))
        elif parsed.path == "/api/approvals":
            status = qs.get("status", [None])[0]
            limit = int(qs.get("limit", ["100"])[0])
            # 只读查询
            self._send_json(self.approval.list_approvals(status=status, limit=limit))
        elif parsed.path == "/api/dag":
            # 依赖视图（只读）：波次分层 / 就绪集 / 全图 / 每任务等待上游 / 统计
            self._send_json(dag_view(self.registry))
        else:
            self._send_json({"error": "not found"}, code=404)

    def log_message(self, *args):
        pass  # 静默访问日志


def dag_view(registry) -> dict:
    """从登记处只读构建 DAG 视图（复用 orchestration.DagScheduler 的调度语义）。

    返回：waves（并行波次）/ ready（可认领）/ graph（依赖全图）/
          tasks（{task_id: 状态、名称、等待中的上游、是否被阻塞}）/ stats。
    阻塞传播：上游 failed/blocked 的下游标记 blocked_view，一眼看清"卡在哪"。
    """
    from orchestration import DagScheduler
    s = DagScheduler(registry=registry, events=False)
    waves = s.waves()
    ready = [t["task_id"] for t in s.ready_tasks()]
    graph = s.graph()
    tasks = {}
    for t in s._all():
        tid = t["task_id"]
        waiting = s.waiting_on(tid)
        payload = t.get("payload") or {}
        # 上游失败/被阻塞 -> 本任务事实上已不可能执行（红色警示）
        dead_upstream = [d for d in t["depends_on"]
                         if (s.registry.get_task(d) or {}).get("status")
                         in ("failed", "blocked")]
        tasks[tid] = {
            "status": t["status"],
            "name": payload.get("name", ""),
            "depends_on": list(t["depends_on"]),
            "waiting_on": waiting,
            "dead_upstream": dead_upstream,
            "runner_id": t.get("runner_id", ""),
        }
    return {"waves": waves, "ready": ready, "graph": graph,
            "tasks": tasks, "stats": s.stats()}


def main():
    ap = argparse.ArgumentParser(description="task_center Web 查看页面（只读）")
    ap.add_argument("--db-path", default="data/task_registry.db")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8090)
    args = ap.parse_args()

    # 只读：仅 list_tasks / list_runners / list_approvals，绝不做写操作
    Handler.registry = TaskRegistry(db_path=args.db_path, runner_id="task_center_web")
    Handler.approval = ApprovalCenter(db_path=args.db_path)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(
        f"task_center 查看页面已启动： http://{args.host}:{args.port}/  "
        f"(只读，按 Ctrl+C 退出)"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        Handler.registry.close()
        Handler.approval.close()


if __name__ == "__main__":
    main()
