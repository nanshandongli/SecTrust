# 安全白皮书（Security Whitepaper）

> **信任锚点（先讲天花板）**：本白皮书描述的安全能力，均来自仓库内**已落地、可审计**的代码
> （`sandbox/`、`approval.py`、`execution/workspace.py`、`scanner/`）。我们**不声称**已通过任何
> 第三方合规认证（SOC2/ISO 27001 等），也**不声称**本地 `advisory` 模式是"沙箱"。本白皮书是
> **能力陈述 + 诚实边界**，不是合规背书。所有对外材料统一采用
> [诚实边界范式](docs/honest_boundary_paradigm.md)（先讲天花板，再讲数据）。

---

## 0. 能力边界一览（读完这节就知道我们保什么、不保什么）

| 维度 | 强隔离（Docker / E2B） | 本地降级（local-enforced / local） |
|------|----------------------|----------------------------------|
| 进程隔离 | OS 级（容器命名空间） | 无（同宿主进程） |
| 文件系统隔离 | 只读根文件系统 + 独立挂载 | 仅 cwd + 命令预检 |
| 网络 | 默认断网，审批后才外发 | **不隔离网络** |
| 资源上限 | cgroup（内存/CPU/进程数） | RLIMIT（POSIX）／超时强杀（Win） |
| 适用 | 执行**不可信**代码 | 仅执行**可信**代码 / 本地 demo |

**结论先说**：要用本项目跑**不可信代码**，必须处在 `containment="os"` 后端（Docker daemon 可达
或已配 E2B）。本地两档**不隔离文件系统与网络**——这是诚实边界，也是 `SANDBOX_REQUIRE_STRONG=1`
开关存在的理由（宁可报错也不伪装成沙箱）。

---

## 1. 沙箱隔离机制

### 1.1 三级 containment 强度（`sandbox/__init__.py`）

后端在 `SandboxResult.containment` 上**诚实标注真实隔离强度**，调用方不得假设高于声明的隔离：

- **`advisory`**（`LocalSandbox`，`sandbox/__init__.py:142`）：**仅命令文本预检**，不是真沙箱。
  子进程该吃多少内存、开多少进程都没人管，也不断网。用于本地可信 demo。
- **`limited`**（`EnforcedLocalSandbox`，`sandbox/__init__.py:250`）：在 `advisory` 之上加
  **内核强制资源上限**（`RLIMIT_AS`/`RLIMIT_CPU`/`RLIMIT_NPROC`/`RLIMIT_FSIZE`/`RLIMIT_CORE=0`，
  见 `:263-272`），超时则进程树强杀 + 输出截断。仍是同宿主、无文件系统/网络隔离。
- **`os`**（`DockerSandbox` / `E2BSandbox`）：操作系统级隔离，进程、文件系统、网络均受限。

`SandboxManager.auto()`（`:708`）按优先级路由：`docker(daemon 可达) → e2b(有 key) →
local-enforced → local`。两条本地降级都**不隔离文件系统与网络**；设 `SANDBOX_REQUIRE_STRONG=1`
时**拒绝降级**（`:730`），宁可报错也不让"以为是沙箱其实只是命令过滤"发生。

### 1.2 Docker 强隔离「隔离工位八条规格」（`DockerSandbox`，`sandbox/__init__.py:359`）

每条都是代码里**实际传的 flag**，不是设计愿望：

1. **每任务独立容器，结束即销毁**：每次 `run` 起带名容器 `--rm`（`:559`），跨进程残留由
   `cleanup_stale()`（`:475`，前缀 `sectrust-sbx-` 只删自家）兜底强杀遗留容器。
