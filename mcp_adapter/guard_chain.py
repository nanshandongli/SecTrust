"""
guard_chain.py —— 把 EvidenceGate / QualityGate 挂到 MCP 调用链（B3）。

B2 的 `SecureProxy` 在 `_invoke()` 成功返回前预留了 `evidence_gate` 钩子：
`verify_mcp_result(server, tool, result) -> {"accepted", "reasons", ...}`。
本模块提供该钩子的正式实现 `MCPEvidenceAdapter`，把"AI 说这个写操作完成了"
变成必须提交可核验证据才放行——**默认不信任 AI**。

规则（全部可判定，不依赖模型自我声明）：
  · 只读工具（search_/get_/list_ 等）无"完成声明"，不要求证据；
  · 写/声明类工具（create_/update_/delete_/publish_/send_/merge_ 等）必须在
    `structuredContent.evidence` 里给出 command/exit_code/stdout/ran_at/claim，
    由 `agentskills.evidence.EvidenceGate` 核验（命令形态/退出码/输出非空/
    新鲜度/期望与禁止正则/措辞 Red Flags），并落审计库；
  · 缺 evidence 字段 → 直接拒绝（不静默当成"已完成"）；
  · 可选 QualityGate 复核产出文本，可选必检工件清单（缺任一不得标记 done）；
  · 证据/质量任一不过 → 返回 accepted=False + 具体拒因，SecureProxy 会把它
    变成 `stage="evidence"` 的如实拒绝（而不是 ok=True）。

诚实边界：这里核验的是**结构化证据的形态与新鲜度**，不证明外部系统的副作用
真的发生（如 GitHub issue 是否真的创建）。真正的活体验证由对端返回的
structuredContent + 平台审批链共同负责，代码不越界自证。
"""

from __future__ import annotations

import json
import time
from typing import Dict, List, Optional, Set

from agentskills.evidence import Evidence, EvidenceGate
from agentskills.quality import QualityGate

# 无"完成声明"语义的只读动作：不要求证据
READ_ONLY_PREFIXES = ("search_", "get_", "list_", "read_", "fetch_", "query_",
                      "find_", "describe_", "show_", "check_")
# 有副作用/完成声明语义的动作：必须带证据
WRITE_PREFIXES = ("create_", "update_", "delete_", "remove_", "push_",
                  "publish_", "send_", "post_", "merge_", "put_", "upload_",
                  "deploy_", "apply_", "close_", "reopen_")


def _result_text(result) -> str:
    """把 MCP tools/call 结果摊平成可核验文本（content + structuredContent）。"""
    if not isinstance(result, dict):
        return str(result or "")
    parts: List[str] = []
    for c in (result.get("content") or []):
        if isinstance(c, dict) and c.get("type") == "text":
            parts.append(str(c.get("text", "")))
    sc = result.get("structuredContent")
    if sc is not None:
        parts.append(json.dumps(sc, ensure_ascii=False, default=str))
    return "\n".join(parts)


class MCPEvidenceAdapter:
    """SecureProxy.evidence_gate 钩子的实现（鸭子类型，无需继承）。"""

    def __init__(self, gate: Optional[EvidenceGate] = None,
                 db_path: Optional[str] = None,
                 required_tools: Optional[Set[str]] = None,
                 quality_gate: Optional[QualityGate] = None,
                 quality_rules: Optional[Dict] = None,
                 required_artifacts: Optional[List[str]] = None):
        self.gate = gate or EvidenceGate(db_path=db_path)
        self.explicit_required = set(required_tools or ())
        self.quality_gate = quality_gate
        self.quality_rules = quality_rules or {}
        self.required_artifacts = list(required_artifacts or [])

    def needs_evidence(self, tool: str) -> bool:
        if tool in self.explicit_required:
            return True
        if tool.startswith(READ_ONLY_PREFIXES):
            return False
        return tool.startswith(WRITE_PREFIXES)

    def verify_mcp_result(self, server: str, tool: str, result) -> Dict:
        out: Dict = {"accepted": True, "checked": False, "reasons": [],
                     "artifacts": [], "checks": {}, "quality": None,
                     "server": server, "tool": tool}
        if not self.needs_evidence(tool):
            out["note"] = "只读动作无完成声明，不要求证据"
            return out

        out["checked"] = True
        structured = (result or {}).get("structuredContent") \
            if isinstance(result, dict) else None
        raw = structured.get("evidence") if isinstance(structured, dict) else None
        if not isinstance(raw, dict) or not raw:
            out["accepted"] = False
            out["reasons"].append(
                "工具 %s 的产出缺少 structuredContent.evidence——默认不信任 AI："
                "声明完成必须提交可核验证据（command/exit_code/stdout/ran_at/claim）"
                % tool)
            return out

        try:
            ev = Evidence(
                command=str(raw.get("command") or ""),
                exit_code=int(raw.get("exit_code", -1)),
                stdout=str(raw.get("stdout") or ""),
                stderr=str(raw.get("stderr") or ""),
                claim=str(raw.get("claim") or ""),
                task_id=str(raw.get("task_id") or ("mcp:%s:%s" % (server, tool))),
                ran_at=float(raw.get("ran_at") or time.time()),
                expect=str(raw.get("expect") or ""),
                forbid=str(raw.get("forbid") or ""),
                ttl=float(raw.get("ttl") or 300.0),
            )
        except (TypeError, ValueError) as e:
            out["accepted"] = False
            out["reasons"].append("evidence 字段非法（无法构造 Evidence）：%s" % e)
            return out

        verdict = self.gate.verify(ev)
        self.gate.record(ev, verdict)   # 被驳回也留痕（试图蒙混本身要可追责）
        out["checks"] = verdict.to_dict()
        if not verdict.accepted:
            out["accepted"] = False
            out["reasons"].extend(verdict.reasons)
            return out

        text = _result_text(result)
        if isinstance(structured, dict):
            out["artifacts"] = list(structured.get("artifacts") or [])

        if self.quality_gate is not None:
            qv = self.quality_gate.check_output(text, self.quality_rules)
            out["quality"] = qv.to_dict()
            if not qv.accepted:
                out["accepted"] = False
                out["reasons"].extend(qv.violations)

        required = list(self.required_artifacts)
        if isinstance(structured, dict):
            required += [str(a) for a in (structured.get("required_artifacts") or [])]
        missing = [a for a in dict.fromkeys(required) if a and a not in text]
        if missing:
            out["accepted"] = False
            out["reasons"].append(
                "缺少必检工件：%s（默认不信任 AI，缺任一不得标记 done）"
                % "、".join(missing))
        return out


def attach_evidence_gate(proxy, *, gate=None, db_path=None,
                         required_tools=None, quality_gate=None,
                         quality_rules=None, required_artifacts=None
                         ) -> MCPEvidenceAdapter:
    """把证据门挂到已构造的 SecureProxy 上，返回适配器便于查看历史。"""
    adapter = MCPEvidenceAdapter(
        gate=gate, db_path=db_path, required_tools=required_tools,
        quality_gate=quality_gate, quality_rules=quality_rules,
        required_artifacts=required_artifacts)
    proxy.evidence_gate = adapter
    return adapter
