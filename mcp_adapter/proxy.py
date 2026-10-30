"""
proxy.py —— SecureProxy：外部 MCP Server 调用的安全代理层（默认不信任 AI）。

每一次 `tools/call`（以及 resources/prompts 读操作）都串行穿过 6 道门，
任何一道不过就**如实返回拒绝原因**，绝不静默放行、绝不偷偷删内容：

  1. 熔断门   TTLCircuitBreaker：对端连续失败 → TTL 窗口内直接拒绝；
  2. 审批门   高危工具（写操作 / requires_approval_tools）必须持已批准审批单；
              未配置审批中心时 fail-closed 拒绝，不默认放行；
  3. 域名门   allowed_domains 白名单（空 = 不允许任何出域），校验参数内 URL
              与非 loopback 的传输 URL，防 SSRF / 数据外发；
  4. 凭证门   参数里禁止自带 token/api_key 等字段（模型不得持有凭证）；
              凭证由 CredentialVault 从环境变量现取，只注入子进程 env / HTTP
              header，绝不写进 params、绝不落注册表；缺失不伪造、如实标注，
              由对端自己返回 credential_unavailable；
  5. 入向扫描 请求参数过 scanner.detectors（200+ 规则），命中高危 → 挂起；
  6. 出向扫描 响应内容同样过密钥扫描，命中 → **如实挂起并留脱敏 findings，
              不偷偷删除**（与 write_files 的"命中高危如实挂起"同纪律）。

另留 `evidence_gate` 钩子（B3 实现 verify_mcp_result），把"AI 说完成了"变成
可验证证据后再标记 done——保持"默认不信任 AI"。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from .breaker import TTLCircuitBreaker
from .protocol import (INITIALIZE, NOTIFY_INITIALIZED, PING, TOOLS_LIST,
                       TOOLS_CALL, RESOURCES_LIST, RESOURCES_READ, PROMPTS_LIST,
                       PROMPTS_GET)
from .registry import MCPServerRegistry, ServerSpec

# 反馈学习③：结构化错误（agentskills.error_structs 仅依赖标准库，无循环依赖）
from agentskills.error_structs import ErrorInfo

# 参数里出现这些键 = 模型试图自带凭证，一律拒绝（凭证必须走代理注入）
FORBIDDEN_PARAM_KEYS = frozenset({
    "token", "api_key", "apikey", "secret", "password", "passwd",
    "authorization", "credential", "access_token", "private_key",
})
HIGH_RISK_PREFIXES = ("create_", "update_", "delete_", "push_", "send_",
                      "post_", "merge_", "publish_", "put_", "remove_")
SECRET_SCAN_SEVERITIES = frozenset({"critical", "high", "medium"})
_URL_RE = re.compile(r"https?://[^\s\"'<>\\]+", re.I)


class CredentialVault:
    """凭证代理：只从环境变量现取现注入，不落盘、不进 params、不进注册表。"""

    def __init__(self, env: Optional[Dict] = None):
        self._env = os.environ if env is None else env

    def resolve(self, ref: str) -> Tuple[bool, str, str]:
        if not ref:
            return True, "", "未声明凭证引用（对端自行决定可用性）"
        val = str(self._env.get(ref, ""))
        if not val.strip():
            return True, "", ("credential_unavailable: 环境变量 %s 未配置；"
                              "不注入、不伪造，由对端如实返回不可用" % ref)
        return True, val, "resolved"

    def describe(self, ref: str) -> Dict:
        return {"ref": ref or None,
                "present": bool(ref and str(self._env.get(ref, "")).strip())}


def _contains_key(obj: Any, keys: frozenset) -> Optional[str]:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if str(k).lower() in keys:
                return str(k)
            found = _contains_key(v, keys)
            if found:
                return found
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            found = _contains_key(v, keys)
            if found:
                return found
    return None


def _extract_urls(obj: Any) -> List[str]:
    out: List[str] = []
    if isinstance(obj, str):
        out.extend(m.group(0) for m in _URL_RE.finditer(obj))
    elif isinstance(obj, dict):
        for v in obj.values():
            out.extend(_extract_urls(v))
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            out.extend(_extract_urls(v))
    return out


def _host_allowed(host: str, patterns: List[str]) -> bool:
    h = (host or "").lower().rstrip(".")
    for raw in patterns or []:
        p = str(raw).strip().lower().rstrip(".")
        if not p:
            continue
        if p.startswith("*."):
            base = p[2:]
            if h == base or h.endswith("." + base):
                return True
        elif h == p:
            return True
    return False


def scan_secrets(text: str) -> List[Dict]:
    """复用 scanner.detectors 的 200+ 规则；只回脱敏摘要，绝不回显完整凭证。

    沿用 scanner 既定策略：`suppressed_by` 命中抑制词（如 EXAMPLE/aaaaaa 这类
    低熵示例值）的发现不拦截，避免误报挡住正常调用；这是 scanner 的防误报
    设计，不是"漏扫"——真实高熵凭证不会命中抑制词。"""
    try:
        from scanner.detectors import find_in_text
        findings = find_in_text(text or "")
    except Exception:  # pragma: no cover - 扫描器异常不应变成"未命中"放行
        return [{"rule_id": "scanner_error", "severity": "high",
                 "note": "密钥扫描器异常，fail-closed 拒绝"}]
    out: List[Dict] = []
    for f in findings:
        if f.get("suppressed_by"):
            continue
        if f.get("severity") not in SECRET_SCAN_SEVERITIES:
            continue
        out.append({
            "rule_id": f.get("rule_id"), "severity": f.get("severity"),
            "provider": f.get("provider", ""), "line_no": f.get("line_no"),
            "preview": str(f.get("secret", ""))[:4] + "***",
        })
    return out


@dataclass
class ProxyResult:
    ok: bool
    stage: str
    reason: str = ""
    data: Any = None
    approval_id: str = ""
    findings: List[Dict] = field(default_factory=list)
    via: str = ""
    fixture: bool = False
    evidence: Optional[Dict] = None
    audit: Dict = field(default_factory=dict)

    @property
    def text(self) -> str:
        if isinstance(self.data, dict):
            parts = [str(c.get("text", "")) for c in
                     (self.data.get("content") or [])
                     if isinstance(c, dict) and c.get("type") == "text"]
            if parts:
                return "\n".join(parts)
        return json.dumps(self.data, ensure_ascii=False) if self.data else ""

    def to_dict(self) -> Dict:
        return {
            "ok": self.ok, "stage": self.stage, "reason": self.reason,
            "data": self.data, "approval_id": self.approval_id,
            "findings": self.findings, "via": self.via,
            "fixture": self.fixture, "evidence": self.evidence,
            "audit": self.audit,
        }


class SecureProxy:
    """把安全策略门串到 MCP 调用链上。"""

    def __init__(self, registry: MCPServerRegistry,
                 breaker: Optional[TTLCircuitBreaker] = None,
                 approvals=None, vault: Optional[CredentialVault] = None,
                 evidence_gate=None, transport_factory=None,
                 timeout: float = 15.0, trace_store=None, trace_id: str = "",
                 state=None):
        self.registry = registry
        self.breaker = breaker or TTLCircuitBreaker()
        self.approvals = approvals           # ApprovalCenter 鸭子类型
        self.vault = vault or CredentialVault()
        self.evidence_gate = evidence_gate   # B3：verify_mcp_result(server, tool, result)
        self.transport_factory = transport_factory
        self.timeout = float(timeout)
        # 审计轨迹：每次调用（含被拒）记一条 mcp_call 事件。缺 trace 上下文时
        # 如实标注未写，绝不硬造孤儿轨迹（与 approval.py 同纪律）。
        self.trace_store = trace_store
        self.trace_id = trace_id
        # v2 运行时状态（反馈闭环）：可选注入，回写 mcp:<server> 维度状态
        self.state = state

    # ---- 单道门 ----
    @staticmethod
    def _needs_approval(spec: ServerSpec, tool: str) -> bool:
        if tool in (spec.requires_approval_tools or []):
            return True
        return (spec.risk_level in ("high", "critical")
                and tool.startswith(HIGH_RISK_PREFIXES))

    def _approval_gate(self, spec: ServerSpec, tool: str,
                       approval_id: str, actor: str) -> Tuple[bool, str, str]:
        if not self._needs_approval(spec, tool):
            return True, "", ""
        if self.approvals is None:
            return (False, "高危工具 %s 需要审批，但未配置审批中心（fail-closed 拒绝）"
                    % tool, "")
        if approval_id:
            try:
                if self.approvals.is_approved(approval_id):
                    return True, "", ""
            except Exception:
                pass
            return False, "审批单 %s 尚未批准（状态非 approved）" % approval_id, ""
        try:
            aid = self.approvals.request(
                "mcp.tools.call",
                json.dumps({"server": spec.name, "tool": tool}, ensure_ascii=False),
                channel="mcp_adapter", user_id=actor)
        except Exception as e:  # noqa: BLE001
            return (False, "发起审批失败（fail-closed 拒绝）：%s: %s"
                    % (type(e).__name__, e), "")
        return (False, "高危工具 %s 需人工审批；审批单 %s 已挂起（默认不信任 AI）"
                % (tool, aid), aid)

    @staticmethod
    def _domain_gate(spec: ServerSpec, arguments: Any) -> Tuple[bool, str, List[str]]:
        patterns = spec.allowed_domains or []
        if not patterns:
            return (False, "未配置 allowed_domains（fail-closed：不允许任何出域）", [])
        hosts: List[str] = []
        t = spec.transport or {}
        if t.get("type") == "http":
            host = urlparse(str(t.get("url") or "")).hostname or ""
            # loopback 是本地 fixture，不作为"出域"判据（via 会诚实标注）
            if host and host.lower() not in ("127.0.0.1", "localhost", "::1"):
                hosts.append(host)
        for u in _extract_urls(arguments):
            host = urlparse(u).hostname or ""
            if host:
                hosts.append(host)
        for h in hosts:
            if not _host_allowed(h, patterns):
                return (False, "域名 %s 不在白名单 %s 内（防 SSRF/外发）"
                        % (h, patterns), hosts)
        return True, "", hosts

    def _credential_gate(self, spec: ServerSpec, arguments: Any
                         ) -> Tuple[bool, str, Dict, Dict, Dict]:
        leaked = _contains_key(arguments, FORBIDDEN_PARAM_KEYS)
        if leaked:
            return (False, "工具参数不得自带凭证字段 %r（凭证一律走代理注入）"
                    % leaked, {}, {}, {})
        if not spec.credential_ref:
            return True, "", {}, {}, {"state": "none"}
        ok, val, why = self.vault.resolve(spec.credential_ref)
        if not ok:
            return False, "凭证代理不可用：%s" % why, {}, {}, {"state": "error"}
        if not val:
            # 缺凭证不伪造、不阻断请求：对端会如实返回 credential_unavailable
            return True, "", {}, {}, {"state": "unavailable", "detail": why}
        t = spec.transport or {}
        if t.get("type") == "stdio":
            return True, "", {spec.credential_ref: val}, {}, {"state": "injected_env"}
        return (True, "", {}, {"Authorization": "Bearer " + val},
                {"state": "injected_header"})

    # ---- 审计：每次调用（含被各道门拒绝）都留痕 ----
    def _emit_audit(self, server: str, method: str, params: Dict,
                    result: "ProxyResult") -> None:
        audit = dict(result.audit or {})
        canonical = ""
        try:
            canonical = json.dumps(params or {}, ensure_ascii=False,
                                   sort_keys=True, default=str)
            digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
        except (TypeError, ValueError):
            digest = "unavailable"
        stages = audit.get("stages") or []
        parallel = [s for s in stages if s.get("parallel")]
        decision = ("allow" if result.ok else
                    ("hold" if result.stage in ("approval", "upstream")
                     else "reject"))
        audit.update({
            "tool": str((params or {}).get("name") or ""),
            "params_keys": sorted((params or {}).keys()),
            "params_digest": digest,          # 只留指纹，不存参数原文
            "stage": result.stage, "ok": result.ok, "via": result.via,
            "fixture": result.fixture,
            "cost_estimate": None,
            "cost_note": "无计费器接入，未估算成本（如实标注，不编造数字）",
            # ---- v2 可观测：门禁决策白盒（哪道门/哪条规则/放行或拦）----
            "tier": "black",                # MCP 侧六道门全开，不分级免检
            "decision": decision,
            "blocked_by": audit.get("blocked_by", ""),
            "rule_id": audit.get("rule_id", ""),
            "parallel_gates": len(parallel),
            "parallel_wall_ms": round(max(
                [s.get("duration_ms", 0) or 0 for s in parallel], default=0), 1),
            "serial_sum_ms": round(sum(
                (s.get("duration_ms", 0) or 0) for s in parallel), 1),
        })
        result.audit = audit

        # v2 反馈闭环：MCP 对端状态回写（mcp:<server> 维度，与技能维度隔离）
        if self.state is not None:
            rejected = (not result.ok) and result.stage in (
                "approval", "domain", "credential", "secret_scan_in",
                "secret_scan_out", "evidence", "registry")
            try:
                self.state.record("mcp:" + server, bool(result.ok),
                                  stage=result.stage, reason=result.reason,
                                  rejected=rejected)
            except Exception:  # noqa: BLE001 - 状态记账是旁路
                pass

        if self.trace_store is None or not self.trace_id:
            audit["trace_written"] = False
            audit["trace_note"] = ("未提供 trace_store/trace_id，未硬造孤儿轨迹；"
                                   "调用摘要仍在 result.audit 中可查")
            result.audit = audit
            return
        try:
            self.trace_store.record(
                self.trace_id, "mcp_call", task_id="", actor="mcp_adapter",
                summary="MCP 调用 %s/%s → %s" % (server, method, result.stage),
                detail=audit, content=canonical)
            # W4：门禁决策白盒（stages 泳道 + blocked_by + rule_id）
            self.trace_store.record(
                self.trace_id, "guard_decision", task_id="", actor="mcp_adapter",
                summary="门禁 mcp:%s → %s（%s）"
                        % (server, decision, audit.get("blocked_by") or "-"),
                detail={"target": "mcp:%s/%s" % (server, audit.get("tool", "")),
                        "tier": "black", "stages": stages,
                        "blocked_by": audit.get("blocked_by", ""),
                        "rule_id": audit.get("rule_id", ""),
                        "decision": decision, "via": result.via,
                        "fixture": result.fixture,
                        "parallel_gates": audit["parallel_gates"],
                        "parallel_wall_ms": audit["parallel_wall_ms"],
                        "serial_sum_ms": audit["serial_sum_ms"]},
                content=None)
            audit["trace_written"] = True
        except Exception as e:  # noqa: BLE001 - 审计是旁路，失败不挡调用结果
            audit["trace_written"] = False
            audit["trace_error"] = "%s: %s" % (type(e).__name__, e)

        # ③ 结构化错误：失败路径统一收敛 ErrorInfo（供主循环自我反思 / 开发者定位）
        if not result.ok:
            info = ErrorInfo.build(result.stage, result.reason,
                                   audit.get("rule_id", ""), kind="proxy")
            audit["error_info"] = info.to_dict()
            # ③ 开发者告警：同一 server 同一技术细节重复失败 → 优化建议日志
            if self.state is not None:
                try:
                    self.state.record_error_signature(
                        "mcp:" + server, info.technical_detail)
                except Exception:  # noqa: BLE001
                    pass

        result.audit = audit

    # ---- 统一调用链：包装层保证所有返回点（含拒绝）都写审计 ----
    def _invoke(self, server: str, method: str, params: Dict, *,
                approval_tool: str = "", approval_id: str = "",
                actor: str = "operator", timeout: Optional[float] = None,
                allow_high_risk: bool = False) -> ProxyResult:
        result = self._invoke_inner(
            server, method, params, approval_tool=approval_tool,
            approval_id=approval_id, actor=actor, timeout=timeout,
            allow_high_risk=allow_high_risk)
        self._emit_audit(server, method, params, result)
        return result

    def _invoke_inner(self, server: str, method: str, params: Dict, *,
                      approval_tool: str = "", approval_id: str = "",
                      actor: str = "operator", timeout: Optional[float] = None,
                      allow_high_risk: bool = False) -> ProxyResult:
        started = time.time()
        audit: Dict = {"server": server, "method": method, "at": started,
                       "stages": []}

        def _stage(name: str, ok: bool, note: str = "", rule_id: str = "",
                   parallel: bool = False, duration_ms: float = 0.0):
            """v2：每道门补 rule_id / 耗时 / 是否并行；拒绝时记 blocked_by。"""
            audit["stages"].append({
                "stage": name, "ok": bool(ok), "note": str(note)[:200],
                "rule_id": rule_id, "parallel": parallel,
                "duration_ms": round(duration_ms, 1)})
            # 首个拒绝门胜出（并行组按固定顺序落 stage，保证 blocked_by 稳定）
            if not ok and not audit.get("blocked_by"):
                audit["blocked_by"] = name + (":" + rule_id if rule_id else "")
                audit["rule_id"] = rule_id or name

        spec = self.registry.get(server)
        if spec is None:
            return ProxyResult(False, "registry", "未登记的 MCP Server：%s" % server,
                               audit=audit)
        if not spec.enabled:
            return ProxyResult(False, "registry", "MCP Server 已禁用：%s" % server,
                               audit=audit)

        # 1) 熔断（必须先串行：决定是否放行）
        ok, why, bst = self.breaker.allow(server)
        _stage("breaker", ok, why, rule_id="breaker.open" if not ok else "")
        if not ok:
            return ProxyResult(False, "breaker", why, audit=audit)

        try:
            # 2~5) 入向四门并行 fan-out（互不依赖、纯入参可判定）。
            # **入向密钥扫描不参与短路**：它是安全底线，必须跑完，不因审批/域名门
            # 已拒绝就跳过（默认不信任 AI）。其余三门可被 best-effort 取消。
            # 传输依赖凭证注入结果 → 串行；出向扫描依赖响应 → 串行。
            def _approval_task():
                t0 = time.time()
                if not (approval_tool and not allow_high_risk):
                    return ("approval", True, "无需审批（只读或显式放行）", "", t0, "")
                ok2, why2, aid2 = self._approval_gate(spec, approval_tool,
                                                       approval_id, actor)
                return ("approval", ok2, why2,
                        "approval.not_approved" if not ok2 else "", t0, aid2)

            def _domain_task():
                t0 = time.time()
                ok2, why2, hosts2 = self._domain_gate(spec, params)
                return ("domain", ok2, why2,
                        "domain.not_allowed" if not ok2 else "", t0, hosts2)

            def _credential_task():
                t0 = time.time()
                ok2, why2, ee, hh, cred2 = self._credential_gate(spec, params)
                return ("credential", ok2, why2,
                        "credential.forbidden_param" if not ok2 else "", t0,
                        (ee, hh, cred2))

            def _scan_in_task():
                t0 = time.time()
                hits2 = scan_secrets(json.dumps(params, ensure_ascii=False))
                rid2 = (str(hits2[0].get("rule_id") or "secret_scan_in")
                        if hits2 else "")
                return ("secret_scan_in", not hits2, "%d hit(s)" % len(hits2),
                        rid2 if hits2 else "", t0, hits2)

            parallel_tasks = [
                ("approval", _approval_task),
                ("domain", _domain_task),
                ("credential", _credential_task),
                ("secret_scan_in", _scan_in_task),
            ]
            results: Dict[str, tuple] = {}
            short_circuited = False
            ex = ThreadPoolExecutor(max_workers=4)
            try:
                futs = {ex.submit(fn): name for name, fn in parallel_tasks}
                for fut in as_completed(futs):
                    name, ok2, why2, rid2, t0, payload = fut.result()
                    results[name] = (ok2, why2, rid2,
                                     (time.time() - t0) * 1000, payload)
                    if not ok2 and name != "secret_scan_in" and not short_circuited:
                        short_circuited = True
                        # best-effort 短路：取消尚未开始的非扫描门
                        for f2, nm2 in futs.items():
                            if nm2 != "secret_scan_in" and not f2.done():
                                f2.cancel()
            finally:
                ex.shutdown(wait=True, cancel_futures=True)

            # 按固定顺序落 stage（并行不改变可审计的泳道顺序）；被取消的门
            # 如实标 skipped，不伪装成"通过"。
            gate_order = ("approval", "domain", "credential", "secret_scan_in")
            for name in gate_order:
                if name in results:
                    ok2, why2, rid2, dur, _ = results[name]
                    _stage(name, ok2, why2, rule_id=rid2, parallel=True,
                           duration_ms=dur)
                else:
                    audit["stages"].append({
                        "stage": name, "ok": None,
                        "note": "短路取消（另一道入向门已拒绝）",
                        "rule_id": "", "parallel": True, "duration_ms": 0.0})

            aid = (results.get("approval") or (True, "", "", 0, ""))[4]
            hosts = (results.get("domain") or (True, "", "", 0, []))[4]
            extra_env, headers, cred = (results.get("credential") or
                                        (True, "", "", 0, ({}, {}, {})))[4]
            hits_in = (results.get("secret_scan_in") or (True, "", "", 0, []))[4]
            audit["credential"] = cred

            # 聚合拒绝（按门顺序；密钥扫描命中也如实挂起，不偷偷删除）
            for name in gate_order:
                if name not in results:
                    continue
                ok2, why2, rid2, dur, _ = results[name]
                if ok2:
                    continue
                if name == "approval":
                    return ProxyResult(False, "approval", why2,
                                       approval_id=aid, audit=audit)
                if name == "domain":
                    return ProxyResult(False, "domain", why2, audit=audit)
                if name == "credential":
                    return ProxyResult(False, "credential", why2, audit=audit)
                if name == "secret_scan_in":
                    return ProxyResult(
                        False, "secret_scan_in",
                        "请求参数命中密钥/敏感信息规则 %d 条（如实挂起，不偷偷删除）"
                        % len(hits_in), findings=hits_in, audit=audit)

            # 传输（依赖凭证注入结果，必须串行）
            build = self.transport_factory or self.registry.build_transport
            tr = build(server, extra_env=extra_env, headers=headers,
                       timeout=float(timeout or self.timeout))
            try:
                tr.request(INITIALIZE, {
                    "protocolVersion": "2025-06-18", "capabilities": {},
                    "clientInfo": {"name": "sectrust-mcp-adapter", "version": "1.0"},
                }, timeout=float(timeout or self.timeout))
                tr.notify(NOTIFY_INITIALIZED)
                raw = tr.request(method, params, timeout=float(timeout or self.timeout))
            finally:
                tr.close()
            _stage("transport", True, tr.via)

            # 6) 出向密钥扫描（依赖响应，必须串行）
            hits = scan_secrets(json.dumps(raw, ensure_ascii=False))
            rid_out = (str(hits[0].get("rule_id") or "secret_scan_out")
                       if hits else "")
            _stage("secret_scan_out", not hits, "%d hit(s)" % len(hits),
                   rule_id=rid_out if hits else "")
            if hits:
                return ProxyResult(
                    False, "secret_scan_out",
                    "响应内容命中密钥/敏感信息规则 %d 条（如实挂起，不回显原文）"
                    % len(hits), findings=hits, via=tr.via, audit=audit)

            fixture = bool(isinstance(raw, dict) and (
                raw.get("_fixture")
                or (raw.get("structuredContent") or {}).get("mode") == "fixture"))
            audit["hosts_checked"] = hosts
            audit["duration_ms"] = round((time.time() - started) * 1000, 1)

            # 对端明确返回 isError（如无凭证 / fixture 下拒绝写）→ 绝不把它
            # 包装成 ok=True：如实标 upstream 并保留原始内容供上层展示。
            if isinstance(raw, dict) and raw.get("isError"):
                _stage("upstream", False, "对端返回 isError=True",
                       rule_id="upstream.is_error")
                err = (raw.get("structuredContent") or {}).get("error", "")
                parts = [str(c.get("text", "")) for c in (raw.get("content") or [])
                         if isinstance(c, dict)]
                return ProxyResult(
                    False, "upstream",
                    "对端明确返回错误（如实传递，不伪造）：%s"
                    % (err or (parts[0][:200] if parts else "（无详情）")),
                    data=raw, via=tr.via, fixture=fixture, audit=audit)

            self.breaker.record_success(server)

            # B3 钩子：EvidenceGate 校验（默认不信任 AI 的"我完成了"）
            evidence = None
            if self.evidence_gate is not None and method in (TOOLS_CALL,):
                verdict = self.evidence_gate.verify_mcp_result(
                    server, approval_tool, raw)
                evidence = verdict
                _stage("evidence", bool(verdict.get("accepted")),
                       "；".join(verdict.get("reasons") or []),
                       rule_id=("evidence.missing_or_stale"
                                if not verdict.get("accepted") else ""))
                if not verdict.get("accepted"):
                    return ProxyResult(
                        False, "evidence",
                        "证据门禁未通过：%s" % "；".join(verdict.get("reasons") or []),
                        data=raw, via=tr.via, fixture=fixture,
                        evidence=evidence, audit=audit)

            return ProxyResult(True, "ok", "", data=raw, via=tr.via,
                               fixture=fixture, evidence=evidence, audit=audit)
        except Exception as e:  # noqa: BLE001 - 传输/协议异常如实记并计失败
            self.breaker.record_failure(server, str(e))
            _stage("transport", False, "%s: %s" % (type(e).__name__, e),
                   rule_id="transport.error")
            return ProxyResult(
                False, "transport", "MCP 调用失败：%s: %s" % (type(e).__name__, e),
                audit=audit)

    # ---- 对外：三原语 ----
    def call_tool(self, server: str, tool: str, arguments: Optional[Dict] = None,
                  *, approval_id: str = "", actor: str = "operator",
                  timeout: Optional[float] = None) -> ProxyResult:
        return self._invoke(
            server, TOOLS_CALL,
            {"name": tool, "arguments": arguments or {}},
            approval_tool=tool, approval_id=approval_id, actor=actor,
            timeout=timeout)

    def list_tools(self, server: str, **kw) -> ProxyResult:
        return self._invoke(server, TOOLS_LIST, {}, **kw)

    def list_resources(self, server: str, **kw) -> ProxyResult:
        return self._invoke(server, RESOURCES_LIST, {}, **kw)

    def read_resource(self, server: str, uri: str, **kw) -> ProxyResult:
        return self._invoke(server, RESOURCES_READ, {"uri": uri}, **kw)

    def list_prompts(self, server: str, **kw) -> ProxyResult:
        return self._invoke(server, PROMPTS_LIST, {}, **kw)

    def get_prompt(self, server: str, name: str,
                   arguments: Optional[Dict] = None, **kw) -> ProxyResult:
        return self._invoke(server, PROMPTS_GET,
                            {"name": name, "arguments": arguments or {}}, **kw)

    def ping(self, server: str, **kw) -> ProxyResult:
        return self._invoke(server, PING, {}, **kw)
