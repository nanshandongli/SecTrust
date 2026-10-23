# 更新日志

本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/)。
所有版本均为**未上架**状态（PyPI / MCP Registry 物料已就绪，实际发布待账号与凭据）。

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
