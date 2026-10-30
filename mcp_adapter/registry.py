"""
registry.py —— MCPServerRegistry：外部 MCP Server 清册（含安全策略声明）。

每个外部 Server 在**接入前**必须登记：
  · 传输方式（stdio 子进程命令 / HTTP URL）
  · 出域域名白名单（默认空 = 不允许任何外发，fail-closed）
  · 需要审批的工具名（写操作）
  · 凭证引用名（环境变量名，绝不把令牌明文写进注册表）
  · 风险等级

清册落盘 data/mcp_servers.json，进程无关；文件损坏**不静默覆盖**（备份 +
抛错），与 breaker 同纪律。注册表只存"策略"，不存"凭证值"——凭证由
proxy 的 CredentialVault 从环境变量现取现注入。

诚实边界：登记一个 Server ≠ 它真实可用。`probe()` 必须真的握手 + ping 才
能标 healthy；连不上如实写 last_error，绝不假装在线。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import threading
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

from .transport import HttpTransport, StdioTransport, Transport

DEFAULT_REGISTRY_PATH = os.path.join("data", "mcp_servers.json")
NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
ENV_REF_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
RISK_LEVELS = ("low", "medium", "high", "critical")


class RegistryError(RuntimeError):
    """注册表损坏 / 规格非法。"""


@dataclass
class ServerSpec:
    name: str
    transport: Dict
    description: str = ""
    allowed_domains: List[str] = field(default_factory=list)
    requires_approval_tools: List[str] = field(default_factory=list)
    credential_ref: str = ""
    risk_level: str = "medium"
    enabled: bool = True

    def validate(self) -> List[str]:
        p: List[str] = []
        if not self.name or not NAME_RE.match(self.name):
            p.append("name 只允许字母/数字/点/下划线/连字符")
        t = self.transport or {}
        kind = t.get("type")
        if kind == "stdio":
            cmd = t.get("command")
            if not isinstance(cmd, list) or not cmd or not all(
                    isinstance(c, str) and c.strip() for c in cmd):
                p.append("stdio 需要非空 command 字符串列表")
        elif kind == "http":
            u = str(t.get("url") or "")
            if not u or urlparse(u).scheme not in ("http", "https") \
                    or not urlparse(u).hostname:
                p.append("http 需要合法的 http(s) URL")
        else:
            p.append("transport.type 必须是 stdio / http")
        if not isinstance(self.allowed_domains, list) or any(
                not isinstance(d, str) or not d.strip() for d in self.allowed_domains):
            p.append("allowed_domains 必须是字符串列表")
        if not isinstance(self.requires_approval_tools, list) or any(
                not isinstance(x, str) for x in self.requires_approval_tools):
            p.append("requires_approval_tools 必须是字符串列表")
        if self.credential_ref and not ENV_REF_RE.match(self.credential_ref):
            p.append("credential_ref 必须是大写环境变量名（如 GITHUB_TOKEN）")
        if self.risk_level not in RISK_LEVELS:
            p.append("risk_level 应为 %s" % "/".join(RISK_LEVELS))
        return p

    def to_dict(self) -> Dict:
        return {
            "name": self.name, "description": self.description,
            "transport": self.transport,
            "allowed_domains": list(self.allowed_domains),
            "requires_approval_tools": list(self.requires_approval_tools),
            "credential_ref": self.credential_ref,
            "risk_level": self.risk_level, "enabled": bool(self.enabled),
        }

    @classmethod
    def from_dict(cls, d: Dict) -> "ServerSpec":
        return cls(
            name=str(d.get("name") or ""),
            description=str(d.get("description") or ""),
            transport=dict(d.get("transport") or {}),
            allowed_domains=list(d.get("allowed_domains") or []),
            requires_approval_tools=list(d.get("requires_approval_tools") or []),
            credential_ref=str(d.get("credential_ref") or ""),
            risk_level=str(d.get("risk_level") or "medium"),
            enabled=bool(d.get("enabled", True)),
        )


class MCPServerRegistry:
    def __init__(self, path: str = DEFAULT_REGISTRY_PATH):
        self.path = path
        self._lock = threading.RLock()

    # ---- 底层读写 ----
    def _load(self) -> Dict:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as e:
            try:
                shutil.copyfile(self.path, self.path + ".corrupt")
            except OSError:
                pass
            raise RegistryError("MCP Server 注册表损坏（已备份为 .corrupt）：%s" % e)
        if not isinstance(data, dict) or "servers" not in data:
            raise RegistryError("MCP Server 注册表根节点须为含 servers 的对象")
        if not isinstance(data["servers"], dict):
            raise RegistryError("servers 必须是对象")
        return data

    def _save(self, data: Dict) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    # ---- 增删改查 ----
    def register(self, spec: ServerSpec) -> Tuple[bool, str]:
        problems = spec.validate()
        if problems:
            return False, "；".join(problems)
        with self._lock:
            data = self._load()
            data.setdefault("servers", {})[spec.name] = spec.to_dict()
            self._save(data)
        return True, ""

    def unregister(self, name: str) -> bool:
        with self._lock:
            data = self._load()
            if name not in data.get("servers", {}):
                return False
            data["servers"].pop(name)
            self._save(data)
            return True

    def get(self, name: str) -> Optional[ServerSpec]:
        d = self._load().get("servers", {}).get(name)
        return ServerSpec.from_dict(d) if d else None

    def list(self, include_disabled: bool = True) -> List[Dict]:
        out = [ServerSpec.from_dict(d).to_dict()
               for d in self._load().get("servers", {}).values()]
        if not include_disabled:
            out = [s for s in out if s["enabled"]]
        return sorted(out, key=lambda x: x["name"])

    def set_enabled(self, name: str, enabled: bool) -> bool:
        with self._lock:
            data = self._load()
            d = data.get("servers", {}).get(name)
            if not d:
                return False
            d["enabled"] = bool(enabled)
            self._save(data)
            return True

    # ---- 构造传输（凭证注入点在调用方：extra_env / headers）----
    def build_transport(self, name: str, *, extra_env: Optional[Dict] = None,
                        headers: Optional[Dict] = None,
                        timeout: float = 15.0) -> Transport:
        spec = self.get(name)
        if spec is None:
            raise RegistryError("未登记的 MCP Server：%s" % name)
        if not spec.enabled:
            raise RegistryError("MCP Server 已禁用：%s" % name)
        t = spec.transport
        if t.get("type") == "stdio":
            return StdioTransport(t["command"], extra_env=extra_env,
                                  cwd=t.get("cwd") or None, timeout=timeout)
        return HttpTransport(t["url"], headers=headers, timeout=timeout)

    # ---- 探活：真的握手 + ping 才标 healthy ----
    def probe(self, name: str, *, extra_env: Optional[Dict] = None,
              headers: Optional[Dict] = None, timeout: float = 8.0) -> Dict:
        out = {"name": name, "healthy": False, "via": "", "error": None,
               "server_info": None, "protocol_version": None}
        try:
            tr = self.build_transport(name, extra_env=extra_env,
                                      headers=headers, timeout=timeout)
        except RegistryError as e:
            out["error"] = str(e)
            return out
        try:
            out["via"] = tr.via
            init = tr.request("initialize", {
                "protocolVersion": "2025-06-18",
                "capabilities": {}, "clientInfo": {"name": "sectrust", "version": "1.0"},
            }, timeout=timeout)
            tr.notify("notifications/initialized")
            out["protocol_version"] = (init or {}).get("protocolVersion")
            out["server_info"] = (init or {}).get("serverInfo")
            tr.request("ping", {}, timeout=timeout)
            out["healthy"] = True
        except Exception as e:  # noqa: BLE001 - 探活失败如实记，不假装在线
            out["error"] = "%s: %s" % (type(e).__name__, e)
        finally:
            tr.close()
        return out

    def describe(self) -> Dict:
        try:
            servers = self.list()
        except RegistryError as e:
            return {"error": str(e), "servers": [], "total": 0}
        return {"total": len(servers), "servers": servers,
                "note": "登记 ≠ 在线；healthy 需 probe() 真握手 + ping 确认。"}
