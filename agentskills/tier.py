"""
tier.py —— Skill 分级安全（W3，建议③）v2「更松、速度优先」版

用户拍板：**白名单边界要更松，保证速度**。本模块相对 v2 设计稿的放宽点：

  1. risk 门槛 low → **low + medium**（medium 的纯只读技能也能进 white）；
  2. **去掉「allowed_domains 为空 / 不出域」硬约束**（「彻底数据不出域」已被用户
     豁免）；改由运行期动作探测兜底（见下）；
  3. 只读工具范围扩大：`web_search` / `file_read` 等**只读意图**工具不再被当作
     副作用排除（white 执行层本就不发起任何网络/写动作，只激活 L2 正文）；
  4. 缺省 `security_tier="auto"` **自动判定**：满足硬条件的纯提示词技能自动进
     white，**无需手写 manifest.json**——内置 4 个纯提示词技能因此直接可用且免检；
  5. white 免 manifest 安装校验（纯提示词、无副作用，manifest 的权限/域名/审批
     声明对它无意义），只校验 SKILL.md 本身合格。

但「更松」不等于「不设防」——以下是**任何声明都不能突破的绝对底线**（违反即
强制 black，声明 white 也不例外）。这不是速度问题，是安全事故问题：

  · 带 `scripts/` 可执行脚本 → 强制 **>= gray**（绝不可 white，沙箱不可免）；
  · `allowed-tools` 含写/执行/删除/发送/发布/支付/改配置等副作用工具 → 强制 **black**；
  · `requires_approval=true` 或 `risk_level ∈ {high, critical}` → 强制 **black**；
  · 显式 `security_tier=black` → 强制 black，不可被 auto 提级（运维收紧通道）。

声明 white 但不满足硬条件 → **自动降级**（white→gray/black）并记录降级原因，
绝不静默放行；`security_tier` 未知取值不猜测，按 auto 处理。

分级与门的关系（每个 tier 过哪些门、免哪些，全部写进 audit / guard_decision Trace）：
  white : L1→L2 直执行（execution_mode=l2_only）；免 沙箱/密钥扫描/审批/证据/
         工件/质量；仍写 audit + 轻量输出 PII 回潮检查 + Trace。
  gray  : 过沙箱 + 审批(按 risk) + 证据 + 工件/质量；**免两段密钥扫描**（省时间）。
  black : 五层门全开 + 脚本 stdout 密钥扫描（新增）+ 审批 + 全程留痕（不放松）。

运行期越界探测（「白」是可撤销的信任）：white 技能若被请求执行 `scripts/`、
或声明了副作用工具却试图走 white 快速通道 → `escalate_on_action()` 立即降级
black + 拒绝 + 触发熔断计数 + 写 Trace，不因「上次是白」就放行。

零第三方依赖：仅标准库。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List

VALID_TIERS = ("white", "gray", "black", "auto")

# 副作用 / 高危工具：声明即强制 black（绝对底线②）。
# 注意 `web_search` / `file_read` / `search` 等**只读意图**工具不在此列（更松点3）。
SIDE_EFFECT_TOOLS = frozenset({
    "file_write", "write_files", "exec", "shell", "run_cmd", "bash",
    "delete", "remove", "send", "publish", "pay", "buy", "post",
    "merge", "push", "put", "chmod", "chown", "kill", "network_write",
    "db_write", "apply_patch", "overwrite",
})
_SIDE_EFFECT_PREFIXES = (
    "create_", "update_", "delete_", "remove_", "send_", "publish_",
    "push_", "merge_", "put_", "post_", "write_", "exec_", "high_risk",
    "set_", "drop_", "truncate_", "grant_",
)


def _field(spec, name, default=None):
    """鸭子类型取字段：兼容 StandardSkillSpec 对象与 load_l1_metadata() 的 dict。"""
    if isinstance(spec, dict):
        return spec.get(name, default)
    return getattr(spec, name, default)


def _is_side_effect(tool: str) -> bool:
    tl = str(tool).lower().strip()
    if tl in SIDE_EFFECT_TOOLS:
        return True
    return any(tl.startswith(p) for p in _SIDE_EFFECT_PREFIXES)


@dataclass
class TierDecision:
    tier: str                       # 最终生效 white/gray/black
    declared: str                   # 原始声明 auto/white/gray/black
    downgraded: bool = False
    reasons: List[str] = field(default_factory=list)
    checks: Dict[str, bool] = field(default_factory=dict)
    side_effect_tools: List[str] = field(default_factory=list)
    trust_tier: str = ""            # 方案④ 语义信任声明（意图参考，非安全判定依据）

    def to_dict(self) -> Dict:
        return {
            "tier": self.tier, "declared": self.declared,
            "downgraded": self.downgraded, "reasons": list(self.reasons),
            "checks": dict(self.checks),
            "side_effect_tools": list(self.side_effect_tools),
            "trust_tier": self.trust_tier,
        }


# 每个 tier 过哪些门（可审计：免检依据一目了然）
GATE_PLAN = {
    "white": {"install": "sk_md_only", "approval": False, "sandbox": False,
              "secret_scan": False, "evidence": False, "artifacts": False,
              "quality": False, "pii_check": True, "execution_mode": "l2_only"},
    "gray": {"install": "full", "approval": "by_risk", "sandbox": True,
             "secret_scan": False, "evidence": True, "artifacts": True,
             "quality": True, "pii_check": True, "execution_mode": "sandbox"},
    "black": {"install": "full", "approval": "by_risk", "sandbox": True,
              "secret_scan": True, "evidence": True, "artifacts": True,
              "quality": True, "pii_check": True, "execution_mode": "sandbox"},
}


def classify(spec) -> TierDecision:
    """按硬条件判定技能安全分级。spec 可为 StandardSkillSpec 或 L1 dict。

    绝不因「声明 white」就放行：硬条件不满足一律自动降级并记原因。
    """
    declared = str(_field(spec, "security_tier", "auto") or "auto")
    if declared not in VALID_TIERS:
        declared = "auto"          # 未知取值不猜（诚实边界）
    trust_tier = str(_field(spec, "trust_tier", "") or "")
    tools = [str(t) for t in (_field(spec, "allowed_tools", []) or [])]
    risk = str(_field(spec, "risk_level", "medium") or "medium")
    manifest = _field(spec, "manifest", {}) or {}
    requires_approval = (bool(manifest.get("requires_approval"))
                         if isinstance(manifest, dict) else False)
    has_scripts = bool(_field(spec, "has_scripts", False))

    side = sorted({t for t in tools if _is_side_effect(t)})
    high_risk = risk in ("high", "critical") or requires_approval
    checks = {
        "no_scripts": not has_scripts,
        "no_side_effect_tools": not side,
        "risk_acceptable": risk in ("low", "medium"),   # 更松点1：medium 可 white
        "no_approval_required": not high_risk,
    }

    # auto 判定：绝对路径只有「无脚本 + 无副作用 + low/medium + 不需审批」才是 white
    if side or high_risk:
        auto_tier = "black"
    elif has_scripts:
        auto_tier = "gray"
    else:
        auto_tier = "white"

    reasons: List[str] = []
    downgraded = False
    if declared == "auto":
        tier = auto_tier
        reasons.append("auto 自动判定（无显式声明）")
        if auto_tier == "white":
            reasons.append("满足 white 硬条件：无脚本 + 无副作用工具 + "
                           "risk∈{low,medium} + 不需审批")
        elif auto_tier == "gray":
            reasons.append("带 scripts/ 可执行脚本：强制 >= gray（沙箱不可免）")
        else:
            reasons.append("命中绝对底线（副作用工具或高危/需审批）：强制 black")
    elif declared == "black":
        tier = "black"
        reasons.append("运维显式声明 black（不可被 auto 提级）")
    elif declared == "white":
        if auto_tier == "white":
            tier = "white"
            reasons.append("显式 white 且硬条件复核通过")
        else:
            tier = auto_tier
            downgraded = True
            reasons.append("声明 white 但硬条件不满足，自动降级为 %s" % auto_tier)
            if not checks["no_scripts"]:
                reasons.append("带 scripts/：white 不可执行脚本")
            if side:
                reasons.append("含副作用工具：%s" % "、".join(side))
            if high_risk:
                reasons.append("risk=%s / requires_approval=%s" % (risk, requires_approval))
    else:  # declared == "gray"
        if auto_tier == "black":
            tier = "black"
            downgraded = True
            reasons.append("声明 gray 但命中绝对底线（副作用/高危），强制 black")
        else:
            tier = "gray"
            reasons.append("显式 gray（收紧通道，即使 auto=white 也保持 gray）")

    if trust_tier and trust_tier in ("high", "medium", "low"):
        reasons.append("trust_tier 意图声明=%s（仅供参考，安全分级以硬条件判定为准）"
                       % trust_tier)

    return TierDecision(tier=tier, declared=declared, downgraded=downgraded,
                        reasons=reasons, checks=checks,
                        side_effect_tools=side, trust_tier=trust_tier)


def gate_plan(tier: str) -> Dict:
    """该 tier 的门计划（供 guard_chain 与 audit 使用）。未知 tier 一律最严。"""
    return dict(GATE_PLAN.get(tier, GATE_PLAN["black"]))


def escalate_on_action(tier: str, action: str) -> tuple:
    """运行期越界探测：返回 (是否越界, 原因)。

    white 技能一旦被请求 `run_script`（执行脚本）即视为越界——「白」是可撤销的信任。
    调用方应据此立即降级 black + 拒绝 + 触发熔断 + 写 Trace。
    """
    if tier != "white":
        return False, ""
    if action in ("run_script", "exec", "shell", "file_write", "write_files"):
        return (True, "white 技能请求执行副作用动作 %s：越界，立即降级 black "
                      "并拒绝（白是可撤销的信任）" % action)
    return False, ""
