"""
记忆增强（memory_semantic.py）—— 在 SQLite 长期记忆之上可选叠向量层（路线 3）。

设计取舍：
- 默认零依赖：用「字符 3-gram + 哈希桶 + 余弦」的轻量向量，足以做模糊语义回忆
  （"这个用户喜欢用什么做代码审查" 这类自然语言查询能命中 Qwen-Max 这条偏好）；
- 可选升级：接入真正的 embedding（OpenAI / Ollama 本地，见 EmbeddingProvider 与
  embedding_from_env()），或把记忆整体委托给 Mem0 / OpenMemory MCP
  （见 mem0_adapter / openmemory_adapter）；
- 向量与记忆同库同文件（单独的 embeddings 表），不破坏既有 memory_items 结构；
- **换 embedding provider 会自动重建向量**（版本键含 provider 标识）——否则
  1024 维哈希向量与 1536 维神经向量混算，余弦静默错乱，语义检索跨会话失效。

⚠️ 不上向量库并非偷懒：用户偏好/指令模板/repo 路径是少量、结构化数据，SQLite + 子串 +
   轻量向量已够用；真要语义图谱/时序/多用户合规，再叠 Mem0。本模块把"可插拔"留好了。
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import threading
from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Tuple

from memory import MemoryStore, _memory_db_path

# 向量编码版本：编码方式一旦改变（如修掉不稳定 hash），旧向量必须重建，
# 否则「新查询向量」与「旧存储向量」不在同一桶位空间，余弦恒为 0（跨会话失效）。
EMBED_VERSION = "v2-stable-hash-cjk-bigram"


# ---------------- 向量索引（离线默认：哈希 n-gram 余弦） ----------------
class VectorIndex(ABC):
    @abstractmethod
    def embed(self, text: str) -> List[float]:
        ...

    @abstractmethod
    def add(self, key: str, vector: List[float]) -> None:
        ...

    @abstractmethod
    def search(self, vector: List[float], k: int = 5) -> List[Tuple[str, float]]:
        ...


def _cosine(a: List[float], b: List[float]) -> float:
    """余弦相似度。**维度不等直接判 0**——绝不 zip 截断。

    为什么不能截断：zip 会把 1536 维与 1024 维对齐到前 1024 维，算出一个
    "看起来合理但完全错误"的分数（不是 0，是半错的值）。这类错误最危险：
    检索照常返回结果，只是排序全乱，现场无法察觉。宁可判 0（不召回）也不要
    给出一个假的相似度。
    """
    if not a or not b:
        return 0.0
    if len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


# CJK 统一表意文字（含扩展 A）
_CJK_RUN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")
# 拉丁/数字词元（保留路径与常见的 . / _ - : 让 "template_scan_demo" 这类可切分）
_WORD = re.compile(r"[a-z0-9]+(?:[./_\-:][a-z0-9]+)*")


def _stable_bucket(gram: str, dim: int) -> int:
    """**跨进程、跨平台稳定**的桶位函数。

    ⚠️ 绝不能用内置 hash()：Python 3 对 str 默认启用 PYTHONHASHSEED 随机加盐，
    同一字符串在不同进程里 hash 值不同（实测 "abc"%1024 → 456 / 319 / 638）。
    用它做向量桶位会导致「进程 A 写入的向量」在「进程 B」里全部错位，
    跨会话语义检索余弦恒为 0——即长期记忆"跨会话"这一核心承诺失效。
    """
    digest = hashlib.blake2b(gram.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big") % dim


def _grams(text: str) -> List[str]:
    """切分文本为 gram。中文无空格，3-gram 太脆（"主仓库在哪" 与 "主业务仓库路径"
    的 3-gram 完全不重合，余弦为 0），因此对 CJK 用字符 bigram；拉丁用词元 + 3-gram。"""
    low = (text or "").lower()
    grams: List[str] = []
    for tok in _WORD.findall(low):
        if len(tok) < 2:
            continue
        grams.append(tok)                       # 整词，最稳的信号
        for i in range(len(tok) - 2):
            grams.append(tok[i:i + 3])          # 词内 3-gram，容忍拼写/词形变化
    for run in _CJK_RUN.findall(low):
        if len(run) == 1:
            grams.append(run)
            continue
        for i in range(len(run) - 1):
            grams.append(run[i:i + 2])          # 中文字符 bigram
    return grams


class HashedNGramIndex(VectorIndex):
    """稳定哈希 n-gram 向量：零依赖、确定性、跨进程可复现。

    与上一版（v1）的关键差别：桶位用 blake2b 而非内置 hash()，并对 CJK 用 bigram。
    """

    def __init__(self, dim: int = 1024):
        self.dim = dim
        self._vecs: Dict[str, List[float]] = {}

    def embed(self, text: str) -> List[float]:
        vec = [0.0] * self.dim
        for gram in _grams(text):
            vec[_stable_bucket(gram, self.dim)] += 1.0
        # L2 归一化
        norm = math.sqrt(sum(v * v for v in vec))
        if norm:
            vec = [v / norm for v in vec]
        return vec

    def add(self, key: str, vector: List[float]) -> None:
        self._vecs[key] = vector

    def search(self, vector: List[float], k: int = 5) -> List[Tuple[str, float]]:
        scored = [(key, _cosine(vector, v)) for key, v in self._vecs.items()]
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:k]


# ---------------- 可选 embedding provider（真实向量） ----------------
class EmbeddingProvider(ABC):
    """向量提供方。子类至少实现 embed()；available() 默认可用。

    available() 语义：**当前这一刻能否真的产出向量**（如本地 Ollama 没启动、
    API key 缺失）。调用方据此决定"用真向量"还是"暂缓写入"，而不是悄悄
    换成另一种空间的向量。
    """

    name = "base"

    def available(self) -> bool:
        return True

    @abstractmethod
    def embed(self, text: str) -> List[float]:
        ...


class OpenAIEmbeddingProvider(EmbeddingProvider):
    """OpenAI / 兼容端点 embedding（text-embedding-3-small 等）。需配置 API key。
    用标准库 urllib，零硬依赖；不可用时 available()=False。"""

    name = "openai"

    def __init__(self, api_key: str = "", base_url: str = "https://api.openai.com/v1",
                 model: str = "text-embedding-3-small"):
        self.api_key = api_key or os.getenv("OPENAI_API_KEY", "")
        self.base_url = base_url or os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
        self.model = model

    def available(self) -> bool:
        return bool(self.api_key)

    def embed(self, text: str) -> List[float]:
        import urllib.request
        url = f"{self.base_url.rstrip('/')}/embeddings"
        req = urllib.request.Request(
            url,
            data=json.dumps({"input": text, "model": self.model}).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.api_key}"},
        )
        with urllib.request.urlopen(req, timeout=20) as r:  # noqa: S310
            data = json.loads(r.read())
        return data["data"][0]["embedding"]


class OllamaEmbeddingProvider(EmbeddingProvider):
    """本地 Ollama embedding（nomic-embed-text 等）——**不需要云 API key 的真向量**。

    适合"要语义质量又要数据不出本机"的场景。探测用 GET /api/tags（不加载模型），
    嵌入用 POST /api/embed。零硬依赖（urllib）； Ollama 没起时 available()=False，
    调用方自动回退到默认哈希索引。"""

    name = "ollama"

    def __init__(self, base_url: str = "", model: str = ""):
        self.base_url = (base_url or os.getenv("OLLAMA_BASE_URL")
                         or "http://127.0.0.1:11434").rstrip("/")
        self.model = model or os.getenv("OLLAMA_EMBED_MODEL") or "nomic-embed-text"

    def available(self) -> bool:
        import urllib.request
        try:
            with urllib.request.urlopen(f"{self.base_url}/api/tags", timeout=3) as r:
                return r.status == 200
        except Exception:  # noqa: BLE001
            return False

    def embed(self, text: str) -> List[float]:
        import urllib.request
        req = urllib.request.Request(
            f"{self.base_url}/api/embed",
            data=json.dumps({"model": self.model, "input": text}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310
            data = json.loads(r.read())
        # Ollama 两种返回形态：{"embeddings": [[...]]}（新）或 {"embedding": [...]}（旧）
        if isinstance(data.get("embeddings"), list) and data["embeddings"]:
            return data["embeddings"][0]
        if isinstance(data.get("embedding"), list):
            return data["embedding"]
        raise RuntimeError(f"Ollama 返回里找不到 embedding: {list(data.keys())}")


def _resolve_env_provider() -> Optional[EmbeddingProvider]:
    """按 EMBEDDING_PROVIDER 解析出 provider 实例——**不检查可用性**。

    为什么要有这个"不检查可用性"的版本：provider 身份定义了**向量空间**，
    而空间是配置决定的，不是运行期状态决定的。若按"当前可用"来定空间，
    本地 Ollama 停一次就会把库里的真向量当成"过期空间"洗成哈希向量——
    真语义被静默降级，且 provider 恢复时还要再重建一次（来回震荡）。

    公开 API `embedding_from_env()` 仍返回"此刻可用的 provider 或 None"，
    那是给"要不要现在就用真向量"用的判断，与"空间是谁"是两件事。
    """
    name = (os.getenv("EMBEDDING_PROVIDER") or "").strip().lower()
    if name in ("", "none", "off", "hashed", "default"):
        return None
    if name == "openai":
        return OpenAIEmbeddingProvider()
    if name == "ollama":
        return OllamaEmbeddingProvider()
    return None  # 未知名字不猜——静默猜一个比不用更危险


def embedding_from_env() -> Optional[EmbeddingProvider]:
    """按环境变量选 embedding provider；未配置/不可用/未知名字 -> None（走默认哈希索引）。

    EMBEDDING_PROVIDER = openai | ollama | none（缺省 none）
      openai : 需 OPENAI_API_KEY（可用 OPENAI_BASE_URL 指向兼容端点）
      ollama : 需本机 Ollama（可用 OLLAMA_BASE_URL / OLLAMA_EMBED_MODEL）
    未知取值不猜——静默猜一个 provider 比不用更危险（你以为在用真向量，其实没有）。
    """
    p = _resolve_env_provider()
    if p is None:
        return None
    return p if p.available() else None


# ---------------- 语义记忆（组合 MemoryStore + VectorIndex） ----------------
class SemanticMemory:
    """在 MemoryStore 之上叠加向量索引，支持语义检索。向量持久化到同一记忆库文件。"""

    def __init__(self, db_path: Optional[str] = None,
                 index: Optional[VectorIndex] = None,
                 embedding: Optional[EmbeddingProvider] = None,
                 synchronous: str = "FULL"):
        """embedding 参数：
          None        —— 不配置真向量，走默认哈希 n-gram 索引（零依赖）
          provider 实例 —— 显式指定（测试/编程场景）
          "auto"      —— 按 EMBEDDING_PROVIDER 环境变量自动选（生产默认用法）

        "auto" 用的是 _resolve_env_provider()（**不检查可用性**）：只要环境变量
        指名了 provider，就认它定义的空间。这样 provider 临时不可用时，
        库里已有的真向量不会被误判成"过期空间"而被洗掉；等它恢复，
        版本键一致，无需重建。
        """
        self.db_path = db_path or _memory_db_path()
        self.store = MemoryStore(db_path=self.db_path, synchronous=synchronous)
        self.index = index or HashedNGramIndex()
        if isinstance(embedding, str):
            if embedding.strip().lower() != "auto":
                raise ValueError(
                    f"embedding 只接受 None / provider 实例 / 'auto'，收到 {embedding!r}")
            embedding = _resolve_env_provider()
        self.embedding = embedding
        self._local = threading.local()
        self._init_embed_table()
        if self._stored_embed_version() != self._version_key:
            # 编码方式变了（或换了 embedding provider，或库是旧版写的）→
            # 旧向量与新查询不在同一空间（维度/桶位都不同），必须重建。
            # 不重建的后果：zip 截断后余弦恒为 0 或乱分——跨会话语义检索静默失效。
            self.reindex()
        else:
            self._rebuild_index()
        # 补索引：核心库里由 MemoryStore 直接写入（如 MCP memory_set）的记录没有向量
        self.backfill_missing()

    def _provider_id(self) -> str:
        """当前生效的向量来源标识（进版本键：换 provider = 换向量空间 = 必须重建）。

        关键：**用 provider 身份而非"是否可用"**。
        若按 available() 来判定，provider 中途掉线会被误判成"换了 provider"，
        触发一次不必要的全量重建（把好向量洗成降级向量）。反过来，provider
        从"掉线"恢复时又会被误判成"换了 provider"重建一次——来回震荡。
        身份才是空间的定义，可用性只决定"这一轮能不能算"。
        """
        if self.embedding is not None:
            return f"{self.embedding.name}:{getattr(self.embedding, 'model', '')}"
        return "hashed"

    @property
    def _version_key(self) -> str:
        return f"{EMBED_VERSION}|{self._provider_id()}"

    def _embedder(self):
        """返回本轮实际用于计算向量的 callable，或 None（表示暂不可算）。

        None 的含义是"provider 暂时不可用"——此时**不能**用别的编码器顶上，
        因为那会往同一个空间里灌进另一种维度的向量（见 _write_vector 守卫）。
        调用方遇到 None 应理解为：这轮不写向量，等 provider 恢复后重建。
        """
        if self.embedding is None:
            return self.index.embed
        if self.embedding.available():
            return self.embedding.embed
        return None

    def _write_vector(self, category: str, key: str, text: str,
                      conn=None) -> bool:
        """按当前空间写一条向量。返回是否真的写入。

        守卫逻辑（这是防"维度/空间错位"的关键）：
        - provider 可用  -> 用 provider 算（真向量）
        - 没有 provider  -> 用哈希索引算（默认路径）
        - provider 配了但**这一轮不可用** -> **不写**，返回 False。

        最后一条是重点：绝不能在这里"顺手降级成哈希向量"。库里的既有向量
        是 provider 空间（如 1536 维），降级写入的哈希向量是 1024 维，两者
        同表混存后，_cosine 的 zip 截断会算出"半错的分数"——检索照常返回
        结果，只是排序全乱，现场根本发现不了。宁可这轮不写，等 provider
        恢复后走 reindex 统一重建。
        """
        embedder = self._embedder()
        if embedder is None:
            return False
        vec = embedder(text)
        self.index.add(f"{category}::{key}", vec)
        if conn is not None:
            conn.execute(
                "INSERT OR REPLACE INTO memory_embeddings(category, key, vec) "
                "VALUES (?,?,?)",
                (category, key, json.dumps(vec)),
            )
        return True

    def _connect(self):
        if not hasattr(self._local, "conn") or self._local.conn is None:
            conn = sqlite3.connect(self.db_path, timeout=30, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=30000")
            self._local.conn = conn
        return self._local.conn

    def _init_embed_table(self):
        conn = self._connect()
        conn.execute(
            "CREATE TABLE IF NOT EXISTS memory_embeddings ("
            "category TEXT NOT NULL, key TEXT NOT NULL, vec TEXT NOT NULL, "
            "PRIMARY KEY (category, key))"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS memory_embed_meta ("
            "k TEXT PRIMARY KEY, v TEXT NOT NULL)"
        )
        conn.commit()

    def _stored_embed_version(self) -> Optional[str]:
        conn = self._connect()
        row = conn.execute(
            "SELECT v FROM memory_embed_meta WHERE k='embed_version'"
        ).fetchone()
        return row[0] if row else None

    def _set_embed_version(self) -> None:
        conn = self._connect()
        conn.execute(
            "INSERT OR REPLACE INTO memory_embed_meta(k, v) VALUES ('embed_version', ?)",
            (self._version_key,),
        )
        conn.commit()

    def _rebuild_index(self):
        conn = self._connect()
        rows = conn.execute("SELECT category, key, vec FROM memory_embeddings").fetchall()
        for cat, key, vec in rows:
            self.index.add(f"{cat}::{key}", json.loads(vec))

    def backfill_missing(self) -> int:
        """把核心记忆库里**尚未建向量**的条目补进索引。

        场景：记忆可能由 `MemoryStore`（或 MCP `memory_set`）直接写入，语义层当时并不在场。
        这样保证「先写后查」与「换了入口写」都能被语义检索召回。返回补建条数。
        """
        conn = self._connect()
        rows = conn.execute(
            "SELECT m.category, m.key, m.value, m.note FROM memory_items m "
            "LEFT JOIN memory_embeddings e ON e.category=m.category AND e.key=m.key "
            "WHERE e.vec IS NULL"
        ).fetchall()
        n = 0
        for cat, key, value, note in rows:
            try:
                val = json.loads(value) if isinstance(value, str) else value
            except (ValueError, TypeError):
                val = value
            text = f"{key} {note or ''} {val}"
            key_id = f"{cat}::{key}"
            if not self._write_vector(cat, key, text, conn):
                # provider 暂不可用：这轮不写向量（见 _write_vector 的守卫说明）。
                # 记忆本体已在 memory_items 里，provider 恢复后由 backfill/reindex 补齐。
                continue
            n += 1
        if n:
            conn.commit()
        return n

    def reindex(self) -> int:
        """全量重建向量索引（换 embedding provider 后使用）。返回条目数。

        重建前先确认这一轮**算得出**向量：provider 配了却不可用时，
        不能用哈希向量去重建 provider 空间——那等于把真向量洗成降级向量。
        此时直接放弃重建（保留原有向量不动），等 provider 恢复后再重建。
        """
        if self._embedder() is None:
            return 0
        self.index = type(self.index)() if not isinstance(self.index, HashedNGramIndex) \
            else HashedNGramIndex()
        conn = self._connect()
        conn.execute("DELETE FROM memory_embeddings")
        rows = conn.execute("SELECT category, key, value, note FROM memory_items").fetchall()
        for cat, key, value, note in rows:
            try:
                val = json.loads(value) if isinstance(value, str) else value
            except (ValueError, TypeError):
                val = value
            text = f"{key} {note or ''} {val}"
            self._write_vector(cat, key, text, conn)
        conn.commit()
        self._set_embed_version()
        return len(rows)

    def remember(self, category: str, key: str, value, note: str = "",
                 semantic_text: Optional[str] = None) -> None:
        """写入记忆并同步更新向量索引。semantic_text 缺省时用 value+note 生成。"""
        self.store.remember(category, key, value, note=note)
        text = semantic_text if semantic_text is not None else f"{key} {note} {value}"
        conn = self._connect()
        # 记忆本体必落库；向量受空间守卫约束（provider 不可用时这轮不写向量）
        self._write_vector(category, key, text, conn)
        conn.commit()

    def search_semantic(self, query: str, k: int = 5,
                       category: Optional[str] = None) -> List[Tuple[str, float, float, dict]]:
        """语义检索：返回 [(category, key, score, record)]，按相似度降序。

        检索前会做一次廉价的「条目数比对」，若有记忆是别的入口写入的（未建向量）则先补索引，
        保证跨入口写入也能被召回。
        """
        try:
            conn = self._connect()
            n_items = conn.execute("SELECT COUNT(*) FROM memory_items").fetchone()[0]
            n_vecs = conn.execute("SELECT COUNT(*) FROM memory_embeddings").fetchone()[0]
            if n_items != n_vecs:
                self.backfill_missing()
        except sqlite3.Error:
            pass
        vec = self._embedder()
        if vec is None:
            # provider 配了但这一轮不可用：库里存的是 provider 空间的向量，
            # 用哈希查询向量去比会跨空间（维度不等 -> 余弦恒 0 或乱分）。
            # 此时不做语义召回，如实返回空，由调用方决定是否回退到字面检索。
            return []
        qvec = vec(query)
        hits = self.index.search(qvec, k=k * 4)
        out = []
        for key_id, score in hits:
            cat, key = key_id.split("::", 1)
            if category and cat != category:
                continue
            rec = self.store.get_record(cat, key)
            if rec is None:
                continue
            out.append((cat, key, round(score, 4), rec))
        out.sort(key=lambda x: x[2], reverse=True)
        return out[:k]

    def forget(self, category: str, key: str) -> bool:
        removed = self.store.forget(category, key)
        conn = self._connect()
        conn.execute("DELETE FROM memory_embeddings WHERE category=? AND key=?",
                     (category, key))
        conn.commit()
        self.index._vecs.pop(f"{category}::{key}", None)
        return removed

    def close(self):
        self.store.close()
        if hasattr(self._local, "conn") and self._local.conn:
            self._local.conn.close()
            self._local.conn = None


if __name__ == "__main__":
    import tempfile
    d = tempfile.mkdtemp()
    db = os.path.join(d, "mem.db")
    sm = SemanticMemory(db_path=db)
    sm.remember("preference", "code_review_model", "Qwen-Max",
                note="用户偏好用 Qwen-Max 做代码审查")
    sm.remember("preference", "editor", "Neovim", note="用户喜欢用 Neovim")
    sm.remember("repo", "main", "/data/foo", note="主仓库路径")
    res = sm.search_semantic("代码审查 用哪个模型", k=3)
    print("语义召回 top1:", res[0][0], res[0][1], res[0][2])
    sm.close()
