# Skill 运行时 + MCP 适配器落地报告

> 阶段 B 交付 · 承接 `任务交接_合规收尾_Skill运行时MCP_20261029.md` 第 3、4 节
> 前置：阶段 A（合规治理收尾）已全量验收通过，本阶段严格串行在其之后执行。

---

## 0. 摘要

用户明确的「先做这三件事」**全部跑通并各有离线单测**：

| # | 用户原文要求 | 落地位置 | 单测 |
|---|--------------|----------|------|
| 1 | 写 SKILL.md 解析器，让现有 SkillHub/agentskills 跑通标准格式 | `agentskills/skill_md.py` + 标准样板 `competitor-analysis/` | `tests/test_skill_md.py` |
| 2 | 接一个 GitHub MCP Server 样板，跑通 Tools/Resources/Prompts 全链路 | `mcp_adapter/` 包（协议/双传输/Registry/熔断/安全代理）+ `mcp_adapter/servers/github.py` | `tests/test_mcp_adapter.py` |
| 3 | 把 EvidenceGate 和审批闸门挂到 Skill/MCP 调用链 | `agentskills/guard_chain.py`（Skill 侧）+ `mcp_adapter/guard_chain.py`（MCP 侧） | `tests/test_guard_chain.py` |

路线图口径：工单第六节的 **P0（Skill 运行时）与 P1（MCP 适配器）已完成**；
**P2–P4 列为待办**（见第 8 节），未冒充已完成。
（注：这里的 P2–P4 是**工单第六节路线图编号**；本轮另完成的 v2 智能运行时优化
P0–P4 见第 9 节，是**另一套分期编号**，两者不同，勿混淆。）

阶段 B 新增/扩展单测合计 **136 passed, 41 subtests**（离线，零外网依赖、
零真实 GitHub token）。全量回归结果见第 6 节。

---

## 1. P0 · Skill 运行时（B1）

### 1.1 标准 SKILL.md 解析器 `agentskills/skill_md.py`

零第三方依赖（不引入 PyYAML），支持 `key: value` 与 `key: [a, b]` 两种
frontmatter 形式；遇到嵌套/多行 YAML **明确抛 `SkillMdError`**，不猜测语义。

- `parse_frontmatter_standard(text)` / `read_frontmatter_only(path)`
- `parse_skill_md(path, include_body=False, load_manifest=True) -> StandardSkillSpec`
- `_validate_meta()`：name 格式、description 必填且 ≤1024、risk_level/cost_tier 白名单
- `validate_manifest(manifest, spec)`：安装期校验，与 `governance.rbac.PERMISSIONS`
  （平台权限域含 `.` 必须在权限目录内）和 `content_guard.blocked_patterns`
  （正则合法性）对齐；缺 `manifest.json` 明确报错，不静默当无配置

### 1.2 渐进式 L1/L2/L3

| 层 | 接口 | 保证 |
|----|------|------|
| L1 | `load_l1_metadata()` / `match_l1()` | 只暴露 name/description/risk_level/cost_tier/allowed_tools，**绝不夹带正文** |
| L2 | `load_l2_body(name)` | 命中后才读完整 Markdown 并缓存 |
| L3 | `load_l3_resource(name, rel_path)` | `realpath` 规范化 + 一级目录白名单（references/assets/scripts） |

**L1「不读正文」是硬证明，不是声明**：单测 patch 文件迭代器计数，构造含
1000 行正文标记的文件，断言 `read_frontmatter_only()` 只读 5 行 frontmatter
（`test_l1_proven_not_to_read_body`）。

**路径穿越防护**覆盖 10 个攻击向量：`../secret.txt`、`references/../../secret.txt`、
`/etc/passwd`、`C:\Windows\win.ini`、空串、`SKILL.md`、`manifest.json`、目录名等，
全部 `SkillSecurityError`；归一化后仍在 scripts/ 内的合法写法放行。

### 1.3 沙箱脚本执行 + 标准样板

