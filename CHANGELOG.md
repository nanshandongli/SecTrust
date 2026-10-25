# 更新日志

本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/)。
所有版本均为**未上架**状态（PyPI / MCP Registry 物料已就绪，实际发布待账号与凭据）。

---

## [0.9.0] — 未发布

### 新增
- **观测层：Trace 轨迹回放（`trace.py`）**——多 AI 协同出事时的"黑匣子"：
  - 每条任务/流水线一条 `trace_id`，贯穿拆解→派单→执行→审批→质检→
    收尾；`seq` 按 trace 单调分配，回放顺序即发生顺序，`after_seq` 游标
    支持增量拉取。
  - **哈希指针不存原文**：`content=` 只算 blake2b 指纹；敏感键
    （密钥/Cookie/Token 等，按名匹配）脱敏成 `{"redacted": true,
    "sha256": …}`；detail 超限截断并留全文指纹。回放时可重新投喂原文
    验真（`verify()`）——审计材料不囤敏感原文。
  - **留痕是旁路不是主链路**：四个挂点——`planner.build(trace_id=…)`
    传播 + `plan_built` 事件；`DagScheduler(tracer=…)` 自动记
    登记/认领/完成/失败（含级联阻塞名单）；`BrowserSkill(tracer=…,
    trace_id=…)` 记浏览器动作（成功/失败都留痕、type 的明文只留指纹）；
    手工 `trace_record` 覆盖审批/质检/成本快照等任何事件。tracer 缺失或
    故障绝不挡调度与浏览器动作。
  - 入口三件套：MCP `trace_start/record/show/list`（36 → 40 工具）、
    REST `GET /traces` 与 `GET /traces/{id}`（只读，仍走鉴权）、
    CLI（`--new/--record/--show/--task/--list/--stats/--verify/--finish`）。
  - 测试：`tests/test_trace.py` 19 项（含"直接读库文件断言敏感串不在"
    的最强证据、多线程 seq 不重号、跨实例）+ `tests/test_trace_wiring.py`
    14 项（四挂点 + 两入口 + tracer 故障不挡主链路 + 无 tracer 向后兼容）。
- **执行层（对照 Grok Bot 差距分析后开建，全部纯标准库零新依赖）**：
  - `execution/workspace.py`：每 bot_id 一个独立工作区（`work/` `outbox/`
    `state.json`），bot_id 白名单正则 + 相对路径越界拒绝（绝对路径/`..`），
    state 原子写（mkstemp + os.replace），`run()` 一律委托 `SandboxManager`。
  - `execution/daemon.py`：常驻执行守护。`claim_ready` 写锁内选+认领 →
    按 payload.op 派发（shell 走沙箱 / skill 激活 / noop）→ complete/fail；
    **审批门红线**：payload 带 `approval_action` 时先人工批准，
    被拒/超时 → `needs_human` 挂起不执行。Windows spawn 兼容（模块级入口）。
  - `scheduler.py`：纯标准库 cron 五段式（`*` `*/n` `a-b` `a-b/n` 列表；
    星期 0/7=周日；Vixie 日月或语义；366 天无命中返回 None 明说）+
    interval；`fire_due()` 到期建任务并推进 next_run，**登记失败不推进**，
    重启不重复触发。
  - `connectors/`：`base.py` 契约（未知 action 拒绝 / 未配置明确报错不返
    假数据 / HTTP ≥400 一律抛错）+ `github.py`（urllib REST，token 只进
    Header）+ `imap.py`（EmailRule/EmailTrigger：邮件命中 → 建任务 →
    **登记成功才 mark_seen**，不丢邮件；RFC2047 头正确解码）。
  - `proposal.py`：主动提议循环（chief of staff）。五条规则（停滞 / 无人认领 /
    审批积压 / 失败待处理 / needs_human 积压汇总）→ 提案进审批中心等人裁，
    **绝不自动执行**；冷却表 + 未决提案双重去重；可选 LLM 摘要
    （路由落 Mock 即缺 key 时返回 None，诚实降级规则文本）。
  - `execution/browser.py`：可编程浏览器。**手写最小 WebSocket 客户端**
    （纯标准库：握手/客户端掩码/分片重组/ping-pong/64 位长度）说 CDP：
    goto / get_text / click（坐标计算）/ type / screenshot / wait_for；
    CDP 起不来 → 诚实降级 Edge 一次性 headless 批处理（交互动作明确报错）。
    线上格式由独立实现的环回服务器互操作测试锁定；真 Edge 冒烟通过
    （点击真改 DOM、截图真 PNG）。
  - `threads.py`：会话线程持久化（thread + messages 落 SQLite），
    `history()` 取最近 N 条 + `before` 游标翻页，`search()` 关键词；
    **往不存在的线程 append 明确报错**（不自动兜）。
  - `voice.py`：语音输出，复用 multimodal `MediaRouter` 合成 TTS；
    缺 key 时 Mock 假音频明示 `fake=True`；渠道无语音能力时文本兜底
    附文件路径，不谎称已发语音。

