"""LLM 语义重排（认知升级第3步）回归测试。

覆盖：
  · 无 LLM → heuristic 不变，llm_rerank.applied=False
  · LLM 返回有效重排 → applied=True，顺序与 LLM 一致，reranker="llm-v1"
  · LLM 返回候选集之外的技能名 → 忽略，绝不复活被过滤技能
  · LLM 抛 RuntimeError(no_llm_available) → 降级，applied=False
  · LLM 返回垃圾/不可解析 → 降级，applied=False
  · 候选 < 2 → 不重排
  · 主链路集成：SkillRouter(llm=...) 选技能时 llm_rerank 字段正确落盘且 LLM 顺序生效
"""

import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from agentskills.retrieval import SkillRetriever, SkillCandidate  # noqa: E402
from agentskills.router import SkillRouter  # noqa: E402
from agentskills.skill_md import StandardSkillLibrary  # noqa: E402
from agentskills.llm_rerank import llm_rerank, DEFAULT_MIN_COMPETITIVE  # noqa: E402


def _write_skill(root, name, description, extra=""):
    d = os.path.join(root, name)
    os.makedirs(d, exist_ok=True)
    text = ("---\nname: %s\ndescription: %s\nversion: 1.0.0\nlicense: MIT\n"
            "allowed-tools: [web_search, file_read]\nrisk_level: low\n"
            "cost_tier: low\n%s---\n\n# %s\n\nBODY\n"
            % (name, description, extra, name))
    with open(os.path.join(d, "SKILL.md"), "w", encoding="utf-8") as fh:
        fh.write(text)
    return d


def _fixture_library(tmp):
    root = os.path.join(tmp, "lib")
    _write_skill(root, "disk-cleanup",
                 "磁盘释放工具：清理电脑垃圾文件、释放磁盘空间、系统优化瘦身。")
    _write_skill(root, "competitor-report",
                 "竞品分析报告：梳理竞争对手功能对比、输出竞品矩阵与调研文稿。")
    _write_skill(root, "meeting-summary",
                 "会议纪要工具：把会议录音/笔记整理成结构化要点与行动项。")
    return StandardSkillLibrary(roots=[root]), root


class _FakeLLM:
    """返回预设 JSON 的假 LLM（鸭子类型：.complete(...)）。"""

    def __init__(self, answer, raise_runtime=False, raise_other=False):
        self.answer = answer
        self.raise_runtime = raise_runtime
        self.raise_other = raise_other
        self.calls = 0

    def complete(self, question, context="", history=None, model=None):
        self.calls += 1
        if self.raise_runtime:
            raise RuntimeError("no_llm_available")
        if self.raise_other:
            raise ValueError("boom")
        return {"answer": self.answer, "model": "fake/test", "usage": {}}


class LLMRerankUnitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.lib, _ = _fixture_library(self.tmp)
        self.ret = SkillRetriever(self.lib.load_l1_metadata(include_broken=True),
                                  embedding=False)
        self.cands = self.ret.recall("清理电脑", k=10)
        self.desc = {c.name: str((self.ret._by_name.get(c.name) or {})
                                 .get("description") or "") for c in self.cands}

    def test_no_llm_returns_heuristic_unchanged(self):
        out, meta = llm_rerank("清理电脑", self.cands, self.desc, llm=None)
        self.assertFalse(meta["applied"])
        self.assertEqual([c.name for c in out], [c.name for c in self.cands])

    def test_valid_rerank_reorders_and_marks_llm(self):
        # LLM 把 meeting-summary 排第一（与 heuristic 不同），验证重排生效。
        # 关掉竞争力 gate（min_competitive=0）以隔离"重排逻辑"与"是否触发"决策。
        answer = ('[{"name": "meeting-summary", "reason": "用户要整理会议"},'
                  '{"name": "disk-cleanup", "reason": "磁盘清理相关"}]')
        llm = _FakeLLM(answer)
        out, meta = llm_rerank("帮我整理一下会议记录", self.cands, self.desc,
                               llm=llm, min_competitive=0)
        self.assertTrue(meta["applied"])
        self.assertEqual(meta["reranker"], "llm-v1")
        self.assertEqual(out[0].name, "meeting-summary")
        self.assertEqual(out[0].reranker, "llm-v1")
        self.assertEqual(out[0].llm_reason, "用户要整理会议")
        # 第三个候选（LLM 未提及）补在末尾，且未被丢弃
        self.assertIn("competitor-report", [c.name for c in out])

    def test_llm_cannot_resurrect_filtered_skill(self):
        # LLM 返回一个根本不在候选集里的技能名 → 必须忽略
        answer = '[{"name": "nonexistent-skill", "reason": "x"}]'
        llm = _FakeLLM(answer)
        out, meta = llm_rerank("清理电脑", self.cands, self.desc, llm=llm)
        self.assertFalse(meta["applied"])  # 无有效命中 → 降级
        names = [c.name for c in out]
        self.assertNotIn("nonexistent-skill", names)

    def test_runtime_error_degrades(self):
        llm = _FakeLLM("", raise_runtime=True)
        out, meta = llm_rerank("清理电脑", self.cands, self.desc, llm=llm,
                               min_competitive=0)
        self.assertFalse(meta["applied"])
        self.assertIn("不可用", meta["reason"])

    def test_garbage_response_degrades(self):
        llm = _FakeLLM("我觉得应该清理磁盘吧，随便选一个")
        out, meta = llm_rerank("清理电脑", self.cands, self.desc, llm=llm,
                               min_competitive=0)
        self.assertFalse(meta["applied"])
        self.assertIn("不可解析", meta["reason"])

    def test_less_than_two_candidates_skips(self):
        out, meta = llm_rerank("x", self.cands[:1], self.desc, llm=_FakeLLM("[]"))
        self.assertFalse(meta["applied"])
        self.assertIn("不足", meta["reason"])