- `run_script()` 必须走 `sandbox.SandboxManager.auto()`（可注入后端），
  且仅 `scripts/` 下文件可执行；结果如实带 `backend` / `containment`
  （本地降级是 `advisory`，**不冒充 OS 级隔离**）。
- 标准样板 `agentskills/library/competitor-analysis/`：
  `SKILL.md`（frontmatter 含 allowed-tools 列表）+ `manifest.json`
  + `references/sources.md` + `assets/report_template.md`
  + `scripts/collect_outline.py`（离线产出大纲与三项必检工件）。
- **双加载器并存、互不破坏**：旧 `loader.SkillLibrary` 仍按严格写作规范扫描，
  `total=5, valid=5, broken=0`（新样板 description 以 `Use when` 开头，同时满足旧规范）；
  新 `StandardSkillLibrary` 能解析全部 5 个的 frontmatter，但只有带 manifest 的
  标准样板通过安装期校验（`installed=1`）。旧测试 `test_builtin_skills_all_valid`
  的硬编码集合改为「核心 4 个为子集 + 全部 ok + broken==0」，避免每加内置技能就误报回归。

### 1.4 本轮修复的两个真实缺陷

1. **Windows 子进程中文乱码**：`collect_outline.py` 复用项目 `SAFE_SCAN_SCRIPT`
   既有做法 `sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")`，
   父进程按 UTF-8 解码一致。
2. **argv off-by-one**：`main(sys.argv[1:])` 里取 `argv[1]` 导致首个参数丢失，
   修正为 `argv[0]`。

---

## 2. P1 · MCP 适配器（B2）

### 2.1 包结构（已在 `pyproject.toml` 的 `[tool.setuptools] packages` 申报）

```
mcp_adapter/
├── protocol.py      JSON-RPC 2.0 帧 + 三原语数据结构 + 私有错误码
├── transport.py     StdioTransport / HttpTransport（双传输）
├── registry.py      MCPServerRegistry（data/mcp_servers.json）
├── breaker.py       TTLCircuitBreaker（per-server TTL 熔断，进程无关）
├── proxy.py         SecureProxy（六道安全门 + 审计）
├── guard_chain.py   MCPEvidenceAdapter（B3：证据门钩子实现）
└── servers/
    └── github.py    GitHub MCP 三原语样板（stdio 可独立运行 / HTTP handler 复用）
```

与平台自身 `mcp_server.py` 职责相反：那是**被连接的 Server**（暴露本平台任务
登记处），本包是**去连接别人的 Host**。两者共用线格式但各自独立实现。

### 2.2 双传输

- **stdio**：启动子进程，换行分隔 JSON 帧；Windows 上 `select` 不能作用于管道，
  故用后台线程把 stdout 泵进队列、主线程 `queue.get(timeout=...)` 实现可超时读取；
  stderr 另起线程读走防管道阻塞；整条连接串行调用避免 id 串扰。
- **HTTP**：POST 一个 JSON-RPC 帧，读回纯 JSON 或 SSE `data:` 行；
  默认**直连不走系统代理**（本机代理会拦截 127.0.0.1 造成假性不可达），
  需要代理时显式 `use_proxy=True`。
- `via` 诚实标注：`stdio+json(loopback:subprocess)` / `http+json(loopback)` /
  `http+json(remote)`，**不把本地 mock 说成真跨网**。
- 简化点如实说明：HTTP 只做「一请求一响应」，不做会话保持/长轮询 SSE 流，
  不宣称完整 Streamable HTTP 会话语义；每次调用新建连接并重新握手。

### 2.3 MCPServerRegistry

- 落盘 `data/mcp_servers.json`（原子替换，损坏文件备份 `.corrupt` 后抛错，
  **不静默覆盖**）。
- 每个 Server 登记：传输、出域域名白名单、需审批工具、凭证引用名（**只存引用，
  不存值**）、风险等级、启停。
- 校验：name 格式、stdio command / http URL、allowed_domains、credential_ref
  大写环境变量名、risk_level 白名单。
- **登记 ≠ 在线**：`probe()` 必须真的 `initialize` + `notifications/initialized`
  + `ping` 全通过才标 `healthy`；连不上如实写 `last_error`，绝不假装在线。

