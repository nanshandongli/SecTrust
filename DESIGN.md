# 多渠道 / 多技能 / 多模型 运行时扩展设计

> 本文档描述如何在现有 **跨进程任务登记处（task_registry）** 之上，把「只会 mock 收消息的 runner」
> 升级为一个**多渠道接入 + 从 GitHub 拉取技能 + 多模型路由**的 agent 运行时。
>
> 设计前提沿用既有契约：**runner 与 task_center 同机**，SQLite 文件共享作为唯一任务交换通道；
> 本仓库只新增「运行时（runtime）」与「三层扩展点」，不动 `task_registry` 作为监控/控制平面的定位。

---

## 0. 一句话架构

```
                 ┌─────────────── 运行时 Runtime (runner.py) ───────────────┐
                 │                                                          │
   微信/企业微信 ─┤  ChannelAdapter (渠道适配层)  收消息 → 标准化 InboundMessage│
   飞书/Feishu  ─┤        │                                                 │
   Telegram     ─┤        ▼                                                 │
   Discord      ─┤  AgentRunner._on_message()                               │
   (mock 测试)  ─┤     1. register_task  → task_registry (登记处)            │
                 │     2. ModelProvider.complete()  → 多模型推理             │
                 │     3. SkillProvider  → 选技能执行(含 GitHub 拉取的技能)  │
                 │     4. channel.send()  → 回消息给原渠道                   │
                 │     5. update_task_status(done/failed) → 登记处           │
                 │     心跳 beat() → runners 表                             │
                 └───────────────────────────┬──────────────────────────────┘
                                             │ 共享 SQLite 文件 (同机)
                                             ▼
                            task_registry.db  ◄──── task_center_web.py (只读监控)
```

**核心思想**：`task_registry` + `task_center_web` 继续当**监控与控制平面**——
任何渠道进来的任务都照常写进登记处，中心照样能看到 `channel / model / skill` 等元数据；
而「多渠道 / 多技能 / 多模型」全部实现为**可插拔的扩展点（抽象基类）**，互不影响。

---

## 1. 三个扩展点与职责边界

| 层 | 抽象基类 | 职责 | 不负责 |
|----|----------|------|--------|
| 渠道 (Channel) | `ChannelAdapter` | 把各平台 inbound 消息标准化为 `InboundMessage`；把 `OutboundMessage` 发回原平台 | 业务处理、模型调用 |
| 技能 (Skill) | `SkillProvider` | 列举/加载/执行技能；GitHub 源按 tag/commit 拉取并加载 | 消息收发、模型推理 |
| 模型 (Model) | `ModelProvider` | 统一 chat 接口，按模型名路由到不同 LLM | 消息收发、技能编排 |

每一层都只通过**自己的 ABC 接口**被 `AgentRunner` 调用，层与层之间解耦。
要加新渠道/新模型/新技能源，只需实现对应 ABC，并在 `runner.py` 的工厂里登记一行。

---

## 2. 渠道适配层（Channel）

文件：`channels/` —— `ChannelAdapter`(ABC) + `InboundMessage` / `OutboundMessage` 数据类。

### 2.1 标准化消息
```python
@dataclass
class InboundMessage:
    channel: str        # "wecom" / "feishu" / "telegram" / "discord" / "mock"
    message_id: str
    user_id: str
    text: str
    timestamp: float
    raw: Any = None     # 平台原始体，便于调试与平台专属能力

@dataclass
class OutboundMessage:
    channel: str
    user_id: str
    text: str
    raw: Any = None
```

### 2.2 适配器契约
```python
class ChannelAdapter(abc.ABC):
    name: str = "base"
    @abc.abstractmethod
    def start(self, handler: Callable[[InboundMessage], None]) -> None:
        """阻塞运行：收到消息后调用 handler。"""
    @abc.abstractmethod
    def send(self, msg: OutboundMessage) -> None:
        """把回复发回原渠道。"""
    def stop(self) -> None: ...
```

