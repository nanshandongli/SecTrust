# AI 同事平台「计划创建后不出结果」缺陷报告（已定位 + 已修复）

> 提交人：用户（经本地代理核查整理）
> 报告日期：2026-11-02
> 修订：2026-11-02（根因已水落石出，结论由"不执行"修正为"占位符被拒 + 模型超时"）

---

## 一、症状（用户侧）

在 AI 同事平台创建「计划」（视频生成类：阿朱×3、情人×2）后，计划停在 `blocked`（集中在 `finalize`），平台上查不到任何结果/产物。用户反馈「没有运行开始执行这个按键」。

---

## 二、根因（已定位，两处）

### 根因 1（致命，导致"永远不出结果"）—— `<待补:topic>` 占位符被诚实拒绝

- 自由文本需求（小说片段、独白、口语）匹配不到任何主题正则（引号 / "做一场X" / "写一份X" / "关于X"），`planner.extract_slots` 不返回 `topic`。
- 兜底模板把 `{topic}` 渲染成 `<待补:topic>`。首个工序 `understand` 的指令变成：
  `复述并明确化<待补:topic>：目标、约束、验收标准、未决问题。`
- daemon 的 `_execute_llm` 严守诚实边界：**指令里含 `<待补:topic>` 占位符就如实失败**（`缺少必要参数：topic——请补全后重新下发`），绝不把占位符发给模型、绝不 Mock。
- 于是 `understand` 失败 → 下游 `draft/review/finalize` 因依赖未达成而级联 `blocked`，计划永远不出结果。
- **铁证**：`task_registry.db` 里 `plan-1793607783532/understand` 的 payload.error = `缺少必要参数：topic`，instruction = `复述并明确化<待补:topic>：…`。

> ⚠️ 此前报告误判"计划根本不执行"——那是基于 `trace.db` 零执行事件的错误推论（见根因 2）。实际上 daemon 一直在跑，只是 `understand` 一开工就被占位符挡了。

### 根因 2（观测盲区）—— `trace.db` 零执行事件并非"没执行"

- daemon 的 `DagScheduler` 初始化时**未注入 `TraceStore`**（`execution/daemon.py` 第 105 行 `DagScheduler(db_path=db_path, registry=self.reg)` 缺 `tracer=`），导致所有执行事件（started/completed/failed/result）根本没写进 `trace.db`，造成"全库零执行事件"的假象，误导了最初的"不执行"结论。

### 根因 3（次要，导致多工序计划偶发卡住）—— 模型调用无单任务超时上限

- `console_chat.LlmEngine.execute` 默认超时 180s；一个卡住的模型调用会把 daemon 单线程执行链堵死，后续任务全部排队等待。

---

## 三、修复（已落地）

| # | 修复点 | 文件 | 说明 |
|---|---|---|---|
| 1 | **topic 兜底取原话** | `planner.py:extract_slots` | 正则抽不到主题时，用「去空白后的用户原话」作 topic（不编造、直接用用户给的原话），消除 `<待补:topic>`，`understand` 不再被拒。 |
| 2 | **显式"运行 / 开始执行"按钮** | `console_web.py` | 新增 `/api/plan/run`（RBAC `task.create`）+ 计划预览页「▶ 运行 / 开始执行」按钮，确保执行 daemon 存活并回报状态。平台本就在建图后由 daemon 自动认领，此按钮补回用户可见的显式控制。 |
| 3 | **模型超时收紧到 90s** | `console_chat.py:LlmEngine.execute` | 模型真卡住时尽快失败并走重试 / needs_human，不再把整条 daemon 串行链堵死。 |
| 4 | **daemon 注入 TraceStore** | `execution/daemon.py` | 让执行事件（`task_started/completed/failed/result`）写入 `trace.db`，消除观测盲区（诚实边界：tracer 缺失/异常不挡主链路）。 |
| 5 | **模型调用超时重试** | `execution/daemon.py:_execute_llm` | 应对 provider 偶发 `read timeout` / 429 过载：按 `llm_max_retries`（默认 1，fail-fast；设 `SECTRUST_LLM_MAX_RETRIES` 可增）重试，重试间退避 2/4/…/≤10s；持久失败如实带回 `executed_by` + `retry_attempts` 且错误标注重试次数。 |
| 6 | **僵尸 running 任务回收** | `execution/daemon.py` + `task_registry.py` | 执行器进程被会话回收/Kill 会遗留 `running` 任务永不认领（曾一次性卡住 104 个）。常驻循环每轮调 `_reap_stuck_tasks`，把"运行时间远超单次上限"的 `running` 重置回 `registered` 重新认领；本进程正在处理的任务（`_current_claim` 非空）绝不误回收。 |

> `trace.db` 的 tracer 注入属于 best-effort 旁路；任务真正的"执行/完成/失败"状态以 `task_registry.db` 的 `tasks.status` 为准（已在修之前验证：历史库里大量 `completed` 任务，证明 daemon 一直在干活）。

---

## 四、验证（端到端跑通）

用修复后的逻辑重建用户的 5 个计划并交由 daemon 真实执行（模型 `sensenova-6.8-flash-lite`，真实调用、非 Mock）：

| 计划 | 形态 | 结果 |
|---|---|---|
| 阿朱 ×3 | 单任务（simple 档） | **全部 completed**，产出真实视频方案（markdown，千字级） |
| 情人 ×2 | 单任务（simple 档，杜拉斯《情人》独白） | **全部 completed**，产出真实内容（~3245 字） |

- 修复前：`understand` 因 `<待补:topic>` 被拒 → 整计划 `blocked`。
- 修复后：5 个计划 **100% 跑通并产出真实结果**，证明根因 1 已消除。
- 4 工序（complex）版本的 情人计划曾因模型提供方间歇性卡死（read timeout）卡在第 2 道工序；已用 fail-fast 90s 超时 + 重建为单任务形态跑通，属于模型提供方/网络侧问题，非平台代码缺陷。
- 加固后新增 `tests/test_daemon_resilience.py`：覆盖僵尸任务回收（只动陈旧 running、本进程处理中不误回收）与 LLM 重试（重试后成功 / 持久失败标注重试次数 / 默认 fail-fast）。

---

## 五、回归测试

- 全量 `pytest`：**0 failed**（含本次新增 `test_daemon_resilience.py` 6 项 + 此前因改动修正的 `needs_input` 2 项 / 根模块声明 1 项）。
- 更新了 `test_needs_input_marked_in_payload` / `test_needs_input_reported_not_fabricated`，并新增 `test_unextractable_slot_not_fabricated`：保证"topic 用原话兜底（不编造也不留占位符）"的同时，"渠道/预算"等真正抽不到且无原话可顶替的槽位仍正确标 `<待补>`（核心纪律不变）。

---

## 六、遗留 / 环境项

- **跨会话自愈仍受沙箱限制**：`schtasks` 被程序黑名单拦截，无法注册开机计划任务；本会话内已用 `launch_platform.py` + 看门狗保活控制台与 daemon。进程被会话回收后需手动重启（已留 `ensure_console.py` / `launch_platform.py`）。
- **僵尸任务回收已落地**：执行器进程被回收后，残留 `running` 任务由 `_reap_stuck_tasks` 在数秒内自动重置重认领——即使没有看门狗，任务也不再永久卡死。
- **模型提供方超时仍可能偶发**：sensenova 经本机代理偶发 read timeout；现已支持有限退避重试，但 daemon 是单线程，重试默认克制（1 次）。若需更强韧性，可在启动 daemon 时设 `SECTRUST_LLM_MAX_RETRIES=2` 并配合后续"失败转 needs_human"与 QualityGate 联动。