2. **非 root 用户**：`--user`（默认 `nobody`，见 `_default_container_user` `:84`），容器进程默认无特权。
3. **根文件系统只读**：`--read-only` + `/tmp` 走 tmpfs（`:572-576`）；受控模式 `/work` 也仅 tmpfs，不落宿主盘。
4. **默认断网**：`--network none`（`:560`）；外发网络必须持**已批准**的 `approval_id`（见 §1.3）。
5. **资源上限**：`--memory`/`--memory-swap`（swap=内存即禁 swap）、`--cpus`、`--pids-limit`（`:565-568`）。
6. **输入只读 / 输出分离挂载**：`-v ...:/input:ro` 与 `-v ...:/output`（`:580-582`），缺省退化为单一 `/work`。
7. **命令白名单先行**：`_whitelist_reject()`（`:430`）——被拒命令**根本不进容器**。
8. **`--runtime` 预留**：为未来 gVisor / Kata 预留（`:571`），当前只验证 runc 调用链。

外加两条通用硬化：`--cap-drop ALL`（`:563`，默认不给任何 capability）与
`--security-opt no-new-privileges`（`:564`，禁 setuid 提权链），`--init`（`:562`，PID1 收尸防僵尸）。

### 1.3 网络默认拒绝门（`SandboxManager._network_gate`，`sandbox/__init__.py:741`）

"默认断网、审批后才外发"在**管理层强制**，而非依赖后端自觉：`SandboxManager.run()`（`:769`）
在把请求交给后端**之前**先过 `_network_gate`；即便绕过管理层直调 `DockerSandbox.run`，
后端也会**二次校验**（`:543`）。鸭子类型校验 `approval.is_approved(approval_id)` 为真才放行。
这条规则对所有后端一致生效。

---

## 2. 最小权限原则（Principle of Least Privilege）

### 2.1 审批中心：默认拒绝 + 超时过期（`approval.py`）

`ApprovalCenter` 是 Human-in-the-loop 的核心，四态 `pending/approved/rejected/expired`：

- **保守默认拒绝**：任何高权限 / 副作用 / 触碰外部资源的动作（外发、发布、购买、支付、删除、扫描，
  见 `HIGH_RISK_ACTIONS`）先挂起为 `pending`，**无人决定绝不默认放行**。
- **超时自动 expire**：`sweep_expired()` 把挂起超阈值的审批标记 `expired`；`wait_for_decision()`
  超时也返回 `expired`（绝不返回 approved）。
- **审计证据链**：每次请求的发起/决策/过期都在 Trace 留事件（高危动作带 `risk=high`），
  `audit()` 返回"审批行 + 轨迹事件"，证据缺口如实标注（无轨迹上下文则 `trace_id=None`、`events=[]`）。

### 2.2 容器最小权限（见 §1.2）

non-root（nobody）+ `--cap-drop ALL` + 只读根文件系统 + 独立挂载，叠加"默认断网需审批"。

### 2.3 工作区隔离（`execution/workspace.py`）

`BotWorkspace` 按 `bot_id` 分配**独立目录**，内含：
- `work/`：执行 jail（路径经 `_resolve` 校验，拒绝绝对路径与 `..` 逃逸）；
- `outbox/`：产出物**需审批才能外发**（不是直接落盘即发送）；
- `state.json`：**原子替换**写（先写临时文件再 `os.replace`），避免半写状态被并发读。

`_validate_bot_id` 用正则 `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$` 限制标识字符面，防注入。
`run()` **委托给 `SandboxManager`**，绝不直接 `subprocess` 裸跑——执行永远走上面的隔离与审批链路。

### 2.4 OAuth 令牌作用域（`api/`）

令牌签发（`issue_token`）与校验（`TokenValidator`）按 **scope** 收敛权限，避免"一个令牌通吃"。

### 2.5 对话引擎的信任面（`console_chat.py`）

协作审计台的「对话」面板会按需调用模型，这条链路是新引入的信任面，明确边界：

| 面 | 做法 | 为什么 |
|---|---|---|
| **密钥来源** | `OPENAI_API_KEY` / `OLLAMA_BASE_URL` 只从**环境变量**读取，源码零硬编码 | 自扫可验证：全仓 0 真实泄露 |
| **模型选择** | 优先**本地 Ollama**（数据不出机），其次 OpenAI 兼容端点 | 审计数据敏感，默认不出本地 |
| **降级语义** | LLM 不可用时回答标记 `engine=unavailable`，并**明说不可用** | 绝不用 Mock 冒充模型输出——伪造比缺失更危险 |
| **事实约束** | 调LLM 时把当前审计态作为 `<facts>` 喂入，system prompt 限定"只能引用事实" | 抑制自由发挥，避免审计结论被模型编造 |
| **只读** | 对话链路只读任务/审批/轨迹/成本，**不写**任何业务表 | 问答不应改变被审计对象 |
| **来源可见** | 每条回答都带 `engine` 字段（rule / llm / unavailable），前端强制展示 | 使用者永远知道这句话是谁说的、依据哪个面板 |

