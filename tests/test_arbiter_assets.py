"""冲突仲裁（Arbiter）与素材库（AssetStore）测试。"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from arbiter import Arbiter, ArbiterError
from assets import AssetError, AssetStore


class TestArbiter(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="arbiter_test_")
        self.a = Arbiter(os.path.join(self.tmp, "a.db"))

    def test_submit_and_positions(self):
        pid = self.a.submit("t1", "月活", 1200, "research", note="报告")
        poss = self.a.positions("t1", "月活")
        self.assertEqual(len(poss), 1)
        self.assertEqual(poss[0]["value"], 1200)
        self.assertEqual(poss[0]["position_id"], pid)

    def test_empty_key_rejected(self):
        with self.assertRaises(ArbiterError):
            self.a.submit("t1", "", 1, "src")

    def test_conflicts_detection(self):
        self.a.submit("t1", "月活", 1200, "research")
        self.a.submit("t1", "月活", 850, "roi")
        cf = self.a.conflicts("t1")
        self.assertEqual(len(cf), 1)
        self.assertEqual(cf[0]["positions"], 2)
        self.assertEqual(set(cf[0]["sources"]), {"research", "roi"})

    def test_single_position_auto_adopted(self):
        self.a.submit("t1", "预算", 500, "strategy")
        r = self.a.resolve("t1", "预算")
        self.assertEqual(r["status"], "decided")
        self.assertEqual(r["value"], 500)
        self.assertEqual(r["strategy"], "single")

    def test_default_pending_human(self):
        self.a.submit("t1", "月活", 1200, "research")
        self.a.submit("t1", "月活", 850, "roi")
        r = self.a.resolve("t1", "月活")
        self.assertEqual(r["status"], "pending_human")
        self.assertIsNone(self.a.decision("t1", "月活"))
        self.assertEqual(len(self.a.conflicts("t1")), 1)  # 仍未决

    def test_resolve_recency(self):
        self.a.submit("t1", "k", "old", "research")
        self.a.submit("t1", "k", "new", "roi")  # 后提交
        r = self.a.resolve("t1", "k", strategy="recency")
        self.assertEqual(r["value"], "new")

    def test_resolve_source_priority(self):
        self.a.submit("t1", "k", "估算值", "estimate")
        self.a.submit("t1", "k", "报告值", "research")  # 权威序更靠前
        r = self.a.resolve("t1", "k", strategy="source_priority")
        self.assertEqual(r["value"], "报告值")
        self.assertIn("research", r["reason"])

    def test_resolve_evidence_without_evidence_pending(self):
        self.a.submit("t1", "k", "a", "research")
        self.a.submit("t1", "k", "b", "roi")
        r = self.a.resolve("t1", "k", strategy="evidence")
        self.assertEqual(r["status"], "pending_human")

    def test_resolve_evidence(self):
        self.a.submit("t1", "k", "无证据", "research")
        self.a.submit("t1", "k", "有证据", "roi", evidence_id="ev-123")
        r = self.a.resolve("t1", "k", strategy="evidence")
        self.assertEqual(r["value"], "有证据")

    def test_unknown_strategy_falls_back_human(self):
        self.a.submit("t1", "k", "a", "research")
        self.a.submit("t1", "k", "b", "roi")
        r = self.a.resolve("t1", "k", strategy="magic")
        self.assertEqual(r["status"], "pending_human")
        self.assertIsNone(self.a.decision("t1", "k"))

    def test_decide_records_human(self):
        self.a.submit("t1", "k", "a", "research")
        self.a.submit("t1", "k", "b", "roi")
        r = self.a.decide("t1", "k", "人工取折中", by="alice",
                          reason="两边各有依据，取保守值")
        self.assertEqual(r["decided_by"], "human:alice")
        d = self.a.decision("t1", "k")
        self.assertEqual(d["value"], "人工取折中")
        self.assertEqual(d["reason"], "两边各有依据，取保守值")
        # 裁定后不再算未决
        self.assertEqual(self.a.conflicts("t1"), [])

    def test_superseded_after_decision(self):
        p1 = self.a.submit("t1", "k", "a", "research")
        self.a.submit("t1", "k", "b", "roi")
        self.a.decide("t1", "k", "a", by="alice", chosen_from=p1)
        # 默认查询只见胜者；含已 superseded 才见全部
        self.assertEqual(len(self.a.positions("t1", "k")), 1)
        self.assertEqual(len(self.a.positions("t1", "k",
                                              include_superseded=True)), 2)

    def test_report(self):
        self.a.submit("t1", "k1", "a", "research")
        self.a.submit("t1", "k1", "b", "roi")   # 未决
        self.a.submit("t1", "k2", "x", "roi")
        self.a.submit("t1", "k2", "y", "roi")
        self.a.decide("t1", "k2", "x", by="alice")  # 已决
        rep = self.a.report("t1")
        self.assertEqual([c["key"] for c in rep["pending"]], ["k1"])
        self.assertEqual(len(rep["decided"]), 1)

    def test_resolve_no_positions_raises(self):
        with self.assertRaises(ArbiterError):
            self.a.resolve("t1", "ghost")


class TestAssetStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="assets_test_")
        self.store = AssetStore(root=os.path.join(self.tmp, "blobs"),
                                db_path=os.path.join(self.tmp, "a.db"))

    def test_put_get_roundtrip(self):
        r = self.store.put(b"hello-bytes", "a.txt", task_id="t1", by="w1")
        self.assertEqual(self.store.get(r["asset_id"]), b"hello-bytes")
        info = self.store.info(r["asset_id"])
        self.assertEqual(info["name"], "a.txt")
        self.assertEqual(info["task_id"], "t1")
        self.assertEqual(info["bytes"], 11)
        self.assertFalse(info["deleted"])

    def test_dedupe(self):
        r1 = self.store.put(b"same-content", "a.bin")
        r2 = self.store.put(b"same-content", "b.bin")  # 不同名字同内容
        self.assertEqual(r1["asset_id"], r2["asset_id"])
        self.assertTrue(r2["deduped"])
        self.assertEqual(self.store.stats()["assets"], 1)  # 只存一份

    def test_different_content_different_id(self):
        r1 = self.store.put(b"v1", "主视觉.png", kind="image")
        r2 = self.store.put(b"v2", "主视觉.png", kind="image")
        self.assertNotEqual(r1["asset_id"], r2["asset_id"])

    def test_versions_chain(self):
        self.store.put(b"v1", "主视觉.png", kind="image",
                       task_id="t1", by="design-ai")
        self.store.put(b"v2", "主视觉.png", kind="image",
                       task_id="t2", by="design-ai")
        vs = self.store.versions("主视觉.png")
        self.assertEqual([v["version"] for v in vs], [1, 2])
        self.assertEqual([v["bytes"] for v in vs], [2, 2])
        self.assertEqual([v["task_id"] for v in vs], ["t1", "t2"])

    def test_path_traversal_rejected(self):
        for bad in ("../evil", "a/b", "..", ""):
            with self.assertRaises(AssetError, msg=bad):
                self.store.put(b"x", bad)

    def test_kind_invalid_falls_back(self):
        r = self.store.put(b"x", "a.xyz", kind="不存在的类型")
        self.assertEqual(r["kind"], "file")

    def test_max_bytes_rejected(self):
        small = AssetStore(root=os.path.join(self.tmp, "b"),
                           db_path=os.path.join(self.tmp, "b.db"),
                           max_bytes=10)
        with self.assertRaises(AssetError):
            small.put(b"x" * 11, "big.bin")

    def test_link_and_list_by_task(self):
        r = self.store.put(b"x", "a.txt")
        self.assertTrue(self.store.link(r["asset_id"], "p1/design"))
        self.assertEqual(len(self.store.list(task_id="p1/design")), 1)
        self.assertEqual(self.store.list(task_id="other"), [])

    def test_list_by_kind(self):
        self.store.put(b"x", "a.png", kind="image")
        self.store.put(b"y", "b.md", kind="document")
        self.assertEqual(len(self.store.list(kind="image")), 1)

    def test_soft_delete(self):
        r = self.store.put(b"x", "a.txt")
        self.assertTrue(self.store.delete(r["asset_id"]))
        self.assertFalse(self.store.delete(r["asset_id"]))  # 幂等
        with self.assertRaises(AssetError):
            self.store.get(r["asset_id"])
        self.assertEqual(self.store.list(), [])  # 列表默认不含已删
        self.assertTrue(self.store.info(r["asset_id"])["deleted"])

    def test_get_missing_raises(self):
        with self.assertRaises(AssetError):
            self.store.get("0" * 64)

    def test_info_missing_none(self):
        self.assertIsNone(self.store.info("0" * 64))

    def test_stats(self):
        self.store.put(b"xxx", "a.png", kind="image")
        self.store.put(b"yyyy", "b.md", kind="document")
        st = self.store.stats()
        self.assertEqual(st["assets"], 2)
        self.assertEqual(st["bytes"], 7)
        self.assertEqual(st["by_kind"], {"image": 1, "document": 1})


if __name__ == "__main__":
    unittest.main(verbosity=2)
