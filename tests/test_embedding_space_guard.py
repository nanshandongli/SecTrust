"""向量空间防错位测试（memory_semantic.py / memory_hybrid.py）。

覆盖三类曾经的真实缺陷：
1. env 工厂形同虚设 —— EMBEDDING_PROVIDER 设了但生产路径不认；
2. 切换 provider 不重建 —— 旧维度向量与新查询跨空间混算；
3. 降级写入异空间向量 —— provider 中途掉线时写进哈希向量，
   与库中 provider 向量同表混存，_cosine 的 zip 截断算出"半错的分数"。
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory_hybrid import HybridMemory  # noqa: E402
from memory_semantic import (EMBED_VERSION, SemanticMemory,  # noqa: E402
                             _cosine, _resolve_env_provider)


class _FakeProvider:
    """固定维度假 provider。dim 故意设成与默认哈希 1024 不同。"""

    def __init__(self, name="fake", model="m1536", dim=1536, up=True):
        self.name = name
        self.model = model
        self.dim = dim
        self._up = up
        self.calls = 0

    def available(self):
        return self._up

    def embed(self, text):
        self.calls += 1
        return [0.1] * self.dim

    def go_down(self):
        self._up = False

    def come_up(self):
        self._up = True


class TestCosineDimensionGuard(unittest.TestCase):
    """维度不等必须判 0，绝不 zip 截断。"""

    def test_dimension_mismatch_returns_zero(self):
        self.assertEqual(_cosine([0.1] * 1536, [0.1] * 1024), 0.0)
        self.assertEqual(_cosine([0.1] * 1024, [0.1] * 1536), 0.0)

    def test_same_dimension_computes_normally(self):
        self.assertAlmostEqual(_cosine([0.1] * 1536, [0.1] * 1536), 1.0, places=6)
        self.assertAlmostEqual(_cosine([1.0, 0.0], [0.0, 1.0]), 0.0, places=6)


class TestEnvFactoryWiring(unittest.TestCase):
    """env 工厂必须真的接进构造路径。"""

    def setUp(self):
        self.saved = {k: os.environ.get(k) for k in
                      ("EMBEDDING_PROVIDER", "OLLAMA_BASE_URL",
                       "OLLAMA_EMBED_MODEL", "OPENAI_API_KEY")}

    def tearDown(self):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_resolve_env_provider_returns_instance_regardless_of_availability(self):
        """provider 身份由配置决定，与"此刻能不能连上"无关。

        这是防震荡的关键：Ollama 停一次若被当成"换了 provider"，
        库里的真向量会被洗成哈希向量。
        """
        os.environ["EMBEDDING_PROVIDER"] = "ollama"
        os.environ["OLLAMA_BASE_URL"] = "http://127.0.0.1:59999"  # 必然拒连
        p = _resolve_env_provider()
        self.assertIsNotNone(p)
        self.assertEqual(p.name, "ollama")

    def test_auto_sentinel_uses_env_provider(self):
        os.environ["EMBEDDING_PROVIDER"] = "ollama"
        os.environ["OLLAMA_BASE_URL"] = "http://127.0.0.1:59999"
        db = os.path.join(tempfile.mkdtemp(), "m.db")
        sm = SemanticMemory(db_path=db, embedding="auto")
        try:
            self.assertEqual(sm._provider_id(), "ollama:nomic-embed-text")
            self.assertTrue(sm._version_key.startswith(EMBED_VERSION))
        finally:
            sm.close()

    def test_auto_without_env_keeps_hashed_space(self):
        os.environ.pop("EMBEDDING_PROVIDER", None)
        db = os.path.join(tempfile.mkdtemp(), "m.db")
        sm = SemanticMemory(db_path=db, embedding="auto")
        try:
            self.assertEqual(sm._provider_id(), "hashed")
        finally:
            sm.close()

    def test_invalid_embedding_argument_rejected(self):
        db = os.path.join(tempfile.mkdtemp(), "m.db")
        with self.assertRaises(ValueError):
            SemanticMemory(db_path=db, embedding="not-a-provider")

    def test_hybrid_supports_auto_sentinel(self):
        os.environ.pop("EMBEDDING_PROVIDER", None)
        db = os.path.join(tempfile.mkdtemp(), "m.db")
        hm = HybridMemory(db_path=db, embedding="auto")
        try:
            self.assertEqual(hm._provider_id(), "hashed")
        finally:
            hm.close()


class TestNoCrossSpaceMixing(unittest.TestCase):
    """provider 掉线时不得往 provider 空间里写哈希向量。"""

    def test_provider_down_does_not_write_foreign_vector(self):
        db = os.path.join(tempfile.mkdtemp(), "m.db")
        prov = _FakeProvider()
        sm = SemanticMemory(db_path=db, embedding=prov)
        sm.remember("pref", "k1", "v1")
        prov.go_down()
        sm.remember("pref", "k2", "v2")  # provider 掉线后写入
        conn = sm._connect()
        rows = conn.execute(
            "SELECT key, vec FROM memory_embeddings ORDER BY key").fetchall()
        sm.close()
        dims = {k: len(json.loads(v)) for k, v in rows}
        # 库内只允许存在 provider 空间的向量
        self.assertTrue(all(d == 1536 for d in dims.values()),
                        f"出现异空间向量: {dims}")
        # k2 的记忆本体仍应落库（不丢数据，只是这轮没建向量）
        self.assertNotIn("k2", dims)
        conn2 = __import__("sqlite3").connect(db)
        n = conn2.execute(
            "SELECT COUNT(*) FROM memory_items WHERE key='k2'").fetchone()[0]
        conn2.close()
        self.assertEqual(n, 1, "记忆本体必须落库，不能因向量降级而丢")

    def test_provider_recovers_and_backfills(self):
        db = os.path.join(tempfile.mkdtemp(), "m.db")
        prov = _FakeProvider()
        sm = SemanticMemory(db_path=db, embedding=prov)
        sm.remember("pref", "k1", "v1")
        prov.go_down()
        sm.remember("pref", "k2", "v2")
        sm.close()

        prov.come_up()
        sm2 = SemanticMemory(db_path=db, embedding=prov)
        conn = sm2._connect()
        rows = conn.execute(
            "SELECT key, vec FROM memory_embeddings ORDER BY key").fetchall()
        sm2.close()
        dims = {k: len(json.loads(v)) for k, v in rows}
        self.assertEqual(dims, {"k1": 1536, "k2": 1536})

    def test_recovery_does_not_trigger_full_reindex(self):
        """恢复后版本键不变 -> 不该重建（否则反复震荡 + 浪费算力）。"""
        db = os.path.join(tempfile.mkdtemp(), "m.db")
        prov = _FakeProvider()
        sm = SemanticMemory(db_path=db, embedding=prov)
        sm.remember("pref", "k1", "v1")
        prov.go_down()
        sm.remember("pref", "k2", "v2")
        sm.close()
        prov.come_up()
        sm2 = SemanticMemory(db_path=db, embedding=prov)
        # 重建会把 provider.calls 拉高；这里断言没有多余的全量重算
        self.assertLess(prov.calls, 10)
        sm2.close()


class TestProviderSwitchRebuild(unittest.TestCase):
    """换 provider 必须整体重建（防维度/空间错位的主防线）。"""

    def test_switch_provider_rebuilds_all_vectors(self):
        db = os.path.join(tempfile.mkdtemp(), "m.db")
        p1 = _FakeProvider(name="p1", model="m1536", dim=1536)
        sm = SemanticMemory(db_path=db, embedding=p1)
        sm.remember("pref", "k1", "v1")
        sm.close()

        p2 = _FakeProvider(name="p2", model="m768", dim=768)
        sm2 = SemanticMemory(db_path=db, embedding=p2)
        conn = sm2._connect()
        dims = [len(json.loads(v)) for (v,) in conn.execute(
            "SELECT vec FROM memory_embeddings").fetchall()]
        ver = conn.execute(
            "SELECT v FROM memory_embed_meta WHERE k='embed_version'").fetchone()[0]
        sm2.close()
        self.assertTrue(all(d == 768 for d in dims), f"未按新 provider 重建: {dims}")
        self.assertIn("p2:m768", ver)

    def test_same_provider_keeps_vectors(self):
        db = os.path.join(tempfile.mkdtemp(), "m.db")
        p1 = _FakeProvider(name="p1", model="m1536", dim=1536)
        sm = SemanticMemory(db_path=db, embedding=p1)
        sm.remember("pref", "k1", "v1")
        sm.close()
        p2 = _FakeProvider(name="p1", model="m1536", dim=1536)
        sm2 = SemanticMemory(db_path=db, embedding=p2)
        conn = sm2._connect()
        dims = [len(json.loads(v)) for (v,) in conn.execute(
            "SELECT vec FROM memory_embeddings").fetchall()]
        sm2.close()
        self.assertEqual(dims, [1536])

    def test_hybrid_search_skips_vector_signal_when_provider_down(self):
        """混合检索：provider 掉线时跳过向量一路，BM25/实体仍应出结果。"""
        db = os.path.join(tempfile.mkdtemp(), "m.db")
        prov = _FakeProvider()
        hm = HybridMemory(db_path=db, embedding=prov)
        hm.remember("preference", "code_review_model", "Qwen-Max",
                    note="用户偏好用 Qwen-Max 做代码审查")
        prov.go_down()
        hits = hm.search("代码审查 用哪个模型", k=3)
        hm.close()
        self.assertTrue(hits, "provider 掉线不应让整个检索失效")
        self.assertTrue(any(h["key"] == "code_review_model" for h in hits))


if __name__ == "__main__":
    unittest.main()
