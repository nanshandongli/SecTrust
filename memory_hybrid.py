"""
混合记忆检索（memory_hybrid.py）—— 借鉴 Mem0 v2/v3 的"多信号融合"改造记忆层。

原实现的瓶颈（自检已记录）：只靠一层哈希 n-gram 余弦，属于"近似语义"：
  · 关键词漏检 —— 查询里的专有名词（Qwen-Max、/data/foo）被拆成 gram 后权重被稀释；
  · 实体关系缺失 —— "关于 Alice 我们知道什么" 这类跨记忆的实体聚合做不到；
  · 无时间维度   —— 三个月前的事实和昨天的事实同等权重，旧偏好会压过新偏好。

Mem0 2026 的做法（已公开的算法要点）是**多信号检索**：
    语义向量 + BM25 关键词 + 实体匹配 → 各自归一化 → 融合成单一分数
并配套 **记忆衰减 + 强化循环**（被检索到的记忆更新访问历史，常用事实自然上浮，
分数 clamp 到 [0,1]），以及 **ADD-only 提取**（新事实追加而非覆盖，历史留给排序）。

本模块在**零依赖**前提下实现同一套结构：
  · 信号 1 语义：复用 HashedNGramIndex（可选真实 embedding）
  · 信号 2 词项：SQLite 上的 BM25（k1=1.5, b=0.75），解决专有名词漏检
  · 信号 3 实体：显式实体抽取 + 高 IDF 词项提权，实现"实体中心"的跨记忆聚合
  · 融合    ：RRF（k=60，权重 0.25/0.50/0.25，与 Mem0 公开实现同参数）
  · 时间    ：半衰期衰减 + 访问强化；ADD-only，旧事实不删只降权

兼容性：继承 SemanticMemory，既有 `remember/search_semantic` 行为不变；
新增能力走 `HybridMemory.search()` 与可选参数，老代码无需改动。
"""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import time
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from memory_semantic import (EMBED_VERSION, HashedNGramIndex, SemanticMemory,
                             VectorIndex, _grams)

# ---------------------------------------------------------------- 记忆分层

# 借鉴 Mem0 对记忆类型的划分：不同类型**衰减速度**不同
KINDS = ("semantic", "episodic", "procedural", "working")

# 半衰期（天）：工作记忆几小时就失效，语义事实可留存数月
DEFAULT_HALF_LIFE_DAYS = {
    "working": 0.5,
    "episodic": 14.0,
    "procedural": 60.0,
    "semantic": 180.0,
}

# 五维作用域（Mem0 的 scoping 维度）
SCOPES = ("user_id", "agent_id", "run_id", "org_id", "app_id")


# ---------------------------------------------------------------- 分词与实体

_CJK_RUN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")
_WORD = re.compile(r"[a-z0-9]+(?:[./_\-:][a-z0-9]+)*")

# 显式实体：专有名词/路径/版本号/模型名/邮箱/URL——这类"不可切分"的标识符
_ENTITY_PATTERNS = [
    re.compile(r"[A-Z][A-Za-z0-9]*(?:[_\-][A-Za-z0-9]+)+"),   # Qwen-Max / OpenAI_API
    re.compile(r"(?:/[\w.\-]+){2,}"),                          # /data/foo/bar
    re.compile(r"\b[A-Za-z][A-Za-z0-9+.\-]*://[^\s]+"),       # URL
    re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+"),                 # 邮箱
    re.compile(r"\b[A-Z][a-z]+(?:[A-Z][a-z]*)+"),              # CamelCase 专有名词
    re.compile(r"[「『《【\"]{1}([^」』》】\"]{2,20})[」』》】\"]{1}"),  # 引号内专名
]


# ---------------------------------------------------------------- 查询扩展

