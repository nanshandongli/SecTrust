"""
轻量长期记忆 —— 单元测试（回归保障）

覆盖：set/get 往返、覆盖更新、forget、list_category、search 子串、top_used 访问频次、
跨会话持久化（写后关、新实例读）、JSON 值类型、backup 含未 checkpoint 数据、WAL 模式生效。

零第三方依赖：unittest 编写，pytest 也能直接收集。
运行：
    python -m unittest tests.test_memory -v
    pytest tests/test_memory.py
"""

import json
import os
import tempfile
import unittest

from memory import (
    CAT_COMMAND,
    CAT_PREFERENCE,
    CAT_REPO,
    MemoryStore,
)


class MemoryTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "memory.db")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestCore(MemoryTestBase):
    def test_set_get_roundtrip(self):
        mem = MemoryStore(db_path=self.db)
        mem.remember("preference", "theme", "dark")
        self.assertEqual(mem.recall("preference", "theme"), "dark")
        mem.close()

    def test_upsert_updates_value_and_time(self):
        mem = MemoryStore(db_path=self.db)
        mem.remember("repo", "a", "/old")
        first = mem.recall("repo", "a", touch=False)
        import time
        time.sleep(0.01)
        mem.remember("repo", "a", "/new")  # 覆盖
        second = mem.recall("repo", "a", touch=False)
        self.assertEqual(second, "/new")
        self.assertGreater(mem.list_category("repo")[0]["updated_at"],
                           mem.list_category("repo")[0]["created_at"])
        mem.close()

    def test_forget(self):
        mem = MemoryStore(db_path=self.db)
        mem.remember("preference", "x", 1)
        self.assertTrue(mem.forget("preference", "x"))
        self.assertIsNone(mem.recall("preference", "x"))
        self.assertFalse(mem.forget("preference", "x"))  # 再删返回 False
        mem.close()

    def test_list_category(self):
        mem = MemoryStore(db_path=self.db)
        mem.remember(CAT_REPO, "a", "/a")
        mem.remember(CAT_REPO, "b", "/b")
        mem.remember(CAT_PREFERENCE, "p", "v")  # 不同分类，不应串
        rows = mem.list_category(CAT_REPO)
        self.assertEqual({r["key"] for r in rows}, {"a", "b"})
        self.assertEqual(mem.list_category(CAT_PREFERENCE)[0]["key"], "p")
        mem.close()

    def test_search_substring(self):
        mem = MemoryStore(db_path=self.db)
        mem.remember("preference", "code_review_model", "Qwen-Max", note="代码审查模型")
        mem.remember("repo", "sample_repo", "/path/sample", note="样例仓库")
        # 命中 key
        hit_key = mem.search("review")
        self.assertEqual({r["key"] for r in hit_key}, {"code_review_model"})
        # 命中 note
        hit_note = mem.search("样例")
        self.assertEqual({r["key"] for r in hit_note}, {"sample_repo"})
        # 按分类过滤
        only_pref = mem.search("model", category="preference")
        self.assertEqual(len(only_pref), 1)
        mem.close()

    def test_top_used_ranking(self):
        mem = MemoryStore(db_path=self.db)
        mem.remember(CAT_COMMAND, "rare", "r")
        mem.remember(CAT_COMMAND, "common", "c")
        for _ in range(5):
            mem.recall(CAT_COMMAND, "common")  # 访问 5 次
        top = mem.top_used(CAT_COMMAND, n=5)
        self.assertEqual(top[0]["key"], "common")
        self.assertEqual(top[0]["access_count"], 5)
        self.assertEqual(top[1]["key"], "rare")
        self.assertEqual(top[1]["access_count"], 0)
        mem.close()

    def test_recent_ordering(self):
        mem = MemoryStore(db_path=self.db)
        mem.remember("preference", "a", 1)
        mem.remember("preference", "b", 2)
        recent = mem.recent("preference", n=1)
        self.assertEqual(recent[0]["key"], "b")
        mem.close()

    def test_stats_and_categories(self):
        mem = MemoryStore(db_path=self.db)
        mem.remember_preference("lang", "Python")
        mem.remember_command("run_tests", "pytest")
        stats = mem.stats()
        self.assertEqual(stats.get(CAT_PREFERENCE), 1)
        self.assertEqual(stats.get(CAT_COMMAND), 1)
        self.assertIn(CAT_PREFERENCE, mem.categories())
        self.assertIn(CAT_COMMAND, mem.categories())
        mem.close()


