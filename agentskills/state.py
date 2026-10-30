"""
state.py —— Skill 运行时状态治理与自适应熔断（W2，建议②）

解决的问题：现有架构「按需触发、每次调用独立」，AI 不知道某个技能之前有没有
被调用过、失败或被驳回过，于是同一个坏技能会被反复选中、反复消耗 Token 与算力。

三层状态（语义别混）：
  · 会话内计数   —— 内存 dict[session_id][skill]（成功/失败/驳回/连续失败/最后阶段）
  · 跨会话熔断   —— **复用** `mcp_adapter.breaker.TTLCircuitBreaker`，把「server」
                     维度泛化成「skill」维度（path=data/skill_breaker.json，
                     threshold=3 连续失败自动打开、TTL 到期半开探测、落盘进程无关、
                     损坏备份 .corrupt）。「连续失败3次标不可用」几乎现成，不另写。
  · 全局紧急熔断 —— `governance.circuit_breaker`（人手动、fail-open），与本模块无关。

反馈闭环：门禁驳回/执行失败 → `record()` 回写状态 → 生成结构化 `feedback_hint`
（「上次在 X 阶段被驳回，建议尝试备选」）→ 回灌两处：
  ① 下一轮 `SkillRouter` 的 Rerank（history_map 降权该技能、连续失败惩罚）；
  ② daemon 任务上下文 / result.feedback_hint，让 AI 后续能自我纠正。
反馈是**提示**，不是自动改道：备选技能仍要过检索 + 角色白名单 + 门禁，
不因「上次失败」绕过任何安全检查。

诚实边界：
  · 熔断是降权 / TTL 半开 / 可人工 reset，**不是永久删除**（一次环境抖动不该
    永久拉黑）；驳回（门禁拦截）不计入「执行失败」熔断——那是安全策略生效，
    不是技能故障。
  · 跨会话记忆 = 落盘熔断计数（TTL 时间衰减），会话内 = 内存计数；不引入
    LLM 记忆，不宣称「AI 真的记住了」。
  · Trace 只在提供 trace_store/trace_id 时写 `skill_state` 事件，缺上下文如实
    标注未写，不硬造孤儿轨迹。

零第三方依赖：标准库 + 仓库既有 TTLCircuitBreaker。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Dict, Optional

from mcp_adapter.breaker import TTLCircuitBreaker

DEFAULT_BREAKER_PATH = "data/skill_breaker.json"

# 缓存参数时按**键名**判定敏感（与 trace.REDACT_KEYS 同口径，复制避免跨包耦合）。
# 命中敏感键的值不落库，只记键名，绝不缓存密钥/令牌原文。
_SENSITIVE_KEYS = (
    "secret", "token", "cookie", "password", "passwd", "credential",
    "api_key", "apikey", "private_key", "authorization", "bearer",
    "access_key", "signature", "otp", "session",
)

# 同一技能因同一技术细节失败达到此次数 → 触发优化建议日志（反馈学习模块③）。
ERR_SIG_THRESHOLD = 3


def _is_sensitive_key(key: str) -> bool:
    k = str(key).lower()
    return any(s in k for s in _SENSITIVE_KEYS)


@dataclass
class SkillState:
    skill: str
    session_id: str = "default"
    success: int = 0
    fail: int = 0
    rejected: int = 0
    consecutive_failures: int = 0
    last_stage: str = ""
    last_error: str = ""
    last_at: float = 0.0
    # 会话内缓存的「已获取环境变量」（非敏感键/值），供上下文注入避免 AI 重复询问
    cached_params: Dict[str, str] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return self.success + self.fail + self.rejected

    @property
    def success_rate(self) -> float:
        # 无历史时中性 0.5（不因「没用过」被无端降权或抬举）
        return self.success / self.total if self.total else 0.5

    def status(self, breaker_open: bool = False) -> str:
        if breaker_open:
            return "circuit_open"
        if self.consecutive_failures >= 3:
            return "circuit_open"
        if self.consecutive_failures > 0:
            return "degraded"
        if self.rejected > 0 and self.success == 0:
            return "rejected"
        return "ok"

    def to_dict(self, breaker_open: bool = False) -> Dict:
        return {
            "skill": self.skill, "session_id": self.session_id,
            "status": self.status(breaker_open), "success": self.success,
            "fail": self.fail, "rejected": self.rejected,
            "consecutive_failures": self.consecutive_failures,
            "success_rate": round(self.success_rate, 4),
            "last_stage": self.last_stage, "last_error": self.last_error[:200],
            "last_at": self.last_at, "breaker_open": breaker_open,
            "cached_params": {k: ("<redacted>"
                                  if _is_sensitive_key(k) else v)
                              for k, v in self.cached_params.items()},
        }


def feedback_hint(skill: str, state: "SkillState", status: str) -> str:
    """把门禁驳回 / 失败 / 熔断信息生成结构化提示（反馈闭环）。"""
    if status == "circuit_open":
        return ("技能「%s」连续失败 %d 次已触发熔断（TTL 半开前不再消耗 Token）；"
                "建议改用备选技能。备选仍需过检索 + 角色白名单 + 门禁，不因上次失败"
                "绕过安全。" % (skill, state.consecutive_failures))
    if status == "rejected":
        return ("上次执行技能「%s」在【%s】阶段被门禁驳回（原因：%s）；"
                "是否尝试修正参数或改用备选技能？备选仍需过完整检索与门禁。"
                % (skill, state.last_stage or "unknown",
                   (state.last_error or "无详情")[:120]))
    if status == "degraded":
        return ("技能「%s」最近在【%s】阶段失败（连续 %d 次，原因：%s）；"
                "已降权，建议检查参数或改用备选。"
                % (skill, state.last_stage or "unknown",
                   state.consecutive_failures,
                   (state.last_error or "无详情")[:120]))
    return ""


class SkillStateStore:
    """会话内状态 + 跨会话熔断 + 反馈闭环 + skill_state Trace。"""

    def __init__(self, breaker: Optional[TTLCircuitBreaker] = None,
                 session_id: str = "default", trace_store=None, trace_id: str = ""):
        self.breaker = breaker or TTLCircuitBreaker(
            path=DEFAULT_BREAKER_PATH, threshold=3, ttl=60.0)
        self.session_id = session_id or "default"
        self._sessions: Dict[str, Dict[str, SkillState]] = {}
        self._lock = threading.RLock()
        self.trace_store = trace_store
        self.trace_id = trace_id
        # 反馈学习③：同一技能同一技术细节失败计数（达阈值触发优化建议）
        self._err_sig: Dict[tuple, int] = {}

    # ---- 内部 ----
    def _state(self, skill: str, session_id: Optional[str] = None) -> SkillState:
        sid = session_id or self.session_id
        sess = self._sessions.setdefault(sid, {})
        if skill not in sess:
            sess[skill] = SkillState(skill=skill, session_id=sid)
        return sess[skill]

    def _breaker_open(self, skill: str) -> bool:
        try:
            return bool(self.breaker.state(skill).get("open"))
        except Exception:  # noqa: BLE001 - 熔断状态读失败按未熔断（不静默拉黑）
            return False

    def allow(self, skill: str) -> tuple:
        """L1 剔除用：(是否放行, 原因)。"""
        try:
            ok, why, st = self.breaker.allow(skill)
            return ok, why
        except Exception as e:  # noqa: BLE001
            return True, "熔断状态不可用（放行，不静默拉黑）：%s" % e

    # ---- 写：成败 / 驳回 ----
    def record(self, skill: str, ok: bool, stage: str = "", reason: str = "",
               rejected: bool = False, session_id: Optional[str] = None) -> Dict:
        """回写状态 + 熔断计数 + 生成反馈提示 + 写 skill_state Trace。"""
        with self._lock:
            st = self._state(skill, session_id)
            before = st.status(self._breaker_open(skill))
            if ok:
                st.success += 1
                st.consecutive_failures = 0
                try:
                    self.breaker.record_success(skill)
                except Exception:  # noqa: BLE001 - 熔断记账是旁路
                    pass
            elif rejected:
                # 门禁驳回 = 安全策略生效，不算执行故障，不计入连续失败熔断
                st.rejected += 1
            else:
                st.fail += 1
                st.consecutive_failures += 1
                try:
                    self.breaker.record_failure(skill, reason or stage or "执行失败")
                except Exception:  # noqa: BLE001
                    pass
            st.last_stage = str(stage or "")
            st.last_error = str(reason or "")
            st.last_at = time.time()
            after = st.status(self._breaker_open(skill))
            hint = feedback_hint(skill, st, after)
            self._emit_state(skill, before, after, st, hint)
            out = st.to_dict(self._breaker_open(skill))
            out["hint"] = hint
            return out

    def _emit_state(self, skill: str, before: str, after: str,
                    st: "SkillState", hint: str) -> None:
        if self.trace_store is None or not self.trace_id:
            return   # 缺上下文不硬造孤儿轨迹
        try:
            self.trace_store.record(
                self.trace_id, "skill_state", task_id="", actor="agentskills",
                summary="技能 %s 状态 %s → %s" % (skill, before, after),
                detail={"skill": skill, "from": before, "to": after,
                        "success": st.success, "fail": st.fail,
                        "rejected": st.rejected,
                        "consecutive_failures": st.consecutive_failures,
                        "success_rate": round(st.success_rate, 4),
                        "last_stage": st.last_stage,
                        "last_error": st.last_error[:200],
                        "hint": hint[:300]},
                content=None)
        except Exception:  # noqa: BLE001 - 审计旁路，失败不挡执行
            pass

    # ---- 读 ----
    def history_map(self, session_id: Optional[str] = None) -> Dict[str, Dict]:
        """给 Rerank 用：{name: {success_rate, consecutive_failures, circuit_open}}。"""
        sid = session_id or self.session_id
        with self._lock:
            names = set(self._sessions.get(sid, {}))
            try:
                names.update(self.breaker.snapshot().keys())
            except Exception:  # noqa: BLE001
                pass
            out = {}
            for name in names:
                st = self._state(name, sid)
                out[name] = {
                    "success_rate": round(st.success_rate, 4),
                    "consecutive_failures": st.consecutive_failures,
                    "circuit_open": self._breaker_open(name),
                }
            return out

    def snapshot(self) -> Dict:
        with self._lock:
            names = set()
            for sess in self._sessions.values():
                names.update(sess.keys())
            try:
                names.update(self.breaker.snapshot().keys())
            except Exception:  # noqa: BLE001
                pass
            skills = {}
            for name in sorted(names):
                st = self._state(name)
                skills[name] = st.to_dict(self._breaker_open(name))
            return {
                "skills": skills, "session_id": self.session_id,
                "threshold": self.breaker.threshold, "ttl": self.breaker.ttl,
                "breaker_path": self.breaker.path,
            }

    def hint_for(self, skill: str, session_id: Optional[str] = None) -> str:
        """取某技能当前状态的自我纠正提示（无异常返回空串）。"""
        with self._lock:
            st = self._state(skill, session_id)
            return feedback_hint(skill, st, st.status(self._breaker_open(skill)))

    # ---- ② 会话记忆：cached_params 参数缓存（避免 AI 重复询问）----
    def cache_params(self, skill: str, params: Optional[Dict]) -> None:
        """把「已获取的环境变量」记入会话状态。

        诚实边界：按**键名**判敏感，命中敏感键（secret/token/...）的值不入库，
        只记键名；绝不缓存密钥/令牌原文。None / 空不动作。
        """
        if not params:
            return
        with self._lock:
            st = self._state(skill)
            for k, v in dict(params).items():
                if _is_sensitive_key(k):
                    # 敏感键仍记键名占位（让 AI 知道「该参数之前已提供过」），
                    # 但值标记为已脱敏，绝不落明文。
                    st.cached_params[str(k)] = "<redacted>"
                else:
                    st.cached_params[str(k)] = str(v)

    def get_cached_params(self, skill: str, session_id: Optional[str] = None) -> Dict[str, str]:
        """取出某技能会话内缓存的「已获取环境变量」（供上下文注入）。

        返回的是原始缓存（敏感键为 <redacted>），调用方据此**只注入键名**
        提示 AI「该参数已提供过」，而非把脱敏占位当真实值下发。
        """
        with self._lock:
            st = self._state(skill, session_id)
            return dict(st.cached_params)

    # ---- ③ 反馈学习：错误签名监控（开发者告警）----
    def record_error_signature(self, skill: str, technical_detail: str) -> Dict:
        """记录「技能 + 技术细节」失败签名；达阈值返回优化建议，否则返回空。

        诚实边界：这是**会话内** best-effort 计数（不持久化、不跨会话），达阈值
        仅打 `skill_optimization_suggestion` 日志，不构成自动改道或拦截，也不
        伪造「已分析根因」的结论。
        """
        key = (str(skill), str(technical_detail or "?"))
        with self._lock:
            self._err_sig[key] = self._err_sig.get(key, 0) + 1
            count = self._err_sig[key]
            if count == ERR_SIG_THRESHOLD:
                import logging
                logging.warning(
                    "skill_optimization_suggestion: 技能 %s 已连续 %d 次因技术细节 "
                    "[%s] 失败，建议检查技能定义（参数 schema / 命令路径 / 依赖）",
                    skill, count, technical_detail)
                return {"skill": skill, "technical_detail": technical_detail,
                        "count": count,
                        "suggestion": ("同一技术细节重复失败，建议检查技能定义")}
        return {}

    def reset(self, skill: str) -> Dict:
        """人工 / 新版本技能时解除熔断（避免一次环境抖动永久拉黑）。"""
        with self._lock:
            st = self._state(skill)
            st.consecutive_failures = 0
            st.fail = 0
            try:
                self.breaker.reset(skill)
            except Exception:  # noqa: BLE001
                pass
            return st.to_dict(self._breaker_open(skill))
