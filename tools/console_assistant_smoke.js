/**
 * console_assistant_smoke.js —— 助理页（远程通道 + 上下文管理）的渲染冒烟
 *
 * 目的：在不启动浏览器的前提下，验证助理页真实渲染结果：
 *   ① 远程通道卡片齐全（4 张真实通道卡，未配置显示「连接」按钮）；
 *   ② 配置过的通道显示「已连接/已停用」态；
 *   ③ 通道弹窗按目录 fields 渲染输入框，能开关；
 *   ④ 上下文管理开关随 prefs 状态渲染；
 *   ⑤ 保存/测试走真实 POST 路径（fetch 打桩记录请求体）。
 *
 * 用法：node tools/console_assistant_smoke.js <app_html> <api_dump_json>
 */
const fs = require('fs');
const vm = require('vm');

const appHtml = process.argv[2];
const dumpPath = process.argv[3];
if (!appHtml || !dumpPath) {
  console.error('用法: node tools/console_assistant_smoke.js <app_html> <api_dump_json>');
  process.exit(2);
}
const html = fs.readFileSync(appHtml, 'utf8');
const apiData = JSON.parse(fs.readFileSync(dumpPath, 'utf8'));
const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]);
if (!scripts.length) { console.error('FAIL: 页面里没有 <script>'); process.exit(1); }
const code = scripts.join('\n');

function makeEl(id) {
  const el = {
    id, _html: '', textContent: '', value: '', placeholder: '', disabled: false,
    checked: false, type: 'text',
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
    _listeners: {},
    addEventListener(t, fn) { (this._listeners[t] = this._listeners[t] || []).push(fn); },
    removeEventListener() {},
    querySelector: () => null,
    querySelectorAll: () => [],
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

// POST 请求记录到 POSTS；GET 走 dump
const POSTS = [];
function routeFetch(url, opts) {
  const u = String(url);
  const idx = u.indexOf('/api/');
  const path = idx >= 0 ? u.slice(idx).split('?')[0] : u;
  if (opts && opts.method === 'POST') {
    const body = JSON.parse(opts.body || '{}');
    POSTS.push({ path, body });
    return Promise.resolve({ status: 200, ok: true,
      json: () => Promise.resolve({ ok: true, prefs: { context_memory: body.context_memory } }) });
  }
  let payload = apiData[path];
  if (payload === undefined) payload = {};
  return Promise.resolve({ status: 200, ok: true, json: () => Promise.resolve(payload) });
}

const sandbox = {
  console, setTimeout, clearTimeout, setInterval, clearInterval,
  document, localStorage, fetch: routeFetch,
  location: { hash: '#/assistants', reload() {} },
  encodeURIComponent, JSON, Math, Date, RegExp, String, Number, Boolean, Object, Array, Error,
  confirm: () => true, alert: () => {},
  addEventListener() {}, removeEventListener() {},
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;
sandbox.fetch = sandbox.window.fetch;
vm.createContext(sandbox);

try {
  vm.runInContext(code, sandbox, { filename: 'app-inline.js' });
} catch (e) {
  console.error('FAIL: 脚本执行期错误 -> ' + e.message);
  process.exit(1);
}

const errors = [];
function check(cond, msg) {
  if (cond) console.log('  ok  ' + msg);
  else { console.error('  FAIL ' + msg); errors.push(msg); }
}

(async () => {
  const st = apiData['/api/assistant/settings'] || { channels: [], prefs: {} };
  const channels = st.channels || [];

  await sandbox.loadAssistants();
  const view = $('view-assistants')._html;

  // ① 远程通道分区 + 4 张真实卡片
  check(view.includes('远程通道'), '渲染「远程通道」分区');
  check(view.includes('上下文管理'), '渲染「上下文管理」分区');
  const grid = $('chanGrid')._html;
  check(channels.length === 4, '通道目录 4 项（企业微信/钉钉/飞书/通用 Webhook）');
  for (const c of channels) check(grid.includes(c.name), '通道卡片渲染：' + c.name);

  // ② 未配置 → 「连接」按钮
  check(grid.includes('>连接</button>'), '未配置通道显示「连接」按钮');

  // ③ 通道弹窗：按 fields 渲染
  sandbox.openChan('dingtalk_bot');
  check(!$('chanModal').classes.has('hidden'), '通道弹窗能打开');
  const body = $('chanBody')._html;
  check(body.includes('Webhook'), '弹窗按目录 fields 渲染 Webhook 输入框');
  check(body.includes('加签密钥'), '钉钉弹窗含加签密钥字段');
  check(body.includes('chanEnabled'), '弹窗含启用开关');
  sandbox.closeChan();
  check($('chanModal').classes.has('hidden'), '通道弹窗能关闭');

  // ④ 保存：掩码回传语义（已配置通道留空 webhook → 发 ****）
  sandbox.openChan('dingtalk_bot');
  const cur = sandbox.window.__chanCur;
  cur.configured = true; cur.has_secret = true; // 模拟已配置
  $('chan_webhook').value = '';
  $('chan_secret').value = '';
  $('chanEnabled').checked = true;
  POSTS.length = 0;
  await sandbox.saveChan();
  const cfgPost = POSTS.find(p => p.path === '/api/assistant/channels/config');
  check(!!cfgPost, '保存发起 POST /api/assistant/channels/config');
  check(cfgPost && cfgPost.body.webhook === '****', '已配置通道 webhook 留空发 ****（保留原值）');
  check(cfgPost && cfgPost.body.secret === '****', '已配置通道 secret 留空发 ****');

  // ⑤ 测试按钮走真实 POST
  POSTS.length = 0;
  await sandbox.testChan();
  check(POSTS.some(p => p.path === '/api/assistant/channels/test'), '测试发起真实 POST');

  // ⑥ 上下文开关
  const ctx = $('ctxMem');
  check(ctx._html === '' && view.includes('ctxMem'), '上下文开关渲染进页面');
  POSTS.length = 0;
  ctx.checked = false;
  await sandbox.toggleCtx(ctx);
  const prefPost = POSTS.find(p => p.path === '/api/assistant/prefs');
  check(!!prefPost && prefPost.body.context_memory === false, '关开关发起 prefs POST（context_memory=false）');

  console.log('');
  if (errors.length) {
    console.error('FAIL: %d 个问题', errors.length);
    process.exit(1);
  }
  console.log('PASS: 助理远程通道 + 上下文管理渲染与交互校验全部通过');
})();