# 中英别名词表：BM25 是词项精确匹配，遇到"编辑器 / editor"、"模型 / model"这类
# 跨语言同义就失效。工业界惯例是给词项检索配同义词表（Elasticsearch synonym graph
# 同理）。这里只放**通用技术概念**，不放业务词——业务词应该由用户自己在 note 里写全。
#
# ⚠️ 诚实边界：这是**规则式**扩展，不是语义理解。它能救"编辑器→editor"，
#    救不了"那个跑得很慢的批处理任务"这类需要真正 embedding 的查询。
#    真语义路线：给 HybridMemory 传 EmbeddingProvider（OpenAI 等），零依赖底线不变。
SYNONYMS: Dict[str, List[str]] = {
    "编辑器": ["editor", "vim", "neovim", "ide", "vscode"],
    "模型": ["model", "qwen", "gpt", "llm", "claude"],
    "仓库": ["repo", "repository", "git", "path"],
    "路径": ["path", "repo", "dir", "目录"],
    "审查": ["review", "cr", "codereview"],
    "偏好": ["preference", "prefer", "喜欢"],
    "事故": ["incident", "故障", "oom", "线上问题"],
    "密钥": ["secret", "key", "token", "credential"],
    "扫描": ["scan", "scanner", "检测"],
    "数据库": ["database", "db", "sql"],
    "部署": ["deploy", "发布", "上线"],
    "editor": ["编辑器"],
    "model": ["模型"],
    "repo": ["仓库"],
    "review": ["审查"],
    "secret": ["密钥"],
}


def expand_query(query: str, synonyms: Optional[Dict[str, List[str]]] = None) -> str:
    """把查询按同义词表扩展成更长的查询串（追加，不替换原文）。"""
    syn = synonyms if synonyms is not None else SYNONYMS
    if not query:
        return ""
    extra: List[str] = []
    low = query.lower()
    for k, vs in syn.items():
        if k in low:
            extra.extend(v for v in vs if v not in low)
    if not extra:
        return query
    return query + " " + " ".join(extra)


def tokenize(text: str) -> List[str]:
    """BM25 用词项：拉丁整词 + CJK bigram（与向量侧 _grams 保持同源，避免两套切分）。"""
    low = (text or "").lower()
    toks: List[str] = []
    for tok in _WORD.findall(low):
        if len(tok) >= 2:
            toks.append(tok)
    for run in _CJK_RUN.findall(low):
        if len(run) == 1:
            toks.append(run)
        else:
            toks.extend(run[i:i + 2] for i in range(len(run) - 1))
    return toks


def extract_entities(text: str) -> List[str]:
    """轻量实体抽取（离线、无 LLM）：靠形态而非语义。

    与 Mem0 的差别：Mem0 用 LLM 抽实体；这里零依赖，只抽**形态上不可切分**的标识符。
    够用场景：模型名、仓库路径、工具名、人名（拉丁）——正是用户偏好记忆的核心对象。
    """
    ents: List[str] = []
    for pat in _ENTITY_PATTERNS:
        for m in pat.findall(text or ""):
            if isinstance(m, tuple):
                m = m[0] if m else ""
            m = (m or "").strip()
            if 2 <= len(m) <= 64:
                ents.append(m.lower())
    # 去重保序
    seen = set()
    out = []
    for e in ents:
        if e not in seen:
            seen.add(e)
            out.append(e)
    return out


# ---------------------------------------------------------------- BM25

