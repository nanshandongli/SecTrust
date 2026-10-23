# 自检评测报告（SELF AUDIT）· v3

> **评测时间**：2026-10-23 · **环境**：Windows / Python 3.13.12 · **样本量**：n=500（性能）、37 条（扫描语料）
> **方法**：不采信文档，全部数据由 `tools/self_audit.py` 从**代码与运行时**实测取证，可复现：
> `python tools/self_audit.py`（含 `depth` 段，专测扫描/记忆/技能/证据门禁）
>
> **本轮性质**：针对 v1 报告里"**不够新、不够深**"的短板做定向补课，
> 并联网调研了 gitleaks / TruffleHog / Semgrep / Mem0 / MCP 2026-07-28 规范后借鉴落地；
> 此外从 GitHub 高星项目 **obra/superpowers（303K★, MIT）** 借鉴了「证据优于声明」的工程实践，
> 落地为可拦截的 **EvidenceGate 硬门禁** + 渐进式披露的**技能库**。
> **诚实声明**：测不了或没有真实凭据的项目一律标注 `未验证`；**自造语料的分数不与公开基准等价**。

---

## 一、结论速览（改进前 → 改进后）

| 维度 | v1（改进前） | v3（本轮） | 变化 |
|------|-------------|-----------|------|
| 扫描能力 | **C+**（5 条正则，深度差 1~2 数量级） | **B+**（78 条结构化检测器 + 熵兜底 + 金标准基准 F1=1.0） | ↑↑ |
| 记忆能力 | **C+**（单路 n-gram，无时间维度） | **B**（三路融合 RRF + 衰减强化 + 分层作用域） | ↑ |
| 规范与生态时效 | **C**（声明 2024-11-05，落后 3 个大版本） | **A-**（2026-07-28 + discover + OAuth 2.1 资源服务器） | ↑↑ |
| 可信执行（技能+证据门禁） | 无 | **A**（4 技能库 + EvidenceGate 拦 4/4 虚假声明，放行 1/1 合法证据） | 新增 |
| 工程严谨度 | A- | **A-**（零依赖仍成立，硬依赖 = 空集） | 持平 |
| 安全叙事 | B+ | **A**（3/3 穿透仍被挡 + 证据门禁使"Done!"无法凭空成立） | ↑ |
| 性能 | B+ | **B+** | 持平 |
| **综合** | **7.2 / 10** | **8.6 / 10** | **↑1.4** |

**一句话**：短板从"不够新、不够深"变成了"**够新、够深、且有可拦截的可靠性闸门，但仍未与公开基准对齐、未真上架**"。

---

## 二、改进前后对照（功能面，全部程序化清点）

| 项 | v1 | v3 | 来源 |
|----|----|----|------|
| MCP 工具 | 22 | **29（全部带风险注解，新增 scan_deep / memory_hybrid_search / skill_list / skill_load / evidence_submit / evidence_run / task_complete_verified）** | `mcp_server._TOOLS` / `_annotated_tools` |
| REST 端点 | 18 | **20**（+ `.well-known/oauth-protected-resource`、+ `/scan/deep`） | `api.app.RegistryAPI._ROUTES` |
| 扫描模板（合规模板·广度） | 5 | 5 | `templates.TEMPLATES` |
| **结构化检测器（深度）** | **0（仅散落正则）** | **78 条 / 覆盖 50 个厂商** | `scanner.detectors.detector_stats` |
| — 其中带熵门槛 | — | 10 | 同上 |
| — 其中带 keywords 预检 | — | 77 | 同上 |
| — critical / high | — | 22 / 44 | 同上 |
| **记忆检索信号** | 1（n-gram 余弦） | **3（向量 + BM25 + 实体）RRF 融合** | `memory_hybrid` |
| **代理技能库（借鉴 superpowers）** | 无 | **4 个技能 / 0 损坏 / 渐进式披露 / 全激活 6,088 tokens** | `agentskills/loader.py` + `library/*` |
| **证据门禁 EvidenceGate** | 无 | **5 探测→拦 4 虚假声明、放 1 合法证据（block_rate 1.0）** | `agentskills/evidence.py` |
| 回归测试 | 80 | **299（全过，含象棋原型 25 + DAG 调度 22 + 编排层应用 76）** | `unittest discover` |
| 代码规模 | 45 模块 / 6,952 行 | **54 模块 / 10,836 行**（self_audit 全量清点，含 tests/examples/tools） | `tools/self_audit.py` inventory |
| **硬依赖（必装）** | 空集 `[]` | **空集 `[]`（仍成立）** | 全量 import 扫描 |
| 可选依赖 | 5 | 6（Crypto / e2b / mem0 / otel / redis / **agentskills**） | 均在 try/except 内 |

