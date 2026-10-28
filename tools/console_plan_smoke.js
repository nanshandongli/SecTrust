/**
 * console_plan_smoke.js —— 「新建任务」拆解面板渲染与交互冒烟
 *
 * 锁住的承诺：
 * 1. 面板有**双模式**：「一句话拆成流水线」（新）与「手动登记一条」（旧）。
 * 2. 拆解结果渲染：工序数 / 波次数 / 复杂度档 / 各道工序 / 待补参数警示。
 * 3. 缺参数时**必须显示警示**（不让模型乱猜），不是静默通过。
 * 4. 脚本可执行（Node vm + DOM 桩）——防「只验 HTTP 200 = 没验渲染」。
 *
 * 用法：node tools/console_plan_smoke.js <app_html> <api_dump.json>
 */
const fs = require('fs');
const vm = require('vm');

const htmlPath = process.argv[2] || 'data/app_html_snap.html';
const dumpPath = process.argv[3] || 'data/api_dump.json';

let pass = 0, fail = 0;
function ok(cond, msg) {
  if (cond) { pass++; console.log('  ok  ' + msg); }
  else { fail++; console.log('  FAIL ' + msg); }
}

const html = fs.readFileSync(htmlPath, 'utf8');
const dump = JSON.parse(fs.readFileSync(dumpPath, 'utf8'));

// ---- 抽取 script 块 ----
const scriptMatch = html.match(/<script>([\s\S]*?)<\/script>/);
if (!scriptMatch) { console.log('FAIL: 页面里没有 script 块'); process.exit(1); }
const script = scriptMatch[1];

// ---- DOM 桩 ----
const elements = {};
function makeEl(id) {
  const el = {
    id, value: '', textContent: '', innerHTML: '', style: {}, dataset: {},
    disabled: false,
    classList: { _s: new Set(), add(c){this._s.add(c);}, remove(c){this._s.delete(c);},
                 toggle(c,f){ if(f===undefined){this._s.has(c)?this._s.delete(c):this._s.add(c);} else {f?this._s.add(c):this._s.delete(c);} },
                 contains(c){return this._s.has(c);} },
    querySelector: () => null, querySelectorAll: () => [],
    addEventListener: (ev, fn) => { (el._h ||= {}); (el._h[ev] ||= []).push(fn); },
    appendChild: (c) => el,
    setAttribute: () => {}, getAttribute: () => null,
  };
  el.click = () => {};
  return el;
}
function getEl(id) {
  if (!elements[id]) elements[id] = makeEl(id);
  return elements[id];
}

const fetchCalls = [];
const sandbox = {
  console,
  setTimeout: (fn) => { try { fn(); } catch(e) {} },
  location: { hash: '#/newtask' },
  fetch: async (url, opts) => {
    fetchCalls.push({ url, opts });
    if (url === '/api/plan/preview') {
      return { ok: true, json: async () => ({
        ok: true, template: 'research_report', tier: 'medium',
        cost: { multiplier_vs_single_ai: 5 },
        needs_input: ['channels'],
        waves: [['数据拉取'], ['分析建模'], ['报告撰写']],
        subtasks: [
          { key: 'data', name: '数据拉取', instruction: '拉取最新数据', needs_input: [] },
          { key: 'analysis', name: '分析建模', instruction: '深度分析', needs_input: ['channels'] },
          { key: 'writing', name: '报告撰写', instruction: '写成报告', needs_input: [] },
        ],
      })};
    }
    if (url === '/api/plan/build') {
      return { ok: true, json: async () => ({ ok: true, plan_id: 'plan-x', created: ['plan-x/data','plan-x/analysis','plan-x/writing'] }) };
    }
    if (url.startsWith('/api/')) {
      const key = url.split('?')[0];
      if (dump[key] !== undefined) return { ok: true, json: async () => dump[key] };
      return { ok: true, json: async () => ({}) };
    }
    return { ok: true, json: async () => ({}) };
  },
  window: {},
  document: {
    getElementById: getEl,
    querySelector: () => null,
    querySelectorAll: () => [],
    createElement: (tag) => makeEl('created-' + tag),
    addEventListener: () => {},
  },
  addEventListener: () => {},
  navigator: { userAgent: 'node' },
  localStorage: { getItem: () => null, setItem: () => {} },
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;
sandbox.window.addEventListener = () => {};
sandbox.window.fetch = sandbox.fetch;

try {
  vm.createContext(sandbox);
  vm.runInContext(script, sandbox, { timeout: 15000 });
} catch (e) {
  console.log('FAIL: script 执行异常: ' + e.message);
  process.exit(1);
}

console.log('== 拆解面板 ==');

// 1) 双模式存在
ok(/id="ntModeSeg"/.test(html), '有模式切换控件 ntModeSeg');
ok(/data-mode="plan"/.test(html), '存在「一句话拆成流水线」模式');
ok(/data-mode="single"/.test(html), '存在「手动登记一条」模式');

// 2) 关键函数存在
ok(/function previewPlan\(\)/.test(script), 'previewPlan 函数存在');
ok(/function buildPlan\(\)/.test(script), 'buildPlan 函数存在');
ok(/function setNtMode\(/.test(script), 'setNtMode 函数存在');

// 3) 端点调用正确
ok(/'\/api\/plan\/preview'/.test(script), '调用 /api/plan/preview');
ok(/'\/api\/plan\/build'/.test(script), '调用 /api/plan/build');
ok(/'\/api\/plan\/templates'/.test(script), '调用 /api/plan/templates');

// 4) 渲染拆解结果：工序/波次/复杂度/待补警示
const previewSrc = (script.match(/async function previewPlan\(\)\{[\s\S]*?\n\}/) || [''])[0];
ok(/subtasks/.test(previewSrc), '渲染工序清单');
ok(/waves/.test(previewSrc), '渲染并行波次');
ok(/planTierLabel/.test(previewSrc), '渲染复杂度档');
ok(/needs_input/.test(previewSrc), '渲染待补参数警示');
ok(/按此建图并创建任务/.test(previewSrc), '有「按此建图」按钮');

// 5) 待补参数必须有警示文案（不能静默）
ok(/不会让模型乱猜/.test(previewSrc), '缺参数时明说不会让模型乱猜');

// 6) 真的能跑：调 previewPlan 并检查渲染结果
getEl('ntReq').value = '写一份 2026 年中国新能源汽车行业分析报告';
try {
  const fn = sandbox.previewPlan || (vm.runInContext('previewPlan', sandbox));
  const p = fn();
  if (p && typeof p.then === 'function') {
    p.then(() => {
      const out = getEl('ntPlanOut').innerHTML;
      ok(out.includes('数据拉取'), '预览结果渲染出工序名');
      ok(out.includes('ntBuild'), '预览结果渲染出建图按钮');
      ok(out.includes('channels'), '待补参数出现在预览里');
      console.log('\n' + (fail === 0 ? 'PASS' : 'FAIL') + ': ' + pass + ' 项通过 / ' + fail + ' 项失败');
      process.exit(fail === 0 ? 0 : 1);
    }).catch(e => {
      console.log('FAIL: previewPlan 执行异常: ' + e.message);
      process.exit(1);
    });
  }
} catch (e) {
  console.log('FAIL: previewPlan 调用异常: ' + e.message);
  process.exit(1);
}
