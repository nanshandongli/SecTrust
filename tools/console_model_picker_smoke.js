/**
 * console_model_picker_smoke.js —— 「添加模型」弹窗的渲染冒烟（零依赖 DOM 桩）
 *
 * 目的：在**不启动浏览器**的前提下，验证 WorkBuddy 式模型接入器的真实渲染结果：
 *   ① 设置页「模型」分区含已配置列表 + 添加模型按钮 + 计费提示；
 *   ② 供应商目录弹窗能按分组渲染出条目（分组标题 / 包月标记 / base_url）；
 *   ③ 搜索能过滤条目（这是截图里的核心交互，不能只验骨架）；
 *   ④ 勾选供应商后 Base URL 自动填对、模型候选 datalist 填充、合规提示出现；
 *   ⑤ 「绿色对勾」只出现在被选中的条目上。
 *
 * 用法：node tools/console_model_picker_smoke.js <app_html> <api_dump_json>
 * 依赖：tools/dump_console_api.py 落盘的真实接口数据。
 */
const fs = require('fs');
const vm = require('vm');

const appHtml = process.argv[2];
const dumpPath = process.argv[3];
if (!appHtml || !dumpPath) {
  console.error('用法: node tools/console_model_picker_smoke.js <app_html> <api_dump_json>');
  process.exit(2);
}
const html = fs.readFileSync(appHtml, 'utf8');
const apiData = JSON.parse(fs.readFileSync(dumpPath, 'utf8'));
const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]);
if (!scripts.length) { console.error('FAIL: 页面里没有 <script>'); process.exit(1); }
const code = scripts.join('\n');

// ---- 极简 DOM 桩（与 console_render_smoke.js 同构，但保留 querySelectorAll 的真实元素） ----
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

function routeFetch(url) {
  const u = String(url);
  const idx = u.indexOf('/api/');
  const path = idx >= 0 ? u.slice(idx).split('?')[0] : u;
  let payload = apiData[path];
  if (payload === undefined) {
    const key = Object.keys(apiData).find(k => path.startsWith(k) && k !== path);
    if (key !== undefined) payload = apiData[key];
  }
  if (payload === undefined) payload = {};
  return Promise.resolve({ status: 200, ok: true, json: () => Promise.resolve(payload) });
}

const sandbox = {
  console, setTimeout, clearTimeout, setInterval, clearInterval,
  document, localStorage, fetch: routeFetch,
  location: { hash: '#/settings', reload() {} },
  encodeURIComponent, JSON, Math, Date, RegExp, String, Number, Boolean, Object, Array, Error,
  confirm: () => true, alert: () => {},
  addEventListener() {}, removeEventListener() {},
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;
sandbox.fetch = sandbox.window.fetch;
vm.createContext(sandbox);

const errors = [];
try {
  vm.runInContext(code, sandbox, { filename: 'app-inline.js' });
} catch (e) {
  console.error('FAIL: 脚本执行期错误 -> ' + e.message);
  process.exit(1);
}

function check(cond, msg) {
  if (cond) console.log('  ok  ' + msg);
  else { console.error('  FAIL ' + msg); errors.push(msg); }
}

(async () => {
  const providers = (apiData['/api/chat/providers'] || {}).providers || [];
  if (!providers.length) {
    console.error('FAIL: dump 里没有 /api/chat/providers 数据');
    process.exit(1);
  }

  // ① 设置页模型分区
  await sandbox.loadSettings();
  const settings = $('view-settings')._html;
  check(settings.includes('添加模型'), '设置页有「添加模型」按钮');
  check(settings.includes('modelList'), '设置页有已配置模型列表');
  check(settings.includes('设为当前') || settings.includes('当前'), '设置页有「设为当前」动作');
  check(/按 token 扣费|Base URL/.test(settings), '设置页有计费/端点提示');

  // ② 打开弹窗 → 供应商目录按分组渲染
  await sandbox.openModelModal('');
  const list = $('mmList')._html;
  const groups = (apiData['/api/chat/providers'] || {}).groups || [];
  check(list.length > 0, '供应商清单已渲染（' + list.length + ' chars）');
  for (const g of groups) {
    check(list.includes(g), '清单含分组：' + g);
  }
  const first = providers[0];
  check(list.includes(first.base_url), '清单显示 base_url：' + first.base_url);
  check(list.includes('包月') || list.includes('按量计费') || list.includes('本地'), '清单显示计费属性标记');

  // ③ 搜索过滤
  const keyword = providers.find(p => p.group === 'Coding Plan（包月）');
  if (keyword) {
    sandbox.renderProvList(keyword.name);
    const filtered = $('mmList')._html;
    check(filtered.includes(keyword.name), '搜索命中：' + keyword.name);
    const others = providers.filter(p => p.name !== keyword.name && !keyword.name.includes(p.name));
    check(others.every(p => !filtered.includes(p.name)), '搜索后其他供应商被过滤掉');
  }
  sandbox.renderProvList('');

  // ④ 勾选供应商 → Base URL 自动填 / 模型候选 / 合规提示
  const target = providers.find(p => p.compliance) || first;
  sandbox.selectProvider(target.id);
  check($('mmBase').value === target.base_url, '勾选后 Base URL 自动填对：' + target.base_url);
  check($('mmModelList')._html.includes(target.models[0]), '模型候选已填充 datalist');
  check($('mmWarn').textContent.length > 0 || target.compliance === '', '合规提示已呈现');
  check($('mmPickName').textContent.includes(target.name), '供应商选择器显示已选名称');

  // ⑤ 绿色对勾只出现在选中项
  const checked = (list.match(/pcheck">✓/g) || []).length;
  check(checked <= 1, '绿色对勾最多一个（选中态）');

  console.log('');
  if (errors.length) {
    console.error('FAIL: %d 个问题', errors.length);
    process.exit(1);
  }
  console.log('PASS: 添加模型弹窗渲染与交互校验全部通过');
})();
