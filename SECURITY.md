# 安全设计白皮书（SECURITY）

> **产品定位**：这不是一个"更会聊天的 AI"，而是一个 **更安全、可审计、可常驻的 AI 同事执行平台**。
> 设计哲学是 **保守默认拒绝（Secure-by-default, Deny-by-default）**——任何有副作用的动作，
> 先停下来等人确认，再在隔离环境里执行，全程留痕。当同类产品因"默认权限过高"被质疑时，
> 这种保守设计正是我们的差异化优势。

---

## 1. 六大安全支柱

| # | 支柱 | 一句话 | 实现位置 |
|---|------|--------|----------|
| 1 | **Human-in-the-loop 审批** | 高权限动作先挂起等真人批准，超时默认拒绝 | `approval.py` |
| 2 | **沙箱隔离执行** | 危险动作只在容器/远程/受限环境里跑 | `sandbox/` |
| 3 | **跨进程任务审计** | 所有动作经统一登记处留痕，可查可回溯 | `task_registry.py` |
| 4 | **文件越权拦截** | 本地降级模式拦截明显跳出工作目录的访问 | `sandbox/LocalSandbox` |
| 5 | **多模型降级** | 缺密钥/厂商不可用时自动回落到离线 Mock，不崩溃 | `models/ModelRouter` |
| 6 | **WAL 高并发登记处** | 同机多进程安全读写，锁冲突降到极低 | `task_registry.py` (WAL) |

---

## 2. 威胁模型（我们防什么）

| 威胁 | 来源 | 缓解措施 | 残余风险 |
|------|------|----------|----------|
| 模型/技能产生危险指令（删库、外发数据） | LLM 幻觉、恶意技能 | 审批中心拦截所有副作用动作；沙箱隔离执行；Docker 默认断网，外发须审批 | 审批人误点"批准" |
| 不可信远程技能代码执行 | GitHub 拉取技能 | **必须 pin commit/tag + 可信仓库 allowlist + 隔离环境** | 仓库被攻破投毒 |
| 本地提权 / 文件越权 | 被扫描代码含恶意路径 | 沙箱 jail + 越权路径预检；强隔离用 Docker/E2B | 本地降级为弱隔离 |
| 单点故障/数据丢失 | 进程崩溃、断电 | WAL + synchronous=FULL；`backup()` 整库合并 | NORMAL 模式丢最近数秒 |
| 密钥泄露 | 硬编码凭据 | **密钥只走环境变量/密钥服务，仓库零硬编码** | 环境变量被本机其他进程读取 |
| 监控端口被滥用 | 仪表盘暴露 | 默认 `127.0.0.1` 仅本机；**只读**，不暴露登记处文件 | 端口被转发到公网 |
| Redis 被未授权访问/嗅探 | 跨机协同引入的网络服务 | **默认不启用**（`SECTRUST_REDIS` 未设 = 零接触）；启用时 `REDIS_URL` 内嵌密码走 AUTH；锁/心跳/死信只存任务 ID 与token，不存任务内容 | 内网嗅探、Redis 无密码弱部署 |

### 明确不防（诚实声明）
- **本地降级沙箱无法防本地提权**。它只是把执行约束在 jail 工作目录、清洗危险环境变量、拦截明显越权路径，
  并对超时进程强杀。请勿在本地降级模式下运行不可信代码——务必上 Docker 或 E2B 强隔离。
- **审批依赖"人"**。审批中心默认拒绝，但若审批人随意点"批准"，危险动作仍会执行。
  生产建议把审批权限收敛到少数可信角色，并对批准动作本身留痕。

---

## 3. 各支柱详解

### 3.1 Human-in-the-loop 审批（默认拒绝）
任何高权限动作（如 `/scan repo`、调用外部 API、写文件）在 `AgentRunner`/Demo 管线里先调用
`ApprovalCenter.request()`，任务进入 `pending` 并挂起。`wait_for_decision()` 阻塞等待，
**超时自动 expire，绝不默认放行**。真人通过 CLI（`python approval.py --approve <id>`）或
未来接入的 Web 按钮决策。