### 修复
- **planner fanout 自建 Ballot 连接泄漏（真缺陷，新测试暴露）**：
  `Plan.build()` 开票时自建的 `Ballot()` 从不关闭（`-W error::ResourceWarning`
  下可复现）。修为自建实例用完即关、调用方传入的所有权归调用方。
- **看板 JS 数月不可渲染（真缺陷）**：HTML 模板是普通三引号字符串，
  JS `'\n等待: '` 的 `\n` 被 Python 解释成真换行 → 整个 `<script>` 块
  解析失败 → 看板从未在浏览器里渲染过（此前只验过 HTTP 200 + 骨架）。
  已修为 `'\\n等待: '` 并补 5 项逐字符扫描回归测试
  （`tests/test_dashboard_js.py`）。
- **WebSocket 握手丢帧（真缺陷，开发中捕获）**：`connect()` 读握手响应
  的局部 buffer 把紧随 101 到达的数据帧丢弃。修为多余字节保留进 `_buf`。
- **打包申报遗漏（回归测试捕获）**：`scheduler` / `connectors` /
  `proposal` / `execution` / `threads` / `voice` 未在 pyproject 声明，
  `test_release_packaging` 全数抓出并补齐。
- **history() 语义错误**：`limit=N` 原返回"最早 N 条"（聊天场景要的是
  "最近 N 条"）。修为倒序取 N 再翻正序，`before` 游标翻页随之正确。

### 纪律（新增，改动时不可破坏）
- `execution` 包"命令走 SandboxManager 不自己起 subprocess"对
  `browser.py` 是**文档化例外**（CDP 需长驻进程 + 调试端口 + 生命周期自管）。
- 提案只进审批中心等人裁；`rank` 式"推荐≠生效"边界在投票与提议两侧一致。
- 批处理模式交互动作明确报错；Mock 媒体/模型一律带 `fake` 明示。
- 连接管理：thread-local + 显式 `close()`；测试在 `-W error::ResourceWarning`
  下零泄漏。

---

## [0.8.0] — 未发布

### 变更
- **项目更名为 SecTrust**：发行名 `sectrust`，console 入口 `sectrust-mcp / sectrust-api / sectrust-runner`，
  MCP server 名与 SARIF 驱动名同步。
- **许可由 MIT 改为 AGPL-3.0-only**：防止托管服务方拿去闭源商用而不回馈源码；
  未来商业双许可可议。选择 AGPL 的时机刻意放在**第一个贡献者出现之前**——
  之后换许可需征得全体贡献者同意。

### 新增
- **投票/辩论协同模式**（此前 arbiter 只算半成品，本次补齐两个缺口）：
  - `ballot.py`：`Ballot` 择优汇总器。与 `Arbiter` 分工明确——Arbiter 裁
    "同一事实的不同主张"（冲突），Ballot 排"同一任务的多个完整方案"（择优）。
    打分直接复用 `agentskills/quality` 的同一套规则判据（谁定义谁执行），
    每条违规 -10 分，无隐藏权重。
  - `planner.py`：**fanout 展开**。stage 带 `fanout: N` 时，`Plan.build()`
    展开为 N 个并行分支 + 1 个汇合点，下游依赖自动改指向汇合点——
    DAG 仍自洽，**DagScheduler 零改动**（展开纯属建图技巧）。
  - **投票模式入口**：需求含"多方案/择优/比稿/多视角"等明确意图时，
    `Planner` 给最终交付 stage 注入 `fanout: 3`。只认明确意图，不猜。
- **MCP 三工具**（`ballot_submit` / `ballot_status` / `ballot_confirm`），
  外部 AI 可作为投票参与者接入；工具总数 33 → 36。
