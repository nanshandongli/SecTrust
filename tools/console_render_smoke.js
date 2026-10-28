/**
 * console_render_smoke.js —— 协作审计台 SPA 渲染冒烟（零依赖 DOM 桩）
 *
 * 目的：在**不启动浏览器**的前提下，用真实 API JSON 驱动 console_web.py 里的前端脚本，
 * 逐个执行每个面板的 loader，捕获 JS 运行时错误（语法错、undefined、取错字段等）。
 *
 * 依赖：先用 tools/dump_console_api.py 把真实接口数据落盘。
 * 用法：node tools/console_render_smoke.js <app_html> <api_dump_json>
 */
const fs = require('fs');
const vm = require('vm');

const appHtml = process.argv[2];
const dumpPath = process.argv[3];
const html = fs.readFileSync(appHtml, 'utf8');
const apiData = JSON.parse(fs.readFileSync(dumpPath, 'utf8'));

// ---- 抽出页面里的主 <script>（跳过注入 __ROLES__ 的那一段也一并包含）----
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
    scrollIntoView() {},
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

// fetch 桩：按路径（去 query）匹配落盘数据
function routeFetch(url) {
  const u = String(url);
  const idx = u.indexOf('/api/');
  const path = idx >= 0 ? u.slice(idx).split('?')[0] : u;
  let payload = apiData[path];
  // /api/trace/<id> 与 /api/audit/approval/<id> 用带 id 的键
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
  location: { hash: '#/workspace' },
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

// ---- 逐个执行面板 loader ----
const loaders = ['loadWorkspace', 'loadNewTask', 'loadRoster', 'loadAssistants',
  'loadConnectors', 'loadSchedule', 'loadLibrary', 'loadOverview', 'loadChat',
  'loadTasks', 'loadTraceList', 'loadCost', 'loadApprovals', 'loadSecurity',
  'loadSkills', 'loadSettings'];

(async () => {
  for (const name of loaders) {
    const fn = sandbox[name];
    if (typeof fn !== 'function') { errors.push(name + ': 未定义'); continue; }
    try {
      await fn();
      const viewKey = ({ loadTraceList: 'trace' })[name] || name.replace('load', '').toLowerCase();
      const el = $('view-' + viewKey);
      const rendered = el && el._html.length > 0;
      if (!rendered) errors.push(name + ': 渲染结果为空');
      else console.log('  ok  %s (%d chars)', name, el._html.length);
    } catch (e) {
      errors.push(name + ': ' + e.message);
    }
  }
  // 新增：审计动作与调度触发相关函数存在性
  for (const fnName of ['runAction', 'scFire', 'scDue', 'postJSON', 'doLogin', 'restoreSession']) {
    if (typeof sandbox[fnName] !== 'function') errors.push(fnName + ': 未定义');
  }

  // ---- 第一性原理分析（对话面板分段切换 → /api/first-principles 渲染）----
  for (const f of ['id="chatModeSeg"', 'data-mode="fp"', 'id="fpResult"',
                   '/api/first-principles', 'runFirstPrinciples', 'badgefp', 'fp-verdict']) {
    if (!html.includes(f)) errors.push('桌面端页面缺少片段: ' + f);
  }
  if (typeof sandbox.runFirstPrinciples !== 'function') {
    errors.push('runFirstPrinciples: 未定义');
  } else {
    const FP_PAYLOAD = {
      topic: '为什么审批流程这么慢',
      engine: 'llm',
      model: 'openai/fake',
      perspectives: [
        { id: 'first_principles', name: '第一性原理视角', content: '回到本质重构', classification: 'first_principles', essence_score: 9, reasoning: '回到底层事实重构' },
        { id: 'analogy_break', name: '反类比 / 破假设视角', content: '推翻隐含假设', classification: 'mixed', essence_score: 6, reasoning: '部分推翻假设' },
        { id: 'novelty_skin', name: '标新立异（换皮）对照', content: '只是换皮', classification: 'novelty_seeking', essence_score: 2, reasoning: '只是换皮' },
      ],
      verdict: { best_id: 'first_principles', best_classification: 'first_principles', summary: '真正回到本质的是「第一性原理视角」', counts: { first_principles: 1, mixed: 1, novelty_seeking: 1, unknown: 0 } },
    };
    const realFetch = sandbox.fetch;
    try {
      sandbox.fetch = () => Promise.resolve({ status: 200, ok: true, json: () => Promise.resolve(FP_PAYLOAD) });
      await sandbox.runFirstPrinciples('为什么审批流程这么慢');
      const html2 = $('fpResult')._html || '';
      for (const f of ['class="fp-card"', 'badgefp first_principles', 'badgefp novelty_seeking',
                       'badgefp mixed', 'class="fp-verdict"', '本质度 9/10', 'width:90%',
                       '第一性原理视角', '真正回到本质']) {
        if (html2.indexOf(f) < 0) errors.push('第一性原理渲染缺少: ' + f);
      }
      if (html2.indexOf('class="fp-card"') === html2.lastIndexOf('class="fp-card"')) {
        errors.push('第一性原理：只渲染了 1 张视角卡（应为 3 张）');
      }
      console.log('  ok  runFirstPrinciples (%d chars)', html2.length);
    } catch (e) {
      errors.push('runFirstPrinciples: ' + e.message);
    }
    // 诚实降级：engine=unavailable 时渲染警示而非伪造卡片
    try {
      sandbox.fetch = () => Promise.resolve({
        status: 200, ok: true,
        json: () => Promise.resolve({ engine: 'unavailable', error: 'no_llm_available', perspectives: [], verdict: { summary: '当前没有可用的算力模型' } }),
      });
      await sandbox.runFirstPrinciples('主题');
      const h3 = $('fpResult')._html || '';
      if (h3.indexOf('无法分析') < 0) errors.push('第一性原理：unavailable 时未渲染诚实提示');
      if (h3.indexOf('class="fp-card"') >= 0) errors.push('第一性原理：unavailable 时不应渲染视角卡');
      console.log('  ok  runFirstPrinciples(unavailable 降级)');
    } catch (e) {
      errors.push('runFirstPrinciples(unavailable): ' + e.message);
    }
    sandbox.fetch = realFetch;
  }

  // ---- 落盘结果渲染（op=write_files 的结构化 result 直接消费）----
  const RES = {
    op: 'write_files', root: 'C:\\tmp\\repo',
    written: [
      { path: 'src/calc.py', bytes: 31, sha256: '8f75a68646c879fd0cff5c02708c010fb1c01d67ef9930d4e419106feb7897aa', overwrote: false },
      { path: 'src/util.py', bytes: 25, sha256: 'e2ad06d0f0362f04699275c8159f6c75eabef10dee9ddb1a23769216092692eb', overwrote: true },
    ],
    blocked: [{ path: '.env', reason: '拒绝写入敏感文件：.env' }],
    scan: { should_fail: false, summary: { total: 0 }, findings: [] },
    output: '已落盘 2 个文件（根目录：C:\\tmp\\repo）',
  };
  if (typeof sandbox.renderWriteFiles !== 'function') {
    errors.push('renderWriteFiles: 未定义');
  } else {
    const h = sandbox.renderWriteFiles(RES);
    for (const f of ['代码落盘结果', 'src/calc.py', 'src/util.py', '覆盖',
                     '拒写', '.env', '拒绝写入敏感文件', '密钥扫描', '闸门']) {
      if (h.indexOf(f) < 0) errors.push('落盘结果渲染缺少: ' + f);
    }
    console.log('  ok  renderWriteFiles (%d chars)', h.length);
    // 高危：必须渲染"触发"，且把命中规则/严重度摆出来
    const BAD = JSON.parse(JSON.stringify(RES));
    BAD.scan = { should_fail: true, summary: { total: 1 }, findings: [
      { severity: 'critical', rule_name: 'Slack Bot Token', path: 'src/leak.py',
        line_no: 1, secret_masked: 'xoxb-…' }] };
    const h2 = sandbox.renderWriteFiles(BAD);
    if (h2.indexOf('触发') < 0) errors.push('落盘结果：高危时未渲染"触发"');
    if (h2.indexOf('Slack Bot Token') < 0) errors.push('落盘结果：未渲染扫描命中规则');
    if (h2.indexOf('critical') < 0) errors.push('落盘结果：未渲染严重度');
    console.log('  ok  renderWriteFiles(高危) (%d chars)', h2.length);
  }

  // ---- 任务详情：点开任务 → /api/task/<id> ----
  if (typeof sandbox.showTaskDetail !== 'function') {
    errors.push('showTaskDetail: 未定义');
  } else {
    const realFetch2 = sandbox.fetch;
    sandbox.fetch = () => Promise.resolve({ status: 200, ok: true,
      json: () => Promise.resolve({
        task_id: 't1', status: 'completed', op: 'write_files', name: '代码落盘',
        runner_id: 'exec-1', instruction: '把代码落盘',
        result: { op: 'write_files', root: 'C:\\tmp',
                  written: [{ path: 'a.py', bytes: 3, sha256: 'f'.repeat(64), overwrote: false }],
                  blocked: [], scan: { should_fail: false, summary: { total: 0 } } } }) });
    try {
      await sandbox.showTaskDetail('t1');
      const hd = $('taskDetail')._html || '';
      for (const f of ['t1', '代码落盘', 'a.py']) {
        if (hd.indexOf(f) < 0) errors.push('任务详情渲染缺少: ' + f);
      }
      console.log('  ok  showTaskDetail (%d chars)', hd.length);
    } catch (e) { errors.push('showTaskDetail: ' + e.message); }
    sandbox.fetch = realFetch2;
  }

  // ---- 检查更新（左下方浮层）：关键是查不到不许显示"已是最新" ----
  if (typeof sandbox.checkUpdate !== 'function') {
    errors.push('checkUpdate: 未定义');
  } else {
    const realFetch3 = sandbox.fetch;
    // a) 已是最新
    sandbox.fetch = () => Promise.resolve({ status: 200, ok: true,
      json: () => Promise.resolve({ state: 'up_to_date', message: '已是最新：本地 HEAD 与远端 origin 一致',
        local: { commit: 'abc123456789', branch: 'main', describe: 'v1.0', dirty: false, dirty_files: 0 } }) });
    try {
      await sandbox.checkUpdate();
      const hu = $('updatePop')._html || '';
      for (const f of ['已是最新', 'abc123456789', 'main', '自动检查']) {
        if (hu.indexOf(f) < 0) errors.push('检查更新渲染缺少: ' + f);
      }
      console.log('  ok  checkUpdate(up_to_date) (%d chars)', hu.length);
    } catch (e) { errors.push('checkUpdate: ' + e.message); }
    // b) 无法判断：必须显示"无法判断"+原因，绝不能显示"已是最新"
    sandbox.fetch = () => Promise.resolve({ status: 200, ok: true,
      json: () => Promise.resolve({ state: 'unknown', reason: '无法访问远端 origin：模拟网络不可达',
        local: { commit: 'abc123456789', branch: 'main', describe: 'v1.0', dirty: false, dirty_files: 0 } }) });
    try {
      await sandbox.checkUpdate();
      const hk = $('updatePop')._html || '';
      if (hk.indexOf('无法判断') < 0) errors.push('检查更新：unknown 未显示"无法判断"');
      if (hk.indexOf('模拟网络不可达') < 0) errors.push('检查更新：unknown 未显示原因');
      if (hk.indexOf('已是最新') >= 0) errors.push('检查更新：unknown 时不得显示"已是最新"');
      console.log('  ok  checkUpdate(unknown 不谎报)');
    } catch (e) { errors.push('checkUpdate(unknown): ' + e.message); }
    sandbox.fetch = realFetch3;
  }
  console.log('');
  if (errors.length) {
    console.error('FAIL: %d 个问题', errors.length);
    errors.forEach(e => console.error('  - ' + e));
    process.exit(1);
  }
  console.log('PASS: 全部 %d 个面板渲染无运行时错误', loaders.length);
})();
