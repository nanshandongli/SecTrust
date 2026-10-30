"""
error_structs.py —— 反馈学习（方案③）的统一结构化错误

把分散在 guard_chain / mcp_adapter/proxy 各处的「失败字符串」收敛成统一的
`ErrorInfo`，让主 Agent 循环能消费 **human_readable_message** 触发自我反思，
让开发者能从 **technical_detail** 定位根因，并给后台监控提供稳定的
**error_code** 做同类失败聚合（生成 skill_optimization_suggestion）。

诚实边界：
  · 这是**结构化描述**，不是自动修复；绝不宣称「AI 已自我纠正」或「已分析根因」。
  · human_readable_message 面向 AI（可直接作为下一条 System/User 消息的一部分），
    但必须基于真实 stage/reason 生成，不编造友好话术掩盖真实失败。
  · technical_detail 面向开发者，保留原始 reason（截断防库膨胀）。

零第三方依赖：仅标准库。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional


# 门禁 stage → 错误码的映射（Skill 侧 + MCP 侧共用）
_GUARD_CODE_BY_STAGE = {
    "registry": "UNKNOWN_SKILL",
    "install": "INSTALL_INVALID",
    "tier_escalation": "SECURITY_POLICY_VIOLATION",
    "l2": "L2_LOAD_FAILED",
    "pii_check": "PII_OUTPUT_DETECTED",
    "approval": "APPROVAL_PENDING",
    "sandbox": "SANDBOX_FAILED",
    "evidence": "EVIDENCE_MISSING",
    "artifacts": "ARTIFACT_MISSING",
    "quality": "QUALITY_VIOLATION",
}

_MCP_CODE_BY_STAGE = {
    "registry": "SERVER_UNKNOWN",
    "breaker": "CIRCUIT_OPEN",
    "approval": "APPROVAL_PENDING",
    "domain": "DOMAIN_NOT_ALLOWED",
    "credential": "CREDENTIAL_UNAVAILABLE",
    "secret_scan_in": "SECRET_DETECTED",
    "secret_scan_out": "SECRET_DETECTED",
    "transport": "TRANSPORT_ERROR",
    "upstream": "UPSTREAM_ERROR",
    "evidence": "EVIDENCE_MISSING",
}


# 给 AI 看的标准话术骨架（基于真实 reason 拼接，不编造）
_AI_PROMPT = {
    "UNKNOWN_SKILL": "请求的技能在当前技能库中不存在，请确认技能名称或改用其他技能。",
    "INSTALL_INVALID": "技能未通过安装校验，无法执行。",
    "SECURITY_POLICY_VIOLATION": "技能触发了安全策略拦截（信任分级越界）。",
    "L2_LOAD_FAILED": "技能正文加载失败。",
    "PII_OUTPUT_DETECTED": "技能输出命中 PII / 注入回潮检查，被拦截。",
    "APPROVAL_PENDING": "该操作需要人工审批，已挂起。",
    "SANDBOX_FAILED": "技能脚本在沙箱中执行失败。",
    "EVIDENCE_MISSING": "证据门禁未通过：缺少可验证的实跑产出。",
    "ARTIFACT_MISSING": "缺少必检工件的实际内容。",
    "QUALITY_VIOLATION": "产出未通过质量门（含未填槽位/占位符）。",
    "SERVER_UNKNOWN": "请求的 MCP Server 未登记或已禁用。",
    "CIRCUIT_OPEN": "目标处于熔断状态，暂不可用。",
    "DOMAIN_NOT_ALLOWED": "请求域名不在白名单（防 SSRF / 外发）。",
    "CREDENTIAL_UNAVAILABLE": "所需凭证不可用，调用被拒绝。",
    "SECRET_DETECTED": "请求/响应命中密钥扫描规则，被挂起。",
    "TRANSPORT_ERROR": "传输层（stdio/HTTP）异常。",
    "UPSTREAM_ERROR": "对端服务明确返回错误。",
    "UNKNOWN": "操作失败，原因见技术细节。",
}


@dataclass
class ErrorInfo:
    """结构化错误：error_code / human_readable_message / technical_detail。

    to_dict() 用于落 result / Trace；from_guard / from_proxy 把已有的
    stage + reason 收敛成统一的 ErrorInfo（不重构既有调用方的返回结构）。
    """

    error_code: str = "UNKNOWN"
    human_readable_message: str = ""
    technical_detail: str = ""
    stage: str = ""
    rule_id: str = ""

    def to_dict(self) -> Dict:
        return {
            "error_code": self.error_code,
            "human_readable_message": self.human_readable_message,
            "technical_detail": self.technical_detail,
            "stage": self.stage,
            "rule_id": self.rule_id,
        }

    @classmethod
    def build(cls, stage: str, reason: str, rule_id: str = "",
              *, kind: str = "guard") -> "ErrorInfo":
        """从 (stage, reason, rule_id) 构造。kind=guard|proxy 选择错误码表。"""
        table = _GUARD_CODE_BY_STAGE if kind == "guard" else _MCP_CODE_BY_STAGE
        code = table.get(stage, "UNKNOWN")
        reason = str(reason or "")
        human = _AI_PROMPT.get(code, _AI_PROMPT["UNKNOWN"])
        # 真实 reason 拼接在话术之后（不编造、不掩盖）
        if reason and reason not in human:
            human = "%s 具体原因：%s" % (human, reason[:300])
        return cls(error_code=code, human_readable_message=human,
                   technical_detail=reason[:500], stage=stage, rule_id=rule_id)

    # 便捷构造：明确错误码（用于显式语义，如 MISSING_PARAM）
    @classmethod
    def explicit(cls, code: str, human: str, technical: str = "",
                 stage: str = "", rule_id: str = "") -> "ErrorInfo":
        return cls(error_code=code, human_readable_message=human,
                   technical_detail=technical, stage=stage, rule_id=rule_id)


def self_correction_prompt(info: Optional[Dict]) -> str:
    """把 ErrorInfo 转成给 AI 的「自我反思」提示（主 Agent 循环消费）。

    返回空串表示没有可消费的失败信息（不应触发反思）。
    """
    if not info:
        return ""
    msg = info.get("human_readable_message") or ""
    code = info.get("error_code") or ""
    if not msg:
        return ""
    return ("上一步操作失败了（错误码 %s）。错误信息：%s "
            "请根据这个错误，思考下一步该怎么做（例如向用户询问缺失的信息，"
            "或尝试一个备选方案）。" % (code, msg))
