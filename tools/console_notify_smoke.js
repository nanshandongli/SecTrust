/**
 * console_notify_smoke.js —— 事件订阅面板 + 飞书入站面板 渲染冒烟（零依赖 DOM 桩）
 *
 * 目的：验证 notify 相关的**真实内联 JS** 在 DOM 桩下能跑通，且渲染出
 * 预期的结构（事件类型 × 通道的勾选表、入站端点与令牌、诚实边界文案）。
 *
 * 用法：node tools/console_notify_smoke.js <app_html>
 */
const fs = require('fs');
const vm = require('vm');

const appHtml = process.argv[2];
if (!appHtml) { console.error('用法: node tools/console_notify_smoke.js <app_html>'); process.exit(1); }
const html = fs.readFileSync(appHtml, 'utf8');

const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]);
if (!scripts.length) { console.error('FAIL: 页面里没有 <script>'); process.exit(1); }
const code = scripts.join('\n');

// ---- 极简 DOM 桩 ----
function makeEl(id) {
  const el = {
    id, _html: '', textContent: '', value: '', placeholder: '', disabled: false,
    style: {}, dataset: {}, className: '', classes: new Set(),
    classList: {
      add: c => el.classes.add(c), remove: c => el.classes.delete(c),
      toggle: (c, on) => { if (on === undefined) { el.classes.has(c) ? el.classes.delete(c) : el.classes.add(c); } else { on ? el.classes.add(c) : el.classes.delete(c); } },
      contains: c => el.classes.has(c),
    },
    get innerHTML() { return el._html; },
    set innerHTML(v) { el._html = String(v); },
    insertAdjacentHTML(pos, h) { el._html += String(h); },
    appendChild() {}, remove() {}, focus() {}, scrollTop: 0, scrollHeight: 0,
    addEventListener() {}, removeEventListener() {},
    querySelector: () => null, querySelectorAll: () => [],
    getAttribute: () => null, setAttribute() {},
  };
  return el;
}
const ELEMENTS = {};
function $(id) {
  const key = String(id);
  if (!ELEMENTS[key]) ELEMENTS[key] = makeEl(key);
  return ELEMENTS[key];
}

const document = {
  getElementById: $,
  querySelector: () => null,
  querySelectorAll: () => [],
  createElement: tag => makeEl(tag),
  documentElement: { getAttribute: () => null, setAttribute() {} },
  body: makeEl('body'),
  addEventListener() {},
};
const localStorage = {
  _m: {}, getItem(k) { return Object.prototype.hasOwnProperty.call(this._m, k) ? this._m[k] : null; },
  setItem(k, v) { this._m[k] = String(v); }, removeItem(k) { delete this._m[k]; },
};

// API 桩
const NOTIFY_PAYLOAD = {
  events: [
    { id: 'approval.pending', label: '审批请求', desc: '有新的高风险动作等待人工审批。', severity: 'warn' },
    { id: 'approval.decided', label: '审批决定', desc: '审批被通过或拒绝。', severity: 'info' },
    { id: 'security.high_risk', label: '高危安全发现', desc: '自扫发现高危问题。', severity: 'high' },
    { id: 'task.completed', label: '任务完成', desc: '任务执行成功。', severity: 'info' },
    { id: 'task.failed', label: '任务失败', desc: '任务失败或挂起。', severity: 'error' },
    { id: 'cost.anomaly', label: '成本异常', desc: '成本超阈值。', severity: 'warn' },
  ],
  subscriptions: { feishu_bot: ['approval.pending', 'approval.decided'], wecom_bot: [] },
  enabled_channels: ['feishu_bot', 'wecom_bot'],
  recent: [],
  inbound: {
    endpoint: '/api/assistant/inbound',
    header: 'X-Notify-Token',
    token: 'TESTTOKEN123',
    commands: ['/approve <审批ID>', '/reject <审批ID>', '/pending', '/help'],
    note: '群机器人 webhook 只出不进，群消息收不到；要「在群里回复即控制」需用飞书企业自建应用 + 事件订阅，把群消息 POST 到本端点（带 X-Notify-Token 头）。本端点只认上面四条白名单指令，认不出就报错，不猜不执行。',
  },
  note: '推送失败不会影响审批等主流程；每条推送都有记录可查。',
};

const ROUTES = {
  '/api/assistant/notify': NOTIFY_PAYLOAD,
  '/api/assistant/settings': {
    channels: [
      { id: 'feishu_bot', name: '飞书群机器人', desc: 'd', letter: '飞', hue: 210, fields: [], note: '', configured: true, enabled: true, webhook_masked: 'https://open.feishu.cn/open-apis/bot/v2/hook/****4e16', has_secret: false },
      { id: 'wecom_bot', name: '企业微信群机器人', desc: 'd', letter: '企', hue: 210, fields: [], note: '', configured: true, enabled: true, webhook_masked: 'https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=****wxyz', has_secret: false },
    ],
    prefs: { context_memory: true },
    note: '只收录真实可接通的通道。',
  },
};

