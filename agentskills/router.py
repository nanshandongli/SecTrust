"""
router.py —— SkillRouter：把标准 Skill 运行时接进真实执行链（P0，W1.1）

现状（核查过 daemon / planner / console / orchestration 全仓）：B1/B3 建的标准
运行时是「未接线的能力孤岛」——`StandardSkillLibrary.match_l1()` 与
`SkillExecutionGuard` 全仓无生产调用方；daemon 的 `skill` op 走旧
`loader.activate(name)` 按名激活，planner 的 `skill` 字段是模板硬编码的提示
字符串。所以「语义检索加得再好」也必须先接通主链路才有落点。

本模块是那把钥匙：
  `select(query, role, context, k)` → 多路召回 → 角色白名单/安装校验/熔断过滤
  → 规则式 Rerank（含状态降权）→ Top-K → 写 `skill_match` Trace（打分瀑布可查）。

接入策略（用户拍板「用你推荐的渐进式」）：保留 planner 硬编码 `skill` 提示
字符串作 fallback（模板已验证，不一刀切），新增 `payload.skill_hint` /
`payload.query` 触发**检索选技能**；角色白名单 `ROLES[].skills` 与本库取交集，
但两套命名体系无交集时回退不过滤并留痕 `no_overlap_fallback`（不制造零召回）。

诚实边界：
  1. router **只做「选技能」**，不替模型决策；`LlmEngine.complete()`=审计问答 /
     `execute()`=干活的纪律不变。
  2. 选中后必须过 `SkillExecutionGuard`（审批→沙箱→证据→工件→质量）或 white
     快速通道，**绝不绕过门禁**；white 执行语义是「L2 正文激活」，模型驱动的
     下游执行归 AgentRunner，不在此越界（与旧 skill op 注释一致）。
  3. Trace 只在提供 trace_store/trace_id 时写 `skill_match`，缺上下文如实标注
     未写，不硬造孤儿轨迹。
  4. Top1/Top2 分差 < TIE_GAP → `needs_human=True`（复用 ballot.rank 的置信度
     语义：分不出高下时系统不硬选；rank 只推荐、confirm 才生效的纪律不变）。

零第三方依赖：仅标准库 + 仓库既有模块。
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence

from .retrieval import SkillRetriever, SkillCandidate, RERANKER_ID, TIE_GAP
from .skill_md import StandardSkillLibrary
from .tier import classify
from .llm_rerank import llm_rerank, DEFAULT_MIN_COMPETITIVE
from .reflection import ReflectionEngine, ConfidenceLoop


class SkillRouter:
    """L1 语义选技能 → Top-K 可解释候选 → 交下游门禁执行。"""

    def __init__(self, library: Optional[StandardSkillLibrary] = None,
                 retriever: Optional[SkillRetriever] = None,
                 state=None, role_skills: Optional[Sequence[str]] = None,
                 trace_store=None, trace_id: str = "",
                 llm=None, llm_model: Optional[str] = None,
                 llm_rerank_min_competitive: int = DEFAULT_MIN_COMPETITIVE,
                 reflection: Optional[ReflectionEngine] = None,
                 confidence_loop: Optional[ConfidenceLoop] = None):
        self.library = library or StandardSkillLibrary()
        self.state = state
        # None / 空 = 不过滤；非空 = 角色白名单（有交集才过滤，见 select）
        self.role_skills = set(role_skills) if role_skills else None
        self.trace_store = trace_store
        self.trace_id = trace_id
        # 认知升级：可选 LLM 语义重排（无则纯 heuristic-v1，诚实降级）
        self.llm = llm
        self.llm_model = llm_model
        # 竞争力阈值：与榜首有竞争力的候选数 < 此值跳过 LLM（速度优先；0=始终触发）
        self.llm_rerank_min_competitive = llm_rerank_min_competitive
        # 夜间演进：方向一经验复用（预填 feedback，提示而非自动改道）+ 方向三
        # Confidence Loop（决定是否升级更强模型）。二者均为可选装配，None=行为不变。
        self.reflection = reflection
        self.confidence_loop = confidence_loop
        self._retriever = retriever or SkillRetriever(
            self.library.load_l1_metadata(include_broken=True))

    def refresh(self) -> None:
        """技能库变更后重建索引（L1 只读 frontmatter，重建毫秒级）。"""
        self.library.refresh()
        self._retriever = SkillRetriever(
            self.library.load_l1_metadata(include_broken=True))

    # ------------------------------------------------------------------
    def _enrich(self, name: str) -> Dict:
        """把 L1 dict 补上 spec.manifest（供 tier.classify 读 requires_approval）。"""
        item = dict(self._retriever._by_name.get(name) or {"name": name})
        spec = self.library.get(name)
        if spec is not None:
            item["manifest"] = dict(spec.manifest or {})
            item["allowed_tools"] = list(spec.allowed_tools)
            item["risk_level"] = spec.risk_level
            item["cost_tier"] = spec.cost_tier
            item["security_tier"] = spec.security_tier
            item["trust_tier"] = spec.trust_tier
            item["has_scripts"] = spec.has_scripts
        return item

    def select(self, query: str, role: str = "", context: Optional[Dict] = None,
               k: int = 5, session_id: str = "default") -> Dict:
        """用户查询 → 可解释候选 → 选中技能（供下游门禁执行）。"""
        ctx = dict(context or {})
        if role:
            ctx["role"] = role
        # 方向一·经验复用：相似 query / 相同错误命中历史修正方案 → 预填 feedback。
        # 这是「提示」不是「自动改道」：命中经验只影响给下游 AI 的反馈上下文，
        # 绝不绕过检索 / 角色白名单 / 分级门禁（纪律与状态反馈一致）。
        experience_hint = ""
        experience_match = None
        if self.reflection is not None:
            try:
                hit = self.reflection.reuse(query=query)
                if hit:
                    experience_match = hit
                    experience_hint = ("[经验复用·%s] %s"
                                       % (hit.get("match_type", ""),
                                          hit.get("correction", "")))
                    # 有效性记账不在此处：只有执行层拿到 ok/fail 后才知道复用是否奏效，
                    # 由 daemon 统一 mark_used(exp_id, success=...) 记一次，避免
                    # 「命中即计数」与「执行后计数」双重累加导致 used_count 虚高、
                    # 有效率被稀释。router 只负责命中 + 预填提示。
            except Exception:  # noqa: BLE001 - 经验库不可用不挡选技能
                experience_hint = ""
        # 1) 多路召回（Top-K 的 3 倍，给过滤留余量）
        cands = self._retriever.recall(query, k=max(k * 3, 10))

        # 2) 角色白名单：两套命名体系无交集时回退不过滤并留痕（不制造零召回）
        role_filter = "disabled"
        if self.role_skills:
            overlap = self.role_skills & {c.name for c in cands}
            if overlap:
                role_filter = "applied"
                for c in cands:
                    if c.name not in overlap and not c.filtered_by:
                        c.filtered_by = "role_whitelist"
                        c.reason = "角色未安装该技能（白名单交集过滤）"
            else:
                role_filter = "no_overlap_fallback"
                # 两套技能命名体系未对齐：不过滤，但 Trace 里如实说明原因

        # 3) 分级 + 安装校验 + 熔断过滤
        hist = self.state.history_map(session_id) if self.state else {}
        ctx["history"] = hist
        kept: List[SkillCandidate] = []
        for c in cands:
            item = self._enrich(c.name)
            dec = classify(item)
            c.tier = dec.tier
            if (dec.tier in ("gray", "black")
                    and not item.get("installed_ok", True)):
                c.filtered_by = c.filtered_by or "install_invalid"
                c.reason = c.reason or "gray/black 技能未通过 manifest 安装校验"
            if hist.get(c.name, {}).get("circuit_open"):
                c.filtered_by = c.filtered_by or "circuit_open"
                c.reason = c.reason or "连续失败熔断中，L1 阶段剔除（省 Token/算力）"
            if not c.filtered_by:
                kept.append(c)

        # 4) 规则式 Rerank（历史成功率 / 连续失败 / 风险成本罚项已含在 ctx）
        ranked = self._retriever.rerank(kept, ctx)
        # 4.5) 认知升级：LLM 语义重排（仅重排已过滤的安全候选；无 LLM/异常
        #      整体降级回 heuristic-v1，绝不冒充已做语义重排）
        descriptions = {
            c.name: str((self._retriever._by_name.get(c.name) or {})
                        .get("description") or "")
            for c in ranked}
        # 方向三·Confidence Loop：配了 weak/strong 分级模型才走「小模型优先 +
        # 排序一致性不够再升级强模型」；未配置则退回原单层 LLM 重排（行为不变）。
        if self.confidence_loop is not None and self.confidence_loop.configured:
            ranked, llm_meta = self.confidence_loop.decide(
                query, ranked, descriptions, self.llm, k=k,
                conversation=ctx.get("conversation"),
                min_competitive=self.llm_rerank_min_competitive)
        else:
            ranked, llm_meta = llm_rerank(
                query, ranked, descriptions, self.llm, k=k,
                model=self.llm_model, conversation=ctx.get("conversation"),
                min_competitive=self.llm_rerank_min_competitive)
        # 被过滤的候选也参与展示（rank 置末尾），保证「为什么弃」可查
        filtered = [c for c in cands if c.filtered_by]
        for i, c in enumerate(filtered, start=len(ranked) + 1):
            c.rank = i
            c.reason = c.reason or "被过滤：%s" % c.filtered_by
        all_cands = ranked + filtered

        # 5) Top-K + chosen + 置信度
        final = ranked[:k]
        for c in ranked[k:]:
            c.reason = c.reason or "Rerank 排名在 Top-%d 之外" % k
        chosen = final[0].name if final else ""
        if final:
            final[0].chosen = True
            final[0].reason = final[0].reason or "Rerank 首位（%s）" % RERANKER_ID
        gap = ((final[0].scores.get("rerank", 0.0)
                - final[1].scores.get("rerank", 0.0))
               if len(final) > 1 else 1.0)
        needs_human = bool(final) and len(final) > 1 and gap < TIE_GAP

        # ⑤ 可观测性：语义检索/重排的中间结果（供 SkillTrace 白盒复现打分瀑布）
        vector_results = [
            {"name": c.name, "rrf": round(c.scores.get("rrf", 0.0), 6)}
            for c in ranked if c.scores.get("rrf")]   # 多路召回 RRF 融合分
        rerank_results = [
            {"name": c.name,
             "rerank": round(c.scores.get("rerank", 0.0), 6),
             "llm_rerank_score": round(c.scores.get("llm_rerank", 0.0), 6)}
            for c in final]                            # 最终 Top-K 重排分

        # 6) 反馈闭环：选中技能若有失败/驳回历史，给出自我纠正提示
        feedback = self.state.hint_for(chosen, session_id) if (self.state and chosen) else ""

        # ② 会话记忆：把选中技能会话内「已获取的环境变量」注入上下文，避免 AI 重复询问
        cached_params: Dict[str, str] = {}
        if self.state and chosen and hasattr(self.state, "get_cached_params"):
            cached_params = self.state.get_cached_params(chosen, session_id)
            if cached_params:
                keys = ", ".join(
                    "%s%s" % (k, "" if v == "<redacted>" else "=%s" % v)
                    for k, v in cached_params.items())
                hint = "（已缓存环境变量：%s — 无需重复向用户询问）" % keys
                feedback = (feedback + " " + hint).strip()
        # 方向一：经验复用命中 → 预填到 feedback（不改 chosen，不绕过任何门禁）
        if experience_hint:
            feedback = (feedback + " " + experience_hint).strip()

        out = {
            "ok": bool(final), "chosen": chosen,
            "candidates": [c.to_dict() for c in all_cands],
            "embedding_status": self._retriever.status,
            "reranker": llm_meta["reranker"], "top1_top2_gap": round(gap, 6),
            "needs_human": needs_human,             "role": role or "",
            "role_filter": role_filter, "feedback": feedback,
            "cached_params": cached_params,
            "llm_rerank": llm_meta,
            "experience_hint": experience_hint,       # 方向一：预填的修正提示
            "experience_match": experience_match,     # 方向一：命中的经验（含 match_type/score）
            "confidence_loop": llm_meta.get("confidence_loop",
                                            {"applied": False}),  # 方向三：是否升级模型
            "vector_results": vector_results,
            "rerank_results": rerank_results,
            "final_top_k": k,
            "execution_note": ("检索 + 门禁已接通；white 走 L2 正文激活、gray/black "
                               "走 SkillExecutionGuard 五层门，模型驱动的下游执行归 "
                               "AgentRunner，不在此越界"),
            "trace_written": False,
        }
        self._emit_match(query, out, all_cands)
        return out

    # ------------------------------------------------------------------
    def _emit_match(self, query: str, out: Dict,
                    cands: List[SkillCandidate]) -> None:
        if self.trace_store is None or not self.trace_id:
            return   # 缺上下文不硬造孤儿轨迹
        try:
            self.trace_store.record(
                self.trace_id, "skill_match", task_id="", actor="agentskills",
                summary="L1 选技能：%s → %s（%d 候选）"
                        % (query[:60], out["chosen"] or "无命中", len(cands)),
                detail={"query": query, "embedding_status": out["embedding_status"],
                        "reranker": out["reranker"],
                        "llm_rerank": out["llm_rerank"],
                        "experience_hint": out["experience_hint"],
                        "experience_match": out["experience_match"],
                        "confidence_loop": out["confidence_loop"],
                        "vector_results": out["vector_results"],
                        "rerank_results": out["rerank_results"],
                        "final_top_k": out["final_top_k"],
                        "top1_top2_gap": out["top1_top2_gap"],
                        "needs_human": out["needs_human"],
                        "role": out["role"], "role_filter": out["role_filter"],
                        "candidates": [c.to_dict() for c in cands]},
                content=None)
            out["trace_written"] = True
        except Exception:  # noqa: BLE001 - 审计旁路，失败不挡选技能
            out["trace_written"] = False