### 2.3 已落地的适配器
- **`MockChannel`**：后台线程按 `mock_interval` 周期吐 `InboundMessage`，**离线可跑**，用于 demo 与回归测试。
- **`WeComChannel`（企业微信，官方 API）**：
  - 入站：起一个 HTTP 回调端点（标准库 `http.server`），校验 `msg_signature` + `AES` 解密企业微信回调体；
  - 出站：调用企业微信「应用消息」接口（`/cgi-bin/message/send`），用 `urllib` 发 HTTPS（零依赖）。
  - 需在**企业微信管理后台**配置可信回调 URL、Token、EncodingAESKey。
- **`FeishuChannel`（飞书，官方 API）**：
  - 入站：事件订阅 webhook，校验 `timestamp` + `sign` 签名，解析 `url_verification` 与挑战；
  - 出站：调用飞书「发送消息」`/open-apis/im/v1/messages`，`urllib` 发 HTTPS。
  - 需在**飞书开放平台**配置事件订阅 URL 与 `verification_token` / `app_secret`。

> 真实渠道的签名校验、加解密已在桩里给出结构，但**未接真实网络测试**——接手时按官方文档补全即可。

---

## 3. 技能层（Skill）—— 从 GitHub 拉取

文件：`skills/` —— `SkillProvider`(ABC) + `Skill` 数据类。

### 3.1 技能数据模型
```python
@dataclass
class Skill:
    name: str
    description: str
    entry: Callable[[dict, "RunContext"], "SkillResult"]  # 执行入口
    source: str = "local"     # "local" / "github"
    version: str = "0.0.0"
```

### 3.2 两种来源
- **`LocalSkillRegistry`**：从本地目录（默认 `skills/builtin/`）加载技能模块，每个模块用 `register(registry)` 注册。零依赖、可信。
- **`GitHubSkillSource`**：
  - `sync()`：用 **git**（`subprocess` 调用本机 git）把仓库 clone 或 pull 到本地缓存目录，**必须 pin 到 tag 或 commit**（不允许浮动 `main` 用于生产）；
  - `load()`：扫描缓存里的技能清单/模块并导入，注册进 `SkillRegistry`；
  - 支持 allowlist（只允许指定技能名加载），避免仓库里混入意外代码。

### 3.3 ⚠️ 信任边界（重要）
「从 GitHub 拉取技能」本质是**加载并执行远程代码**。务必：
1. **pin 到具体 commit/tag**，不要用浮动分支；
2. 只对**自己/团队可信仓库**开启，必要时加 allowlist；
3. 尽量在隔离环境/容器内运行，不要把 GitHub 技能的执行权限等同本地可信代码；
4. 技能 `entry` 只拿到受限的 `RunContext`（不直接给全局能力）。

---

## 4. 模型层（Model）—— 多模型路由

文件：`models/` —— `ModelProvider`(ABC) + `ChatMessage` / `ChatResult`。

### 4.1 统一接口
```python
@dataclass
class ChatMessage:
    role: str        # "system" / "user" / "assistant"
    content: str

@dataclass
class ChatResult:
    content: str
    model: str
    raw: Any = None
    usage: dict = None

class ModelProvider(abc.ABC):
    @abc.abstractmethod
    def complete(self, messages: list[ChatMessage], model: str = None, **opts) -> ChatResult:
        ...
```

### 4.2 已落地的 provider
- **`MockModelProvider`**：规则化回显，**离线可跑**，用于 demo 与回归测试。
- **`OpenAIModelProvider`**：兼容 OpenAI 协议的模型（OpenAI 官方、`通义`、`GLM`、`DeepSeek` 等 OpenAI-compatible 端点）。懒加载 `openai` SDK；`api_key` / `base_url` **走 env**。
- **`AnthropicModelProvider`**：懒加载 `anthropic` SDK，`api_key` 走 env。
- **`OllamaModelProvider`**：本地模型，直接 `urllib` 打 `http://127.0.0.1:11434/api/chat`，**零依赖、可离线**。

### 4.3 路由（`ModelRouter`）
按模型名前缀/关键词选择 provider，例如：
| 模型名示例 | 路由到 |
|-----------|--------|
| `gpt-4o*` / `o1*` | OpenAI |
| `claude-*` | Anthropic |
| `qwen-*` / `glm-*` / `deepseek-*` | OpenAI-compatible（改 `base_url`） |
| `ollama/llama3` | Ollama（本地） |
| 其它 / 未配置 key | `MockModelProvider`（安全兜底，不报错） |