> **零依赖承诺在新增约 3,900 行之后依然为真**——这是本项目最硬的差异化，本轮改造没有破坏它。
> EvidenceGate 把"**证据优于声明**"从一句 prompt 软约束升级成**可拦截的硬门禁**：任务声明完成前，
> 必须提交真实可核验的证据（命令 + 退出码 + 输出 + TTL），否则任务状态**真的不会变**。

---

## 三、本轮借鉴了谁（联网调研来源与落地点）

| 借鉴对象 | 学到的关键点 | 落到本项目哪里 |
|---------|-------------|---------------|
| **gitleaks v8**（规则形态） | 规则 = `id/regex/keywords/entropy/secretGroup/path/tags`；keywords 是**预检 + 正确性**双保障；三档抑制（行内 → `.gitleaksignore` 指纹 → 配置 allowlist）；baseline 只报新增 | `scanner/detectors.py`、`scanner/findings.py`（指纹 / 三档抑制 / 基线 / gate） |
| **TruffleHog**（熵判据） | 香农熵 H>3.5 bits/char 可捕获**形态未知**的凭证；对未验证结果做熵过滤降噪 | `scanner/entropy.py`（熵 / 滑动窗口 / assess 三层收敛 / 抑制词） |
| **Semgrep**（生态与增量） | SARIF 2.1.0 是 GitHub Code Scanning / IDE 的通用输入；`--baseline-commit` 只扫变更；`nosemgrep` 行内抑制 | `scanner/sarif.py`、`engine.changed_files()`（diff-aware）、`scanner:allow` |
| **Mem0 v2/v3**（记忆算法） | 语义 + BM25 + 实体三信号「各自归一化 → 融合为单一分数」；RRF(k=60)；**记忆衰减 + 强化循环**（被召回的刷新访问历史，分数 clamp [0,1]）；ADD-only 不覆盖；4 类记忆 + 5 维作用域 | `memory_hybrid.py`（BM25 / 实体倒排 / RRF / 半衰期衰减 / kind / scope） |
| **MCP 2026-07-28**（规范） | 无状态核心：移除 initialize 握手与 Mcp-Session-Id；新增 `server/discover`；客户端信息走 `_meta`；版本不符 → `UnsupportedProtocolVersionError`；Roots/Sampling/Logging 进入 12 个月弃用 | `mcp_server.py`（版本协商 / discover / 能力声明 / 工具注解） |
| **OAuth 2.1 + RFC 9728/8707/9207 + CIMD** | 服务器是资源服务器：暴露 Protected Resource Metadata；令牌绑定 aud 防跨服务重放；校验 iss；DCR 弃用改 CIMD | `security/oauth.py`、`api/app.py`（元数据端点 + 401 WWW-Authenticate） |
| **obra/superpowers（303K★, MIT）** | 「证据优于声明」：完成前必须给出可验证证据，杜绝"Done!"式空口自认；SKILL.md frontmatter 规范 + 渐进式披露（不读正文即估预算） | `agentskills/evidence.py`（EvidenceGate 硬门禁）、`agentskills/loader.py`（SkillLibrary 渐进式披露）、`library/*`（4 个严格按规范写作的技能） |

---

## 四、深度实测（新增 `--only depth`）

### 4.1 扫描：金标准语料（自造，可复现）

```
python tools/scan_bench.py
语料 37 条（真阳性 27 / 诱饵 10）· 规则库 78 条
TP=27  FN=0  FP=0  TN=10
召回率 100.0%   精确率 100.0%   F1 1.000   SARIF 结构 OK
```

诱饵刻意包含行业公认误报源：AWS 文档示例密钥 `AKIAIOSFODNN7EXAMPLE`、SHA-256、UUID、
纯数字流号、`changeme`、`your_api_key_here`、CI 表达式 `${{ secrets.X }}`——**全部未报**。

⚠️ **这份 100% 不能与 TruffleHog 的公开成绩直接比较**：语料是本仓库自造（37 条），
TruffleHog 用 Detect-Test-Secrets 且声称 800+ 检测器 / 89~96%。本分数证明的是
「**形态覆盖 + 降噪逻辑成立**」，不证明「比 TruffleHog 强」。