class TestJsonValues(MemoryTestBase):
    def test_various_json_types(self):
        mem = MemoryStore(db_path=self.db)
        mem.remember("preference", "vec", {"a": 1, "b": [1, 2, 3]})
        mem.remember("preference", "num", 42)
        mem.remember("preference", "lst", ["x", "y"])
        self.assertEqual(mem.recall("preference", "vec"), {"a": 1, "b": [1, 2, 3]})
        self.assertEqual(mem.recall("preference", "num"), 42)
        self.assertEqual(mem.recall("preference", "lst"), ["x", "y"])
        mem.close()

    def test_missing_returns_default(self):
        mem = MemoryStore(db_path=self.db)
        self.assertIsNone(mem.recall("preference", "nope"))
        self.assertEqual(mem.recall("preference", "nope", default="fallback"), "fallback")
        mem.close()


class TestCrossSession(MemoryTestBase):
    def test_persist_across_instances(self):
        # 会话 A：写
        a = MemoryStore(db_path=self.db)
        a.remember_preference("code_review_model", "Qwen-Max")
        a.remember_repo("this_project", "/abs/path")
        a.close()

        # 会话 B：新实例（模拟下次启动）读
        b = MemoryStore(db_path=self.db)
        self.assertEqual(b.recall_preference("code_review_model"), "Qwen-Max")
        self.assertEqual(b.recall_repo("this_project"), "/abs/path")
        b.close()

    def test_rebuild_after_delete(self):
        mem = MemoryStore(db_path=self.db)
        mem.remember("preference", "x", 1)
        mem.close()
        for f in (self.db, self.db + "-wal", self.db + "-shm"):
            try:
                os.remove(f)
            except FileNotFoundError:
                pass
        mem2 = MemoryStore(db_path=self.db)
        mem2.remember("preference", "y", 2)
        self.assertEqual(mem2.recall("preference", "y"), 2)
        mem2.close()


class TestHardening(MemoryTestBase):
    def test_wal_enabled(self):
        mem = MemoryStore(db_path=self.db)
        with mem._connect() as conn:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        mem.close()
        self.assertEqual(mode, "wal")

    def test_backup_includes_uncheckpointed(self):
        mem = MemoryStore(db_path=self.db)
        for i in range(10):
            mem.remember("preference", f"k{i}", i)
        mem.close()

        dest = os.path.join(self.tmp, "backup.db")
        MemoryStore(db_path=self.db).backup(dest)

        bk = MemoryStore(db_path=dest)
        self.assertEqual(len(bk.list_category("preference")), 10)
        bk.close()

    def test_synchronous_invalid(self):
        with self.assertRaises(ValueError):
            MemoryStore(db_path=self.db, synchronous="WEIRD")


class TestConvenience(MemoryTestBase):
    def test_convenience_methods(self):
        mem = MemoryStore(db_path=self.db)
        mem.remember_preference("lang", "Python")
        mem.remember_command("t", "pytest -q")
        mem.remember_repo("r", "/repo")
        self.assertEqual(mem.recall_preference("lang"), "Python")
        self.assertEqual(mem.recall_command("t"), "pytest -q")
        self.assertEqual(mem.recall_repo("r"), "/repo")
        # 便捷方法间不串类
        self.assertIsNone(mem.recall_preference("t"))
        mem.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
