"""
OAuth 2.1 鉴权（零依赖默认实现）

提供产品化所需的最小鉴权能力，契合 OAuth 2.1 建议：
- 优先 Bearer 令牌（access token）；
- 授权码模式需 PKCE（此处给出说明与校验钩子，真实发放由 AS 完成）；
- 客户端凭据（client_credentials）用于机器对机器（M2M，如 runner 远程连 API）；
- 默认拒绝未携带/无效令牌；令牌可为静态列表（本地/内网），也可走 introspection 端点（对接真实 AS）。

为保持零硬依赖，JWT 的签发/验签用标准库 HMAC-SHA256 自实现（HS256），
不依赖 PyJWT；如需 RS256/ES256 等非对称算法，可接入 cryptography（可选 extras）。

⚠️ 生产环境务必：
  - 用 HTTPS 传输（本 API 仅负责校验，传输层 TLS 由反向代理/网关提供）；
  - 静态令牌仅用于本地/内网，公网请用真实授权服务器 + 短期令牌 + 刷新；
  - 详见仓库 OAuth_ROADMAP.md。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
import urllib.request
from typing import Dict, List, Optional


# ---------------- base64url ----------------
def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


# ---------------- 最小 JWT（HS256） ----------------
def issue_token(client_id: str, secret: str, scopes: Optional[List[str]] = None,
                expires_in: int = 3600, extra: Optional[Dict] = None) -> str:
    """签发 HS256 JWT（本地开发 / M2M 短期令牌）。"""
    header = {"alg": "HS256", "typ": "JWT"}
    now = int(time.time())
    payload = {
        "iss": "ai-colleague",
        "sub": client_id,
        "iat": now,
        "exp": now + expires_in,
        "scope": " ".join(scopes or ["*"]),
    }
    if extra:
        payload.update(extra)
    signing_input = _b64url_encode(json.dumps(header, separators=(",", ":")).encode()) \
        + "." + _b64url_encode(json.dumps(payload, separators=(",", ":")).encode())
    sig = hmac.new(secret.encode("utf-8"), signing_input.encode("utf-8"), hashlib.sha256).digest()
    return signing_input + "." + _b64url_encode(sig)


def verify_token(token: str, secret: str,
                 allowed_scopes: Optional[List[str]] = None) -> Optional[Dict]:
    """验签 HS256 JWT；返回 payload 或 None（失效/签名错/过期/越权）。"""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        signing_input = parts[0] + "." + parts[1]
        sig = _b64url_decode(parts[2])
        expected = hmac.new(secret.encode("utf-8"), signing_input.encode("utf-8"),
                            hashlib.sha256).digest()
        if not hmac.compare_digest(sig, expected):
            return None
        payload = json.loads(_b64url_decode(parts[1]))
        if payload.get("exp", 0) < int(time.time()):
            return None
        if allowed_scopes:
            sc = set(payload.get("scope", "").split())
            if not (sc & set(allowed_scopes) or "*" in sc):
                return None
        return payload
    except Exception:  # noqa: BLE001
        return None


# ---------------- 令牌校验器 ----------------
class TokenValidator:
    """Bearer 令牌校验：静态令牌列表 + 可选 introspection 端点（对接真实 AS）。"""

    def __init__(self, static_tokens: Optional[List[str]] = None,
                 static_secret: Optional[str] = None,  # 用于校验本服务签发的 JWT
                 introspection_url: Optional[str] = None,
                 jwt_secret: Optional[str] = None):
        # 静态令牌：默认可从环境变量 API_TOKENS（逗号分隔）读取
        env_tokens = os.getenv("API_TOKENS", "")
        self.static_tokens = set(static_tokens or [])
        if env_tokens:
            self.static_tokens.update(t.strip() for t in env_tokens.split(",") if t.strip())
        self.introspection_url = introspection_url or os.getenv("OAUTH_INTROSPECTION_URL")
        self.jwt_secret = jwt_secret or os.getenv("JWT_SECRET")

    def validate(self, token: Optional[str]) -> Dict:
        """返回 {active, client, scopes}。active=False 表示拒绝。"""
        if not token:
            return {"active": False, "client": None, "scopes": []}
        # 1) 静态令牌
        if token in self.static_tokens:
            return {"active": True, "client": "static", "scopes": ["*"]}
        # 2) 本服务签发的 JWT
        if self.jwt_secret:
            payload = verify_token(token, self.jwt_secret)
            if payload:
                return {"active": True, "client": payload.get("sub"),
                        "scopes": payload.get("scope", "").split()}
        # 3) 远程 introspection（对接真实授权服务器）
        if self.introspection_url:
            remote = self._introspect(token)
            if remote:
                return remote
        return {"active": False, "client": None, "scopes": []}

    def _introspect(self, token: str) -> Optional[Dict]:
        try:
            req = urllib.request.Request(
                self.introspection_url,  # noqa: S310 (受信 AS)
                data=json.dumps({"token": token}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=5) as r:  # noqa: S310
                data = json.loads(r.read())
            if data.get("active"):
                return {"active": True, "client": data.get("sub"),
                        "scopes": (data.get("scope") or "").split()}
        except Exception:  # noqa: BLE001
            return None
        return None


def extract_bearer(authorization: Optional[str]) -> Optional[str]:
    if not authorization:
        return None
    parts = authorization.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return None


# ---------------- 客户端凭据交换（M2M） ----------------
def exchange_client_credentials(token_url: str, client_id: str, client_secret: str,
                                scope: str = "") -> Dict:
    """OAuth 2.1 client_credentials 授权：向 AS 换取 access_token。
    仅封装标准请求；网络调用由调用方环境决定，本函数不联网自测。"""
    body = {"grant_type": "client_credentials"}
    if scope:
        body["scope"] = scope
    req = urllib.request.Request(
        token_url,  # noqa: S310 (受信 AS)
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Basic " + _b64url_encode(
                f"{client_id}:{client_secret}".encode()
            ).replace("-", "+").replace("_", "/"),  # Basic 用标准 base64
        },
    )
    with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310
        return json.loads(r.read())


# ---------------- PKCE 辅助（授权码模式） ----------------
def generate_pkce_pair() -> Dict[str, str]:
    """生成 PKCE code_verifier / code_challenge（S256）。"""
    verifier = _b64url_encode(os.urandom(32)).replace("-", "_")
    challenge = _b64url_encode(hashlib.sha256(verifier.encode("utf-8")).digest())
    return {"code_verifier": verifier, "code_challenge": challenge,
            "code_challenge_method": "S256"}


# ================================================================
# MCP 2026-07-28：服务器即 OAuth 2.1 资源服务器
# ----------------------------------------------------------------
# 2026-07-28 是自 MCP 立项以来最大的一次修订，其中与鉴权直接相关的是把"MCP 服务器
# 是 OAuth 2.1 资源服务器"从建议变成要求，并引入三份配套 RFC：
#   · RFC 9728 —— Protected Resource Metadata：客户端要能**自动发现**该服务器的授权服务器，
#     要么读 .well-known/oauth-protected-resource，要么读 WWW-Authenticate 里的
#     resource_metadata。这是"无状态 + 零配置接入"的前提。
#   · RFC 8707 —— Resource Indicators：令牌必须绑定具体受众（aud=本服务器 URI）。
#     没有它，发给 A 服务器的令牌可以被重放到 B 服务器（跨服务重放）。
#   · RFC 9207 —— Issuer Identification：授权响应带 iss，客户端必须校验，
#     防止把授权码送到伪造的 AS。
# 另：DCR（RFC 7591 动态注册）被**弃用**，推荐 CIMD（Client ID Metadata Documents）。
# ================================================================

def resource_url(default: str = "") -> str:
    """本 MCP 服务器的规范资源 URI（RFC 8707 的 resource / RFC 9728 的 resource）。

    令牌的 aud 必须等于它；客户端请求令牌时必须带上 resource 参数。
    """
    return (os.getenv("MCP_RESOURCE_URL") or default
            or os.getenv("API_BASE") or "http://localhost:8080")


def protected_resource_metadata(resource: str = "",
                                authorization_servers: Optional[List[str]] = None,
                                scopes_supported: Optional[List[str]] = None) -> Dict:
    """RFC 9728 Protected Resource Metadata 文档。

    应挂在 GET /.well-known/oauth-protected-resource（HTTP 传输下必须）。
    """
    res = resource or resource_url()
    if authorization_servers is None:
        env = os.getenv("OAUTH_AUTHORIZATION_SERVERS", "")
        authorization_servers = [s.strip() for s in env.split(",") if s.strip()]
    return {
        "resource": res,
        "authorization_servers": authorization_servers or [],
        "scopes_supported": scopes_supported or ["registry.read", "registry.write",
                                                 "scan.execute", "memory.read",
                                                 "memory.write"],
        "bearer_methods_supported": ["header"],
        "resource_documentation": res,
    }


def www_authenticate(resource: str = "", error: str = "invalid_token",
                     description: str = "") -> str:
    """构造 401 的 WWW-Authenticate 头（RFC 9728 + RFC 6750）。

    形如：
        WWW-Authenticate: Bearer resource_metadata="https://api/.well-known/oauth-protected-resource",
                                 error="invalid_token", error_description="..."
    2026-07-28 起，头里的 resource_metadata 可以**替代** .well-known 端点。
    """
    res = resource or resource_url()
    meta = res.rstrip("/") + "/.well-known/oauth-protected-resource"
    parts = [f'Bearer resource_metadata="{meta}"']
    if error:
        parts.append(f'error="{error}"')
    if description:
        parts.append(f'error_description="{description.replace(chr(34), chr(39))}"')
    return ", ".join(parts)


def validate_resource_indicator(payload: Dict, resource: str = "") -> bool:
    """RFC 8707：校验令牌的 aud 是否绑定本资源服务器。

    aud 可为字符串或数组（多个受众）。任一匹配即通过。
    这是防止"发给别的 MCP 服务器的令牌被重放到本服务器"的关键闸门。
    """
    res = resource or resource_url()
    aud = payload.get("aud")
    if aud is None:
        return False
    if isinstance(aud, str):
        return aud == res
    if isinstance(aud, (list, tuple)):
        return res in aud
    return False


def verify_iss(iss: Optional[str], expected_iss: Optional[str] = None) -> bool:
    """RFC 9207：校验授权服务器标识 iss。

    规范措辞是"若出现则必须校验"；本实现按**最严**处理：未提供 expected_iss 时
    从环境变量 OAUTH_ISSUER 读取，若两者都没配置则返回 False（拒绝静默放行）。
    """
    if not iss:
        return False
    expected = expected_iss or os.getenv("OAUTH_ISSUER") or ""
    if not expected:
        return False
    return hmac.compare_digest(str(iss), expected)


# ---------------- Client ID Metadata Documents（CIMD，替代 DCR） ----------------

class ClientIdMetadataDocument:
    """CIMD 客户端：client_id 本身是一个 HTTPS URL，指向可获取的客户端元数据文档。

    与 DCR 的差别：DCR 要求 AS 提供"注册写接口"（因此成为滥用与 DoS 面），
    CIMD 只读——客户端自己托管一份 JSON，AS/RS 拉取校验。2026-07-28 起为**推荐**路径。
    """

    REQUIRED_FIELDS = ("client_id", "client_name")
    ALLOWED_SCHEMES = ("https",)

    def __init__(self, timeout: float = 8.0):
        self.timeout = timeout

    @classmethod
    def is_cimd(cls, client_id: str) -> bool:
        return (client_id or "").lower().startswith(cls.ALLOWED_SCHEMES)

    def fetch(self, client_id: str) -> Dict:
        """拉取并校验客户端元数据文档。返回 {ok, document?, error?}。

        安全约束（与规范一致）：
          · 必须 https（http 仅在本机回环调试且 CIMD_ALLOW_INSECURE=1 时放行）；
          · 文档里的 client_id 必须与请求的 URL 完全相同（防止文档被托管到别处冒用）。
        """
        if not self.is_cimd(client_id):
            return {"ok": False, "error": "client_id 不是 CIMD URL 形态"}
        allow_insecure = os.getenv("CIMD_ALLOW_INSECURE") == "1"
        if not client_id.lower().startswith("https://") and not allow_insecure:
            return {"ok": False, "error": "CIMD 必须使用 https"}
        try:
            req = urllib.request.Request(
                client_id, headers={"Accept": "application/json"},  # noqa: S310
            )
            with urllib.request.urlopen(req, timeout=self.timeout) as r:  # noqa: S310
                doc = json.loads(r.read())
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"拉取失败: {str(e)[:200]}"}
        if not isinstance(doc, dict):
            return {"ok": False, "error": "元数据文档不是 JSON 对象"}
        for f in self.REQUIRED_FIELDS:
            if not doc.get(f):
                return {"ok": False, "error": f"缺少字段 {f}"}
        if doc.get("client_id") != client_id:
            return {"ok": False, "error": "文档内 client_id 与 URL 不一致"}
        return {"ok": True, "document": doc}

    @staticmethod
    def validate_redirect(doc: Dict, redirect_uri: str) -> bool:
        uris = doc.get("redirect_uris") or []
        return redirect_uri in uris


# ---------------- 资源服务器模式的令牌校验 ----------------

class ResourceServerValidator(TokenValidator):
    """OAuth 2.1 资源服务器口径的令牌校验器（在基类之上加 RFC 8707 受众绑定）。

    基类只回答"令牌是否有效"；这里多回答一层"这个令牌是不是发给**我**的"。
    开启严格模式（默认）时，缺 aud 或 aud 不匹配的令牌一律拒绝。
    """

    def __init__(self, *args, resource: str = "", require_resource: bool = True,
                 **kwargs):
        super().__init__(*args, **kwargs)
        self.resource = resource or resource_url()
        self.require_resource = require_resource

    def validate(self, token: Optional[str]) -> Dict:
        base = super().validate(token)
        if not base.get("active"):
            return base
        if not self.require_resource:
            return base
        # 令牌形态：JWT（可解析出 aud）或静态令牌（无 aud → 按配置拒绝或放行本地）
        payload = None
        if self.jwt_secret:
            payload = verify_token(token, self.jwt_secret)
        if payload is None and token and "." in token:
            # 不验签地读 payload 只为取 aud；有效性已由基类判定过
            try:
                payload = json.loads(_b64url_decode(token.split(".")[1]))
            except Exception:  # noqa: BLE001
                payload = None
        if payload is None:
            # 静态令牌没有 aud 概念：本地/内网允许（RESOURCE_STRICT=0 可关闭）
            if os.getenv("RESOURCE_STRICT", "1") == "1":
                return {"active": False, "client": None, "scopes": [],
                        "error": "静态令牌无 aud，无法满足 RFC 8707 受众绑定",
                        "hint": "改用 JWT 签发（带 aud）或设置 RESOURCE_STRICT=0"}
            return base
        if not validate_resource_indicator(payload, self.resource):
            return {"active": False, "client": None, "scopes": [],
                    "error": "aud 与本资源服务器不匹配（RFC 8707）"}
        return base


if __name__ == "__main__":
    secret = "test-secret"
    tok = issue_token("runner-1", secret, scopes=["scan", "memory"], expires_in=60)
    print("issued:", tok[:20], "...")
    v = verify_token(tok, secret, allowed_scopes=["scan"])
    print("verify with scope scan:", v is not None)
    v2 = verify_token(tok, secret, allowed_scopes=["admin"])
    print("verify with scope admin (应 None):", v2)
    tv = TokenValidator(static_tokens=["abc123"], jwt_secret=secret)
    print("static:", tv.validate("abc123"))
    print("jwt:", tv.validate(tok))
    print("bad:", tv.validate("nope"))
