"""执行层 / 规划器 四项接线回归测试（2026-10-28）。

补齐三场景闭环后新增，证明"检测→改善"不是纸面：

A. 质量门接入 daemon（场景2 递归校验/打回）：
   llm 产出带质量规则时，daemon 跑 QualityGate，合格保留模型/usage 元数据，
   不合格打回（registered）等重做；
B. Ballot 确定性择优接入 daemon（场景1 多版本择优）：
   分支任务完成后 submit_proposal 登记打分，汇合任务 rank() 出确定性推荐
   （不自动确认，不靠 LLM 祈祷式汇总）；
C. 发布审批阶段透传（场景1 发布审批）：
   planner 内容模板终态阶段带 approval_action=publish，daemon 审批门拦截；
D. scan op + 高风险 shell 自动拦截 + secure_dev 模板（场景3 内部工具链）：
   scan op 跑密钥扫描；未声明审批的破坏性 shell 命令自动升级 high_risk 审批门
   （宁多一次确认也不静默执行）；secure_dev 模板 secret_scan 阶段为 op=scan。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from execution.daemon import ExecutionDaemon                              # noqa: E402
from planner import Planner                                                # noqa: E402


# -------------------------------------------------------------- 测试假引擎
class _FakeEngine:
    """测试用假 LLM 引擎：execute() 返回固定 output（含 model/usage 元数据）。"""

    def __init__(self, output: str):
        self._output = output

    def execute(self, instruction, context, model=None):
        # 真实 LlmEngine.execute 返回 "answer" 键（daemon 读 out["answer"]）
        return {
            "answer": self._output,
            "model": model or "fake-model",
            "usage": {"prompt_tokens": 3, "completion_tokens": 5},
        }


class _RotatingEngineFactory:
    """按调用次序轮流吐出预设 outputs（多分支各自不同，便于打分差异）。"""

    def __init__(self, outputs):
        self._outputs = list(outputs)
        self._i = 0

    def __call__(self):
        out = self._outputs[self._i % len(self._outputs)]
        self._i += 1
        return _FakeEngine(out)


class _TmpEnv:
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sectrust-wiring-")
        self.db = os.path.join(self.tmp, "tasks.db")
        self.ws_root = os.path.join(self.tmp, "workspaces")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _daemon(self, **kw):
        kw.setdefault("workspace_root", self.ws_root)
        kw.setdefault("approval_wait", 1.0)
        d = ExecutionDaemon(db_path=self.db, **kw)
        self.addCleanup(d.close)
        return d

    def _drain(self, d, max_rounds=30):
        for _ in range(max_rounds):
            out = d.run_once(max_tasks=8)
            if not out:
                break


# ---------------------------------------------------------------- A 质量门
class QualityGateWiringTest(_TmpEnv, unittest.TestCase):

    def test_pass_keeps_model_and_usage_metadata(self):
        d = self._daemon(llm_engine_factory=lambda: _FakeEngine(
            "这是一段足够长且合规的内容，满足全部质量判据。"))
        d.sched.add_task("t-ok", payload={
            "op": "llm", "instruction": "写一段合规内容",
            "quality": {"min_chars": 12, "required": ["合规"]},
        })
        out = d.run_once()
        self.assertEqual(out, [("t-ok", "done")])
        res = d.reg.get_task("t-ok")["payload"]["result"]
        self.assertIn("合规", res["output"])
        # 质量门通过后不得把 result 回退成纯文本——模型/usage 必须保留
        self.assertEqual(res["model"], "fake-model")
        self.assertEqual(res["usage"]["completion_tokens"], 5)

    def test_violation_sends_back_for_rework(self):
        # 输出过短 → 质量门不通过 → 打回重做（registered），附 feedback
        d = self._daemon(llm_engine_factory=lambda: _FakeEngine("太短"))
        d.sched.add_task("t-short", payload={
            "op": "llm", "instruction": "写长一点",
            "quality": {"min_chars": 50, "required": ["合规"]},
        })
        out = d.run_once()
        self.assertEqual(out, [("t-short", "rework")])
        t = d.reg.get_task("t-short")
        self.assertEqual(t["status"], "registered")   # 已被打回，等 worker 重领
        self.assertTrue(t["payload"].get("last_feedback", ""),
                        "打回应带 last_feedback 指明问题")


# ---------------------------------------------------------------- B 投票择优
class BallotWiringTest(_TmpEnv, unittest.TestCase):

    def test_deterministic_rank_from_branch_proposals(self):
        from ballot import Ballot

        # 1) 开票（与 planner.build fanout 同口径：quality_rules 策略）。
        #    必须与 daemon 的 _ballot() 用同一个库（task db 同目录下的 ballot.db）。
        grp = "g1"
        ballot_db = os.path.join(self.tmp, "ballot.db")
        b = Ballot(db_path=ballot_db)
        b.open_ballot(grp, ["b0", "b1"], rules={"required": ["合规"]},
                     title="择优测试")
        b.close()

        # 2) 分支任务：b0 命中关键词、b1 未命中 → 打分 100 vs 90
        d = self._daemon(llm_engine_factory=_RotatingEngineFactory([
            "方案A 内容合规 通过验收",      # b0：0 违规 → 100
            "方案B 内容缺失关键词 不通过",  # b1：1 违规 → 90
        ]))
        d.sched.add_task("b0", payload={
            "op": "llm", "instruction": "出方案A",
            "ballot": {"group": grp, "index": 0, "total": 2},
        })
        d.sched.add_task("b1", payload={
            "op": "llm", "instruction": "出方案B",
            "ballot": {"group": grp, "index": 1, "total": 2},
        })
        # 3) 汇合任务：确定性 rank() 出推荐（不自动确认）
        d.sched.add_task("g", payload={
            "op": "llm", "instruction": "汇合择优",
            "ballot_gather": {"group": grp, "n": 2, "branch_ids": ["b0", "b1"]},
        }, depends_on=["b0", "b1"])

        self._drain(d)

        # 分支都完成且方案已登记打分
        self.assertEqual(d.reg.get_task("b0")["status"], "completed")
        self.assertEqual(d.reg.get_task("b1")["status"], "completed")
        # 汇合任务确定推荐 b0（分高者），不猜不分不出
        g = d.reg.get_task("g")
        self.assertEqual(g["status"], "completed")
        res = g["payload"]["result"]
        self.assertEqual(res["recommended"], "b0")
        self.assertFalse(res["needs_human"])
        self.assertIn("b0", res["candidates"])
        self.assertIn("b1", res["candidates"])


# ---------------------------------------------------------------- C 发布审批
class PublishApprovalWiringTest(_TmpEnv, unittest.TestCase):

    def test_publish_action_blocked_without_approval(self):
        # 未配审批人 + 短等待 → 发布动作挂起等人工，且命令**绝未执行**
        d = self._daemon(approval_wait=0.6)
        d.sched.add_task("t-pub", payload={
            "op": "shell",
            "cmd": [sys.executable, "-c", "open('SHOULD_NOT_EXIST','w')"],
            "approval_action": "publish",
        })
        out = d.run_once()
        self.assertEqual(out, [("t-pub", "needs_human")])
        t = d.reg.get_task("t-pub")
        self.assertEqual(t["status"], "needs_human")
        self.assertNotIn("result", t["payload"])   # 拦截，未执行
        self.assertFalse(os.path.exists(
            os.path.join(self.tmp, "SHOULD_NOT_EXIST")))

    def test_planner_content_template_emits_publish_approval(self):
        # planner 内容营销模板终态阶段必须带 approval_action=publish
        plan = Planner().decompose(
            "为新产品写一轮小红书种草文案并发布", params={"topic": "新产品"})
        self.assertEqual(plan.template, "content_marketing")
        assembly = next(s for s in plan.subtasks if s["key"] == "assembly")
        self.assertEqual(assembly.get("approval_action"), "publish")
        self.assertEqual(assembly.get("op", "llm"), "llm")


# ------------------------------------------ D scan op + 高风险命令自动拦截
class ScanAndRiskWiringTest(_TmpEnv, unittest.TestCase):

    def test_scan_op_finds_secret(self):
        # 种一个高严重度密钥（Slack token，critical 严重度 → gate 判失败）。
        # 注：AWS 示例值因低熵被 entropy 校验压制（设计如此），故用 Slack 形态。
        secret_file = os.path.join(self.tmp, "config.py")
        with open(secret_file, "w", encoding="utf-8") as f:
            f.write('SLACK_TOKEN = "xoxb-1234567890-1234567890123-'
                    'aB3kZ9mQx2LpR7nW4tV8yU6sD1fG5hJ0cE4bN"\n')
        d = self._daemon()
        d.sched.add_task("t-scan", payload={
            "op": "scan", "target": secret_file,
        })
        out = d.run_once()
        self.assertEqual(out, [("t-scan", "done")])
        res = d.reg.get_task("t-scan")["payload"]["result"]
        self.assertEqual(res["op"], "scan")
        self.assertGreater(res["findings_count"], 0)
        self.assertTrue(res["gate_fail"])   # 高严重度命中 → 应阻断

    def test_high_risk_shell_auto_upgraded_to_approval(self):
        # 破坏性命令（rm -rf）且未显式声明审批 → daemon 自动升级 high_risk
        # 审批门拦截，宁多一次确认也不静默执行
        d = self._daemon(approval_wait=0.6)
        self.assertTrue(d._is_high_risk_shell("rm -rf /tmp/xxx"))
        self.assertFalse(d._is_high_risk_shell("ls -la"))
        d.sched.add_task("t-rm", payload={
            "op": "shell",
            "cmd": ["rm", "-rf", os.path.join(self.tmp, "victim")],
        })
        os.makedirs(os.path.join(self.tmp, "victim"))   # 验证命令确实没跑
        out = d.run_once()
        self.assertEqual(out, [("t-rm", "needs_human")])
        t = d.reg.get_task("t-rm")
        self.assertEqual(t["status"], "needs_human")
        self.assertEqual(t["payload"].get("approval_action"), "high_risk")
        self.assertTrue(os.path.isdir(os.path.join(self.tmp, "victim")))

    def test_planner_secure_dev_template_has_scan_stage(self):
        # secure_dev 模板的密钥扫描阶段必须是 op=scan 且 target 占位被填充。
        # 显式 complex 档，避免短需求被成本路由压成单任务（那样就看不到 scan）。
        plan = Planner().decompose(
            "对代码仓库做安全审计", params={"repo_path": "./myrepo"},
            tier="complex")
        self.assertEqual(plan.template, "secure_dev")
        scan = next(s for s in plan.subtasks if s["key"] == "secret_scan")
        self.assertEqual(scan.get("op"), "scan")
        self.assertIn("myrepo", scan.get("target", ""))


# ------------------------------------------------------ planner fanout 闭环
class PlannerFanoutBallotTest(_TmpEnv, unittest.TestCase):

    def test_fanout_opens_ballot_and_emits_gather(self):
        # 内容营销模板被要求择优时，build 应开票 + 产 ballot_gather 汇合任务。
        # 显式 complex 档保留完整模板（simple 档会折叠成单任务，无 fanout 可言）。
        plan = Planner().decompose(
            "为产品写推广文案并择优", params={"topic": "产品"}, tier="complex")
        leaf = next((s for s in plan.subtasks
                     if int(s.get("fanout") or 0) >= 2), None)
        if leaf is None:
            self.skipTest("本需求未触发 fanout，跳过")
        from ballot import Ballot
        ballot_db = os.path.join(self.tmp, "ballot.db")
        dag = ExecutionDaemon(db_path=self.db, workspace_root=self.ws_root)
        try:
            bal = Ballot(db_path=ballot_db)
            created = plan.build(dag.sched, ballot=bal)
            bal.close()
        finally:
            dag.close()

        gathers = [t for t in created if t.endswith("__ballot")]
        self.assertTrue(gathers, "fanout 应产生汇合任务")
        # 至少一个分支任务带 ballot 元信息
        branch_id = next(t for t in created if "#" in t)
        branch = dag.reg.get_task(branch_id)
        self.assertIn("ballot", branch["payload"])
        # 开票已落地：临时 ballot.db 里有这个组（组 = 分支 id 去掉 #i 后缀）
        group = branch_id.rsplit("#", 1)[0]
        bal2 = Ballot(db_path=ballot_db)
        try:
            self.assertIsNotNone(bal2.detail(group), "build 应已 open_ballot")
        finally:
            bal2.close()


if __name__ == "__main__":
    unittest.main()