class BM25:
    """最小 BM25（k1=1.5, b=0.75）。语料是"记忆条目"，规模小，内存实现即可。

    跨进程：语料从 SQLite 重建（条目数通常是几十~几千，重建耗时可忽略）。
    """

    def __init__(self, k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self.docs: Dict[str, List[str]] = {}
        self.tf: Dict[str, Dict[str, int]] = {}
        self.df: Dict[str, int] = {}
        self.len: Dict[str, int] = {}
        self.avg_len = 0.0

    def add(self, doc_id: str, tokens: Sequence[str]) -> None:
        self.remove(doc_id)
        self.docs[doc_id] = list(tokens)
        tf: Dict[str, int] = {}
        for t in tokens:
            tf[t] = tf.get(t, 0) + 1
        self.tf[doc_id] = tf
        self.len[doc_id] = len(tokens)
        for t in tf:
            self.df[t] = self.df.get(t, 0) + 1
        self._recompute_avg()

    def remove(self, doc_id: str) -> None:
        if doc_id not in self.tf:
            return
        for t in self.tf[doc_id]:
            self.df[t] = self.df.get(t, 0) - 1
            if self.df[t] <= 0:
                self.df.pop(t, None)
        self.tf.pop(doc_id, None)
        self.docs.pop(doc_id, None)
        self.len.pop(doc_id, None)
        self._recompute_avg()

    def _recompute_avg(self) -> None:
        self.avg_len = (sum(self.len.values()) / len(self.len)) if self.len else 0.0

    def idf(self, term: str) -> float:
        n = len(self.docs)
        if n == 0:
            return 0.0
        df = self.df.get(term, 0)
        # BM25 概率 IDF（保证非负）
        return math.log(1 + (n - df + 0.5) / (df + 0.5))

    def scores(self, query_tokens: Sequence[str]) -> Dict[str, float]:
        out: Dict[str, float] = {}
        if not self.docs:
            return out
        for doc_id, tf in self.tf.items():
            dl = self.len.get(doc_id, 0)
            s = 0.0
            for t in set(query_tokens):
                f = tf.get(t, 0)
                if not f:
                    continue
                denom = f + self.k1 * (1 - self.b + self.b * dl / (self.avg_len or 1.0))
                s += self.idf(t) * (f * (self.k1 + 1)) / denom
            if s > 0:
                out[doc_id] = s
        return out


def normalize(scores: Dict[str, float]) -> Dict[str, float]:
    """把一组分数归一化到 [0,1]（最大值归一；全零则保持零）。

    Mem0 的做法是各信号归一化后再融合——否则 BM25 的无界分数会压过余弦。
    """
    if not scores:
        return {}
    mx = max(scores.values())
    if mx <= 0:
        return {k: 0.0 for k in scores}
    return {k: v / mx for k, v in scores.items()}


def rrf(rank_lists: Sequence[Sequence[str]], weights: Sequence[float] = (0.25, 0.5, 0.25),
        k: int = 60) -> Dict[str, float]:
    """倒数排名融合 Reciprocal Rank Fusion：score(d) = Σ w_i / (k + rank_i(d))。

    为什么用 RRF 而不是加权求和：各信号分值量纲不可比（余弦 ∈[0,1]，BM25 无界），
    RRF 只用**名次**，天然免疫量纲问题，且对"某信号缺失"稳健（未出现=不贡献）。
    k=60 是 RRF 原始论文与 Mem0 公开实现的取值。
    """
    out: Dict[str, float] = {}
    for w, ranks in zip(weights, rank_lists):
        if w <= 0:
            continue
        for i, doc_id in enumerate(ranks, start=1):
            out[doc_id] = out.get(doc_id, 0.0) + w / (k + i)
    return out


# ---------------------------------------------------------------- 混合记忆

class HybridMemory(SemanticMemory):
    """在语义记忆之上叠加 BM25 + 实体 + 时间衰减的混合检索。"""

    def __init__(self, db_path: Optional[str] = None,
                 index: Optional[VectorIndex] = None,
                 embedding=None, synchronous: str = "FULL",
                 weights: Tuple[float, float, float] = (0.25, 0.5, 0.25),
                 rrf_k: int = 60,
                 half_life_days: Optional[Dict[str, float]] = None):
        # embedding 支持 None / provider 实例 / "auto"（同 SemanticMemory）
        super().__init__(db_path=db_path, index=index, embedding=embedding,
                         synchronous=synchronous)
        self.weights = tuple(weights)
        self.rrf_k = rrf_k
        self.half_life = dict(DEFAULT_HALF_LIFE_DAYS)
        if half_life_days:
            self.half_life.update(half_life_days)
        self.bm25 = BM25()
        self._init_hybrid_tables()
        self._rebuild_hybrid()

    # ---- 存储结构 ----
    def _init_hybrid_tables(self) -> None:
        conn = self._connect()
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS memory_meta (
                category TEXT NOT NULL, key TEXT NOT NULL,
                kind TEXT NOT NULL DEFAULT 'semantic',
                user_id TEXT, agent_id TEXT, run_id TEXT, org_id TEXT, app_id TEXT,
                importance REAL NOT NULL DEFAULT 0.5,
                created_at REAL NOT NULL, updated_at REAL NOT NULL,
                access_count INTEGER NOT NULL DEFAULT 0,
                last_access REAL,
                PRIMARY KEY (category, key)
            );
            CREATE TABLE IF NOT EXISTS memory_entities (
                entity TEXT NOT NULL, category TEXT NOT NULL, key TEXT NOT NULL,
                PRIMARY KEY (entity, category, key)
            );
            CREATE INDEX IF NOT EXISTS idx_entities_entity ON memory_entities(entity);
            """
        )
        conn.commit()

    def _rebuild_hybrid(self) -> None:
        """从 memory_items 重建 BM25 语料（实体表已持久化，无需重建）。"""
        conn = self._connect()
        rows = conn.execute("SELECT category, key, value, note FROM memory_items").fetchall()
        for cat, key, value, note in rows:
            try:
                val = json.loads(value) if isinstance(value, str) else value
            except (ValueError, TypeError):
                val = value
            text = f"{key} {note or ''} {val}"
            self.bm25.add(f"{cat}::{key}", tokenize(text))

    # ---- 写入 ----
    def remember(self, category: str, key: str, value, note: str = "",
                 semantic_text: Optional[str] = None,
                 kind: str = "semantic",
                 scope: Optional[Dict[str, str]] = None,
                 entities: Optional[Iterable[str]] = None,
                 importance: float = 0.5) -> None:
        """ADD-only 写入：新事实追加；同 (category,key) 更新内容但**保留 created_at**，
        这样"何时首次学到"不会因重写而丢失（Mem0 的 timeline 思路）。"""
        super().remember(category, key, value, note=note, semantic_text=semantic_text)

        text = semantic_text if semantic_text is not None else f"{key} {note} {value}"
        self.bm25.add(f"{category}::{key}", tokenize(text))

        ents = list(entities or []) + extract_entities(text)
        conn = self._connect()
        now = time.time()
        scope = scope or {}
        conn.execute(
            "INSERT INTO memory_meta(category, key, kind, user_id, agent_id, run_id, "
            "org_id, app_id, importance, created_at, updated_at, access_count, last_access) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,0,NULL) "
            "ON CONFLICT(category, key) DO UPDATE SET "
            "kind=excluded.kind, importance=excluded.importance, updated_at=excluded.updated_at",
            (category, key, kind if kind in KINDS else "semantic",
             scope.get("user_id"), scope.get("agent_id"), scope.get("run_id"),
             scope.get("org_id"), scope.get("app_id"),
             max(0.0, min(1.0, importance)), now, now),
        )
        conn.execute("DELETE FROM memory_entities WHERE category=? AND key=?", (category, key))
        for e in set(x.lower() for x in ents if x):
            conn.execute(
                "INSERT OR REPLACE INTO memory_entities(entity, category, key) VALUES (?,?,?)",
                (e, category, key),
            )
        conn.commit()

    def forget(self, category: str, key: str) -> bool:
        self.bm25.remove(f"{category}::{key}")
        conn = self._connect()
        conn.execute("DELETE FROM memory_meta WHERE category=? AND key=?", (category, key))
        conn.execute("DELETE FROM memory_entities WHERE category=? AND key=?", (category, key))
        conn.commit()
        return super().forget(category, key)

    # ---- 读取 ----
    def _meta(self) -> Dict[str, Dict]:
        conn = self._connect()
        rows = conn.execute(
            "SELECT category, key, kind, user_id, agent_id, run_id, org_id, app_id, "
            "importance, created_at, updated_at, access_count, last_access FROM memory_meta"
        ).fetchall()
        cols = ("category", "key", "kind", "user_id", "agent_id", "run_id", "org_id",
                "app_id", "importance", "created_at", "updated_at", "access_count",
                "last_access")
        return {f"{r[0]}::{r[1]}": dict(zip(cols, r)) for r in rows}

    def decay_factor(self, meta: Dict, now: Optional[float] = None) -> float:
        """时间衰减：0.5 ^ (Δt / 半衰期)，再按重要性上浮，clamp 到 [0,1]。

        参考点是**最后访问时间**（有强化则用之，否则用创建时间）——这正是 Mem0
        强化循环的效果：被反复召回的记忆不会因为"年纪大"而下沉。
        """
        now = now if now is not None else time.time()
        kind = meta.get("kind") or "semantic"
        hl = self.half_life.get(kind, 180.0) * 86400.0
        # ⚠️ 不能用 `a or b or c` 取默认值：时间戳 0.0 是合法值（Unix epoch）却是 falsy，
        #    会被一路 or 到 now，于是"1970 年的记忆"被判成"刚刚写的"，衰减彻底失效。
        ref = meta.get("last_access")
        if ref is None:
            ref = meta.get("updated_at")
        if ref is None:
            ref = meta.get("created_at")
        if ref is None:
            ref = now
        dt = max(0.0, now - float(ref))
        base = 0.5 ** (dt / hl) if hl > 0 else 1.0
        imp = float(meta.get("importance") or 0.5)
        # 重要性把半衰期"拉长"：importance=1 → 衰减减半；importance=0 → 衰减加快
        adj = base ** (1.5 - imp)
        return max(0.0, min(1.0, adj))

    def _entity_scores(self, query: str) -> Dict[str, float]:
        """实体匹配分：查询中出现的实体，命中多少条记忆（跨记忆聚合）。"""
        q_ents = set(extract_entities(query))
        if not q_ents:
            return {}
        conn = self._connect()
        out: Dict[str, float] = {}
        for e in q_ents:
            rows = conn.execute(
                "SELECT category, key FROM memory_entities WHERE entity=?", (e,)
            ).fetchall()
            for cat, key in rows:
                did = f"{cat}::{key}"
                out[did] = out.get(did, 0.0) + 1.0
        # 子串兜底：查询里混在句子中的实体（如"Qwen-Max 做代码审查"）
        low_q = query.lower()
        for e in list(q_ents):
            if e in low_q:
                continue
        rows = conn.execute("SELECT entity, category, key FROM memory_entities").fetchall()
        for ent, cat, key in rows:
            if len(ent) >= 3 and ent in low_q:
                out[f"{cat}::{key}"] = out.get(f"{cat}::{key}", 0.0) + 0.5
        return out

    def reinforce(self, doc_ids: Sequence[str]) -> None:
        """强化循环：被检索到的记忆更新访问历史（Mem0 reinforcement loop）。"""
        if not doc_ids:
            return
        conn = self._connect()
        now = time.time()
        for did in doc_ids:
            cat, _, key = did.partition("::")
            if not key:
                continue
            conn.execute(
                "UPDATE memory_meta SET access_count=access_count+1, last_access=? "
                "WHERE category=? AND key=?", (now, cat, key),
            )
        conn.commit()

    def search(self, query: str, k: int = 5,
               scope: Optional[Dict[str, str]] = None,
               kinds: Optional[Sequence[str]] = None,
               category: Optional[str] = None,
               fuse: str = "rrf",
               apply_decay: bool = True,
               explain: bool = False) -> List[Dict]:
        """混合检索。

        三路召回 → 各自排名 → RRF 融合 → 时间衰减 → 排序。
        返回 [{category, key, score, record, signals, meta}]。
        """
        self.backfill_missing()
        conn = self._connect()

        # --- 作用域 / 类型过滤：先确定候选集 ---
        meta_all = self._meta()
        rows = conn.execute("SELECT category, key FROM memory_items").fetchall()
        candidates = []
        for cat, key in rows:
            did = f"{cat}::{key}"
            m = meta_all.get(did)
            if category and cat != category:
                continue
            if kinds and (m or {}).get("kind") not in kinds:
                continue
            if scope:
                mm = m or {}
                if any(mm.get(s) != v for s, v in scope.items() if v is not None):
                    continue
            candidates.append(did)
        if not candidates:
            return []

        # --- 信号 1：语义向量 ---
        # 编码器不可用（provider 配了但这一轮不可用）时跳过向量信号，
        # 而不是用哈希向量跨空间去比——维度不等会算出半错的分数。
        # 混合检索有 BM25 + 实体兜底，跳过一路不影响可用性。
        embedder = self._embedder()
        if embedder is None:
            vec_scores = {}
        else:
            vec = embedder(query)
            vec_hits = dict(self.index.search(vec, k=max(k * 4, 20)))
            vec_scores = {d: s for d, s in vec_hits.items() if d in set(candidates)}

        # --- 信号 2：BM25（查询先做同义词扩展，缓解中英同义漏检）---
        bm = self.bm25.scores(tokenize(expand_query(query)))
        bm_scores = {d: s for d, s in bm.items() if d in set(candidates)}

        # --- 信号 3：实体 ---
        en_scores = {d: s for d, s in self._entity_scores(query).items()
                     if d in set(candidates)}

        def _ranks(scores: Dict[str, float]) -> List[str]:
            return [d for d, _ in sorted(scores.items(), key=lambda kv: kv[1], reverse=True)]

        if fuse == "weighted":
            nv, nb, ne = (normalize(vec_scores), normalize(bm_scores), normalize(en_scores))
            w1, w2, w3 = self.weights
            fused = {}
            for d in candidates:
                s = w1 * nv.get(d, 0.0) + w2 * nb.get(d, 0.0) + w3 * ne.get(d, 0.0)
                if s > 0:
                    fused[d] = s
        else:
            fused = rrf([_ranks(vec_scores), _ranks(bm_scores), _ranks(en_scores)],
                        weights=self.weights, k=self.rrf_k)

        # --- 时间衰减 + 重要性 ---
        now = time.time()
        out: List[Dict] = []
        for did, s in fused.items():
            cat, _, key = did.partition("::")
            # 过滤"三路信号全零"的项：RRF 会给未命中项一个基线分（w/(k+rank)），
            # 若不过滤，无关记忆会以极小分数挤满结果列表，显得像"幻觉召回"。
            if not (vec_scores.get(did) or bm_scores.get(did) or en_scores.get(did)):
                continue
            rec = self.store.get_record(cat, key)
            if rec is None:
                continue
            m = meta_all.get(did, {})
            dec = self.decay_factor(m, now=now) if apply_decay else 1.0
            out.append({
                "category": cat, "key": key,
                "score": round(s * dec, 6),
                "record": rec,
                "signals": {"vector": round(vec_scores.get(did, 0.0), 4),
                            "bm25": round(bm_scores.get(did, 0.0), 4),
                            "entity": en_scores.get(did, 0.0),
                            "decay": round(dec, 4)},
                "meta": m,
            })
        out.sort(key=lambda x: x["score"], reverse=True)
        top = out[:k]
        self.reinforce([f"{t['category']}::{t['key']}" for t in top])

        if not explain:
            for t in top:
                t.pop("signals", None)
                t.pop("meta", None)
        return top

    # 兼容旧接口名
    def search_hybrid(self, query: str, k: int = 5, **kw) -> List[Dict]:
        return self.search(query, k=k, **kw)

    def stats(self) -> Dict:
        conn = self._connect()
        n_items = conn.execute("SELECT COUNT(*) FROM memory_items").fetchone()[0]
        n_meta = conn.execute("SELECT COUNT(*) FROM memory_meta").fetchone()[0]
        n_ent = conn.execute("SELECT COUNT(*) FROM memory_entities").fetchone()[0]
        kinds: Dict[str, int] = {}
        for (kd,) in conn.execute("SELECT kind FROM memory_meta").fetchall():
            kinds[kd] = kinds.get(kd, 0) + 1
        return {"items": n_items, "meta": n_meta, "entities": n_ent,
                "by_kind": kinds, "bm25_docs": len(self.bm25.docs),
                "bm25_terms": len(self.bm25.df)}


if __name__ == "__main__":
    import tempfile
    d = tempfile.mkdtemp()
    hm = HybridMemory(db_path=os.path.join(d, "mem.db"))
    hm.remember("preference", "code_review_model", "Qwen-Max",
                note="用户偏好用 Qwen-Max 做代码审查", kind="semantic")
    hm.remember("preference", "editor", "Neovim", note="用户喜欢用 Neovim", kind="semantic")
    hm.remember("repo", "main", "/data/foo", note="主仓库路径", kind="semantic")
    hm.remember("incident", "oom-2026-10-01", "导出任务 OOM，已加分页",
                note="上周事故", kind="episodic")
    print("stats:", hm.stats())
    for q in ["代码审查用哪个模型", "Qwen-Max", "主仓库路径", "上周的 OOM 事故"]:
        r = hm.search(q, k=2, explain=True)
        print(f"\n查询: {q}")
        for x in r:
            print(f"   {x['category']}/{x['key']} score={x['score']:.4f} {x['signals']}")
    hm.close()
