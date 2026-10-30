"""
retrieval.py —— Skill L1 多路召回 + 规则式 Rerank（W1.2 / W2.3，建议①）

解决的问题：现有 `StandardSkillLibrary.match_l1()` 只做「词项命中率」打分，
用户说「帮我清理电脑」而技能叫「磁盘释放工具」时，纯词项匹配会漏掉语义相近
的技能。本模块在**不读正文**（L1 只读 frontmatter）的前提下做三路召回 + RRF
融合 + 上下文感知的规则式二次排序：

  信号 a  词项 BM25           —— 专有名词/精确词项，复用 memory_hybrid.BM25
  信号 b  本地轻量向量        —— HashedNGramIndex（零依赖哈希近似）或可选真 embedding
  信号 c  同义 / 实体扩展      —— memory_hybrid.SYNONYMS + 技能领域同义表
  融合     RRF 倒数排名融合    —— 免疫量纲差异，对「某信号缺失」稳健
  Rerank   规则式加权          —— 上下文契合 + 历史成功率 − 连续失败 − 风险/成本罚项

诚实边界（不可为「更智能」破防）：
  1. `reranker="heuristic-v1"` 是**规则式打分**，不是神经 cross-encoder（本环境
     无 GPU/密钥，不引无法验证的重型模型）；留 rerank 接口可插拔，不冒充神经重排。
  2. 哈希向量是**近似语义**（n-gram 哈希 + 同义表），不是真语义理解；真 embedding
     可选且 **provider 不可用时不写向量、不混空间**（一次检索内所有文档与查询
     用同一套编码），`embedding_status` 如实标 `hashed_fallback | real:<p>`。
  3. 同义表是**有限的领域词典**，只缓解已知的跨词场景，不宣称通用语义理解。
  4. 全程只读 L1 元数据（name/description/risk_level/cost_tier/allowed_tools），
     绝不读 SKILL.md 正文（渐进式披露省 Token 纪律不破）。

零第三方依赖：仅标准库 + 仓库既有 memory_hybrid / memory_semantic。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from memory_hybrid import (BM25, tokenize, expand_query, extract_entities,
                           normalize, rrf, SYNONYMS)
from memory_semantic import HashedNGramIndex, embedding_from_env

RERANKER_ID = "heuristic-v1"
# RRF 三路权重：向量 > 词项 > 同义/实体（语义相近场景向量贡献更大）
RRF_WEIGHTS = (0.30, 0.50, 0.20)
# Top1 与 Top2 的 Rerank 分差小于此阈值 → needs_human（复用 ballot.rank 的置信度语义：
# 分不出高下时系统不硬选，交人工确认；rank 只推荐、confirm 才生效的纪律不变）
TIE_GAP = 0.03

# 技能领域同义表：处理「清理电脑 ↔ 磁盘释放工具」这类跨词（叠加在通用 SYNONYMS 之上）。
# 这是**有限词典**，只缓解已知场景，不冒充通用语义理解。
SKILL_SYNONYMS: Dict[str, List[str]] = {
    "清理": ["释放", "磁盘", "空间", "垃圾", "缓存", "优化", "打扫"],
    "清理电脑": ["磁盘释放", "释放空间", "系统优化", "垃圾清理", "瘦身"],
    "磁盘": ["硬盘", "存储", "空间", "容量", "disk"],
    "释放": ["清理", "回收", "腾出", "free", "回收站"],
    "竞品": ["竞对", "竞争", "对手", "对标", "competitor", "同行"],
    "报告": ["文档", "文稿", "汇报", "总结", "report"],
    "分析": ["剖析", "调研", "研究", "拆解", "analysis"],
    "写代码": ["开发", "实现", "编程", "编码", "code", "coding"],
    "测试": ["验证", "质检", "检查", "回归", "test"],
    "安全": ["漏洞", "审计", "合规", "风控", "security"],
    "调试": ["排错", "定位", "debug", "排障", "troubleshoot"],
}
_ALL_SYNONYMS: Dict[str, List[str]] = {**SYNONYMS, **SKILL_SYNONYMS}

_RISK_PENALTY = {"low": 0.0, "medium": 0.02, "high": 0.05, "critical": 0.08}
_COST_PENALTY = {"low": 0.0, "standard": 0.01, "high": 0.03}


@dataclass
class SkillCandidate:
    """L1 候选技能的可解释打分（供 W4 skill_match Trace 消费）。"""

    name: str
    scores: Dict[str, float] = field(default_factory=dict)
    embedding_status: str = "unavailable"
    reranker: str = RERANKER_ID
    llm_reason: str = ""    # LLM 重排给出的相关性说明（heuristic 时为空）
    rank: int = 0
    chosen: bool = False
    reason: str = ""
    tier: str = ""          # white/gray/black（由 SkillRouter 回填，供 Trace 展示）
    filtered_by: str = ""   # "" | "role_whitelist" | "circuit_open" | "install_invalid"

    def to_dict(self) -> Dict:
        return {
            "name": self.name, "scores": dict(self.scores),
            "embedding_status": self.embedding_status, "reranker": self.reranker,
            "llm_reason": self.llm_reason,
            "rank": self.rank, "chosen": self.chosen, "reason": self.reason,
            "tier": self.tier, "filtered_by": self.filtered_by,
        }


class SkillRetriever:
    """对一组 L1 元数据建三路索引并召回 / 重排。

    入参 `l1_items` 是 `StandardSkillLibrary.load_l1_metadata()` 的产物
    （List[Dict]，只含 frontmatter），**不接收 spec、不读正文**。
    `embedding` 参数：
      None  —— 按环境变量自动探测真 embedding（缺省生产用法）
      False —— 强制只用哈希索引（测试/离线确定性）
      provider 实例 —— 显式指定（测试真向量路径）
    """

    def __init__(self, l1_items: Sequence[Dict], embedding=None,
                 index: Optional[HashedNGramIndex] = None):
        # L1 召回覆盖所有「SKILL.md 合格」的技能（ok=True）；安装期 manifest
        # 校验（installed_ok）由 SkillRouter 按安全分级决定：white 免 manifest，
        # gray/black 未通过者标 install_invalid 剔除。此处不过早过滤，保证
        # 「可解释打分」覆盖全部合格技能，也避免 4 个纯提示词技能被无声丢弃。
        self._l1 = [x for x in (l1_items or [])
                    if x.get("name") and x.get("ok")]
        self._by_name = {x["name"]: x for x in self._l1}
        # 向量空间身份：一次检索内统一编码，绝不混哈希与真向量
        if embedding is None:
            try:
                provider = embedding_from_env()
            except Exception:  # noqa: BLE001 - 探测失败按不可用处理，不静默混空间
                provider = None
        elif embedding is False:
            provider = None
        else:
            provider = embedding
        self._provider = provider
        self._status = ("real:" + str(getattr(provider, "name", "provider"))
                        if provider else "hashed_fallback")
        self._index = index or HashedNGramIndex()
        self._bm25 = BM25()
        for item in self._l1:
            name = item["name"]
            doc = name + " " + str(item.get("description") or "")
            self._bm25.add(name, tokenize(doc))
            try:
                vec = provider.embed(doc) if provider else self._index.embed(doc)
                self._index.add(name, vec)
            except Exception:  # noqa: BLE001 - 真向量算不出 → 整体降级哈希，不混空间
                self._provider = None
                self._status = "hashed_fallback"
                # 已加入的真向量必须清掉重建（否则与哈希查询不在同一空间）
                self._index = HashedNGramIndex()
                self._bm25 = BM25()
                for it in self._l1:
                    n = it["name"]
                    d = n + " " + str(it.get("description") or "")
                    self._bm25.add(n, tokenize(d))
                    self._index.add(n, self._index.embed(d))
                break

    @property
    def status(self) -> str:
        return self._status

    def _embed(self, text: str):
        return (self._provider.embed(text) if self._provider
                else self._index.embed(text))

    # ------------------------------------------------------------------
    # 三路召回 + RRF 融合
    # ------------------------------------------------------------------
    def recall(self, query: str, k: int = 10) -> List[SkillCandidate]:
        q = query or ""
        if not self._l1:
            return []
        qt = tokenize(q)
        expanded = expand_query(q, _ALL_SYNONYMS)

        # a) 词项 BM25（原查询，保持精确词项匹配）
        bm = self._bm25.scores(qt) if qt else {}
        # b) 向量（同义扩展后的查询，捕捉近似语义；真/哈希同一空间）
        try:
            qvec = self._embed(expanded)
            vec = dict(self._index.search(qvec, k=len(self._l1)))
        except Exception:  # noqa: BLE001 - 向量路失败不拖垮召回，其余两路仍可用
            vec = {}
        # c) 同义 / 实体扩展命中
        ent_toks = set(tokenize(expanded)) | set(extract_entities(expanded))
        ent: Dict[str, float] = {}
        for item in self._l1:
            doc = (item["name"] + " " + str(item.get("description") or "")).lower()
            hits = sum(1 for t in ent_toks if t and t in doc)
            if hits:
                ent[item["name"]] = hits / max(1, len(ent_toks))

        def _ranks(scores: Dict[str, float]) -> List[str]:
            return [n for n, _ in sorted(scores.items(),
                                         key=lambda x: (-x[1], x[0]))]

        fused = rrf([_ranks(bm), _ranks(vec), _ranks(ent)],
                    weights=RRF_WEIGHTS)
        out: List[SkillCandidate] = []
        for name, fscore in sorted(fused.items(), key=lambda x: (-x[1], x[0]))[:k]:
            out.append(SkillCandidate(
                name=name,
                scores={"bm25": round(bm.get(name, 0.0), 4),
                        "vector": round(vec.get(name, 0.0), 4),
                        "entity": round(ent.get(name, 0.0), 4),
                        "rrf": round(fscore, 6)},
                embedding_status=self._status))
        return out

    # ------------------------------------------------------------------
    # 规则式 Rerank（上下文感知，非神经重排）
    # ------------------------------------------------------------------
    @staticmethod
    def _ctx_affinity(item: Dict, context: Dict) -> float:
        tools = set(str(t) for t in (item.get("allowed_tools") or []))
        aff = 0.0
        if context.get("web_search") and tools & {"web_search", "search", "browser"}:
            aff += 0.5
        tt = str(context.get("task_type") or "")
        if tt and (tt in tools or any(tt in t for t in tools)):
            aff += 0.5
        role = str(context.get("role") or "")
        if role and any(role in t for t in tools):
            aff += 0.3
        return min(1.0, aff)

    def rerank(self, candidates: List[SkillCandidate],
               context: Optional[Dict] = None) -> List[SkillCandidate]:
        """规则式二次排序：融合分归一化 + 上下文契合 + 历史成功率 − 连续失败
        − 风险/成本罚项。`context["history"]` = {name: {success_rate,
        consecutive_failures, circuit_open}}，由 SkillRouter 从状态层取好传入
        （retrieval 不直接依赖 state，避免循环依赖）。"""
        ctx = dict(context or {})
        hist = ctx.get("history") or {}
        base = normalize({c.name: float(c.scores.get("rrf", 0.0))
                          for c in candidates})
        for c in candidates:
            item = self._by_name.get(c.name) or {}
            h = hist.get(c.name) or {}
            success_rate = float(h.get("success_rate", 0.5))
            fails = int(h.get("consecutive_failures", 0))
            ctx_aff = self._ctx_affinity(item, ctx)
            risk = _RISK_PENALTY.get(str(item.get("risk_level") or "medium"), 0.02)
            cost = _COST_PENALTY.get(str(item.get("cost_tier") or "standard"), 0.01)
            final = (0.55 * float(base.get(c.name, 0.0))
                     + 0.15 * ctx_aff
                     + 0.10 * (success_rate - 0.5) * 2.0
                     - 0.06 * min(fails, 3)
                     - risk - cost)
            if h.get("circuit_open"):
                final -= 1.0   # 防御：熔断技能即使漏过滤也排到最后
                c.filtered_by = c.filtered_by or "circuit_open"
            c.scores["rerank"] = round(final, 6)
            c.scores["ctx_affinity"] = round(ctx_aff, 4)
        candidates.sort(key=lambda c: (-c.scores.get("rerank", 0.0), c.name))
        for i, c in enumerate(candidates, start=1):
            c.rank = i
        return candidates
