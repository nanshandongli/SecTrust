# OAuth 2.1 鉴权路线图

> **2026-10-23 更新**：MCP 2026-07-28 已把"MCP 服务器 = OAuth 2.1 资源服务器"从建议变为要求。
> 本仓库已完成**资源服务器侧**的全部落地项（RFC 9728 / 8707 / 9207 + CIMD），
> 详见下方「资源服务器（已完成）」。下表中"对接真实授权服务器"仍是**未完成项**——
> 那需要真实的 AS 凭据，不是代码问题。

## 资源服务器（已完成，2026-07-28 对齐）

| 规范 | 落地 | 位置 |
|------|------|------|
| **RFC 9728** Protected Resource Metadata | `GET /.well-known/oauth-protected-resource`（免鉴权）+ 401 响应带 `WWW-Authenticate: Bearer resource_metadata="…"` | `api/app.py` 路由、`security/oauth.py` |
| **RFC 8707** Resource Indicators | 令牌 `aud` 必须等于本服务器 URI，防跨服务重放 | `validate_resource_indicator` / `ResourceServerValidator` |
| **RFC 9207** Issuer Identification | 授权响应 `iss` 校验 | `verify_iss` |
| **CIMD**（替代已弃用的 DCR） | client_id 即 https URL，强制 https + 文档内 client_id 自证 | `ClientIdMetadataDocument` |
| MCP-Protocol-Version 头 | 每个 HTTP 响应带当前协议版本 | `api/app.py` `_send` |

配置：`MCP_RESOURCE_URL` 或 `OAUTH_AUTHORIZATION_SERVERS` 任一被设置即进入**严格模式**
（要求 aud 绑定）；未设置则维持本地宽松模式，避免升级打断既有部署。

---

本平台当前已实现**零依赖的 Bearer 令牌鉴权**（`security/oauth.py`）：

- 静态令牌列表（环境变量 `API_TOKENS`，适合本地/内网 M2M）；
- 自签发的 HS256 JWT（`issue_token` / `verify_token`，标准库 HMAC，无需 PyJWT）；
- 可选的远程 introspection 端点（对接真实授权服务器）；
- 客户端凭据交换（`exchange_client_credentials`）与 PKCE 辅助（`generate_pkce_pair`）。

传输层 TLS 由反向代理 / 网关负责（本服务只做令牌校验与业务处理）。

## 产品化所需的后续步骤（公网 / 多租户场景）

| 阶段 | 内容 | 说明 |
|------|------|------|
| 1. 传输安全 | 前置 HTTPS / mTLS | 用 Nginx / Caddy / API 网关终结 TLS，禁止明文暴露 8080 |
| 2. 授权服务器 | 对接真实 AS（Auth0 / Keycloak / 自建） | 用 `exchange_client_credentials` 拿短期令牌；`OAUTH_INTROSPECTION_URL` 做在线校验 |
| 3. 授权码 + PKCE | 前端/用户登录 | 用 `generate_pkce_pair()` 生成 verifier/challenge，回调换 code→token |
| 4. 令牌生命周期 | 短期 access + refresh | `issue_token(expires_in=900)`；提供 `/token/refresh` 端点 |
| 5.  scopes / RBAC | 细粒度授权 | 令牌携带 scope（scan/memory/admin…），`verify_token(allowed_scopes=...)` 校验 |
| 6. 多租户隔离 | tenant_id + 行级隔离 | 在 task_registry / memory 表加 tenant 维度，按令牌 tenant 过滤 |
| 7. 审计 | 所有鉴权事件入日志 | `security/observability.py` 的 `log_event` 已结构化输出，可接 SIEM |
| 8. 密钥管理 | 不落明文 | JWT 签名密钥 / 静态令牌走 KMS / Vault，禁止写进仓库 |

## 当前已交付（可直接用）

- `python -m security.oauth` 演示 JWT 签发/验签、静态令牌校验。
- `api/app.py` 的 `require_auth`：设置 `API_AUTH_ENABLED=1` + `API_TOKENS=token1,token2` 即开启 Bearer 鉴权；客户端用 `ApiClient(base, token=...)`。
- `security/observability.py`：指标 + 结构化日志 + 阈值告警（OTel 可选启用）。

> 设计原则：默认拒绝、密钥不硬编码、零硬依赖。公网部署前**必须**完成阶段 1–4。