**规则引擎优先**是关键设计：审计类问题（任务/审批/成本/安全/轨迹/总结）由确定性规则直接作答，
**零幻觉、零外部依赖、零延迟**；LLM 只兜底开放式问题。规则未命中且 LLM 不可用时，
系统明确说"无法回答"并给出下一步，而不是编一个看起来像样的答案。

---

## 3. 假密钥扫描：0 真实泄露

### 3.1 自扫纪律（scanner 扫自身）

项目用自带合规扫描器扫自身源码，结果**诚实公示**（见 `PRODUCT_ANALYSIS.md §7.4`）：
全仓扫描 73 命中（critical 13 / high 13 / medium 47，232 文件）。其中绝大多数位于
**扫描器自带回归语料**（`tools/scan_bench.py` 29 条、`examples/sample_repo`、`data/bench_corpus`、
`tests/*`）——这些是扫描器"应当检出"的**假密钥夹具**，并非真实泄露。

### 3.2 真实业务源码：0 真实泄露凭证

对**真实业务源码**的命中，全部是：
- `scanner:allow` 标注的**占位行**（行内抑制，文档化"此处是假密钥"）；或
- 文档 / 向量里的**示例密钥**（gitleaks 公开测试向量、AWS 文档示例密钥——均为非真实凭据）。

结论：**0 真实泄露凭证**。

### 3.3 公开测试向量豁免

明确标注的公开测试向量（如 `AKIALALEMEL33243OLIA` 等）通过 `KNOWN_PUBLIC_TEST_VECTORS`
**豁免**自扫；协议注释里的占位符（如 `redis://[:password@]host:port/db`）用行内
`# scanner:allow` 抑制，而不是改扫描器规则去迎合误报。这保证"文档化的假密钥"对自扫干净。

---

## 4. 诚实边界声明（必须随本文一起读）

本白皮书采用 [诚实边界范式](docs/honest_boundary_paradigm.md)，对外材料统一"先讲天花板，再讲数据"：

- 上述能力**不等于**任何第三方合规认证（SOC2 / ISO 27001 等）。需要合规背书，请走正式审计。
- 本地 `advisory` / `limited` 模式**不是沙箱**；跑不可信代码必须 `containment="os"` 后端。
- 自扫的 73 命中**绝大多数是假密钥夹具**；真实业务源码 0 真实泄露，但"0 泄露"指**当前仓库状态**，
  不代表任何历史版本或下游派生仓库。
- 安全强度依赖**正确部署**：`SANDBOX_REQUIRE_STRONG=1`、`SANDBOX_LOCAL_MODE`、Docker daemon 可达等
  开关决定实际落地的隔离档位。错误配置会把强隔离降级为本地执行。

---

## 5. 安全自检清单（发布 / 部署前）

- [ ] **隔离档位确认**：不可信代码是否确实落在 `containment="os"` 后端（Docker/E2B）？
- [ ] **默认断网**：外发网络是否都过了审批门（或本就 `network=none`）？
- [ ] **默认拒绝**：高权限动作是否都先 `pending`、超时 `expired`、绝不默认放行？
- [ ] **工作区隔离**：`bot_id` 目录、`outbox` 审批、状态原子写是否到位？
- [ ] **自扫干净**：`scanner:allow` 是否只标在文档化假密钥上，真实凭据无命中？
- [ ] **诚实标注**：对外材料是否引用本白皮书 §4 边界，未夸大成合规认证？

---

*维护约定：本白皮书改动须与 `PRODUCT_ANALYSIS.md §7.4`、`docs/honest_boundary_paradigm.md` 保持一致；
行号引用随代码演进更新。*