def _cand(name, rrf):
    return SkillCandidate(name=name, scores={"rrf": rrf})


class CompetitiveGateTest(unittest.TestCase):
    """竞争力 gate：只数"与榜首真有竞争力"的候选（按 RRF 比例）。

    动机（bench_rerank_latency.py 实测）：小技能库里 recall 返回「全部技能」，
    故"候选总数"≈库大小，单纯按总数阈值拦不住；真正该区分的是榜首之外有没有
    并列相关的技能。仅 1 个真相关时 heuristic 已自信选对，LLM 只是重排噪声。
    """

    def test_single_dominant_skips_llm(self):
        # 榜首 0.0164，其余 0.0078（< 0.6*0.0164=0.0098）→ 竞争力=1 < 2 → 跳过
        cands = [_cand("top", 0.0164), _cand("noise1", 0.0078),
                 _cand("noise2", 0.0076)]
        desc = {c.name: "d" for c in cands}
        out, meta = llm_rerank("q", cands, desc, llm=_FakeLLM("[]"))
        self.assertFalse(meta["applied"])
        self.assertIn("竞争力候选", meta["reason"])
        self.assertEqual(meta["reranker"], "heuristic-v1")

    def test_two_competitive_fires_llm(self):
        # 榜首 0.0164，第二 0.012（>= 0.0098）→ 竞争力=2 >= 2 → 触发
        cands = [_cand("top", 0.0164), _cand("second", 0.012),
                 _cand("noise", 0.0076)]
        desc = {c.name: "d" for c in cands}
        llm = _FakeLLM('[{"name": "second", "reason": "歧义"}]')
        out, meta = llm_rerank("q", cands, desc, llm=llm)
        self.assertTrue(meta["applied"])
        self.assertEqual(out[0].name, "second")

    def test_min_competitive_zero_forces_fire(self):
        # min_competitive=0 → 关闭 gate，只要 >=2 候选就触发（即便仅 1 个真相关）
        cands = [_cand("top", 0.0164), _cand("noise", 0.0076)]
        desc = {c.name: "d" for c in cands}
        llm = _FakeLLM('[{"name": "top", "reason": "x"}]')
        out, meta = llm_rerank("q", cands, desc, llm=llm, min_competitive=0)
        self.assertTrue(meta["applied"])

    def test_threshold_tunable_via_env(self):
        # SECTRUST_LLM_RERANK_MIN_COMPETITIVE=3 时，竞争力=2 也应跳过
        old = os.environ.get("SECTRUST_LLM_RERANK_MIN_COMPETITIVE")
        os.environ["SECTRUST_LLM_RERANK_MIN_COMPETITIVE"] = "3"
        try:
            from importlib import reload
            import agentskills.llm_rerank as lr
            reload(lr)
            cands = [_cand("top", 0.0164), _cand("second", 0.012),
                     _cand("noise", 0.0076)]
            desc = {c.name: "d" for c in cands}
            out, meta = lr.llm_rerank("q", cands, desc, llm=_FakeLLM("[]"))
            self.assertFalse(meta["applied"])
            self.assertIn("竞争力候选 2", meta["reason"])
        finally:
            if old is None:
                os.environ.pop("SECTRUST_LLM_RERANK_MIN_COMPETITIVE", None)
            else:
                os.environ["SECTRUST_LLM_RERANK_MIN_COMPETITIVE"] = old

    def test_default_constant_is_two(self):
        self.assertEqual(DEFAULT_MIN_COMPETITIVE, 2)


class SkillRouterLLMRerankTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.lib, _ = _fixture_library(self.tmp)

    def test_router_without_llm_uses_heuristic(self):
        r = SkillRouter(library=self.lib, llm=None)
        out = r.select("清理电脑", k=3)
        self.assertFalse(out["llm_rerank"]["applied"])
        self.assertEqual(out["reranker"], "heuristic-v1")

    def test_router_with_llm_rerank_takes_llm_order(self):
        # heuristic 对「清理电脑磁盘」会把 disk-cleanup 排第一；LLM 改指定
        # meeting-summary 第一，用来证明 LLM 重排顺序覆盖 heuristic。
        # 关掉竞争力 gate 以隔离"重排生效"与"是否触发"决策。
        answer = ('[{"name": "meeting-summary", "reason": "先整理会议"},'
                  '{"name": "disk-cleanup", "reason": "再清磁盘"}]')
        r = SkillRouter(library=self.lib, llm=_FakeLLM(answer),
                        llm_rerank_min_competitive=0)
        out = r.select("帮我清理一下电脑磁盘", k=3)
        self.assertTrue(out["llm_rerank"]["applied"])
        self.assertEqual(out["reranker"], "llm-v1")
        self.assertEqual(out["candidates"][0]["name"], "meeting-summary")
        self.assertEqual(out["candidates"][0]["reranker"], "llm-v1")
        # heuristic 第一的 disk-cleanup 被 LLM 推到第二
        self.assertEqual(out["candidates"][1]["name"], "disk-cleanup")

    def test_router_llm_unavailable_degrades(self):
        r = SkillRouter(library=self.lib, llm=_FakeLLM("", raise_runtime=True))
        out = r.select("清理电脑", k=3)
        self.assertFalse(out["llm_rerank"]["applied"])
        self.assertEqual(out["reranker"], "heuristic-v1")


if __name__ == "__main__":
    unittest.main()