- **成本路由层**（P1.5，修「简单任务也付全价」）：`Planner.decompose()` 缺省按
  复杂度自动分档（`simple` 单任务直出 / `medium` 关键路径线性化 / `complex` 全模板），
  判据全部可解释（`match_info.tier_reasons`：模板关键词命中数、阶段数、需求长度、
  槽位数、多环节连接词、列举密度、多方案意图）；`tier` 参数可强制覆盖。
  成本估算（任务数 × 重做轮次 × 并行度，**相对倍数非假造 token 数**）随 Plan
  落进每个任务的 payload，可观测、可归因。
  **阈值取向不对称**：把复杂任务判成 simple 会漏环节（质量事故），
  把简单任务判成 complex 只是多花钱——所以不确定就往 complex 倒。
  `simple` 档会重写 instruction 使其自包含（模板叶子常写"按审核意见修订"，
  而单任务直出没有上游），且**质量门保留**（便宜不等于放行劣质产出）。

### 修复
- **推荐与生效的边界模糊（真缺陷）**：`rank()` 原先把推荐直接写进
  `ballots.winner_task`，调用方看到非空即误以为已生效。现拆分两个字段：
  `recommended`（推荐，rank 写）与 `winner_task`（生效，**只有 confirm 写**）。
- **Ballot 连接泄漏**：`with self._conn()` 对 sqlite3 是事务上下文而非关闭，
  每次调用泄漏一个 Connection（ResourceWarning）。改为 thread-local 连接缓存
  + `close()`。
- **medium 档误禁投票（回归）**：成本路由初版把 `fanout=0` 的条件写成
  `tier != "complex"`，导致 medium 档也无法投票——但 medium 的叶子仍是唯一
  交付物，并行出多方案完全成立。已收紧为仅 `simple` 档禁投票。
- **简单需求被误判成简单档**：路由初版漏掉「显式列举多环节」这个最强信号，
  一句"包括调研、策略、文案、设计、投放、合规"被判成 simple（会漏环节）。
  已补模板关键词命中数与列举密度两项判据，并上调模板阶段数权重。

### 纪律（与 Arbiter 同源，改动时不可破坏）
- `rank()` 只推荐不生效；`confirm()` 是唯一生效路径，默认人工。
- 平分 / 无质量判据 / 方案未交齐 → 一律 `needs_human`，系统不硬裁。
- 未知策略降级 `human`；单方案、超上限、重复开票、外来 task_id 一律拒绝。

---

## [0.7.0] — 未发布

### 新增
- **编排层四块砖全部落地**（P0）：
  - `orchestration.py`：`DagScheduler` DAG 调度器。`depends_on` 列旧库自动迁移、
    就绪性动态计算、`waves()` 波次、`claim_ready()` 写锁内选+认领、`fail()` 级联、
    建图期四重拒绝（成环 / 未知依赖 / 自依赖 / 重复边）。
  - `planner.py`：任务拆解器。规则模板版（5 套模板 + 槽位抽取，抽不到标
    `<待补:key>` **不编造**）；`LLMPlanner` 同接口真 LLM 版，结构校验失败或
    chat 异常自动回退规则版并记录 `fallback_reason`。
  - `agentskills/quality.py`：`QualityGate` 质量回流回路。产出核验 → 打回
    `registered` 带 feedback → 超 `max_rework` 转 `needs_human`；可与 `EvidenceGate` 双门。
  - `arbiter.py` + `assets.py`：冲突仲裁（默认人工裁决，自动策略白名单）
    与素材库（blake2b 内容寻址去重 + 版本链 + 软删除 + 配额 + GC 审计）。
- **MCP 暴露 `dag_*` 四工具**（`dag_add` / `dag_claim_ready` / `dag_complete` / `dag_status`），
  工具总数 29 → 33。
- **任务看板渲染依赖图**：`task_center_web.py` 新增 DAG 波次区与依赖列，
  `dead_upstream` 阻塞传播标红。
- **沙箱 advisory → enforced**：`EnforcedLocalSandbox` 三级 containment，
  POSIX `preexec_fn` + `setrlimit`（AS/CPU/NPROC/FSIZE/CORE=0），
  `killpg` / `taskkill /T` 杀进程树，输出字节截断。
- **跨机分布式**：`transport/resp.py` 手写 RESP2 客户端（零硬依赖）；
  `RedisQueue` 语义对齐 `LocalQueue`（queue list + processing hash，ack/requeue 非空操作）。