### 2.4 安全代理层 `SecureProxy`（六道门，串在每次调用上）

| 门 | 实现 | 拒绝时 stage |
|----|------|--------------|
| 1 熔断 | `TTLCircuitBreaker`：连续失败 N 次 → TTL 窗口内直接拒；到期半开放行一次探测 | `breaker` |
| 2 审批 | 高危工具（写前缀 / `requires_approval_tools`）必须持已批准审批单；**未配置审批中心 fail-closed 拒绝** | `approval` |
| 3 域名 | `allowed_domains` 白名单（空 = 不允许任何出域），校验参数内 URL 与非 loopback 传输 URL，防 SSRF/外发 | `domain` |
| 4 凭证 | 参数里禁止自带 `token/api_key/secret/...`（模型不得持有凭证）；`CredentialVault` 从环境变量现取，只注入子进程 env / HTTP `Authorization` header | `credential` |
| 5 入向扫描 | 请求参数过 `scanner.detectors`（200+ 规则），命中 critical/high/medium → 如实挂起 | `secret_scan_in` |
| 6 出向扫描 | 响应内容同样过密钥扫描；命中 **如实挂起 + 脱敏 findings，不偷偷删除** | `secret_scan_out` |
| 附加 | 对端 `isError=True` → `upstream` 拒绝，**绝不包装成 ok=True** | `upstream` |

补充设计：
- `scan_secrets` 沿用 scanner 既定抑制词策略（EXAMPLE/低熵填充值不拦截），
  这是防误报不是漏扫；真实高熵凭证不会命中抑制词。
- TTL 熔断与 `governance.circuit_breaker`（全局人手动、fail-open）语义不同：
  本模块是**对端故障熔断**，per-server、进程无关、TTL 自动半开。
- 审计：每次调用（**含被各道门拒绝**）在 `result.audit` 记录
  server/tool/参数键与指纹/各门决策/via/fixture/耗时；提供 `trace_store+trace_id`
  时再落一条 `mcp_call` 轨迹事件（detail 脱敏、content 只留指纹）。
  无 trace 上下文时 `trace_written=False` 并注明「不硬造孤儿轨迹」。
  成本 `cost_estimate=None` + `cost_note`「无计费器接入，未估算成本（不编造数字）」。

### 2.5 GitHub MCP 样板 `servers/github.py`

`handle()` 纯函数同时服务 stdio 子进程与 loopback HTTP handler，实现
`initialize` / `ping` / `tools/list` / `tools/call` / `resources/list` /
`resources/read` / `prompts/list` / `prompts/get`。

- Tools：`search_repositories` / `get_file_contents` / `list_issues` / `create_issue`
- Resources：`repos://sample/readme`、`repos://sample/metadata`（Server 内置示例资源）
- Prompts：`repo_summary`（要求「结论必须带来源，缺工件不得标记 done」）

**三种数据模式（绝不伪造）**：

| 模式 | 触发 | 行为 |
|------|------|------|
| `live` | 设了 `GITHUB_TOKEN` | 真调 `https://api.github.com`，失败如实 `isError` |
| `fixture` | 未设 token 且显式 `SECTRUST_MCP_GITHUB_FIXTURE=1` | 返回明确标注 `_fixture=true` 的离线数据；**写操作拒绝执行**，不返回伪造 issue 号 |
| `unavailable` | 未设 token 且未开 fixture | 明确 `credential_unavailable`，**一条假数据都不给** |

`live` 模式（需真实 token 联网）本环境未验证，代码路径存在但不宣称已实测。

### 2.6 与 A3 联邦互联的区分

A3 `governance/interop.py` 是**智能体互联**（对端也是 AI 同事平台，同规范端点）；
B2 是**工具/数据源接入**（对端是 MCP Server）。两者都走 HTTP+JSON 但语义不同，
代码完全分离，未混用。

---

## 3. 第三件事 · 默认不信任 AI（B3）

### 3.1 Skill 侧 `agentskills/guard_chain.py · SkillExecutionGuard`