`default_model` 在 `config.py` 中配置；缺 key 时自动降级到 mock，避免运行时崩溃。

---

## 5. 组合根（runner.py）

`AgentRunner` 把三层与登记处接起来：
1. 启动时构造 `ChannelAdapter` / `ModelRouter` / `SkillRegistry`，并起心跳线程；
2. 收到 `InboundMessage` → `register_task(task_id, payload={channel,model,skill,text})`；
3. 调 `ModelRouter.complete()` 推理；若文本含「/技能名」则 `SkillRegistry.get().entry()` 执行；
4. `channel.send()` 回消息；`update_task_status(done/failed)`；
5. `task_center_web.py` 只读仪表盘实时可见每条任务的渠道/模型/技能与状态。

**默认行为（零配置即可跑）**：未配置任何真实渠道/模型时，`runner.py --demo` 自动用
`MockChannel` + `MockModelProvider`，端到端可离线验证整条链路。

---

## 6. 密钥与配置（绝不硬编码）

`config.py` 用 dataclass + 环境变量加载，**所有密钥只从 env 读，默认值留空**：
- `OPENAI_API_KEY` / `OPENAI_BASE_URL`
- `ANTHROPIC_API_KEY`
- `WECHAT_TOKEN` / `WECHAT_AES_KEY` / `WECHAT_CORP_ID` / `WECHAT_AGENT_ID` / `WECHAT_SECRET`
- `FEISHU_APP_ID` / `FEISHU_APP_SECRET` / `FEISHU_VERIFICATION_TOKEN`
- `GITHUB_SKILLS_REPO` / `GITHUB_SKILLS_REF`（建议 pin commit）

仓库内提供 `.env.example` 列出全部变量名；**不要把真实密钥写进代码或提交**。

---

## 7. 与现有契约的关系

- **不改 `task_registry.py`**：`tasks` 表字段固定，渠道/模型/技能信息放进 `payload` JSON，向后兼容。
- **不改 `task_center_web.py`**：它只读 `payload`，新加的 `channel/model/skill` 字段会自动显示在详情里（如需在列表页加筛选，按 README「接手者工作流」同步即可）。
- **`runner_daemon.py` 保留**：作为「最小 mock 守护」示例，与本框架的 `runner.py` 并存（后者是功能完整版）。

---

## 8. 后续可交付清单（待办）

- [ ] 企业微信/飞书回调接真实公网 HTTPS + 签名校验真机联调
- [ ] 真实模型 provider 接 SDK 并跑通首条对话
- [ ] GitHub 技能源接真实仓库，跑通「拉取→加载→执行→回消息」
- [ ] 给 `task_center_web.py` 增加 channel/model 维度筛选
- [ ] 给技能执行加沙箱/超时/资源限制

---

## 9. P0 已交付：MCP 生态入口 + 合规扫描模板库

> 2026 年 MCP 已成为 Agent 工具连接事实标准（社区 1000+ MCP Server，主流框架原生支持）。把产品能力
> 暴露为标准 MCP 工具，可获得最大生态杠杆；合规扫描模板库则提供差异化场景。

### 9.1 MCP Server（`mcp_server.py`，零依赖）
- 纯标准库实现 **JSON-RPC 2.0 over stdio**，无需任何第三方 MCP SDK。
- 工具集：`list_templates / request_scan / approve / deny / execute_scan / run_scan / list_tasks / get_task / list_runners / list_approvals`。
- **安全模型不绕过**：`request_scan` 只登记 + 进审批；`execute_scan` 内部强制校验审批状态，未批准拒绝。外部 agent 只能走设计好的入口。

### 9.2 合规扫描模板库（`templates/`）
- `ScanTemplate` 数据类：`name / description / build_command(repo_dir) / parse(text) / severity`。
- 内置 5 模板：`secret_leak / hardcoded_credential / dependency_vuln / cicd_config / generic_sast`。
- 编排：`request_scan`（登记+审批）→ `execute_scan`（沙箱跑，校验审批）→ 结果写回登记处 `payload`。
- 脚本落盘 `templates/_scripts/<name>.py`，统一走 `sandbox` 隔离执行。