- **素材库 GC 与配额**：总额度与单文件上限分离，去重不重复计额，
  软删过期回收 + 孤儿 blob 清理 + `asset_gc_log` 审计（删字节不删证据）。
- **多模态 provider**：`models/multimodal.py` 提供 `MediaProvider` 抽象 +
  Mock（假字节带魔数）+ OpenAI 兼容（images/audio/speech；video 需显式
  `endpoint_overrides`，**不静默造假**）+ `MediaRouter` 降级。
- **扫描对齐公开基准**：接入 gitleaks 官方测试向量，`manifest.json` 记 sha256 与来源，
  `--verify` 可离线校验。实测 2 TP / 1 TN / 0 FP，F1 = 1.0。
- **可插拔真 embedding**：`EmbeddingProvider` ABC + OpenAI / 本地 Ollama 双实现；
  `EMBEDDING_PROVIDER` 环境变量生效（默认仍走内置哈希 n-gram 索引，行为不变）。
- **发布物料**：`pyproject.toml` 全量对齐（16 个根模块 + 11 个子包 + 5 个 console
  入口）、`MANIFEST.in`、`LICENSE`、`CHANGELOG.md`。

### 修复
- **向量空间错位（静默数据损坏级）**：provider 运行中途掉线时不再降级写入
  哈希向量——此前会导致同一张表混存 1536/1024 维，`_cosine` 的 `zip()` 截断
  算出"半错的分数"（不是 0），检索照常返回结果但排序全乱且现场无法察觉。
  现改为：向量写入统一经空间守卫，该轮算不出就不写（记忆本体照落库），
  provider 恢复后由 backfill 自动补齐，且**不触发**不必要的全量重建。
- **余弦维度守卫**：维度不等直接判 0，绝不 `zip` 截断——宁可漏召回，不给假相似度。
- **provider 空间身份改由配置决定**（而非运行时可用性）：避免本地 Ollama 停一次
  就被误判成"换了 provider"，触发全量 reindex 把真向量洗成哈希向量、恢复时
  再重建一次（来回震荡）。
- **env 工厂接线**：`embedding_from_env()` 此前全项目零生产调用点，
  设 `EMBEDDING_PROVIDER` 实际无效；现经 `embedding="auto"` 哨兵接入生产路径。
- **LLM 拆解器 prompt 崩溃**：`_LLM_SYSTEM.format()` 会把 prompt 内 JSON 示例的
  花括号当占位符，导致全量回退；改为 `__PLACEHOLDER__.replace()`。
- **RESP 整数编码真 bug**：`bytes(3)` 产出三个零字节而非 `b"3"`，导致 `SELECT db` 被拒；
  修正为 `str(a).encode()` 并补回归测试。
- **DAG 只读视图误关共享连接**：`DagScheduler` 无条件 `close()` registry，
  看板只读视图会关掉共享句柄；新增 `events` 参数，Web 视图传 `events=False`。
- **质量升级越界**：`QualityGate._escalate` 里 `SELECT payload` 取 `row[1]` 越界。

### 变更
- MCP 工具总数 29 → 33；`mcpserver.json` 版本 0.5.0 → 0.6.0 → 0.7.0。
- `pyproject.toml` 版本 0.3.0 → 0.7.0，补齐此前漏掉的 8 个包
  （`agentskills` / `games` / `sandbox` / `tools` / `arbiter` / `assets` /
  `orchestration` / `planner` 等）。
- 语义检索默认路径不变（内置哈希 n-gram 索引，零依赖、跨进程可复现）。

---

## [0.6.0]

- MCP 暴露 `dag_*` 四工具，工具总数 29 → 33。
- 沙箱 enforced 模式、Redis 分布式后端、素材库 GC/配额、多模态 provider、
  扫描对齐 gitleaks 公开基准。

## [0.5.0]

- 记忆增强：可选向量层 + Mem0 / OpenMemory 适配。
- 安全加固：OAuth 2.1 资源服务器 + 可观测性（OTel）。

## [0.3.0]

- 轻量长期记忆（`memory.py`）、MCP Server 接入记忆工具集。
- 合规扫描（`scanner` 包）、沙箱执行模块、审批中心（HITL）。

## [0.1.0]

- 首个可运行版本：跨进程任务登记处 + 渠道适配层 + 技能层 + 多模型层。
- 零依赖 MCP Server。