`execute()` 串起三层门，**任一不过返回 `ok=False` + 具体 stage，绝不静默标记完成**：

1. **审批门**：`manifest.requires_approval` 或 `risk_level ∈ {high,critical}`
   → 必须持已批准审批单；未配置审批中心 fail-closed。
2. **证据门**：系统亲自在 `sandbox/` 跑脚本，把 exit_code / stdout / ran_at
   构造成 `Evidence` 交 `EvidenceGate.verify()` 核验（命令形态、退出码 0、
   输出非空、新鲜度、措辞 Red Flags）——**Agent 无法伪造实跑输出**；
   被驳回也 `record()` 留痕。
3. **质量门**：脚本声明的 `required_artifacts`（竞品分析三项工件）必须由调用方
   提供**非空的实际内容**，且内容过 `QualityGate.check_output()`
   （禁 TODO / 待补充 / `<待补:x>` 未填槽位）；缺任一工件不得标记 done。

每次执行（含被拒）落 `skill_run` 审计摘要，可提供 trace 上下文落轨迹。

### 3.2 MCP 侧 `mcp_adapter/guard_chain.py · MCPEvidenceAdapter`

实现 B2 预留的 `verify_mcp_result(server, tool, result)` 钩子，挂到 `SecureProxy`
后写类工具（`create_/update_/delete_/publish_/send_/merge_...`）必须提交
`structuredContent.evidence`：

- 缺字段 → 直接拒绝（不静默当成「已完成」）；
- 有字段 → 构造 `Evidence` 交 `EvidenceGate`，校验命令形态/退出码/输出非空/
  新鲜度/expect-forbid/措辞 Red Flags，并落审计库；
- 可叠加 `QualityGate` 与必检工件清单；
- 任一不过 → `SecureProxy` 返回 `stage="evidence"` 的如实拒绝。

只读工具无「完成声明」语义，不要求证据（`checked=False` 并注明原因）。

### 3.3 拒绝矩阵（全部有单测覆盖）

| stage | 触发 | 测试 |
|-------|------|------|
| `registry` / `install` | 未登记/未启用 Server、技能不存在或安装期校验失败 | ✅ |
| `breaker` | 对端连续失败触发 TTL 熔断 | ✅ |
| `approval` | 高危工具无审批 / 审批未批准 / 无审批中心 | ✅ |
| `domain` | 参数 URL 越白名单（SSRF） | ✅ |
| `credential` | 参数自带 token 字段 | ✅ |
| `secret_scan_in` / `secret_scan_out` | 请求/响应命中密钥规则（脱敏 findings） | ✅ |
| `upstream` | 对端明确 `isError` | ✅ |
| `evidence` / `artifacts` / `quality` | 缺证据/坏证据/缺工件/工件含 TODO | ✅ |
| `sandbox` | 脚本路径越界或执行失败 | ✅ |

---

## 4. 诚实边界（逐条落实）

1. **无 token 不可用不造假**：GitHub 样板 `unavailable` 模式返回
   `credential_unavailable`，零假数据；凭证缺失时代理层不注入、不阻断，
   由对端如实返回不可用（不伪造、不静默放行）。
2. **mock fixture 标注清楚**：fixture 数据带 `_fixture=true` + 说明；
   loopback HTTP 标 `http+json(loopback)`；**不冒充真跨厂商/真跨网**。
3. **沙箱外脚本拒绝执行**：`run_script` 路径越界/非 scripts 一律
   `SkillSecurityError`；本地降级 `containment=advisory`，**不冒充 OS 级隔离**。
4. **成本不编造**：无计费器接入 → `cost_estimate=None` + 明确 note。
5. **审计不硬造孤儿轨迹**：无 `trace_store/trace_id` 时 `trace_written=False`
   并注明原因（与 `approval.py` 同纪律）。
6. **证据门不越界自证**：只核验结构化证据的形态与新鲜度，**不证明外部副作用
   真的发生**；工件齐备只验「调用方提供了非空内容」，不做语义审核。