### 4.2 记忆：混合检索 Top1

| 查询 | Top1 | 三路信号（向量 / BM25 / 实体） |
|------|------|------------------------------|
| 代码审查用哪个模型 | code_review_model | 0.144 / **3.237** / 0 |
| Qwen-Max | code_review_model | 0.720 / 1.589 / **1.5** |
| 主仓库路径 | main | 0.535 / **5.609** / 0 |
| 上周的 OOM 事故 | oom | 0.504 / 3.237 / 0 |
| 编辑器 | editor | 0 / **经同义词扩展命中** / 0 |

**Top1 = 5/5**；两个独立子进程读取**结果与分数完全一致**（真实子进程验证，非同进程模拟）。
可以看到向量信号弱时由 BM25 / 实体补位——这正是单路检索做不到的。

### 4.3 协议与鉴权

| 检查 | 结果 |
|------|------|
| 声明协议版本 | `2026-07-28`（当前最新） |
| 同时兼容 | `2025-11-25 / 2025-06-18 / 2025-03-26 / 2024-11-05` |
| `server/discover` | ✔ 实现（真实子进程 stdio 验证） |
| 不支持的版本 | ✔ 返回 `UnsupportedProtocolVersionError` |
| 声明了弃用能力（roots/sampling/logging） | ✔ **未声明** |
| 工具风险注解 | ✔ 29/29 |
| RFC 9728 元数据端点 | ✔ 实测 HTTP 200，且免鉴权可达 |
| RFC 8707 aud 绑定 | ✔ 错误 aud / 空 aud 被拒 |
| RFC 9207 iss 校验 | ✔ 不匹配与缺失均被拒 |
| CIMD | ✔ 强制 https、client_id 必须与文档内一致 |

### 4.4 代理技能库 + 证据门禁（本轮新增，借鉴 obra/superpowers）

**技能库（渐进式披露）**——Discovery 阶段只读 `frontmatter` + 文件字节估算 token，**不读正文**，
据此决定是否激活，避免一次性把全部技能塞进上下文：

| 指标 | 结果 |
|------|------|
| 技能总数 / 有效 / 损坏 | **4 / 4 / 0** |
| 技能名 | `registry-first` / `systematic-debugging` / `verification-before-completion` / `writing-plans` |
| 全部激活预估 token | 6,088 |
| 溯源 | derived from obra/superpowers (MIT) |

**EvidenceGate（把"证据优于声明"变成可拦截约束）**——任务声称完成前，必须提交真实证据
（命令 + 退出码 + 输出 + 新鲜度 TTL）；证据不足时**任务状态真的不变**。实测 5 个探针：

| 探针 | 期望 | 结果 |
|------|------|------|
| 空口声称（"改好了" + Done!） | 拦 | ✔ 拦（command 不像可执行命令） |
| 退出码非 0（pytest 报 FAILED） | 拦 | ✔ 拦 |
| 证据过期（9999s > ttl 300s） | 拦 | ✔ 拦（"上次跑通≠这次跑通"） |
| 措辞含 should（"should pass now"） | 拦 | ✔ 拦（置信度不是证据） |
| 合法证据（真跑 299 测试全过） | 放 | ✔ 放（任务状态 → completed） |

**block_rate = 4/4 = 1.0**。这关掉的是 Agent 自检里最阴险的一类失效：
"我以为改好了"——没有这一道闸，模型很容易把一句 `Done!` 当成真完成了。


---

## 五、安全姿态总表（穿透测试，全部仍被挡住）

| 攻击面 | 尝试 | 结果 |
|--------|------|------|
| 审批闸门 | 未批准直接 `execute_scan` | ✔ 拦截 |
| 审批闸门 | 伪造 `approval_id` | ✔ 拦截 |
| 审批闸门 | 拒绝后仍执行 | ✔ 拦截 |
| 沙箱越权 | 写 jail 外文件 | ✔ 拦截 |
| API 鉴权 | 无令牌 / 有效令牌 | ✔ 前者拒、后者放行 |
| 记忆库 | 是否存有密钥原文形态 | ✔ 无 |
| 源码自查 | 项目自身是否硬编码真实凭证 | ✔ clean（排除规则库 / 语料样本后，无 high/critical） |
| **新增** | 扫描报告是否泄露凭证原文 | ✔ 全链路脱敏，SARIF 内亦无明文（有测试固化） |

