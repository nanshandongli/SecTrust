"""可观测性⑤：全链路 SkillTrace 字段补全（语义检索中间结果 + guard 白盒）。"""
import os
import shutil
import tempfile
import unittest

from agentskills.router import SkillRouter
from agentskills.skill_md import StandardSkillLibrary
from agentskills.guard_chain import SkillExecutionGuard


def _write_skill(root, name, description, extra=""):
    d = os.path.join(root, name)
    os.makedirs(d, exist_ok=True)
    text = ("---\nname: %s\ndescription: %s\nversion: 1.0.0\nlicense: MIT\n"
            "allowed-tools: [web_search, file_read]\nrisk_level: low\n"
            "cost_tier: low\n%s---\n\n# %s\n\nBODY_MARKER\n"
            % (name, description, extra, name))
    with open(os.path.join(d, "SKILL.md"), "w", encoding="utf-8") as fh:
        fh.write(text)
    return d


def _fixture_library(tmp):
    root = os.path.join(tmp, "lib")
    _write_skill(root, "disk-cleanup",
                 "磁盘释放工具：清理电脑垃圾文件、释放磁盘空间、系统优化瘦身。")
    _write_skill(root, "competitor-report",
                 "竞品分析报告：梳理竞争对手功能对比、输出竞品矩阵。")
    return StandardSkillLibrary(roots=[root])


class _FakeTrace:
    def __init__(self):
        self.events = []

    def record(self, trace_id, kind, **kw):
        self.events.append((trace_id, kind, kw.get("detail")))


class SkillTraceFieldsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sectrust-trace-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_router_skill_match_carries_retrieval_fields(self):
        lib = _fixture_library(self.tmp)
        ft = _FakeTrace()
        out = SkillRouter(library=lib, trace_store=ft,
                          trace_id="tr-1").select("清理电脑释放磁盘", k=3)
        # 缺口⑤：语义检索中间结果落 detail
        detail = [e[2] for e in ft.events if e[1] == "skill_match"][0]
        self.assertIn("vector_results", detail)
        self.assertIn("rerank_results", detail)
        self.assertIn("final_top_k", detail)
        self.assertEqual(detail["final_top_k"], 3)
        # vector_results 来自多路召回 RRF 融合分（ranked 候选应有 rrf）
        self.assertTrue(any("rrf" in v for v in detail["vector_results"]))
        # rerank_results 覆盖最终 Top-K
        self.assertEqual(len(detail["rerank_results"]), len(out["candidates"])
                         - sum(1 for c in out["candidates"] if c.get("filtered_by")))

    def test_router_out_exposes_trace_fields(self):
        lib = _fixture_library(self.tmp)
        out = SkillRouter(library=lib).select("清理电脑释放磁盘", k=3)
        self.assertIn("vector_results", out)
        self.assertIn("rerank_results", out)
        self.assertIn("final_top_k", out)

    def test_guard_decision_carries_bypassed_gates_and_trust(self):
        lib = _fixture_library(self.tmp)
        ft = _FakeTrace()
        guard = SkillExecutionGuard(library=lib, trace_store=ft, trace_id="tr-1")
        res = guard.execute("disk-cleanup", "", actor="tester")
        self.assertTrue(res["ok"])
        # guard_decision 白盒：bypassed_gates / check_mode / trust_tier 齐备
        gd = [e[2] for e in ft.events if e[1] == "guard_decision"][0]
        self.assertIn("bypassed_gates", gd)
        self.assertIn("check_mode", gd)
        self.assertEqual(gd["check_mode"], "l2_only")
        self.assertIn("trust_tier", gd)
        # white 免检依据应在 bypassed_gates 中
        for gate in ("approval", "sandbox", "secret_scan", "evidence",
                     "artifacts", "quality"):
            self.assertIn(gate, gd["bypassed_gates"])

    def test_guard_failure_attaches_error_info(self):
        lib = _fixture_library(self.tmp)
        # 构造一个会失败的执行：显式 security_tier=black（无 manifest → 安装校验
        # 不过 → INSTALL_INVALID；无审批中心时也可能到 APPROVAL_PENDING）。
        # 重点是：任一真实门禁失败都收敛成结构化 error_info。
        ft = _FakeTrace()
        guard = SkillExecutionGuard(library=lib, trace_store=ft, trace_id="tr-1")
        res = guard.execute("disk-cleanup", "", security_tier="black",
                            actor="tester")
        self.assertFalse(res["ok"])
        self.assertIn("error_info", res)
        self.assertIn(res["error_info"]["error_code"],
                      {"INSTALL_INVALID", "APPROVAL_PENDING"})


if __name__ == "__main__":
    unittest.main()
