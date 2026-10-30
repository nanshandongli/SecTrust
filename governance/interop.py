"""
interop.py —— 互联互通（GB/Z 185—2026《人工智能 智能体互联》精神）

把平台内每个"AI 同事"（agent 资产）描述成结构化、可被发现与对接的**智能体
描述符**，覆盖标准规范的五个维度：
  身份识别（identity）/ 能力描述（capability）/ 发现匹配（discovery）/
  交互协作（interaction）/ 工具调用（tool_calls）。

目的：解决"跨厂商、跨平台智能体可信管理难"——即便当前是单平台，描述符也
让每个智能体的能力、身份与可调用的工具一目了然、可被程序化匹配。
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import urllib.error
import urllib.request
from typing import Dict, List, Optional

# 身份方案：复用 security.oauth 的本地 JWT（aud 绑定 + 验签）
IDENTITY_SCHEME = "local-jwt-hs256"
DISCOVERY_PROTOCOL = "http+json"
INTERACTION_PROTOCOL = "task-delegation"


def agent_descriptor(asset: Dict) -> Dict:
    """由资产清册里的一条 agent 资产，生成符合互联规范精神的描述符。

    asset 来自 asset_registry.AssetRegistry.list(kind='agent')。
    """
    meta = asset.get("meta") or {}
    skills = meta.get("skills") or []
    return {
        "agent_id": asset["id"],
        "display_name": asset.get("name"),
        "schema_version": "gbz185-2026.draft",
        "identity": {
            "scheme": IDENTITY_SCHEME,
            "issuer": "ai-colleague",        # oauth.issue_token 的 iss
            "audience_bound": True,           # RFC 8707 受众绑定
            "revocable": True,                # SessionStore 黑名单
        },
        "capability": {
            "description": meta.get("desc") or asset.get("name"),
            "skills": skills,
            "backed_model": meta.get("model"),
        },
        "discovery": {
            "protocol": DISCOVERY_PROTOCOL,
            "endpoint": "/api/governance/interop",   # 本描述符的发布端点
            "auth": "bearer-jwt",
            "matchable": True,                        # 支持按能力匹配
        },
        "interaction": {
            "protocol": INTERACTION_PROTOCOL,
            "message_format": "json",
            "delegation": "plan-service",             # 由编排层派发
        },
        "tool_calls": [
            {"tool": "skill:" + s, "via": "skill_engine"} for s in skills
        ] + [
            {"tool": "llm.generate", "via": "model_providers"},
            {"tool": "web_search", "via": "execution.web_search"},
        ],
        "trust": {
            "level": asset.get("classification", "internal"),
            "basis": "本地签发的可校验身份 + 角色权限（RBAC）",
        },
    }


def all_descriptors(project_root: str = ".") -> List[Dict]:
    """发现并产出所有 agent 的描述符（幂等地先 discover 再生成）。"""
    from . import asset_registry
    asset_registry.discover(project_root=project_root)
    agents = asset_registry.list_assets(kind="agent")
    return [agent_descriptor(a) for a in agents]


def match_by_capability(requirement: str, project_root: str = ".") -> List[Dict]:
    """按能力关键词匹配可用智能体（发现匹配维度的最小实现）。"""
    req = (requirement or "").lower()
    out = []
    for d in all_descriptors(project_root=project_root):
        hay = " ".join([
            d["display_name"] or "",
            d["capability"]["description"] or "",
            " ".join(d["capability"]["skills"]),
        ]).lower()
        if req in hay:
            out.append(d)
    return out


# ==========================================================================
# A3 · 跨平台真实互联（联邦）
#
# 与阶段 B 的 MCP 适配器区分：这里对接的是**另一个 AI 同事平台**（对端也发布
# /api/governance/interop 描述符，按 task-delegation 交互）；MCP 对接的是工具/数据源。
#
# 诚实边界（必须遵守）：
#   · 真实跨厂商互联需要对端在线并部署同规范端点；离线测试用本地 http.server
#     fixture 返回合法描述符，federated_via 明确标注 mock_fixture(loopback)，
#     **不冒充真跨厂商**。
#   · 网络失败 / schema 不合法 / 超时一律 ok=False + error，绝不伪造描述符或结果。
#   · Registry 只存 token **引用**（token_ref），绝不落盘明文凭证；调用时由上层
#     显式传 token（凭证隔离，Agent 拿不到）。
# ==========================================================================

DEFAULT_FEDERATION_PATH = os.path.join("data", "interop_federation.json")
TRUST_LEVELS = ("untrusted", "verified", "federated")
DISCOVERY_ENDPOINT = "/api/governance/interop"
DELEGATION_ENDPOINT = "/api/governance/interop/delegate"


def _loopback(url: str) -> bool:
    """是否本机回环地址（用于把离线 fixture 与真跨厂商诚实区分）。"""
    try:
        from urllib.parse import urlparse
        host = (urlparse(url).hostname or "").lower()
    except Exception:  # noqa: BLE001
        return False
    return host in ("127.0.0.1", "localhost", "::1")


def _via(url: str) -> str:
    return "mock_fixture(loopback)" if _loopback(url) else "http+json(remote)"


def _opener(url: str):
    """回环直连（绕开本机代理，否则 127.0.0.1 会被代理吞掉）；远端走环境代理。"""
    if _loopback(url):
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener()


def _blank_federation() -> Dict:
    return {"peers": {}, "discovered": {}, "audits": [], "updated_at": None}


class FederationRegistry:
    """远程平台登记 + 发现缓存 + 委派审计（进程无关，落盘共享）。

    安全：peers 只存 token_ref（如 "env:PEER_TOKEN" / "vault:xxx"），
    不存明文 token；discover_remote / delegate 的 token 由调用方显式传入。
    """

    def __init__(self, path: str = DEFAULT_FEDERATION_PATH):
        self.path = path
        self._lock = threading.RLock()

    def _read(self) -> Dict:
        with self._lock:
            try:
                with open(self.path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
            except FileNotFoundError:
                return _blank_federation()
            except (OSError, ValueError) as e:
                raise RuntimeError("联邦清册损坏或不可读：%s" % e)
            if not isinstance(data, dict) or "peers" not in data:
                raise RuntimeError("联邦清册格式错误")
            data.setdefault("discovered", {})
            data.setdefault("audits", [])
            return data

    def _write(self, data: Dict) -> None:
        with self._lock:
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)

    # ---- 登记 ----
    def register(self, name: str, base_url: str, token_ref: str = "",
                 trust_level: str = "untrusted") -> Dict:
        if not name or not base_url:
            raise ValueError("联邦登记需要 name 与 base_url")
        if trust_level not in TRUST_LEVELS:
            raise ValueError("未知信任级别：%s（应为 %s）"
                             % (trust_level, "/".join(TRUST_LEVELS)))
        data = self._read()
        rec = {
            "name": name, "base_url": base_url.rstrip("/"),
            "token_ref": str(token_ref or ""),   # 仅引用，绝不存明文
            "trust_level": trust_level, "registered_at": time.time(),
        }
        data["peers"][name] = rec
        data["updated_at"] = time.time()
        self._write(data)
        return dict(rec)

    def unregister(self, name: str) -> bool:
        data = self._read()
        if name not in data["peers"]:
            return False
        del data["peers"][name]
        data["discovered"].pop(name, None)
        self._write(data)
        return True

    def list(self) -> List[Dict]:
        """公开视图：不含任何明文凭证（本来也只存引用）。"""
        return sorted(self._read()["peers"].values(), key=lambda r: r["name"])

    def get(self, name: str) -> Optional[Dict]:
        return self._read()["peers"].get(name)

    # ---- 发现缓存 ----
    def cache_discovery(self, name: str, descriptors: List[Dict]) -> None:
        data = self._read()
        data["discovered"][name] = {"descriptors": descriptors,
                                    "at": time.time(), "count": len(descriptors)}
        data["updated_at"] = time.time()
        self._write(data)

    def discovered(self) -> Dict:
        return dict(self._read().get("discovered", {}))

    # ---- 委派审计 ----
    def _append_audit(self, rec: Dict) -> None:
        try:
            data = self._read()
            audits = data.setdefault("audits", [])
            audits.append(rec)
            data["audits"] = audits[-100:]
            self._write(data)
        except Exception:  # noqa: BLE001 - 审计旁路不挡主流程（结果里仍带 audit）
            pass

    def audit_log(self, limit: int = 50) -> List[Dict]:
        return list(self._read().get("audits", []))[-limit:]

    def state(self) -> Dict:
        data = self._read()
        return {"peers": self.list(), "discovered": data.get("discovered", {}),
                "audits": data.get("audits", []), "path": self.path}


def _validate_descriptors(payload) -> tuple:
    """校验远程发现响应 schema。返回 (descriptors, error)。"""
    if not isinstance(payload, dict):
        return [], "远程响应不是 JSON 对象"
    ds = payload.get("descriptors")
    if not isinstance(ds, list):
        return [], "远程响应缺少 descriptors 列表"
    for d in ds:
        if not isinstance(d, dict) or not d.get("agent_id"):
            return [], "描述符 schema 不合法（缺 agent_id 或非对象）"
    return ds, None


def discover_remote(base_url: str, token: str = None, timeout: float = 10.0,
                    peer_name: str = None,
                    registry: FederationRegistry = None) -> Dict:
    """HTTP GET 远程 /api/governance/interop 拉取描述符。

    绝不伪造：失败（网络/超时/HTTP 错误/schema 不合法）一律 ok=False + error。
    成功时按 peer_name 写入发现缓存；federated_via 标注来源（loopback fixture
    明确标 mock_fixture，不宣称真跨厂商）。
    """
    base = (base_url or "").rstrip("/")
    out = {"ok": False, "peer": peer_name, "base_url": base,
           "descriptors": [], "count": 0, "federated_via": _via(base),
           "protocol": DISCOVERY_PROTOCOL, "error": None, "at": time.time()}
    if not base:
        out["error"] = "缺少 base_url"
        return out
    url = base + DISCOVERY_ENDPOINT
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    try:
        req = urllib.request.Request(url, headers=headers)
        with _opener(base).open(req, timeout=timeout) as r:
            raw = r.read()
        payload = json.loads(raw.decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, OSError,
            TimeoutError, ValueError) as e:
        out["error"] = "远程发现失败（%s）：%s" % (type(e).__name__, e)
        return out
    except Exception as e:  # noqa: BLE001 - 任何意外都诚实失败
        out["error"] = "远程发现异常：%s" % e
        return out
    ds, err = _validate_descriptors(payload)
    if err:
        out["error"] = err
        return out
    out["ok"] = True
    out["descriptors"] = ds
    out["count"] = len(ds)
    if peer_name and registry is not None:
        registry.cache_discovery(peer_name, ds)
    return out


def _params_digest(params) -> Dict:
    try:
        raw = json.dumps(params or {}, ensure_ascii=False, sort_keys=True,
                         default=str).encode("utf-8")
    except (TypeError, ValueError):
        raw = repr(params).encode("utf-8", "replace")
    return {"params_digest": hashlib.sha256(raw).hexdigest()[:16],
            "params_bytes": len(raw)}


def delegate(peer, capability: str, params: dict = None, token: str = None,
             timeout: float = 10.0,
             registry: FederationRegistry = None) -> Dict:
    """HTTP POST 远程 task-delegation 端点，单次 TTL 超时熔断 + 全量审计留痕。

    peer 可以是注册名（从 registry 查）或 {"base_url": ..., "name": ...} dict。
    返回 {ok, result, audit, error}；失败绝不伪造结果。
    """
    started = time.time()
    if isinstance(peer, dict):
        name = peer.get("name") or "ad-hoc"
        base = (peer.get("base_url") or "").rstrip("/")
    else:
        name = str(peer)
        rec = registry.get(name) if registry is not None else None
        base = (rec or {}).get("base_url", "")
    dig = _params_digest(params)
    audit = {
        "at": started, "server": base, "peer": name,
        "tool": capability, "protocol": INTERACTION_PROTOCOL,
        **dig,
        "duration_ms": 0, "cost_estimate": None,
        "cost_note": "无计费器接入，未估算成本（如实标注，不编造数字）",
        "ok": False, "error": None,
    }
    out = {"ok": False, "result": None, "audit": audit, "error": None}
    if not base:
        audit["error"] = out["error"] = "未知对端：%s（未登记且无 base_url）" % name
        return out
    url = base + DELEGATION_ENDPOINT
    body = json.dumps({"protocol": INTERACTION_PROTOCOL,
                       "capability": capability, "params": params or {}},
                      ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    try:
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        with _opener(base).open(req, timeout=timeout) as r:
            raw = r.read()
        payload = json.loads(raw.decode("utf-8"))
        out["ok"] = True
        out["result"] = payload
        audit["ok"] = True
    except (urllib.error.URLError, urllib.error.HTTPError, OSError,
            TimeoutError, ValueError) as e:
        audit["error"] = out["error"] = ("委派失败/超时熔断（%s，TTL=%.1fs）：%s"
                                        % (type(e).__name__, timeout, e))
    except Exception as e:  # noqa: BLE001
        audit["error"] = out["error"] = "委派异常：%s" % e
    finally:
        audit["duration_ms"] = int((time.time() - started) * 1000)
        if registry is not None:
            registry._append_audit(audit)
    return out


def _match_hay(requirement: str, d: Dict) -> bool:
    hay = " ".join([
        str(d.get("display_name") or ""),
        str((d.get("capability") or {}).get("description") or ""),
        " ".join((d.get("capability") or {}).get("skills") or []),
    ]).lower()
    return (requirement or "").lower() in hay


def match_federated(requirement: str, project_root: str = ".",
                    registry: FederationRegistry = None) -> Dict:
    """本地 match_by_capability + 联邦发现缓存合并去重。

    诚实：联邦结果只来自 **已成功 discover_remote 并缓存** 的对端；未刷新过
    的对端不编造描述符。返回 {local, federated, merged, federated_via, note}。
    """
    local = match_by_capability(requirement, project_root=project_root)
    reg = registry or FederationRegistry()
    federated = []
    for name, cache in reg.discovered().items():
        for d in cache.get("descriptors", []):
            if _match_hay(requirement, d):
                item = dict(d)
                item["_federated_peer"] = name
                federated.append(item)
    seen = {d.get("agent_id") for d in local}
    merged = list(local)
    for d in federated:
        if d.get("agent_id") not in seen:
            merged.append(d)
            seen.add(d.get("agent_id"))
    return {
        "local": local, "federated": federated, "merged": merged,
        "federated_via": "cache(discover_remote)" if federated else "none",
        "note": ("联邦结果仅来自已缓存的远程发现；真实跨厂商互联需对端在线并"
                 "部署同规范端点，离线 fixture 会标注 mock_fixture(loopback)。"),
    }