**审批审计证据链（#91）**：每条审批在 Trace 轨迹里留下 `approval_requested` /
`approval_decided` / `approval_expired` 事件，构成可回放、可审计的证据链——
- 原文不进事件体，只留 blake2b 指纹（`content_hash`），能验真"批的到底是什么"又不囤敏感原文；
- 高危动作（外发/发布/购买/支付/删除/扫描）事件带 `risk=high` 标记；
- `approvals` 表新增 `trace_id` 列（审批行自描述，任何进程含 CLI 都能直查轨迹）；
- `audit(approval_id)` 返回一条审批的完整证据链（审批行 + 全部审批轨迹事件 + 决策耗时）；
- 留痕全程 best-effort：轨迹写入失败**绝不**挡审批主链路（旁路纪律）。
`python approval.py --audit <id>` 即可调阅。

### 3.2 沙箱隔离执行
- **Docker（强隔离，推荐）**：八条规格硬化——每任务独立容器 `--rm` 结束即毁
  （超时/异常显式 `docker kill`，`cleanup_stale()` 跨进程兜底）；`--user` 非 root；
  `--read-only` 根文件系统 + tmpfs `/tmp`，只有输出目录可写；`--network none`
  默认断网，外发网络须持已批准 `approval_id`（管理层 + 后端双重校验）；
  `--cpus`/`--memory`（禁 swap）/`--pids-limit` 资源上限；输入目录只读挂载、
  输出目录单独挂载；`allowed_commands` 白名单只放行受控 Tool（拒 shell 字符串，
  参数 `shlex.quote` 防注入）；`--init`/`--cap-drop ALL`/`no-new-privileges`。
  `--runtime` 为 gVisor/Kata 预留（当前只验证 runc 调用链）。
- **E2B（远程强隔离）**：远程 VM 沙箱，适合无本机 Docker 的场景（需 `pip install e2b` + `E2B_API_KEY`）。
- **本地降级（弱隔离）**：jail 工作目录 + 清洗环境 + 越权路径预检 + 超时强杀。仅用于可信低强度任务。
- `SandboxManager.auto()` 按可用性自动选择：docker → e2b → local，并打印当前后端。
  Docker 配置面走 `SECTRUST_SANDBOX_*` 环境变量（`SandboxManager.docker_from_env()`），
  不改代码即可套用上述规格。

### 3.3 跨进程任务审计
所有入口（各渠道）、执行、结果都经 `TaskRegistry` 登记：`task_id / runner_id / status /
created_at / updated_at / payload`。`task_center_web.py` 提供 **只读** 仪表盘实时查看，
状态变更、审批、沙箱进度全程留痕。删除登记处文件后首个写入方自动重建表（自愈）。

### 3.4 文件越权拦截
`LocalSandbox._jail_check()` 在执行前扫描命令文本，拦截包含 `../`、`/etc`、`/proc`、`C:\Windows`
等明显越权路径 token 的调用。配合强制 `cwd=workdir`（jail 根），把副作用限制在沙箱目录内。

### 3.5 多模型降级
`ModelRouter` 按模型名前缀路由到 OpenAI 兼容 / Anthropic / Ollama。**任一 provider 缺密钥时
`configured=False`，自动跳过并降级到内置 `MockModelProvider`**，运行时不会因缺 key 崩溃。
保证"离线也能跑、断网也不崩"。

### 3.6 WAL 高并发登记处
本地 SQLite + WAL 日志 + `busy_timeout=30s`，允许多读单写并发，`database is locked` 概率极低。
- 备份必须整库：`backup()`（sqlite3.backup API 自动合并主库+WAL）或先 `wal_checkpoint(TRUNCATE)` 再复制——
  **只复制主文件会丢未 checkpoint 的 WAL 数据**。
- 长读事务监控：`checkpoint()` 返回 `(total, ckpt, status)`，`status != 0` 提示有读事务挂着阻止收缩。
- `synchronous=NORMAL` 可提速约 3x（WAL 下安全），但极端断电丢最近数秒；数据绝对优先时保持 `FULL`。

### 3.7 MCP 暴露面（生态入口的安全边界）
`mcp_server.py` 把审批中心 / 沙箱执行链 / 登记处 / 长期记忆暴露为标准 MCP 工具，供 Claude Code、Cursor、
Open WebUI 等客户端调用。**它是一块新增的暴露面，安全约束如下：**
- **只暴露声明过的工具（共 22 个）**：任务登记处（list_tasks/get_task/list_runners）、审批中心
  （request_scan/approve/deny/execute_scan/run_scan/list_approvals/list_templates）、长期记忆
  （memory_set/get/search/list/top/forget）。没有「直连 DB 写」「绕过审批」的工具。