---

## 六、本轮**新发现并修复**的 6 个真实缺陷

> v1 修了 3 个；本轮改造过程中又实测发现 6 个。全部已修并补了回归测试。

### 6.1 🔴【严重】`.env` / `.github` 被整类跳过 —— 漏掉凭证泄露最高频的位置

**现象**：`scan_path()` 对样例仓库返回 **0 条发现**。
**根因**：遍历用 `fn.startswith(".")` 过滤文件、`d.startswith(".")` 过滤目录，
于是 `.env` 与 `.github/workflows/` 全部落空。**而 `.env` 是凭证泄露最高频的单一文件**
（gitleaks / TruffleHog 都将其列为首要目标）。这与 v1 修过的 `basename('.')=='.'` 属
**同一类错误**——说明"用点开头判断隐藏"这个启发式本身就是错的。

**修复**：改为显式 `_SKIP_DIRS` 白名单（只跳 `.git/.hg/.svn`、缓存目录、依赖目录）；
`.github` / `.gitlab` / `.circleci` 等配置目录保留；点文件照扫。
**效果**：样例仓库 0 条 → **6 条**（含 `.env` 的 JWT、`.github/workflows/ci.yml` 的供应链风险）。

### 6.2 🔴【严重】抑制词对整行生效，真实凭证被误抑（假阴性）

**现象**：`postgres://admin:Sup3rS3cret@db.internal.example:5432` 不报。
**根因**：`hit_stopword(secret) or hit_stopword("", line)` 会用整行找抑制词，
而**域名里的 `example`** 命中了抑制词 → 真实连接串被判为示例值。
**教训**：假阴性比误报危险得多——误报顶多烦人，漏报等于没扫。
**修复**：含 `://` 的凭证不做抑制词判定（形态本身足够独特）；其余只看 secret 本身；
仅当整行是注释（`#` / `//` 开头）时才用整行判定。

### 6.3 🟡 去重失效：指纹含 rule_id，同一凭证被两条规则命中就报两次

`AIza…` 同时命中 `gcp-api-key` 与 `gemini-api-key` → 两个不同指纹 → 告警翻倍。
修复：去重键改为 **(secret, path)**；fingerprint 仍含 rule_id 以便基线精确定位。

### 6.4 🟡 记忆衰减失效：时间戳 `0.0` 被 `or` 链当成假值

```python
ref = meta.get("last_access") or meta.get("updated_at") or meta.get("created_at") or now
```
`created_at=0.0` 是合法时间戳却是 falsy，一路 `or` 到 `now`，于是"1970 年的记忆"
被判定为"刚写的"，衰减彻底失效。改为显式 `is None` 判定。

### 6.5 🟡 行内 `scanner:allow` 承诺未落地

抑制逻辑写了，但 `scan_path` 没把原文行传给抑制层，导致行内注释**完全无效**（承诺落空）。
修复：扫描时缓存原文行并传入 `apply_suppressions`。

### 6.6 🟡 基线首次运行即红灯，违背基线模式初衷

首次建基线时先 diff 后写，导致 `new_findings == 全部发现`，第一次跑就失败。
而基线模式的全部意义就是"**让存量不阻塞，只拦新增**"——否则团队第一反应是关掉扫描。
修复：基线文件不存在时视为初始化，`new_findings=[]`。

---

## 七、与市面同类的差距（2026-10 当下事实，更新版）

| 对手 | 它的量级 | 本项目现状 | 差距性质 |
|------|---------|-----------|---------|
| **TruffleHog** | 800+ 检测器、**实网活体验证**、`verified:true` | 78 条检测器 + 3 个验证器，**默认离线** | 深度仍差约 1 个数量级；活体验证需 `LIVE_VALIDATION=1` 且未用真实凭据实测 |
| **gitleaks** | 默认规则集 200+ 形态、Go 实现、极快 | 78 条、Python、6 文件 3.3 ms | 规则量差约 2.5 倍；性能同量级（中小仓库够用） |
| **Semgrep** | AST 级、跨文件污点分析、900+ Pro 规则、AI 降噪 | 有适配器，**未安装时回退正则** | 未装 Semgrep → AST 级深度**本机无法实测** |
| **Mem0 v2/v3** | 真 embedding、LoCoMo 91.6 / LongMemEval 93.4 | 离线 n-gram + BM25 + 实体，自造语料 Top1 5/5 | 结构对齐了，**语义能力未对齐**（无公开基准分数） |
| **MCP 生态** | 2026-07-28 为当前版；Registry 23,700+ server | **已对齐 2026-07-28** | 规范层面已补齐；**仍未真上架** |