7. **live 模式未实测**：真实 `GITHUB_TOKEN` → api.github.com 路径存在，
   本环境无 token 未验证，报告中如实标注。

---

## 5. 文件清单

### 新建

| 文件 | 说明 |
|------|------|
| `agentskills/skill_md.py` | 标准 SKILL.md 解析器 + StandardSkillLibrary（L1/L2/L3 + manifest + 沙箱） |
| `agentskills/guard_chain.py` | SkillExecutionGuard（审批 → 实跑证据 → 工件/质量门 + 审计） |
| `agentskills/library/competitor-analysis/*` | 标准 SKILL.md 样板（含 manifest/references/assets/scripts） |
| `mcp_adapter/__init__.py` | 包导出 |
| `mcp_adapter/protocol.py` | JSON-RPC 2.0 帧 + 三原语数据结构 + 私有错误码 |
| `mcp_adapter/transport.py` | StdioTransport / HttpTransport 双传输 |
| `mcp_adapter/registry.py` | MCPServerRegistry + ServerSpec |
| `mcp_adapter/breaker.py` | TTLCircuitBreaker（per-server TTL 熔断） |
| `mcp_adapter/proxy.py` | SecureProxy 六道门 + 审计 |
| `mcp_adapter/guard_chain.py` | MCPEvidenceAdapter + attach_evidence_gate |
| `mcp_adapter/servers/__init__.py`、`servers/github.py` | GitHub MCP 三原语样板 |
| `tests/test_skill_md.py` | B1 单测（L1 只读硬证明 / L3 路径穿越 / manifest / 沙箱 / 双加载器） |
| `tests/test_mcp_adapter.py` | B2 单测（双传输三原语 / Registry / 熔断 / 六道门） |
| `tests/test_guard_chain.py` | B3 单测（Skill 三层门 / MCP 证据门 / 审批 / 审计轨迹） |

### 修改

| 文件 | 改动 |
|------|------|
| `pyproject.toml` | `packages` 增加 `mcp_adapter`、`mcp_adapter.servers`；`agentskills` package-data 纳入 references/assets/scripts |
| `tests/test_agentskills.py` | `test_builtin_skills_all_valid` 内置技能集合由硬编码相等改为「核心 4 个子集 + 全部 ok + broken==0」，容纳新增标准样板 |
| `agentskills/library/competitor-analysis/scripts/collect_outline.py` | 修复 Windows UTF-8 输出与 argv off-by-one |

---

## 6. 验证结果

| 范围 | 结果 |
|------|------|
| B1：`tests/test_agentskills.py` + `tests/test_skill_md.py` | 70 passed, 22 subtests |
| B2：`tests/test_mcp_adapter.py` | 37 passed, 12 subtests |
| B3/B4：`tests/test_guard_chain.py`（含审计轨迹） | 29 passed, 7 subtests |
| **阶段 B 新增/扩展合计** | **136 passed, 41 subtests passed（14.10s）** |
| **全量 pytest** | **1284 passed, 2 skipped, 41 subtests passed, 0 failed（203.84s）** |

对账：阶段 A 基线 `1188 passed, 2 skipped, 0 failed`，阶段 B 净增 96 项单测
（`test_skill_md` 30 + `test_mcp_adapter` 37 + `test_guard_chain` 29），
1188 + 96 = **1284**，**零 failed，无新增回归**。阶段 B 未改动阶段 A 的任何生产模块，
仅放宽一处既有测试断言（`test_builtin_skills_all_valid` 用子集替代硬编码相等）。

已知 flaky：`tests/test_updates_check_returns_known_state` 依赖联网，工单标注
可忽略；本轮阶段 A 全量时未触发。

---

## 7. 与工单第 4 节的偏差说明（如实标注）

1. 工单建议 `client.py` 命名为 `MCPClient`；实现拆为 `protocol.py`（帧与数据
   结构）+ `transport.py`（双传输）+ `proxy.py`（带治理的客户端），方法面
   （initialize / tools|resources|prompts 全原语）完全覆盖，命名不同但等价且更分层。