- **执行必须过审批闸门**：`request_scan` 只登记 + 进审批（默认拒绝）；`execute_scan` 内部强制校验审批状态，未批准一律拒绝。**MCP 客户端无法绕过 Human-in-the-loop。**
- **`run_scan(auto_approve=True)` 是可信自动化开关**，仅当你确信调用方可信时才开放；默认不传即走两步式（request → approve）。
- **传输只走 stdio**：由 MCP 客户端以子进程方式拉起，不侦听任何网络端口，降低被远程利用面。

### 3.8 HTTP API 暴露面（分布式入口，新增）
`api/app.py` 把同一套内核暴露为 REST（runner / 外部 agent 可跨机访问）。**安全约束：**
- **默认开放（本地/内网）**；设 `API_AUTH_ENABLED=1` + `API_TOKENS=token1,token2` 后用 Bearer 鉴权（`security/oauth.py`，零依赖：支持静态令牌 / 自签 HS256 JWT / 远程 introspection）。
- **传输层 TLS 由反向代理/网关负责**（Nginx/Caddy），本服务只做令牌校验与业务处理。
- 所有请求自动打点（计数 + 延迟）并结构化日志，可接 SIEM；阈值告警见 `security/observability.py`。
- 完整 OAuth 2.1 落地路线（客户端凭据 / 授权码 + PKCE / scopes / 多租户）见 `OAuth_ROADMAP.md`。

### 3.9 渠道入站验签与解密（真实联调，新增）
- **企业微信**：GET 验签 + echostr 解密回显；POST 验签 + AES-256-CBC 解密消息体。解密默认用
  **零依赖纯 Python AES**（`security/crypto.py`，已通过 NIST 向量），环境装了 `pycryptodome` 会自动优先。
- **飞书**：HMAC-SHA256 验签（或 verification token）；可选开启 Encrypt Key，对加密回调做 AES 解密。
- 所有凭据从环境变量读取（`WECHAT_*` / `FEISHU_*`），不硬编码。

### 3.10 Redis 跨机协同暴露面（分布式，新增）
`transport/distributed.py`（`SECTRUST_REDIS=1` 启用）在 Redis 上存三类数据：任务认领锁（token）、
runner 心跳（runner_id + bot 元数据）、死信条目（task_id + 失败原因 + payload）。**约束：**
- **默认不启用**：`SECTRUST_REDIS` 未设时零接触，不开任何连接，行为与单机版完全一致。
- **绝不静默降级**：强开但 Redis 不可达 → 构造即抛错。跨机场景"以为有分布式保护其实没有"
  比没有保护更危险。
- **不存任务内容**：锁/心跳/死信只存 ID 与 token；任务正文仍在 SQLite/工作区。
- **认证与网络**：`REDIS_URL` 内嵌密码走 AUTH（手写 RESP 客户端支持）；Redis 本身应部署在
  可信内网并按需开 `requirepass`/ACL/ TLS 代理——这属于 Redis 部署侧职责，不在本仓库。
- **部署前自增检查项**：确认 Redis 绑定内网地址且需要密码（见第 5 节清单）。

---

## 4. 密钥与配置安全
- **零硬编码**：所有密钥（OpenAI / Anthropic / WeChat / Feishu / E2B）只从环境变量读取（见 `.env.example`）。
- 部署用 `.env` 或密钥管理服务（Vault / KMS / 云密钥中心），**不要把 `.env` 提交进仓库**（已加入 `.gitignore` 建议）。
- 监控端口、Web 查看端默认绑定 `127.0.0.1`，不暴露到公网；登记处 SQLite 文件本身不被网络直接暴露。

## 5. 部署安全清单（上线前自查）
- [ ] 沙箱后端为 Docker 或 E2B（非本地降级），或已明确接受本地降级风险
- [ ] `.env` 已配置且未被提交；密钥来自密钥管理服务
- [ ] 监控端口绑定 `127.0.0.1`，未被转发公网
- [ ] 审批权限收敛到可信角色；审批动作已留痕
- [ ] 若用 GitHub 拉取技能：已 pin commit/tag + 可信仓库 allowlist + 隔离执行
- [ ] 已配置 WAL 备份（crontab 跑 `python task_registry.py --backup-to <dest>`）
- [ ] `data/` 目录权限正确，所有进程账户可读写
- [ ] 若启用跨机协同（`SECTRUST_REDIS=1`）：Redis 绑定内网、已设密码、`REDIS_URL` 未提交进仓库

## 6. 如何报告安全问题
请通过私有渠道（邮件/工单）报告，勿在公开 Issue 披露细节。我们将在确认后 72 小时内响应。