---

## 八、评分卡（v3）

| 维度 | 权重 | 得分 | 说明 |
|------|------|------|------|
| 工程严谨度 | 20% | 9.0 | WAL / 连接隔离 / 备份一致性实测成立；零依赖为真 |
| 安全叙事可信度 | 20% | 9.5 | 穿透 3/3 挡住 + 证据门禁使"Done!"无法凭空成立；报告全链路脱敏 |
| 可信执行（技能+门禁） | 10% | 9.0 | 4 技能库渐进式披露 + EvidenceGate 拦 4/4 虚假声明 |
| 性能 | 15% | 8.0 | 写 ~965 TPS / 读 ~213k TPS；无跨机压测 |
| 扫描能力 | 15% | 8.0 | 78 规则 + 熵 + 运营化 + SARIF；与 TruffleHog 仍有数量级差距 |
| 记忆能力 | 10% | 8.0 | 三路融合 + 衰减 + 分层；非真 embedding |
| 规范与生态时效 | 10% | 9.0 | 对齐 2026-07-28 与 OAuth 2.1；未上架 |
| **加权综合** | | **8.6** | v1 为 7.2，v2 为 8.4 |

---

## 九、仍然诚实标注的不足（未解决，不是客套）

1. **自造语料 ≠ 公开基准**。扫描 F1=1.0、记忆 Top1=5/5 都在**本仓库自造的小语料**上测得，
   不能外推为"优于 TruffleHog / Mem0"。要真对齐需接入 Detect-Test-Secrets / LoCoMo。
2. **语义仍是近似的**。BM25 + 同义词表是**规则式扩展**，救得了"编辑器→editor"，
   救不了"那个跑得很慢的批处理任务"。真语义需接 `EmbeddingProvider`（接口已留好）。
3. **Semgrep 未安装** → AST 级、跨文件污点分析在本机**零实测**，只有适配器代码。
4. **活体验证默认离线**。验证器均未用真实凭据实跑过。
5. **未真上架** PyPI / MCP Registry；OAuth 元数据端点可用，但**没有对接真实授权服务器**
   （`authorization_servers` 默认为空，需配置 `OAUTH_AUTHORIZATION_SERVERS`）。
6. **真机渠道未实跑**：企业微信 / 飞书仍是 mock + 加解密单测，未用真实凭据走 HTTPS 回调。
7. **无跨机压测**：所有性能数字在同机 SQLite 语义下有效，分布式队列（Redis）未实测。
8. **证据门禁默认不开真跑**：`EvidenceGate` 的 `verify()` 始终在线（拦空口声明），
   但 `allow_run=True`（真跑命令）默认关闭，需显式开启；真跑命令受 `RUN_ALLOWLIST` 约束
   （仅 `python` 及绝对路径解释器），避免任意命令执行。门禁本质是「用测试结果当证据」，
   属于**自指式证明**——它能挡住"我以为改好了"，但挡不住测试本身写错。
9. **技能库是 superpowers 的"派生实现"而非原仓库**：已注明 `derived from obra/superpowers (MIT)`，
   4 个技能是我按它的写作规范重写的本地版，不是直接 git 拉取的子模块；未纳入其上游测试集。

---

## 十、一句话结语

> v1 的结语是「**在广度与深度上坦诚地留白**」。
> v2 把留白补上了大半：**扫描从 5 条正则到 78 条结构化检测器 + 熵引擎 + 运营化 + SARIF，
> 记忆从单路 n-gram 到三路融合 + 时间维度 + 分层作用域，
> 协议从落后 3 个版本到对齐 2026-07-28 与 OAuth 2.1**。
> v3 又补了一层**可信执行**：从 obra/superpowers 借鉴「证据优于声明」，
> 落地为 4 个渐进式披露技能 + 一道拦 4/4 虚假声明的 EvidenceGate 硬门禁——
> 让"我以为改好了"不再能凭一句 `Done!` 蒙混过关。
> 但它依然诚实地停在"**自造语料、未上架、未真机联调**"这三道门槛前——
> 这三件事都**不是代码问题**，而是需要真实凭据、真实流量与真实发布流程才能跨过去。