2. 工单建议「把现有 `skills/` 里至少一个技能改造成标准 SKILL.md 样板」；
   实际**新建** `competitor-analysis` 标准样板（放在 `agentskills/library/`，
   同时被旧 loader 识别为第 5 个内置技能），未改动 `skills/` 目录内容。
   验收要求「至少一个标准 SKILL.md 样板跑通解析→L1→L2→L3→沙箱」已满足。
3. HTTP 传输简化为「一请求一响应」，未实现会话保持/长轮询 SSE 流，
   不宣称完整 Streamable HTTP 会话语义；每次调用重新握手有性能开销。

---

## 8. P2–P4 路线图待办（工单第六节，未做）

> 注：本节 P2–P4 是**工单第六节路线图编号**（专家模板引擎 / 技能市场 MVP /
> 开放开发者入口）。第 9 节 v2 智能运行时优化的 P0–P4 是**另一套分期编号**，
> 已实现；本节三项仍未开始。

| 阶段 | 待办 | 现状 |
|------|------|------|
| **P2 专家模板引擎** | Agent 模板 YAML、技能组合器、审批规则绑定 | 未开始；`SkillExecutionGuard` 已能把审批规则绑到技能执行 |
| **P3 技能市场 MVP** | 搜索/安装/启用/禁用、版本管理、评分、审计回放 | 未开始；`install_validate()` / `MCPServerRegistry.register()` / `set_enabled()` 已提供最小骨架 |
| **P4 开放开发者入口** | 外部上传 Skill/MCP/专家模板、审核流水线 | 未开始；manifest 安装期校验 + 路径穿越防护可作为审核流水线第一关 |

其它可接续的工程项（非本阶段承诺）：

- 控制台新增 `/api/governance/mcp/*` 端点（登记/探活/调用/熔断状态），走 A6 的
  RBAC 门；当前 MCP 能力仅以 Python API 形式提供。
- `cost.py` 成本看板按 Skill / MCP Server / Agent 分拆；当前代理层只留
  `cost_estimate=None` 的诚实占位，未接计费器。
- 接入真实跨厂商 MCP Server（非 loopback fixture）并验证 live 模式。
- MCP 连接会话复用（当前每次调用重连握手）。
- `EvidenceGate` 活体验证（`validators.py`）接到 MCP 写操作结果，进一步证明
  外部副作用真实发生。

---

## 9. v2 智能运行时优化（本轮新增）

> 用户拍板：**「白名单边界要更松，保证速度。接入策略选你的。直接落地。」**
> 决策：① 白名单更松、速度优先；② 接入用推荐的**渐进式**（保留 planner 硬编码
> `skill` 提示串 fallback + `skill_hint/query` 触发检索）；③ 直接实现 P0→P4。
> 编号辨析：本节 P0–P4 指 `智能运行时优化设计_v2.md` 的分期，与第 8 节工单
> 第六节的 P2–P4 是两套编号，勿混淆。

### 9.1 P0 · 接通主链路（关键前提：标准运行时原本是「未接线的能力孤岛」）

核查发现 `StandardSkillLibrary.match_l1()` 与 `SkillExecutionGuard` **全仓无生产调用方**，
daemon 的 `skill` op 走旧 `loader.activate(name)` 按名激活，planner 的 `skill` 字段只是
模板硬编码提示串——「先加语义检索」没有落点，必须先接通执行链。

- 新增 `agentskills/retrieval.py`：三路召回（BM25 词项 / HashedNGramIndex 哈希向量 /
  同义·实体扩展）+ RRF 融合（0.30/0.50/0.20）+ 规则式 Rerank `heuristic-v1`。
- 新增 `agentskills/router.py::SkillRouter`：召回 → 角色白名单（两套命名无交集时
  `no_overlap_fallback` 不过滤并留痕，不制造零召回）→ 分级/安装校验/熔断过滤 →
  Rerank → Top-K → `skill_match` Trace（打分瀑布可查）。
