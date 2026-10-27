/**
 * console_connectors_smoke.js —— 技能连接器画廊（WorkBuddy 式）的渲染冒烟
 *
 * 目的：在不启动浏览器的前提下，验证连接器页的真实渲染结果：
 *   ① 五个分区（内置连接器 / 已安装技能 / 自定义连接器 / SkillHub 推荐 / 搜索结果）齐全；
 *   ② 卡片含徽章、名称、描述、绿色对勾（已就绪）或 + 按钮（可安装）；
 *   ③ 推荐位来自 dump 里的真实 SkillHub 数据（有数据时渲染出卡片，不可达时如实提示）；
 *   ④ 搜索会真实调 fetch 并渲染 + 安装按钮；
 *   ⑤ 自定义连接器弹窗能打开/关闭，移除链接带回调。
 *
 * 用法：node tools/console_connectors_smoke.js <app_html> <api_dump_json>
 */
const fs = require('fs');
const vm = require('vm');

const appHtml = process.argv[2];
const dumpPath = process.argv[3];
if (!appHtml || !dumpPath) {
  console.error('用法: node tools/console_connectors_smoke.js <app_html> <api_dump_json>');
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

// fetch 路由：dump 命中直接回；搜索接口在 dump 之外，用样例数据模拟（验证渲染路径）
const SEARCH_SAMPLE = { ok: true, results: [
  { slug: 'demo-x', name: '演示技能X', desc: '用于冒烟的搜索结果', author: 'tester', category: 'demo' },
  { slug: 'demo-y', name: '演示技能Y', desc: '第二个结果', author: '', category: '' },
] };
function routeFetch(url) {
  const u = String(url);
  const idx = u.indexOf('/api/');
  const path = idx >= 0 ? u.slice(idx).split('?')[0] : u;
  let payload;
  if (path === '/api/connectors/skillhub/search') payload = SEARCH_SAMPLE;
  else payload = apiData[path];
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
  location: { hash: '#/connectors', reload() {} },
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
  const conn = apiData['/api/connectors'] || {};
  const featured = apiData['/api/connectors/skillhub/featured'] || { ok: false, results: [] };

  await sandbox.loadConnectors();
  await new Promise(r => setTimeout(r, 80)); // 推荐位是异步加载的，等一拍
  const view = $('view-connectors')._html;

  // ① 分区齐全
  for (const sec of ['内置连接器', '已安装技能', '自定义连接器', 'SkillHub 推荐', '搜索结果']) {
    check(view.includes(sec), '分区标题渲染：' + sec);
  }
  check(view.includes('自定义连接器') && view.includes('openConnCustom'), '有「自定义连接器」入口');
  check(view.includes('connQ'), '顶栏有搜索框');

  // ② 内置连接器卡片：绿色对勾
  const builtIn = $('connBuiltIn')._html;
  const builtinCount = (conn.local_connectors || []).length;
  check(builtinCount ? builtIn.includes('conn-check') : builtIn.includes('empty'),
    '内置连接器卡片渲染（' + builtinCount + ' 项，含对勾/空态）');

  // ③ 已安装技能
  const installed = $('connInstalled')._html;
  const skills = apiData['/api/skills'] || [];
  if (skills.length) {
    check(installed.includes(skills[0].name), '已安装技能渲染出卡片：' + skills[0].name);
    check(installed.includes('conn-check'), '已安装技能带绿色对勾');
  } else {
    check(installed.includes('empty'), '无技能时显示诚实空态');
  }

  // ④ 推荐位：真实数据渲染 + 可安装 +
  const feat = $('connFeatured')._html;
  if (featured.ok && featured.results.length) {
    const first = featured.results[0];
    check(feat.length > 200, '推荐位渲染出卡片（' + feat.length + ' chars）');
    check(feat.includes((first.name || first.slug || '').slice(0, 6)), '推荐位含真实条目：' + (first.name || first.slug));
    check(feat.includes('conn-plus'), '推荐位条目带 + 安装按钮');
  } else {
    check(feat.includes('不可达') || feat.includes('empty'), 'SkillHub 不可达时如实提示（不伪造）');
  }

  // ⑤ 自定义连接器区
  const custom = $('connCustom')._html;
  const customs = conn.custom_connectors || [];
  check(customs.length ? custom.includes(customs[0].name) : custom.includes('empty'),
    '自定义连接器区渲染（' + customs.length + ' 项）');

  // ⑥ 搜索：真实 fetch → + 按钮
  $('connQ').value = '演示';
  await sandbox.connSearch();
  const results = $('connResults')._html;
  check(results.includes('演示技能X'), '搜索结果渲染出第一项');
  check(results.includes('conn-plus'), '搜索结果带 + 安装按钮');
  check(($('connSearchCnt').textContent || '').includes('2'), '搜索计数更新（2 项）');

  // ⑦ 弹窗开关
  sandbox.openConnCustom();
  check(!$('connModal').classes.has('hidden'), '自定义连接器弹窗能打开');
  check($('ccMsg').textContent.length > 0, '弹窗有说明文案');
  sandbox.closeConnCustom();
  check($('connModal').classes.has('hidden'), '弹窗能关闭');

  console.log('');
  if (errors.length) {
    console.error('FAIL: %d 个问题', errors.length);
    process.exit(1);
  }
  console.log('PASS: 技能连接器画廊渲染与交互校验全部通过');
})();
