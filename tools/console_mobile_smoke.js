/**
 * console_mobile_smoke.js —— 手机远程控制台（mobile_console.html）的渲染冒烟
 *
 * 目的：在不启动浏览器的前提下，验证移动端页面：
 *   ① <script> 能被抽出并在 vm 里执行而不抛错（捕捉拼写/未定义函数）；
 *   ② 关键 DOM 结构齐全（登录表单、5 个底部标签、各视图容器）；
 *   ③ 同源调用的接口路径都已接好（/api/login /overview /chat /tasks/new
 *      /approvals/decide /assistant/settings 等）。
 *
 * 用法：node tools/console_mobile_smoke.js <mobile_console.html>
 */
const fs = require('fs');
const vm = require('vm');

const htmlPath = process.argv[2];
if (!htmlPath) {
  console.error('用法: node tools/console_mobile_smoke.js <mobile_console.html>');
  process.exit(2);
}
const html = fs.readFileSync(htmlPath, 'utf8');
const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]);
if (!scripts.length) { console.error('FAIL: 页面里没有 <script>'); process.exit(1); }
const code = scripts.join('\n');

// ---- DOM 桩 ----
function makeEl(id) {
  const el = {
    id: id || '', _html: '', textContent: '', value: '', placeholder: '', disabled: false,
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
    querySelector: () => makeEl('child'),
    querySelectorAll: () => [],
    getAttribute: () => null, setAttribute() {},
  };
  return el;
}
const ELEMENTS = {};
function byId(id) {
  const key = String(id);
  if (!ELEMENTS[key]) ELEMENTS[key] = makeEl(key);
  return ELEMENTS[key];
}
const document = {
  getElementById: byId,
  querySelector: sel => {
    if (typeof sel === 'string' && sel.charAt(0) === '#') return byId(sel.slice(1));
    return makeEl(sel); // 其它选择器返回哑元，保证链式赋值不崩
  },
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
// fetch 桩：返回已 resolve 的承诺，让 initLogin 的异步分支能跑完而不崩
const fetch = () => Promise.resolve({ status: 200, json: () => Promise.resolve({ roles: ['auditor'] }) });

let threw = null;
const sandbox = { document, localStorage, fetch, console, setTimeout, Promise, window: {} };
try {
  vm.createContext(sandbox);
  vm.runInContext(code, sandbox, { filename: 'mobile_console_inline.js' });
} catch (e) {
  threw = e;
}

const failures = [];
function expect(cond, msg) { if (!cond) failures.push(msg); }

expect(!threw, '脚本执行抛错: ' + (threw && threw.stack || threw));

// 登录态初始为未登录 → 登录屏应可见（不含 hidden），App 应隐藏
const loginEl = byId('login');
const appEl = byId('app');
expect(loginEl.classes.has('hidden') === false, '初始未登录时登录屏应可见');
expect(appEl.classes.has('hidden') === true, '初始未登录时主 App 应隐藏');

// 关键结构片段
const frags = [
  'id="login"', 'id="login-user"', 'id="login-role"', 'id="login-btn"',
  'id="app"', 'id="view"',
  '概览', '对话', '任务', '审批', '助理',
  'id="chat-log"', 'id="task-list"', 'id="ap-list"', 'id="as-content"',
  '/api/login', '/api/overview', '/api/chat', '/api/chat/models',
  '/api/tasks/new', '/api/approvals/decide', '/api/assistant/settings',
  '/api/assistant/channels/toggle', '/api/assistant/prefs',
];
for (const f of frags) {
  expect(html.includes(f), '移动端页面缺少片段: ' + f);
}

if (failures.length) {
  console.error('FAIL (' + failures.length + '):');
  failures.forEach(f => console.error('  - ' + f));
  process.exit(1);
}
console.log('PASS: 移动端控制台渲染冒烟通过（结构 + 接口接线 + 脚本可执行）');