- `console_api.SKILL_PAYLOAD_KEYS` 与 `planner.SKILL_PAYLOAD_KEYS` **双白名单一致**
  （单测硬断言）；`payload.query/skill_hint` → `op=skill`，不声明时保持 `op=llm`。
- `execution/daemon.py::_execute_skill_query`：`op=skill`+query → SkillRouter 选技能 →
  `SkillExecutionGuard` 分级门禁；`name` 精确路径（旧 `loader.activate`）**保留兼容**。
- Trace 纪律：有 `trace_store+trace_id` 才写 `skill_match`，缺上下文
  `trace_written=False` **不硬造孤儿轨迹**。

### 9.2 P1 · 状态治理 + 自适应熔断 + 反馈闭环

- 新增 `agentskills/state.py::SkillStateStore`：会话内计数（内存）+ 跨会话 TTL 熔断
  （**复用** `mcp_adapter.breaker.TTLCircuitBreaker`，threshold=3，落盘
  `data/skill_breaker.json`，TTL 到期半开）+ `feedback_hint` 结构化提示。与
  `governance.circuit_breaker`（全局人手动、fail-open）语义不同，勿混用。
- 连续 3 次失败 → `circuit_open` → L1 阶段 `filtered_by="circuit_open"` 剔除
  （省 Token）→ 改选备选；`reset()` 恢复（不因一次环境抖动永久拉黑）。
- **驳回（门禁拦截）不计入执行故障熔断**——那是安全策略生效，不是技能故障。
- `guard_chain` 注入 state 回写成败/驳回，`daemon` 把 `feedback_hint` 回灌 result。
  反馈是**提示不是自动改道**：备选仍过完整检索 + 角色白名单 + 门禁。

### 9.3 P2 · 语义召回 / Rerank

- 「用户说**清理电脑**、技能叫**磁盘释放工具**」这类跨词场景纯词项会漏；三路召回
  + RRF + 同义表 `SKILL_SYNONYMS` 命中，单测（自建 fixture 技能库）证明
  `disk-cleanup` 稳定排名第一，且打分瀑布（bm25/vector/entity/rrf/rerank）可解释。
- 诚实边界：`heuristic-v1` 是**规则式打分不是神经 cross-encoder**；哈希向量是
  n-gram 近似语义不是真语义理解；同义表是有限领域词典；真 embedding provider
  不可用时**整体降级** `hashed_fallback`、绝不混哈希与真向量空间；全程只读 L1
  frontmatter，L1 单测断言正文标记绝不外泄。

### 9.4 P3 · 分级安全（更松白名单）+ MCP 并行门

- 新增 `agentskills/tier.py`。**放宽点**（用户拍板「更松、速度优先」）：
  ① risk 门槛 low → **low+medium**；② 去掉「allowed_domains 为空/不出域」硬约束
  （「彻底数据不出域」已被豁免），改运行期动作探测兜底；③ `web_search`/`file_read`
  等**只读意图**工具不再被当副作用排除；④ 缺省 `security_tier=auto` 自动判定 white，
  **无需手写 manifest.json**；⑤ white 免 manifest 安装校验。
- **绝对底线（任何声明不可突破，违反即强制 black）**：带 `scripts/` 强制 ≥gray
  （沙箱不可免）；副作用工具（写/执行/删除/发送/发布/支付/改配置）强制 black；
  `risk ∈ {high,critical}` 或 `requires_approval=true` 强制 black；声明 white 但硬条件
  不满足 → **自动降级并记原因**，绝不静默放行。
- 分级门：`white`=L2 正文快速通道（`execution_mode=l2_only`，免沙箱/密钥扫描/审批/
  证据/工件/质量，`waived_gates` 留痕可审计）；`gray`=审批+沙箱+证据（免工件/质量）；
  `black`=**五层门全开**，一条不少。
- **运行期越界** `escalate_on_action()`：white 技能被请求执行脚本/写文件 → 立即降级
  black + 拒绝 + `rule_id=tier.white_script`（「白」是可撤销的信任）。
