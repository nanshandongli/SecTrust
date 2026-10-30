"""
compliance.py —— 全链路审计追溯（能力映射）+ 合规自评报告（可验证优先）

把用户对标的硬性要求拆成一份**能力矩阵**，逐条标注实现状态与证据来源。
这是"全链路审计追溯"在合规维度的落地（每项能力可查、可验证），也直接服务于
"可验证优先"——不空喊合规，而是把"哪条要求由哪个模块支撑"写清楚。

状态取值：
  implemented  已落地并接入主链路
  partial      有模块支撑但仍有缺口（标注缺口）
  designed     已设计/结构就绪，待接入或待外部依赖
  exempt       本次范围外（如用户明确豁免的"数据不出域"）

诚实边界：本矩阵是**自评**，不是认证。国标（国标委〔2026〕41号）为强制性
标准、ETSI TS 104 033 为欧盟准入，二者的最终结论需由第三方检测/认证机构
出具——本产品"经得起测"，但不能自我宣告"已合规"。
"""

from __future__ import annotations

import time
from typing import Dict, List

# --------------------------------------------------------------------------
# 能力矩阵（数据驱动，便于随实现进度更新）
# --------------------------------------------------------------------------
CAPABILITIES: List[Dict] = [
    {
        "id": "asset_inventory",
        "name": "资产盘点与管理",
        "standard": "国标委〔2026〕41号·六项之一",
        "status": "implemented",
        "evidence": "governance.asset_registry（AI同事/工具/模型/数据源清册，落盘可查）",
        "note": "可登记+自动发现平台资产，按分类分级（公开/内部/受限/涉密）标注。",
    },
    {
        "id": "identity_authz",
        "name": "身份认证与权限",
        "standard": "国标委〔2026〕41号·六项之一",
        "status": "implemented",
        "evidence": "security.oauth（JWT验签+aud绑定+吊销）+ governance.rbac（角色→权限强校验）",
        "note": "无密码演示级身份已由 RBAC 补齐端点强校验；高危动作（熔断/落盘/复位）限权。",
    },
    {
        "id": "knowledge_data_control",
        "name": "知识库与数据管控",
        "standard": "国标委〔2026〕41号·六项之一",
        "status": "implemented",
        "evidence": ("governance.kb_control（KB 分级标签 + fail-closed 出域审批，"
                     "data/kb_labels.json / data/egress_approvals.json）+ "
                     "memory_hybrid.classification 分级列 + daemon process_task 出域门"),
        "note": ("知识库条目按 public/internal/restricted/confidential 四级打标；"
                 "public/internal 可出域，restricted 需 approval.act 批准，confidential "
                 "默认拒绝、需 governance.emergency 级批准；无有效批准 → fail-closed 拒绝并留痕，"
                 "daemon 出域门挂起 needs_human。诚实边界：出域批准只约束平台自身动作，"
                 "不等于数据离机后仍受控；未打标条目按默认 internal 处理（建议先打标）。"),
    },
    {
        "id": "multimodal_guard",
        "name": "多模态内容防护",
        "standard": "国标委〔2026〕41号·六项之一",
        "status": "implemented",
        "evidence": ("governance.content_guard.validate_media / check_media_prompt / "
                     "moderate_media + models.multimodal.MediaRouter.generate 前置后置接入"),
        "note": ("图像/音频/视频生成 prompt 过注入/越狱前置；产出过魔数/类型/大小校验，"
                 "拦截改后缀可执行文件；meta.content_guard 记录守卫决策。诚实边界："
                 "未接入视觉/音频语义审核模型时 semantic_check=false 如实标注，只做魔数/"
                 "元数据/大小校验，不宣称已做语义审核；真语义审核需注入 semantic_hook。"),
    },
    {
        "id": "audit_trace",
        "name": "全链路审计追溯",
        "standard": "国标委〔2026〕41号·六项之一",
        "status": "implemented",
        "evidence": "audit_suite.py（审计三件套）+ trace.py（轨迹）+ 任务状态/结果落库",
        "note": "任务派发→执行→审批→结果全链路可追溯；本矩阵进一步把能力映射成可查清单。",
    },
    {
        "id": "emergency_circuit",
        "name": "应急处置与熔断",
        "standard": "国标委〔2026〕41号·六项之一",
        "status": "implemented",
        "evidence": "governance.circuit_breaker（全局紧急熔断，daemon 认领前检查）",
        "note": "触发后新任务不被任何执行器消费，留痕 who/when/why，人工解除。",
    },
    {
        "id": "interop",
        "name": "互联互通（智能体互联）",
        "standard": "GB/Z 185—2026",
        "status": "implemented",
        "evidence": ("governance.interop.FederationRegistry / discover_remote / delegate / "
                     "match_federated（data/interop_federation.json）+ 控制台 /api/governance/interop/*"),
        "note": ("联邦登记/远程发现/任务委派/本地+联邦合并匹配已实现，凭证只存引用不落明文，"
                 "失败一律 ok=False+error，委派留痕 server/tool/参数摘要/耗时/成本（未估算如实标注）。"
                 "诚实边界：联邦发现/委派已由本地 http.server fixture 端到端验证并标注 "
                 "federated_via=mock_fixture(loopback)；真实跨厂商互联需对端在线并部署同规范"
                 "端点，不冒充真跨厂商。"),
    },
    {
        "id": "etsi",
        "name": "AI 计算平台安全（欧盟）",
        "standard": "ETSI TS 104 033",
        "status": "implemented",
        "evidence": ("governance.etsi.hardening_manifest / runtime_self_check / "
                     "LogIntegrityChain（data/audit_chain.jsonl）/ attestation_status + "
                     "docs/ETSI_TS104033_deployment.md + 控制台 /api/governance/etsi"),
        "note": ("应用层交付 systemd/Docker/seccomp 加固清单、运行时自检（探测不到标 "
                 "unknown+reason）、审计日志 sha256 哈希链（篡改可检出）。诚实边界：硬件固件"
                 "校验/TPM/安全启动/OS 级强隔离属部署层，需 TPM/UEFI + 第三方 attestation 服务，"
                 "attestation_status() 恒返回 attested=false，应用不自证；哈希链防篡改不防删除，"
                 "需日志外发/WORM。认证结论需第三方机构。"),
    },
    {
        "id": "hybrid_model",
        "name": "大小模型组合（前后置规则引擎）",
        "standard": "高精度场景选型建议",
        "status": "implemented",
        "evidence": ("governance.content_guard.guard_pipeline 接入 execution/daemon.py "
                     "_execute_llm 主链路（前端脱敏+注入拦截 → 大模型中段语义 → "
                     "check_output 后端 PII 回潮/注入回显）"),
        "note": ("规则引擎前端脱敏，脱敏后文本（而非原始 PII）与搜索词一起喂给大模型；"
                 "高危注入沿用既有文案直接拦；后端产出 PII 回潮阻断外发转人工；"
                 "result.content_guard 记录 input_risk/desensitize_findings/output_safe 供 UI "
                 "透明展示。端到端测试用注入式捕获引擎证明「脱敏后文本进模型」。"),
    },
    {
        "id": "data_in_domain",
        "name": "数据不出域",
        "standard": "涉密/强合规环境底线（用户明确豁免）",
        "status": "exempt",
        "evidence": "本次用户要求中已明确移除该项",
        "note": "用户在本轮修正中去掉了「彻底数据不出域」要求，故不计入本次合规目标。",
    },
    {
        "id": "verifiable",
        "name": "可验证优先",
        "standard": "核心业务数据选型原则",
        "status": "partial",
        "evidence": "各治理模块单测覆盖 + 自评矩阵逐条标注证据",
        "note": "能力可被测试与审查验证；但「合规认证与资质背书」需第三方机构出具，非代码自证。",
    },
]


