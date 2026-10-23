"""Ballot 择优器的单元测试——重点在纪律，不在功能。

纪律（改动时不可破坏）：
- rank 只推荐不生效；confirm 是唯一生效路径
- 平分/无判据/未交齐 → needs_human，不硬裁
- 未知策略降级 human；单方案/超上限/重复开票/外来 task_id 一律拒绝
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ballot import Ballot, BallotError, BALLOT_STRATEGIES, TIE_THRESHOLD

RULES = {"min_chars": 50, "required": ["结论"], "format": "markdown"}
GOOD = ("# 结论\n\n本方案覆盖全部要点：市场判断、进入路径、投入节奏、"
        "风险预案与退出条件均已明确，数据充分，结论可执行。")
BAD = "没有标题也没有明确观点，内容敷衍"  # 无标题行 + 无"结论" + 过短


class BallotUnitTest(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp(prefix="ballot_unit_")
        self.bt = Ballot(db_path=os.path.join(d, "ballot.db"))
        self.addCleanup(self.bt.close)

    # ---- 开票纪律 ----
    def test_single_proposal_rejected(self):
        with self.assertRaises(BallotError):
            self.bt.open_ballot("g", ["only/one"])

    def test_duplicate_open_rejected(self):
        self.bt.open_ballot("g", ["a", "b"], rules=RULES)
        with self.assertRaises(BallotError):
            self.bt.open_ballot("g", ["c", "d"])

    def test_over_max_rejected(self):
        ids = [f"t/{i}" for i in range(self.bt.max_proposals + 1)]
        with self.assertRaises(BallotError):
            self.bt.open_ballot("g", ids)

    def test_unknown_strategy_falls_back_to_human(self):
        d = self.bt.open_ballot("g", ["a", "b"], rules=RULES,
                                strategy="magic_vote")
        self.assertEqual(d["strategy"], "human",
                         "未知策略必须降级 human（不静默乱裁）")
        self.assertIn("human", BALLOT_STRATEGIES)

    def test_empty_group_name_rejected(self):
        with self.assertRaises(BallotError):
            self.bt.open_ballot("", ["a", "b"])

    # ---- 提交纪律 ----
    def test_alien_task_id_rejected(self):
        self.bt.open_ballot("g", ["a", "b"], rules=RULES)
        with self.assertRaises(BallotError):
            self.bt.submit_proposal("g", "outsider", GOOD)

    def test_unknown_group_rejected(self):
        with self.assertRaises(BallotError):
            self.bt.submit_proposal("nope", "a", GOOD)

    def test_resubmit_overwrites(self):
        """同一 task_id 重复提交 → 覆盖（允许修订后重投，最后一次为准）。"""
        self.bt.open_ballot("g", ["a", "b"], rules=RULES)
        self.bt.submit_proposal("g", "a", BAD)
        self.bt.submit_proposal("g", "a", GOOD)   # 修订后重投
        self.bt.submit_proposal("g", "b", BAD)
        r = self.bt.rank("g")
        self.assertEqual(r["recommended"], "a", "修订后的好方案应被推荐")

    # ---- 排序纪律 ----
    def test_rank_requires_all_proposals(self):
        self.bt.open_ballot("g", ["a", "b", "c"], rules=RULES)
        self.bt.submit_proposal("g", "a", GOOD)
        r = self.bt.rank("g")
        self.assertTrue(r["needs_human"])
        self.assertIsNone(r["winner_task"])

    def test_no_rules_never_ranks(self):
        """没有判据就不排序——没有验收标准却"择优"是拍脑袋。"""
        self.bt.open_ballot("g", ["a", "b"], rules=None)
        self.bt.submit_proposal("g", "a", GOOD)
        self.bt.submit_proposal("g", "b", GOOD)
        r = self.bt.rank("g")
        self.assertTrue(r["needs_human"])
        self.assertIn("判据", r["reason"])

    def test_tie_goes_human(self):
        self.bt.open_ballot("g", ["a", "b"], rules=RULES)
        self.bt.submit_proposal("g", "a", GOOD)
        self.bt.submit_proposal("g", "b", GOOD)
        r = self.bt.rank("g")
        self.assertTrue(r["needs_human"])
        self.assertIn("分不出高下", r["reason"])

    def test_clear_winner_recommended_but_not_effective(self):
        """有高下时给出推荐，但**不自动生效**（推荐 ≠ 生效，两个字段分开存）。"""
        self.bt.open_ballot("g", ["a", "b"], rules=RULES)
        self.bt.submit_proposal("g", "a", GOOD)
        self.bt.submit_proposal("g", "b", BAD)
        r = self.bt.rank("g")
        self.assertEqual(r["recommended"], "a")
        self.assertFalse(r["needs_human"])
        d = self.bt.detail("g")
        self.assertEqual(d["recommended"], "a", "推荐有值")
        self.assertIsNone(d["winner_task"],
                          "confirm 前 winner_task 必须为 NULL（推荐不是生效）")
        self.assertNotEqual(d["status"], "confirmed")

    def test_confidence_scales_with_gap(self):
        self.bt.open_ballot("g", ["a", "b"], rules=RULES)
        self.bt.submit_proposal("g", "a", GOOD)   # 0 违规 = 100
        self.bt.submit_proposal("g", "b", BAD)    # 3 违规 = 70
        r = self.bt.rank("g")
        self.assertAlmostEqual(r["confidence"], 0.3, places=4)  # 30/100

    # ---- 生效纪律 ----
    def test_confirm_requires_submitted_proposal(self):
        self.bt.open_ballot("g", ["a", "b"], rules=RULES)
        self.bt.submit_proposal("g", "a", GOOD)
        with self.assertRaises(BallotError):
            self.bt.confirm("g", "b")  # b 未提交

    def test_confirm_alien_rejected(self):
        self.bt.open_ballot("g", ["a", "b"], rules=RULES)
        self.bt.submit_proposal("g", "a", GOOD)
        self.bt.submit_proposal("g", "b", GOOD)
        with self.assertRaises(BallotError):
            self.bt.confirm("g", "outsider")

    def test_confirm_sets_winner(self):
        self.bt.open_ballot("g", ["a", "b"], rules=RULES)
        self.bt.submit_proposal("g", "a", GOOD)
        self.bt.submit_proposal("g", "b", BAD)
        d = self.bt.confirm("g", "a", by="human", reason="复核认可")
        self.assertEqual(d["status"], "confirmed")
        self.assertEqual(d["winner_task"], "a")
        self.assertFalse(d["needs_human"])


if __name__ == "__main__":
    unittest.main()
