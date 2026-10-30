"""
guard_chain.py —— 把审批闸门 + EvidenceGate + QualityGate 挂到 Skill 调用链（B3），
v2 增加分级安全（W3）与全链路 Trace（W4）/ 状态回写（W2）。

B1 的 `StandardSkillLibrary.run_script()` 已经保证脚本**必须走 sandbox/**，但
"脚本跑完"不等于"任务做完了"：AI 完全可能跑一个空脚本然后说"Done!"。
本模块在脚本产出之上再叠硬门，落实"默认不信任 AI"。

分级安全（v2，用户拍板「白名单更松、保证速度」，详见 agentskills/tier.py）：
  white : 无脚本 + 无副作用 + risk∈{low,medium} + 不需审批（auto 自动判定，
          免 manifest 安装校验）→ **L2 正文快速通道**：免沙箱/密钥扫描/审批/
          证据/工件/质量，仅 SKILL.md 校验 + L2 正文 + 轻量 PII 回潮 + 全程留痕。
  gray  : 带脚本但只读 → 审批(按 risk) → 沙箱实跑 → 证据门；**免工件/质量门**
          （只读脚本不产出交付工件，省时间）；仍过沙箱（脚本不可免沙箱）。
  black : 副作用工具 / high/critical / requires_approval / 显式 black →
          **五层门全开**（审批 → 沙箱实跑证据 → 工件齐备 → 质量门），与 B3 行为
          完全一致，不放松任何一道。

绝对底线（任何声明不可突破）：带 scripts/ 强制 >= gray（不可 white）；副作用
工具/高危/需审批强制 black；white 技能被请求执行脚本 → 运行期**越界降级 black
并拒绝**（「白」是可撤销的信任，见 tier.escalate_on_action）。

四层/五层门任一不过 → 返回 `ok=False` + 具体 stage/原因，**绝不偷偷标记完成**。

审计 / 可观测（v2）：
  · 每次执行（含被拒）写 `audit`（含 tier/gate_plan/waived_gates 免检依据）；
  · 有 trace 上下文时写 `skill_run` + **`guard_decision`**（stages 泳道 + blocked_by
    + rule_id + decision），供 W4 仪表盘复现「谁拦的、命中哪条规则」；
  · 注入 `state`（SkillStateStore）时回写成功/失败/驳回 + 生成 feedback_hint，
    构成反馈闭环。缺 trace/state 上下文如实标注未写，不硬造孤儿轨迹。

诚实边界：
  · 证据门核验的是"脚本实跑输出"的形态与新鲜度，不代替对外部副作用的活体验证；
  · 工件齐备检查的是"调用方提供了非空工件内容"，不做语义审核（语义审核需
    校验模型/人工，代码不冒充已做）；
  · 审批只放行"执行许可"，不批准具体产出内容；
  · white 的 "L2 正文激活" 是技能定义交付，模型驱动的下游执行归 AgentRunner，
    不在此越界、不宣称端到端跑通。
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Dict, List, Optional

from .evidence import Evidence, EvidenceGate
from .quality import QualityGate
from .skill_md import StandardSkillLibrary
from .tier import classify, escalate_on_action, gate_plan
from .error_structs import ErrorInfo


def _declared_artifacts(stdout: str) -> List[str]:
    """从脚本 JSON 输出里读它声明的必检工件（脚本自报，不是已完成的证明）。"""
    try:
        data = json.loads(stdout or "")
    except (ValueError, TypeError):
        return []
    arts = data.get("required_artifacts") if isinstance(data, dict) else None
    return [str(a) for a in arts if str(a).strip()] if isinstance(arts, list) else []


class SkillExecutionGuard:
    """Skill 执行的分级门禁（white 快速通道 / gray 三层 / black 五层）。"""

    def __init__(self, library: Optional[StandardSkillLibrary] = None,
                 evidence_gate: Optional[EvidenceGate] = None,
                 quality_gate: Optional[QualityGate] = None,
                 approvals=None, trace_store=None, trace_id: str = "",
                 state=None):
        self.library = library or StandardSkillLibrary()
        self.evidence_gate = evidence_gate or EvidenceGate()
        self.quality_gate = quality_gate or QualityGate()
        self.approvals = approvals
        # 审计轨迹：每次技能执行（含被拒）记 skill_run + guard_decision 事件；
        # 缺 trace 上下文时如实标注未写，不硬造孤儿轨迹。
        self.trace_store = trace_store
        self.trace_id = trace_id
        # 运行时状态（反馈闭环）：可选注入，None 时不影响既有行为
        self.state = state

    @staticmethod
    def _needs_approval(spec) -> bool:
        m = spec.manifest or {}
        return (bool(m.get("requires_approval"))
                or str(spec.risk_level).lower() in ("high", "critical"))

    def _approval_gate(self, spec, name: str, rel_path: str,
                       approval_id: str, actor: str) -> tuple:
        """返回 (放行?, 原因, 新审批单id)。"""
        if not self._needs_approval(spec):
            return True, "", ""
        if self.approvals is None:
            return (False, "技能 %s 需要审批（manifest/risk_level），但未配置审批中心"
                    "（fail-closed 拒绝）" % name, "")
        if approval_id:
            try:
                if self.approvals.is_approved(approval_id):
                    return True, "", ""
            except Exception:
                pass
            return False, "审批单 %s 尚未批准" % approval_id, ""
        try:
            aid = self.approvals.request(
                "skill.run",
                json.dumps({"skill": name, "script": rel_path}, ensure_ascii=False),
                channel="agentskills", user_id=actor)
        except Exception as e:  # noqa: BLE001
            return (False, "发起审批失败（fail-closed 拒绝）：%s: %s"
                    % (type(e).__name__, e), "")
        return (False, "技能 %s 需人工审批；审批单 %s 已挂起（默认不信任 AI）"
                % (name, aid), aid)

    # ------------------------------------------------------------------
    # 审计 / Trace / 状态回写
    # ------------------------------------------------------------------
    def _emit_audit(self, out: Dict) -> Dict:
        """每次执行（含被各道门拒绝）都写审计摘要；有 trace 上下文再落轨迹。"""
        digest = hashlib.sha256(
            (out.get("script_stdout") or "").encode("utf-8", "replace")
        ).hexdigest()[:16]
        stages = out.get("stages") or []
        tier = out.get("tier", "black")
        plan = out.get("gate_plan") or {}
        decision = ("allow" if out["ok"] else
                    ("hold" if out["stage"] in
                     ("approval", "install", "registry", "tier_escalation", "pii_check")
                     else "reject"))
        blocked_by = out.get("blocked_by") or (out["stage"] if not out["ok"] else "")
        waived = ([k for k, v in plan.items() if v is False]
                  if tier == "white" else [])
        audit = {
            "skill": out["skill"], "script": out["script"],
            "stage": out["stage"], "ok": out["ok"],
            "sandboxed": out["sandboxed"], "backend": out["backend"],
            "containment": out["containment"],
            "declared_artifacts": out["declared_artifacts"],
            "stdout_digest": digest,       # 只留指纹，不存脚本原始输出
            "duration_ms": out["duration_ms"],
            "cost_estimate": None,
            "cost_note": "无计费器接入，未估算成本（如实标注，不编造数字）",
            # ---- v2 分级 / 可观测 ----
            "tier": tier, "gate_plan": plan,
            "tier_decision": out.get("tier_decision", {}),
            "execution_mode": out.get("execution_mode", "sandbox"),
            "decision": decision, "blocked_by": blocked_by,
            "rule_id": out.get("rule_id", ""), "stages": stages,
            "waived_gates": waived,        # white 免检依据，可审计、可撤销
        }
        out["audit"] = audit

        # 反馈闭环：回写状态（驳回=安全策略生效，不计入执行失败熔断）
        if self.state is not None:
            rejected = (not out["ok"]) and out["stage"] in (
                "approval", "install", "registry", "tier_escalation", "pii_check")
            try:
                self.state.record(out["skill"], bool(out["ok"]),
                                  stage=out["stage"], reason=out.get("reason", ""),
                                  rejected=rejected)
                out["feedback_hint"] = self.state.hint_for(out["skill"])
            except Exception:  # noqa: BLE001 - 状态记账是旁路，失败不挡执行
                out["feedback_hint"] = ""

        # ③ 结构化错误：失败路径统一收敛 ErrorInfo（供主循环自我反思 / 开发者定位）
        if not out["ok"]:
            info = ErrorInfo.build(out["stage"], out.get("reason", ""),
                                   out.get("rule_id", ""), kind="guard")
            out["error_info"] = info.to_dict()
            # ③ 开发者告警：同一技能同一技术细节重复失败 → 优化建议日志
            if self.state is not None:
                try:
                    self.state.record_error_signature(
                        out["skill"], info.technical_detail)
                except Exception:  # noqa: BLE001
                    pass

        if self.trace_store is None or not self.trace_id:
            audit["trace_written"] = False
            audit["trace_note"] = ("未提供 trace_store/trace_id，未硬造孤儿轨迹；"
                                   "调用摘要仍在 result.audit 中可查")
            return out
        try:
            self.trace_store.record(
                self.trace_id, "skill_run", task_id="", actor="agentskills",
                summary="Skill 执行 %s/%s → %s（tier=%s）"
                        % (out["skill"], out["script"], out["stage"], tier),
                detail=audit, content=out.get("script_stdout") or "")
            # W4：门禁决策白盒（哪道门 / 命中哪条规则 / 放行或拦）
            self.trace_store.record(
                self.trace_id, "guard_decision", task_id="", actor="agentskills",
                summary="门禁 %s → %s（%s）" % (tier, decision, blocked_by or "-"),
                detail={"target": "skill:%s" % out["skill"], "tier": tier,
                        "check_mode": out.get("execution_mode", "sandbox"),
                        "stages": stages, "blocked_by": blocked_by,
                        "rule_id": audit["rule_id"], "decision": decision,
                        "gate_plan": plan, "waived_gates": waived,
                        "bypassed_gates": waived,   # 语义同 waived_gates，便于白盒检索
                        "trust_tier": (out.get("tier_decision") or {})
                                     .get("declared", "")},
                content=None)
            audit["trace_written"] = True
        except Exception as e:  # noqa: BLE001 - 审计是旁路，失败不挡执行结果
            audit["trace_written"] = False
            audit["trace_error"] = "%s: %s" % (type(e).__name__, e)
        return out

    # ------------------------------------------------------------------
    def execute(self, name: str, rel_path: str, args: Optional[List[str]] = None,
                *, approval_id: str = "", actor: str = "operator",
                claim: str = "", artifacts: Optional[Dict[str, str]] = None,
                quality_rules: Optional[Dict] = None, sandbox=None,
                timeout: float = 30.0, security_tier: str = "") -> Dict:
        """包装层：所有返回点（含拒绝）统一写审计后再返回。

        security_tier 可选显式覆盖（运维收紧），留空则按 spec 硬条件自动判定。
        """
        return self._emit_audit(self._execute_inner(
            name, rel_path, args, approval_id=approval_id, actor=actor,
            claim=claim, artifacts=artifacts, quality_rules=quality_rules,
            sandbox=sandbox, timeout=timeout, security_tier=security_tier))

    def _execute_inner(self, name: str, rel_path: str,
                       args: Optional[List[str]] = None,
                       *, approval_id: str = "", actor: str = "operator",
                       claim: str = "", artifacts: Optional[Dict[str, str]] = None,
                       quality_rules: Optional[Dict] = None, sandbox=None,
                       timeout: float = 30.0, security_tier: str = "") -> Dict:
        """分级执行技能。返回结构化结果（ok=False 时绝不视为完成）。"""
        started = time.time()
        out: Dict = {"ok": False, "stage": "", "reason": "", "skill": name,
                     "script": rel_path, "approval_id": "", "sandboxed": False,
                     "backend": "", "containment": "", "declared_artifacts": [],
                     "evidence": None, "quality": None, "artifacts_checked": {},
                     "script_stdout": "", "duration_ms": 0,
                     # v2 分级 / 可观测
                     "stages": [], "tier": "black", "gate_plan": {},
                     "tier_decision": {}, "decision": "reject", "blocked_by": "",
                     "rule_id": "", "execution_mode": "sandbox", "body_bytes": 0}

        def st(stage: str, ok: bool, note: str = "", rule_id: str = ""):
            out["stages"].append({
                "stage": stage, "ok": bool(ok), "note": str(note)[:200],
                "rule_id": rule_id,
                "duration_ms": round((time.time() - started) * 1000, 1)})
            if not ok:
                out["blocked_by"] = stage
                out["rule_id"] = rule_id

        spec = self.library.get(name)
        if spec is None:
            out.update(stage="registry", reason="未知技能：%s" % name)
            st("registry", False, out["reason"])
            return out

        # ---- 分级判定（显式覆盖仅用于收紧，声明 white 不过硬条件会被引擎降级）----
        dec = classify(spec)
        if security_tier in ("white", "gray", "black") and security_tier != "auto":
            # 显式传参只支持收紧：black > gray > white 中取更严者
            order = {"white": 0, "gray": 1, "black": 2}
            if order.get(security_tier, 2) > order.get(dec.tier, 2):
                dec.tier = security_tier
                dec.reasons.append("调用方显式收紧为 %s" % security_tier)
        out["tier"] = dec.tier
        out["tier_decision"] = dec.to_dict()
        out["gate_plan"] = gate_plan(dec.tier)
        st("tier", True, "分级=%s（声明=%s，降级=%s）"
           % (dec.tier, dec.declared, dec.downgraded))

        # ============ white：L2 正文快速通道（免五层门 + 免扫描，留痕）============
        if dec.tier == "white":
            out["execution_mode"] = "l2_only"
            # 越界探测：white 不含脚本，也不允许被请求执行脚本
            if rel_path or args:
                _, why = escalate_on_action("white", "run_script")
                out.update(stage="tier_escalation", reason=why)
                st("tier_escalation", False, why, "tier.white_script")
                return out
            # 安装校验：white 免 manifest，只校验 SKILL.md 本身合格
            if not spec.ok:
                out.update(stage="install",
                           reason="SKILL.md 校验未通过：%s" % "；".join(spec.problems))
                st("install", False, out["reason"])
                return out
            st("install", True, "white 免 manifest 安装校验：仅校验 SKILL.md 合格")
            if spec.has_scripts:
                _, why = escalate_on_action("white", "run_script")
                out.update(stage="tier_escalation", reason=why)
                st("tier_escalation", False, why, "tier.white_script")
                return out
            # L2 正文激活（渐进式披露第二段）
            try:
                body = self.library.load_l2_body(name)
            except Exception as e:  # noqa: BLE001
                out.update(stage="l2", reason="L2 正文加载失败：%s: %s"
                           % (type(e).__name__, e))
                st("l2", False, out["reason"])
                return out
            st("l2", True, "L2 正文 %d 字节" % len(body))
            # 轻量输出 PII 回潮检查（免检不全免）
            try:
                from governance import content_guard as _cg
                chk = _cg.check_output(body)
            except Exception as e:  # noqa: BLE001 - 检查器异常 fail-closed
                chk = {"safe": False, "issues": [{"type": "检查器异常",
                                                  "detail": str(e)}]}
            if not chk.get("safe", True):
                out.update(stage="pii_check",
                           reason="white 正文 PII 回潮检查未通过：%s"
                                  % chk.get("issues"))
                st("pii_check", False, out["reason"], "content_guard.pii")
                return out
            st("pii_check", True, "safe（无 PII 回潮 / 注入指令回显）")
            out.update(ok=True, stage="ok", reason="",
                       body_bytes=len(body.encode("utf-8")),
                       duration_ms=round((time.time() - started) * 1000, 1))
            st("ok", True, "white 快速通道完成（免沙箱/密钥扫描/审批/证据/工件/质量）")
            return out

        # ============ gray / black：走安装校验 + 审批 + 沙箱 + 证据（+ black 工件/质量）============
        inst = self.library.install_validate(name)
        if not inst["ok"]:
            out.update(stage="install", reason="安装期校验未通过：%s"
                       % "；".join(inst["problems"]))
            st("install", False, out["reason"])
            return out
        st("install", True, "manifest 安装期校验通过")

        # 1) 审批门
        ok, why, aid = self._approval_gate(spec, name, rel_path,
                                           approval_id, actor)
        out["approval_id"] = aid
        if not ok:
            out.update(stage="approval", reason=why)
            st("approval", False, why, "approval.not_approved")
            return out
        st("approval", True, why or "无需审批或已批准")

        # 2) 沙箱实跑（系统亲自跑，Agent 无法伪造输出）
        try:
            res = self.library.run_script(name, rel_path, args=args,
                                          sandbox=sandbox, timeout=timeout)
        except Exception as e:  # noqa: BLE001 - 路径/沙箱拒绝如实返回
            out.update(stage="sandbox", reason="%s: %s" % (type(e).__name__, e))
            st("sandbox", False, out["reason"])
            return out
        out.update(sandboxed=bool(res.get("sandboxed")),
                   backend=str(res.get("backend") or ""),
                   containment=str(res.get("containment") or ""),
                   script_stdout=str(res.get("stdout") or ""))
        if not res.get("ok"):
            out.update(stage="sandbox", reason="脚本执行失败：%s"
                       % (res.get("error") or "退出码非 0"))
            st("sandbox", False, out["reason"])
            return out
        st("sandbox", True, "沙箱实跑 ok（backend=%s）" % out["backend"])

        # 3) 证据门：用实跑输出构造 Evidence（不是 AI 自报）
        # command 必须是纯命令形态：looks_like_command() 会拒绝含中文的串，
        # 故参数不进 command（放 claim 里留痕），只保留可执行的脚本路径。
        ev = Evidence(
            command="python " + str(rel_path).replace("\\", "/"),
            exit_code=0 if res.get("ok") else 1,
            stdout=out["script_stdout"],
            stderr=str(res.get("error") or ""),
            claim=claim or ("技能脚本 %s 实跑完成（args=%s）"
                            % (rel_path, list(args or []))),
            task_id="skill:%s" % name,
            ran_at=time.time(),
        )
        verdict = self.evidence_gate.verify(ev)
        self.evidence_gate.record(ev, verdict)  # 驳回也留痕
        out["evidence"] = verdict.to_dict()
        if not verdict.accepted:
            out.update(stage="evidence", reason="证据门未通过：%s"
                       % "；".join(verdict.reasons))
            st("evidence", False, out["reason"], "evidence.missing_or_stale")
            return out
        st("evidence", True, "证据门通过（实跑输出可验证）")

        # gray：只读脚本不产出交付工件 → 免工件/质量门（分级提速，仍过沙箱+证据）
        out["declared_artifacts"] = _declared_artifacts(out["script_stdout"])
        if dec.tier == "gray":
            st("artifacts", True, "gray 免工件门（只读脚本无交付工件）")
            st("quality", True, "gray 免质量门（只读脚本无交付工件）")
            out.update(ok=True, stage="ok", reason="",
                       duration_ms=round((time.time() - started) * 1000, 1))
            st("ok", True, "gray 门禁完成（审批→沙箱→证据）")
            return out

        # black：4) 工件齐备 —— 脚本声明的必检工件必须有非空实际内容
        declared = out["declared_artifacts"]
        provided = artifacts or {}
        checked = {a: bool(str(provided.get(a) or "").strip()) for a in declared}
        out["artifacts_checked"] = checked
        missing = [a for a, ok2 in checked.items() if not ok2]
        if missing:
            out.update(stage="artifacts",
                       reason="缺少必检工件的实际内容：%s（脚本声明 ≠ 已交付，"
                              "默认不信任 AI）" % "、".join(missing))
            st("artifacts", False, out["reason"], "artifacts.missing")
            return out
        st("artifacts", True, "必检工件齐备（%d 项）" % len(declared))

        # black：5) 质量门（禁 TODO/待补充/未填槽位）
        rules = dict(quality_rules or {})
        rules.setdefault("min_chars", 20)
        rules.setdefault("no_unfilled", True)
        rules.setdefault("forbidden", ["TODO", "待补充", "lorem ipsum"])
        rules.setdefault("required", declared)
        # 工件名与内容一起送检：required 检查的是"必含工件名"，内容另查
        # min_chars / forbidden / no_unfilled，二者不可偏废。
        content = ("\n".join("%s: %s" % (a, str(provided.get(a) or ""))
                             for a in declared)
                   or out["script_stdout"])
        qv = self.quality_gate.check_output(content, rules)
        out["quality"] = qv.to_dict()
        if not qv.accepted:
            out.update(stage="quality", reason="质量门未通过：%s"
                       % "；".join(qv.violations))
            st("quality", False, out["reason"], "quality.violation")
            return out
        st("quality", True, "质量门通过")

        out.update(ok=True, stage="ok", reason="",
                   duration_ms=round((time.time() - started) * 1000, 1))
        st("ok", True, "black 五层门全通过")
        return out
