"""投票/辩论协同模式的端到端闭环测试。

验证路径（与象棋 E2E 同级的"活能力"验证）：
  需求（含多方案意图）→ planner 注入 fanout → build 展开 3 分支 + 汇合点
  → 3 个 worker 各自认领产出 → ballot 提交方案 → rank 排序推荐
  → 人工 confirm 生效 → 汇合任务消费胜出方案完成。

纪律断言（改动时不可破坏）：
- rank 只推荐不生效；confirm 是唯一生效路径
- 平分/无判据/未交齐 → needs_human，不硬裁
- 汇合点必须等全部分支 completed 才就绪（图一致性）
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from planner import Planner
from orchestration import DagScheduler
from ballot import Ballot, BallotError


def _fresh():
    d = tempfile.mkdtemp(prefix="ballot_e2e_")
    return (os.path.join(d, "reg.db"), os.path.join(d, "ballot.db"))


def _fake_output(seed: str, quality: bool) -> str:
    """造一份分支产出，严格对照 typeset 判据（min_chars=500/required=['目录']/markdown）。

    quality=True  → 三条全过（0 违规，100 分）
    quality=False → 缺"目录"且无标题（多条违规）
    长度用循环补足，不靠数字数（判据是 500，就确保 >=600）。
    """
    head = (f"# 方案 {seed}：进入东南亚市场评估报告\n\n## 目录\n"
            f"1. 市场概况\n2. 进入策略\n3. 风险与结论\n\n")
    if not quality:
        # 差方案：无标题行、无"目录"、且短（三重违规）
        return f"方案 {seed}：简单说说，进不进都行。"
    body = ""
    chunk = "东南亚市场分阶段投入分析：增长潜力与合规风险并存，需本地化运营与渠道共建。"
    while len(head) + len(body) < 600:
        body += chunk
    return head + body + "\n\n结论：建议分阶段进入，首站印尼与越南。"


class BallotE2ETest(unittest.TestCase):
    def setUp(self):
        self.reg_db, self.ballot_db = _fresh()
        self.planner = Planner()
        self.ballot = Ballot(db_path=self.ballot_db)
        self.sch = DagScheduler(db_path=self.reg_db)

    def tearDown(self):
        self.ballot.close()
        self.sch.close()

    def _run_pipeline(self, outputs: dict, human_pick: str = None):
        """按 DAG 顺序驱动整条流水线，返回 (完成的任务, 投票结果, 最后任务)。

        human_pick：rank 转人工时由测试注入的裁决（None = 留待人类，汇合任务不完成）。
        """
        plan = self.planner.decompose(
            "多个角度分析：要不要进入东南亚市场", params={"topic": "东南亚市场"})
        fanout_stages = [s for s in plan.subtasks if s.get("fanout")]
        self.assertEqual(len(fanout_stages), 1, "应恰好一个 stage 被注入 fanout")
        plan.build(self.sch, plan_id="e2e", ballot=self.ballot)

        done, ballot_res, last = [], None, None
        # 轮询驱动：直到没有就绪任务
        while True:
            task = self.sch.claim_ready(runner_id="w")
            if task is None:
                break
            tid = task["task_id"]
            last = tid
            payload = task["payload"] if isinstance(task["payload"], dict) else {}

            if "ballot" in payload:  # 分支任务：产出 → 提交方案
                meta = payload["ballot"]
                out = outputs.get(meta["index"],
                                  _fake_output(str(meta["index"]), True))
                self.ballot.submit_proposal(meta["group"], tid, out,
                                            source=f"worker-{meta['index']}")
                self.sch.complete(tid, result={"output": out})
                done.append(tid)
            elif "ballot_gather" in payload:  # 汇合任务：rank → 生效 → 完成
                meta = payload["ballot_gather"]
                ballot_res = self.ballot.rank(meta["group"])
                if ballot_res["needs_human"]:
                    if human_pick is None:
                        # 系统不硬裁、测试不代裁：汇合任务留在 running 等人类
                        continue
                    winner = human_pick
                    self.ballot.confirm(meta["group"], winner, by="human",
                                        reason="测试人工裁决")
                else:
                    # rank 只给推荐（recommended），生效必须 confirm
                    winner = ballot_res["recommended"]
                    self.ballot.confirm(meta["group"], winner, by="human",
                                        reason="E2E 人工确认")
                self.sch.complete(tid, result={"winner": winner})
                done.append(tid)
            else:  # 普通任务：直接完成
                self.sch.complete(tid, result={"output": "ok"})
                done.append(tid)
        return done, ballot_res, last

    def test_full_ballot_pipeline(self):
        """三方案质量有高下：最优者胜出，人工确认后汇合完成。"""
        outputs = {
            0: _fake_output("A", True) + " 补充：渠道优先印尼与越南。",
            # B 去掉所有 markdown 标题行（`#` 与 `##` 都算标题）→ 1 条格式违规（90 分）
            1: _fake_output("B", True)
               .replace("# 方案 B：进入东南亚市场评估报告", "方案 B 评估（无标题行）")
               .replace("## 目录", "目录索引"),
            2: _fake_output("C", False),  # 缺"目录"且过短
        }
        done, res, last = self._run_pipeline(outputs)

        # 1) 全部任务完成
        stats = self.sch.stats()
        self.assertEqual(stats["by_status"].get("completed"), len(done),
                         f"应全部完成: {stats}")
        self.assertEqual(len(done), 8, "5 stage 展开为 3 分支 + 汇合 = 8 任务")

        # 2) 胜出者是质量最高的分支
        self.assertEqual(res["recommended"], "e2e/typeset#0",
                         f"方案 A 质量最高应被推荐，实际 {res['recommended']}")
        self.assertFalse(res["needs_human"])
        self.assertGreater(res["confidence"], 0)

        # 3) 生效路径：confirm 后 status=confirmed，winner_task 才有值
        d = self.ballot.detail("e2e/typeset")
        self.assertEqual(d["status"], "confirmed")
        self.assertEqual(d["winner_task"], "e2e/typeset#0")

        # 4) 汇合任务最后完成（图一致性：等全部分支）
        self.assertTrue(last.endswith("__ballot"))

    def test_tie_goes_to_human(self):
        """两个方案同分 → 系统不硬裁，needs_human；注入人工裁决后才生效。"""
        same = _fake_output("同", True)
        outputs = {0: same, 1: same, 2: _fake_output("C", False)}
        done, res, last = self._run_pipeline(outputs, human_pick="e2e/typeset#1")
        self.assertTrue(res["needs_human"], f"平分应转人工: {res['reason']}")
        self.assertIsNone(res["recommended"], "rank 阶段不得有推荐（只报需人工）")
        # 人工裁决后生效
        d = self.ballot.detail("e2e/typeset")
        self.assertEqual(d["status"], "confirmed")
        self.assertEqual(d["winner_task"], "e2e/typeset#1")

    def test_tie_without_human_stays_open(self):
        """平分且无人工介入 → 投票组停在 open，汇合任务不完成（不硬裁）。"""
        same = _fake_output("同", True)
        done, res, last = self._run_pipeline({0: same, 1: same,
                                              2: _fake_output("C", False)})
        d = self.ballot.detail("e2e/typeset")
        self.assertEqual(d["status"], "open")
        self.assertEqual(d["needs_human"], 1)
        # 汇合任务被认领但未完成（等人类），其余 7 个已完成
        self.assertEqual(len(done), 7)

    def test_gather_waits_for_all_branches(self):
        """汇合点在任一分支未完成时不得就绪（图一致性检查点）。"""
        plan = self.planner.decompose("多个角度分析：要不要进入东南亚市场")
        plan.build(self.sch, plan_id="g", ballot=self.ballot)
        gather = "g/typeset__ballot"
        # 手动推进到汇合点就绪前
        for _ in range(4):  # data/analysis/writing/review 四个前置
            t = self.sch.claim_ready(runner_id="w")
            self.assertIsNotNone(t)
            self.sch.complete(t["task_id"], result={"output": "ok"})
        # 三分支应同时就绪
        branches = [t["task_id"] for t in self.sch.ready_tasks()
                    if "#" in t["task_id"]]
        self.assertEqual(len(branches), 3, "三分支应同时就绪")
        t0 = self.sch.claim_ready(task_id="g/typeset#0", runner_id="w")
        self.assertIsNotNone(t0)
        self.sch.complete("g/typeset#0", result={"output": "x"})
        # 汇合点此时不可认领（还有 2 个分支未完）
        self.assertIsNone(self.sch.claim_ready(task_id=gather, runner_id="w"),
                          "分支未齐时汇合点不得就绪")
        # 补齐剩余分支
        for i in (1, 2):
            t = self.sch.claim_ready(task_id=f"g/typeset#{i}", runner_id="w")
            self.assertIsNotNone(t)
            self.sch.complete(f"g/typeset#{i}", result={"output": "y"})
        # 现在汇合点可认领
        t = self.sch.claim_ready(task_id=gather, runner_id="w")
        self.assertIsNotNone(t, "分支齐后汇合点应就绪")

    def test_no_fanout_word_no_ballot(self):
        """没有多方案意图的需求不开票（成本纪律：不猜）。"""
        plan = self.planner.decompose("写一份本周运营周报")
        self.assertEqual([s for s in plan.subtasks if s.get("fanout")], [])
        plan.build(self.sch, plan_id="nf", ballot=self.ballot)
        self.assertEqual(self.ballot.ballots(), [], "不应开任何票")


if __name__ == "__main__":
    unittest.main()