### 9.3 信任边界（重申）
- MCP 客户端 = 外部不可信输入源；它只能调用已声明的工具，**不能绕过审批闸门、不能直连登记处文件、不能改 DB schema**。
- 真正执行扫描的前提是**人工批准**（或显式 `run_scan(auto_approve=True)`，仅限可信自动化）。

---

## 10. P2 轻量长期记忆（memory.py）

> 让产品跨会话「记得住用户」：用户偏好、常用指令模板、常用 repo 路径都落盘 SQLite，
> 例如「这个用户喜欢用 Qwen-Max 做代码审查」。

### 10.1 为什么是 SQLite，而不是向量库
- 这类长期记忆是**少量 + 结构化 + 可精确查**的（偏好键值、指令模板、路径），不需要语义相似度检索；
- SQLite **零运维、事务可靠**，且与 `task_registry` 的 WAL 模式天然同款（连接隔离 / `busy_timeout` / `synchronous`）；
- 备份 / 迁移 / 排查都简单，`memory.backup()` 同样走 `sqlite3.backup` 自动合并 WAL。

### 10.2 数据模型与接口
- 单表 `memory_items(category, key, value JSON, note, created_at, updated_at, access_count, last_accessed)`，主键 `(category, key)`；
- 三类语义化便捷方法：`remember_preference / recall_preference`、`remember_command / recall_command`、`remember_repo / recall_repo`；
- 通用方法：`remember / recall / forget / list_category / search / top_used / recent / categories / stats`；
- `recall(touch=True)` 累加 `access_count`，配合 `top_used` 天然得出「常用指令模板 / 常用 repo」。

### 10.3 与生态入口的关系
- 记忆库 `data/memory.db` 与 MCP 共用文件；MCP 新增 6 个 `memory_*` 工具，外部 agent 也能跨会话读写；
- 本地进程与 MCP 客户端写的是同一份数据，保证「本机脚本记得住的，接进来的 agent 也读得到」。

### 10.4 设计边界
- 不引入向量库 / 不引入额外服务；语义检索若未来需要，可在 `search` 之上叠加，不改存储底座；
- 记忆内容不含任何密钥——密钥始终只从环境变量读（沿用 `config.py` 约定），长期记忆只存偏好/模板/路径等非敏感元信息。

## 11. 编排层：DAG 调度（orchestration.py）

### 11.1 为什么需要它
§1-§10 解决的是"**单个任务**怎么被登记、认领、执行、记忆"。多 AI 协同还要求
"**任务之间**怎么排队"：B 要等 A、无依赖的并行、上游失败怎么级联。此前 `tasks` 表
没有任何依赖字段（全局零编排代码），串行依赖只能由应用层轮询状态自己实现——
每个使用方各写一遍，且没有失败级联语义。

### 11.2 设计决策
| 决策 | 理由 |
|------|------|
| 依赖存 `tasks.depends_on`（JSON 列）而非单独表 | 任务量级为单机数百；一列足够，且旧库 ALTER 迁移零停机 |
| **就绪性动态计算**，不物化 ready 标志位 | 依赖只会"未完成→完成"单向变化，无回退；物化反而要处理"刚完成又回滚"的不可能分支 |
| `claim_ready` 在 `BEGIN IMMEDIATE` 内"选最老就绪 + CAS" | 与 `claim_task` 同一串行化保证：写锁内选择+认领一气呵成，双抢在结构上不可能 |
| 建图期拒绝未知依赖/重复/自依赖/成环 | 快速失败好过静默挂起；环检测在正常建图下不可达（新任务无下游+重复被拒），属防御性兜底 |
| `fail()` 级联只动 `registered` 下游 | 已 running 的下游理论上不存在（它领任务时上游必已完成）；只阻塞未执行的，不杀已执行的 |
| Broker 事件只观战、不参与关键路径 | 通知/观战类事件失败绝不影响调度正确性（关键路径不依赖事件总线） |

### 11.3 状态机
```
registered --claim--> running --complete--> completed
                     +---------------> failed
registered --(上游失败)-------> blocked
```
`complete` 的前置条件：所有依赖已 completed（否则拒绝）——这保证"完成"事件本身
就是图一致性的检查点。

