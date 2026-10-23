"""扫描基准测试：自造语料 + 外部公开语料（gitleaks 向量）双语料评测。"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.scan_bench import load_corpus_dir, run_bench  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PUBLIC_CORPUS = os.path.join(REPO_ROOT, "data", "bench_corpus")


class TestScanBench(unittest.TestCase):
    def test_builtin_corpus_perfect(self):
        r = run_bench()
        self.assertEqual(r["corpus_source"], "builtin")
        self.assertEqual(r["fn"], 0)
        self.assertEqual(r["fp"], 0)
        self.assertEqual(r["f1"], 1.0)

    def test_public_corpus_loads_and_passes(self):
        if not os.path.isfile(os.path.join(PUBLIC_CORPUS, "manifest.json")):
            self.skipTest("公开语料未抓取（tools/fetch_bench_corpus.py）")
        corpus, source = load_corpus_dir(PUBLIC_CORPUS)
        self.assertEqual(len(corpus), 3)
        self.assertIn("gitleaks", source)
        r = run_bench(corpus, source)
        # 公开向量：2 个真阳性（AWS key / 高熵口令）+ 1 个干净文档
        self.assertEqual(r["tp"], 2)
        self.assertEqual(r["fp"], 0)
        self.assertEqual(r["fn"], 0)
        self.assertEqual(r["tn"], 1)
        self.assertEqual(r["f1"], 1.0)
        self.assertNotEqual(r["corpus_source"], "builtin")

    def test_missing_manifest_fails_loudly(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                load_corpus_dir(tmp)  # 空目录必须明确失败，不跑空集报虚高分


if __name__ == "__main__":
    unittest.main()