def _count(status: str) -> int:
    return sum(1 for c in CAPABILITIES if c["status"] == status)


def self_assessment() -> Dict:
    """能力矩阵（不含无关统计）。"""
    return {
        "capabilities": CAPABILITIES,
        "generated_at": time.time(),
    }


def compliance_report(project_root: str = ".") -> Dict:
    """完整合规自评报告：统计 + 矩阵 + 缺口 + 认证边界声明。"""
    counts = {
        "implemented": _count("implemented"),
        "partial": _count("partial"),
        "designed": _count("designed"),
        "exempt": _count("exempt"),
    }
    gaps = [
        {"id": c["id"], "name": c["name"], "status": c["status"], "note": c["note"]}
        for c in CAPABILITIES if c["status"] in ("partial", "designed")
    ]
    return {
        "title": "SecTrust 协作审计台 · 合规自评报告",
        "generated_at": time.time(),
        "baseline": "国标委〔2026〕41号（六项强制能力）、GB/Z 185—2026、ETSI TS 104 033",
        "summary": counts,
        "total_evaluated": len(CAPABILITIES) - counts["exempt"],
        "capabilities": CAPABILITIES,
        "gaps": gaps,
        "certification_notice": (
            "本文件为产品**自评**，非合规认证结论。国标为强制性标准、ETSI 为欧盟准入，"
            "最终合规性须由具备资质的第三方检测/认证机构评定。本产品据此「可验证、经得起测」，"
            "但不能自我宣告通过认证。"
        ),
    }