function routeFetch(url) {
  const u = String(url);
  const idx = u.indexOf('/api/');
  const path = idx >= 0 ? u.slice(idx).split('?')[0] : u;
  let payload = ROUTES[path];
  if (payload === undefined) payload = {};
  return Promise.resolve({ status: 200, ok: true, json: () => Promise.resolve(payload) });
}

const sandbox = {
  console, setTimeout, clearTimeout, setInterval, clearInterval,
  document, localStorage, fetch: routeFetch,
  location: { hash: '#/assistants' },
  encodeURIComponent, JSON, Math, Date, RegExp, String, Number, Boolean, Object, Array, Error,
  confirm: () => true, alert: () => {},
  addEventListener() {}, removeEventListener() {},
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;
vm.createContext(sandbox);

const errors = [];
try {
  vm.runInContext(code, sandbox, { filename: 'app-inline.js' });
} catch (e) {
  console.error('FAIL: 脚本执行期错误 -> ' + e.message);
  process.exit(1);
}

function check(cond, msg) { if (!cond) errors.push(msg); }

(async () => {
  for (const fn of ['loadNotifySubs', 'renderInbound', 'saveNotifySubs', 'testNotifyEvent']) {
    if (typeof sandbox[fn] !== 'function') errors.push(fn + ': 未定义');
  }
  if (errors.length) {
    console.error('FAIL: %d 个问题', errors.length);
    errors.forEach(e => console.error('  - ' + e));
    process.exit(1);
  }

  // ① 订阅表渲染
  await sandbox.loadNotifySubs();
  const h = $('notifySubs')._html || '';
  check(h.length > 0, '订阅表渲染为空');
  check(h.includes('<table'), '订阅表未渲染 table');
  for (const e of NOTIFY_PAYLOAD.events) {
    check(h.includes(e.label), '订阅表缺少事件标签：' + e.label);
    check(h.includes('data-ev="' + e.id + '"'), '订阅表缺少事件勾选框：' + e.id);
  }
  for (const c of ['飞书群机器人', '企业微信群机器人']) {
    check(h.includes(c), '订阅表缺少通道列：' + c);
  }
  check(h.includes('data-ch="feishu_bot"'), '订阅表缺少飞书通道列勾选框');
  check(h.includes('已启用通道'), '订阅表缺少已启用通道提示');
  console.log('  ok  loadNotifySubs (%d chars)', h.length);

  // ② 订阅状态回填（feishu 订阅了前两类 → 对应 checkbox 应为 checked）
  check(h.includes('data-ev="approval.pending" data-ch="feishu_bot" checked'), '飞书 approval.pending 应为已勾选');
  check(h.includes('data-ev="task.failed" data-ch="feishu_bot"'), '飞书 task.failed 勾选框缺失');

  // ③ 入站面板渲染
  sandbox.renderInbound(NOTIFY_PAYLOAD.inbound);
  const ib = $('notifyInbound')._html || '';
  check(ib.length > 0, '入站面板渲染为空');
  check(ib.includes('/api/assistant/inbound'), '入站面板缺少端点');
  check(ib.includes('X-Notify-Token'), '入站面板缺少请求头名');
  check(ib.includes('TESTTOKEN123'), '入站面板缺少令牌');
  check(ib.includes('/approve'), '入站面板缺少指令说明');
  check(ib.includes('只出不进'), '入站面板缺少诚实边界文案（群机器人只出不进）');
  console.log('  ok  renderInbound (%d chars)', ib.length);

  // ④ 未登录态：令牌不显示
  sandbox.renderInbound({ ...NOTIFY_PAYLOAD.inbound, token: '', token_hint: '登录后才能查看入站令牌（未登录不返回令牌）' });
  const ib2 = $('notifyInbound')._html || '';
  check(!ib2.includes('TESTTOKEN123'), '未登录态不应显示令牌');
  check(ib2.includes('登录后才能查看'), '未登录态应显示提示');
  console.log('  ok  renderInbound(未登录不显示令牌)');

  // ⑤ 页面骨架片段
  for (const f of ['id="notifySubs"', 'id="notifyInbound"', 'id="notifySubsMsg"',
                   'id="notifyTestMsg"', 'saveNotifySubs()', 'testNotifyEvent()',
                   '/api/assistant/notify/subscriptions', '/api/assistant/notify/test']) {
    if (!html.includes(f)) errors.push('页面缺少片段: ' + f);
  }

  console.log('');
  if (errors.length) {
    console.error('FAIL: %d 个问题', errors.length);
    errors.forEach(e => console.error('  - ' + e));
    process.exit(1);
  }
  console.log('PASS: 事件订阅 + 飞书入站面板渲染全部通过');
})();
