"""可插拔 embedding provider 测试（memory_semantic.py）：env 工厂、Ollama 双形态、
切换 provider 触发重建（防维度/空间错位）。"""

import io
import json
import os
import sys
import tempfile
import unittest
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory_semantic import (EMBED_VERSION, HashedNGramIndex,  # noqa: E402
                             OllamaEmbeddingProvider,
                             OpenAIEmbeddingProvider, SemanticMemory,
                             embedding_from_env)


class _FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeProvider:
    """测试用假 provider：固定 4 维向量（与默认 1024 维 deliberately 不同）。"""
    name = "fake"
    model = "test-model"

    def available(self):
        return True

    def embed(self, text):
        return [0.1, 0.2, 0.3, 0.4]


class TestEmbeddingFromEnv(unittest.TestCase):
    def setUp(self):
        self.saved = {k: os.environ.get(k) for k in
                      ("EMBEDDING_PROVIDER", "OPENAI_API_KEY",
                       "OLLAMA_BASE_URL", "OLLAMA_EMBED_MODEL")}

    def tearDown(self):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_unset_means_default(self):
        os.environ.pop("EMBEDDING_PROVIDER", None)
        self.assertIsNone(embedding_from_env())

    def test_none_and_unknown_degrade_to_none(self):
        os.environ["EMBEDDING_PROVIDER"] = "none"
        self.assertIsNone(embedding_from_env())
        os.environ["EMBEDDING_PROVIDER"] = "some-typo-provider"
        self.assertIsNone(embedding_from_env())  # 未知名字不猜

    def test_openai_with_key(self):
        os.environ["EMBEDDING_PROVIDER"] = "openai"
        os.environ["OPENAI_API_KEY"] = "k"
        p = embedding_from_env()
        self.assertIsInstance(p, OpenAIEmbeddingProvider)
        os.environ.pop("OPENAI_API_KEY", None)
        self.assertIsNone(embedding_from_env())  # 无 key 不可用 -> None

    def test_ollama_unreachable_is_none(self):
        os.environ["EMBEDDING_PROVIDER"] = "ollama"
        os.environ["OLLAMA_BASE_URL"] = "http://127.0.0.1:59999"  # 必然拒连
        p = embedding_from_env()
        self.assertIsNone(p)


class TestOllamaProvider(unittest.TestCase):
    def _patch(self, payload):
        orig = urllib.request.urlopen

        def fake(req, timeout=None):
            return _FakeResp(json.dumps(payload).encode())
        urllib.request.urlopen = fake
        self.addCleanup(setattr, urllib.request, "urlopen", orig)

    def test_new_response_shape(self):
        self._patch({"embeddings": [[0.5, 0.6]]})
        p = OllamaEmbeddingProvider()
        self.assertEqual(p.embed("hi"), [0.5, 0.6])

    def test_old_response_shape(self):
        self._patch({"embedding": [0.7, 0.8]})
        self.assertEqual(OllamaEmbeddingProvider().embed("hi"), [0.7, 0.8])

    def test_bad_response_raises(self):
        self._patch({"nope": 1})
        with self.assertRaises(RuntimeError):
            OllamaEmbeddingProvider().embed("hi")


class TestProviderSwitchReindex(unittest.TestCase):
    def test_switching_provider_rebuilds_vectors(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "mem.db")
            # 第一遍：默认哈希索引（1024 维）
            sm = SemanticMemory(db_path=db)
            sm.remember("preference", "k1", "v1", note="备注一")
            sm.close()

            # 第二遍：换成假 provider（4 维）——必须重建，不能混算
            sm2 = SemanticMemory(db_path=db, embedding=_FakeProvider())
            hits = sm2.search_semantic("任意查询", k=1)
            self.assertTrue(hits)  # 重建后仍可检索
            # 存储向量应是新 provider 的 4 维，而非旧的 1024 维
            conn = sm2._connect()
            vec = json.loads(conn.execute(
                "SELECT vec FROM memory_embeddings WHERE key='k1'").fetchone()[0])
            self.assertEqual(len(vec), 4)
            ver = conn.execute(
                "SELECT v FROM memory_embed_meta WHERE k='embed_version'").fetchone()[0]
            self.assertIn("fake:test-model", ver)
            sm2.close()

    def test_same_provider_keeps_vectors(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "mem.db")
            sm = SemanticMemory(db_path=db)
            sm.remember("preference", "k1", "v1")
            before = sm.index._vecs.get("preference::k1")
            sm.close()
            sm2 = SemanticMemory(db_path=db)  # 同 provider：不重建
            self.assertEqual(sm2.index._vecs.get("preference::k1"), before)
            sm2.close()

    def test_version_key_contains_encoding(self):
        sm = SemanticMemory(db_path=os.path.join(tempfile.mkdtemp(), "m.db"))
        try:
            self.assertTrue(sm._version_key.startswith(EMBED_VERSION))
            self.assertIn("hashed", sm._version_key)
        finally:
            sm.close()


if __name__ == "__main__":
    unittest.main()
