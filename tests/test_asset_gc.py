"""素材库配额与 GC 测试（tests/test_asset_gc.py）。"""

import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from assets import AssetError, AssetStore, QuotaExceeded  # noqa: E402


class TestAssetQuotaAndGC(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="asset_gc_")
        # max_file_bytes 放足够大：让总额度成为下面测试的约束
        self.store = AssetStore(root=os.path.join(self.tmp, "blobs"),
                                db_path=os.path.join(self.tmp, "a.db"),
                                max_bytes=1000, max_file_bytes=10 ** 6)

    # ---- 总额度 ----
    def test_total_quota_enforced(self):
        self.store.put(b"a" * 400, "a.bin")
        with self.assertRaises(QuotaExceeded):
            self.store.put(b"b" * 700, "b.bin")  # 400+700 > 1000
        self.assertTrue(issubclass(QuotaExceeded, AssetError))

    def test_dedup_does_not_double_count_quota(self):
        self.store.put(b"a" * 400, "a.bin")
        r = self.store.put(b"a" * 400, "a.bin")  # 同内容，不新增字节
        self.assertTrue(r["deduped"])
        # 去重 put 后仍能再放 500（总额度按活素材算）
        self.store.put(b"b" * 500, "b.bin")

    def test_soft_delete_frees_quota(self):
        a = self.store.put(b"a" * 600, "a.bin")
        with self.assertRaises(QuotaExceeded):
            self.store.put(b"b" * 500, "b.bin")
        self.store.delete(a["asset_id"])          # 软删后额度释放
        self.store.put(b"b" * 500, "b.bin")       # 现在放得下

    def test_usage_reports_utilization(self):
        self.store.put(b"a" * 250, "a.bin")
        u = self.store.usage()
        self.assertEqual(u["bytes"], 250)
        self.assertEqual(u["assets"], 1)
        self.assertEqual(u["max_bytes"], 1000)
        self.assertEqual(u["utilization"], 0.25)

    def test_single_file_limit_independent(self):
        small = AssetStore(root=os.path.join(self.tmp, "b2"),
                           db_path=os.path.join(self.tmp, "b2.db"),
                           max_bytes=10 ** 9, max_file_bytes=100)
        with self.assertRaises(AssetError):
            small.put(b"x" * 101, "big.bin")

    # ---- GC：过期软删 ----
    def test_gc_removes_expired_soft_deleted(self):
        a = self.store.put(b"a" * 100, "a.bin")
        self.store.delete(a["asset_id"])
        # retention=0 -> 立即可回收（created_at 已过去）
        r = self.store.gc(retention_days=0)
        self.assertEqual(r["count"], 1)
        self.assertEqual(r["freed_bytes"], 100)
        self.assertEqual(r["removed"][0]["reason"], "soft_deleted_expired")
        # 物理文件没了，记录也没了，但审计日志有
        self.assertFalse(os.path.exists(self.store._store_path(a["asset_id"])))
        self.assertIsNone(self.store.info(a["asset_id"]))
        log = self.store.gc_log()
        self.assertEqual(len(log), 1)
        self.assertEqual(log[0]["asset_id"], a["asset_id"])

    def test_gc_keeps_recent_soft_deleted(self):
        a = self.store.put(b"a" * 100, "a.bin")
        self.store.delete(a["asset_id"])
        r = self.store.gc(retention_days=7)  # 7 天保留期内不动
        self.assertEqual(r["count"], 0)
        self.assertTrue(os.path.exists(self.store._store_path(a["asset_id"])))

    def test_gc_never_touches_live_assets(self):
        a = self.store.put(b"a" * 100, "a.bin")
        r = self.store.gc(retention_days=0)
        self.assertEqual(r["count"], 0)
        self.assertEqual(self.store.get(a["asset_id"]), b"a" * 100)

    def test_gc_dry_run(self):
        a = self.store.put(b"a" * 100, "a.bin")
        self.store.delete(a["asset_id"])
        r = self.store.gc(retention_days=0, dry_run=True)
        self.assertTrue(r["dry_run"])
        self.assertEqual(r["count"], 1)  # 会删，但没删
        self.assertTrue(os.path.exists(self.store._store_path(a["asset_id"])))
        self.assertEqual(self.store.gc_log(), [])

    # ---- GC：孤儿 blob ----
    def test_gc_removes_orphan_blobs(self):
        self.store.put(b"a" * 100, "a.bin")
        # 外部塞一个不在数据库里的 blob + 一个崩溃的 .tmp
        stray_dir = os.path.join(self.store.root, "ff")
        os.makedirs(stray_dir, exist_ok=True)
        stray = os.path.join(stray_dir, "deadbeef")
        with open(stray, "wb") as f:
            f.write(b"x" * 50)
        tmp_file = os.path.join(stray_dir, "cafe.tmp")
        with open(tmp_file, "wb") as f:
            f.write(b"x" * 10)
        r = self.store.gc(retention_days=999, include_orphans=True)
        reasons = {x["reason"] for x in r["removed"]}
        self.assertIn("orphan_blob", reasons)
        self.assertEqual(r["freed_bytes"], 60)
        self.assertFalse(os.path.exists(stray))
        self.assertFalse(os.path.exists(tmp_file))

    def test_gc_can_skip_orphans(self):
        self.store.put(b"a" * 100, "a.bin")
        stray_dir = os.path.join(self.store.root, "ff")
        os.makedirs(stray_dir, exist_ok=True)
        stray = os.path.join(stray_dir, "deadbeef")
        with open(stray, "wb") as f:
            f.write(b"x" * 50)
        r = self.store.gc(retention_days=999, include_orphans=False)
        self.assertEqual(r["count"], 0)
        self.assertTrue(os.path.exists(stray))


if __name__ == "__main__":
    unittest.main()