- `mcp_adapter/proxy.py`：入向四门（审批/域名/凭证/入向密钥扫描）改
  `ThreadPoolExecutor` **并行 fan-out**；熔断仍先串行决定放行，传输/出向扫描/证据仍
  串行（有依赖）。**入向密钥扫描不参与短路**（安全底线必须跑完）。实测 3 门 ×60ms：
  并行墙钟 **60.6ms < 串行累加 181.7ms**。
- 每道门补 `rule_id`/`duration_ms`/`parallel`；`blocked_by` 取首个拒绝门（并行组按
  固定顺序落 stage 保证稳定）；新增 `guard_decision` Trace（stages 泳道 + blocked_by
  + rule_id + decision + parallel 指标）；MCP 侧状态回写 `mcp:<server>` 维度，与技能
  维度隔离。

### 9.5 P4 · 技能执行可观测仪表盘

- 控制台新增 4 个 GET 端点（`governance.read`）：
  `/api/governance/skills/state`（状态热力）、`/decisions`（打分瀑布）、`/guard`（门禁
  泳道）、`/summary`（汇总）；2 个 POST：`/run`（`task.create` 触发 `op=skill` 任务）、
  `/state/reset`（`governance.emergency`，**仅 admin**）。GET 走 `_gov_guard`、POST 走
  集中式 `_PERM_MAP` 门，双保险。
- 前端 `view-skillops` 面板：三视图 + 试跑 + 熔断状态 + 命中规则。页面验证按项目
  纪律 = 抽内嵌 JS `node --check` + **DOM stub 渲染**（测试内置 cjs stub，检查 8 个
  关键串全部出现），**不只验 HTTP 200**。

### 9.6 v2 新增/修改文件与验证

| 文件 | 改动 |
|------|------|
| `agentskills/retrieval.py` | 三路召回 + RRF + 规则式 Rerank（新建） |
| `agentskills/router.py` | SkillRouter 选技能 + skill_match Trace（新建） |
| `agentskills/state.py` | 会话状态 + TTL 熔断 + 反馈闭环（新建） |
| `agentskills/tier.py` | 分级安全 + 绝对底线 + 越界降级（新建） |
| `agentskills/guard_chain.py` | 分级门禁 + guard_decision Trace + state 回写 |
| `agentskills/skill_md.py` | L1 增 `security_tier` / `has_scripts` 字段 |
| `mcp_adapter/proxy.py` | 入向四门并行 + rule_id/duration + guard_decision + state 回写 |
| `console_api.py` / `planner.py` | `SKILL_PAYLOAD_KEYS` 双白名单透传 |
| `execution/daemon.py` | `_execute_skill_query` 接通主链路 |
| `console_web.py` | 6 个 `/api/governance/skills/*` 端点 + `view-skillops` 面板 |
| `tests/test_skill_runtime_v2.py` | **28 项**离线单测（新建） |

| 范围 | 结果 |
|------|------|
| v2 新增 `tests/test_skill_runtime_v2.py` | **28 passed（3.47s）** |
| 相关子集（governance/skill/mcp/编排/console，12 文件） | **347 passed, 41 subtests（29.11s）** |
| **全量 pytest** | **1312 passed, 2 skipped, 41 subtests passed（223.75s），0 failed** |

### 9.7 v2 诚实边界（逐条）

1. Rerank 是规则式打分，不是神经 cross-encoder；哈希向量是 n-gram 近似语义，
   不是真语义理解；同义表是有限词典，不宣称通用语义。
2. 并行只并行互不依赖的入向门，black 级校验项一条不少；入向密钥扫描不短路。
3. white 快速通道只做 **L2 正文激活**，模型驱动的下游执行归 AgentRunner，
   **不在此越界、不宣称端到端跑通**。
4. feedback 是提示不是自动改道；熔断是降权/TTL 半开/可 reset，不是永久删除。
5. 无计费器 `cost_estimate=None`；无 trace 上下文 `trace_written=False` 不硬造孤儿轨迹。
6. 仪表盘 DOM stub 是离线渲染验证，非真浏览器截图；`node` 不可用时测试 skip。
7. 语义召回单测用自建 fixture 技能库，不改动内置 5 个技能的内容。