### 11.4 与 MCP / 看板的关系（后续）
- 任务看板 `task_center_web.py` 可直接读 `depends_on` + `waves()` 渲染依赖图；
- MCP 可将 `dag_add` / `dag_claim_ready` / `dag_complete` 暴露为工具，让外部 agent
  作为 worker 接入同一条流水线（本期未加，保持 29 工具面不变）。

## 12. 编排层应用：拆解 / 质量回流 / 仲裁 / 素材库（§11 之后的另外三块砖）

§11 的 DagScheduler 让"任务之间怎么排队"成立。本节四件套让"一句需求怎么
变成一条多 AI 流水线"成立。共同原则：**复用底座、零新依赖、默认保守**。

### 12.1 planner.py（规则模板版拆解器）

| 决策 | 理由 |
|------|------|
| 先做规则模板版，不上 LLM | 可解释、可测试、零成本零延迟；LLM 版可在同一 `decompose()` 纯函数接口上替换 `_match_template`，输出结构不变 |
| 槽位抽不到标 `<待补:key>` 而非猜测 | "人类补全"优于"AI 编造"；`needs_input` 进 payload，下游/人类显式补 |
| 子任务 spec 带 skill/model/quality 三段 | skill/model 是提示（下游路由用），quality 是硬规则（QualityGate 消费）——提示与规则分离 |
| `Plan` 纯数据 + `build()` 才落库 | 可 JSON 序列化、可回放、可 diff；重复 build 被 DagScheduler 的 task_id 唯一性挡住（幂等） |

### 12.2 agentskills/quality.py（质量回流回路）

状态机在 EvidenceGate 的"证据门"之外补上"产出门"：

    running --review(质量+证据双过)--> completed
           --review(不合格)---------> registered（feedback + rework_count+1，重新就绪）
           --重试 > max_rework------> needs_human（升级人工）

| 决策 | 理由 |
|------|
| 规则与 planner 的 quality 同格式 | 拆解时定的验收标准，送审时自动执行——"谁定义谁执行"不断裂 |
| `no_unfilled` 默认开 | planner 标出的 `<待补:key>` 不许带着占位符交货 |
| `references` 强制提及上游 | 防止下游"自说自话"不建立在产出之上（协同的基本纪律） |
| 打回用 CAS running→registered 而非改状态标志 | 与 claim_task 同一原子性；任务重新就绪，任何 worker 可认领修订 |
| 超限升 needs_human 而非 fail() | fail 的语义是"上游失败级联阻塞下游"；质量超限是"要人决策"，下游是否继续由人定，不级联 |
| 审计表记 output 指纹而非全文 | 可回放"被打回过几次/为什么"，又不让审计库成为泄密载体 |

### 12.3 arbiter.py（冲突仲裁）

| 决策 | 理由 |
|------|------|
| 默认策略 human（不自动裁） | 自动仲裁"看起来智能实则危险"——人类连冲突发生过都不知道。与 ApprovalCenter 同哲学 |
| 自动策略白名单 + 未知策略降级 human | 防 typo（`strategyy`）静默变成不裁或乱裁 |
| decide() 记录 by/reason/chosen_from | 审计可回放；superseded 只标记不删除 |
| 单一主张直接采纳（strategy=single） | 没有冲突就不浪费人工 |

### 12.4 assets.py（素材库）

| 决策 | 理由 |
|------|------|
| 内容寻址（blake2b）而非文件名 | 天然去重；路径与名字解耦，名字永远造不成路径穿越 |
| 同名不同内容 = 版本链（按 created_at） | 文件系统语义（覆盖）会丢历史；版本链留住"谁在哪版改了什么" |
| 软删除 | 审计优先；物理回收留给后续 GC |
| 元数据（task_id/meta/by）与字节分离 | 字节进 blob 目录，关系进 SQLite——各自擅长 |

### 12.5 全局形态与已知缺口

    Planner → DagScheduler → worker(EvidenceGate) → QualityGate
            → Arbiter → AssetStore → ApprovalCenter → 交付

缺口（如实记录，后续路线）：LLM 版拆解器；跨机分布式执行；素材库 GC/配额；
仲裁自动策略的来源类型需注册才可比较；看板与 MCP 尚未暴露新能力（保持 29 工具面）。
