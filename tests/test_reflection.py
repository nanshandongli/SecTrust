"""「从防守到智能」智能运行时演进回归测试（夜间窗口 2026-10-30/31）。

三方向覆盖：
  方向一·自我反思闭环 —— 错误归因(3类) / 经验库(落盘·去重·精确+相似匹配) /
      修正假设(LLM 产出 + 诚实降级规则式) / 复用命中(相似 query 闭环)。
  方向二·分层记忆 —— 工作记忆(cached_params) / 情景记忆(成败轨迹) /
      程序记忆(工具·参数模式提炼) / 经验必须能被后续 query 检索(闭环)。
  方向三·Confidence Loop —— 排序一致性度量(Kendall tau) / 未配置降级 /
      gate 未触发不升级 / 高一致不升级 / 低一致升级强模型 / 阈值可调。
  接线集成 —— SkillRouter 经验复用预填 feedback 不改 chosen；Confidence Loop
      字段暴露；daemon 失败→反思落库→下次相似 query 命中(端到端闭环)；默认关闭
      Confidence Loop 不破坏既有行为。

诚实边界断言（与全仓一致）：
  · 无 LLM / 调不通 / 不可解析 → 规则式降级，generated_by=="rule"，绝不 Mock。
  · 经验复用只预填 feedback，不改 chosen、不绕过任何门禁。
  · Confidence Loop 未配 weak/strong → configured=False，行为与旧版一致。

全部离线：临时目录 + 假 LLM（鸭子类型），不触网、不连真实模型。
零第三方依赖；pytest 与 unittest 均可运行。
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from agentskills.reflection import (  # noqa: E402
    classify_error, ExperienceStore, ReflectionEngine, LayeredMemory,
    ConfidenceLoop, build_runtime, _match_tokens,
    CAT_TOOL_FAILURE, CAT_MISSING_PARAM, CAT_INSUFFICIENT_PERMISSION, CAT_UNKNOWN,
)
from agentskills.retrieval import SkillCandidate  # noqa: E402
from agentskills.router import SkillRouter  # noqa: E402
from agentskills.skill_md import StandardSkillLibrary  # noqa: E402
from execution.daemon import ExecutionDaemon  # noqa: E402


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _err(code, stage="", tech="", rule=""):
    # 模拟真实 ErrorInfo.build：一定带 human_readable_message（否则
    # self_correction_prompt 会诚实返回空串，那是「无可消费信息」不是 bug）
    return {"error_code": code, "stage": stage, "technical_detail": tech,
            "rule_id": rule, "human_readable_message": "操作失败（%s）" % code}


class _FakeLLM:
    """鸭子类型 .complete(question, context, history, model) -> {answer, model}。

    answer 可为 str 或 callable(model)->str 或 None(=返回空)。
    """

    def __init__(self, answer="", raise_exc=None):
        self.answer = answer
        self.raise_exc = raise_exc
        self.calls = []

    def complete(self, question, context="", history=None, model=None):
        self.calls.append(model)
        if self.raise_exc:
            raise self.raise_exc
        a = self.answer(model) if callable(self.answer) else self.answer
        return {"answer": a, "model": model or "", "usage": {}}


def _write_skill(root, name, description, extra=""):
    d = os.path.join(root, name)
    os.makedirs(d, exist_ok=True)
    text = ("---\nname: %s\ndescription: %s\nversion: 1.0.0\nlicense: MIT\n"
            "allowed-tools: [web_search, file_read]\nrisk_level: low\n"
            "cost_tier: low\n%s---\n\n# %s\n\nBODY_SENTINEL_9527\n"
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
                 "会议纪要工具：把会议录音笔记整理成结构化要点与行动项。")
    return StandardSkillLibrary(roots=[root])


# ==========================================================================
# 方向一 · 自我反思闭环
# ==========================================================================
class ErrorAttributionTest(unittest.TestCase):
    """错误归因：工具失败 / 参数缺失 / 权限不足 三分类。"""

    def test_tool_failure_codes(self):
        for code in ("SANDBOX_FAILED", "TRANSPORT_ERROR", "UPSTREAM_ERROR",
                     "QUALITY_VIOLATION"):
            self.assertEqual(classify_error(_err(code))["category"],
                             CAT_TOOL_FAILURE)

    def test_missing_param_codes(self):
        for code in ("MISSING_PARAM", "EVIDENCE_MISSING", "ARTIFACT_MISSING"):
            self.assertEqual(classify_error(_err(code))["category"],
                             CAT_MISSING_PARAM)

    def test_insufficient_permission_codes(self):
        for code in ("APPROVAL_PENDING", "SECURITY_POLICY_VIOLATION",
                     "DOMAIN_NOT_ALLOWED", "CREDENTIAL_UNAVAILABLE",
                     "SECRET_DETECTED"):
            self.assertEqual(classify_error(_err(code))["category"],
                             CAT_INSUFFICIENT_PERMISSION)

    def test_stage_fallback_when_code_unknown(self):
        # 未知 code 但 stage 可判 → 按 stage 归因
        self.assertEqual(classify_error(_err("WEIRD", stage="sandbox"))["category"],
                         CAT_TOOL_FAILURE)
        self.assertEqual(classify_error(_err("WEIRD", stage="approval"))["category"],
                         CAT_INSUFFICIENT_PERMISSION)

    def test_unknown_and_none(self):
        self.assertEqual(classify_error(_err("WEIRD", stage="weird"))["category"],
                         CAT_UNKNOWN)
        self.assertEqual(classify_error(None)["category"], CAT_UNKNOWN)
        self.assertEqual(classify_error({})["category"], CAT_UNKNOWN)


class ExperienceStoreTest(unittest.TestCase):
    """经验库：落盘 / 去重 / 精确+相似匹配 / 损坏降级 / 持久化。"""

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="sectrust-exp-")
        self.path = os.path.join(self.d, "experience.json")

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def _rec(self, code, query, correction="修正X"):
        return {"category": CAT_TOOL_FAILURE, "error_code": code, "stage": "sandbox",
                "query": query, "correction": correction}

    def test_add_and_persist_reload(self):
        s1 = ExperienceStore(path=self.path)
        s1.add_experience(self._rec("SANDBOX_FAILED", "清理磁盘"))
        self.assertTrue(os.path.exists(self.path))
        s2 = ExperienceStore(path=self.path)   # 新实例落盘重载
        self.assertEqual(s2.snapshot()["experiences"], 1)

    def test_dedup_same_id_merges_not_duplicates(self):
        s = ExperienceStore(path=self.path)
        r = dict(self._rec("SANDBOX_FAILED", "清理磁盘"), id="fixed1")
        s.add_experience(dict(r))
        s.add_experience(dict(r, correction="修正Y"))
        self.assertEqual(s.snapshot()["experiences"], 1)
        hit = s.match_experience(query="清理磁盘")
        self.assertEqual(hit["hit"]["correction"], "修正Y")

    def test_error_exact_match(self):
        s = ExperienceStore(path=self.path)
        s.add_experience(self._rec("SANDBOX_FAILED", "清理磁盘"))
        # 真实 APPROVAL_PENDING 的 stage 是 "approval"，与 error_info 一致才精确命中
        s.add_experience(dict(self._rec("APPROVAL_PENDING", "发布文章"),
                              stage="approval"))
        hit = s.match_experience(query="完全不相干",
                                 error_info=_err("APPROVAL_PENDING", "approval"))
        self.assertEqual(hit["match_type"], "error_exact")
        self.assertEqual(hit["score"], 1.0)
        self.assertEqual(hit["hit"]["query"], "发布文章")

    def test_cjk_similar_query_match(self):
        s = ExperienceStore(path=self.path)
        s.add_experience(self._rec("SANDBOX_FAILED", "帮我清理电脑释放磁盘空间"))
        # 短 query 是长 query 的语义子集 → overlap 覆盖率命中
        hit = s.match_experience(query="清理磁盘")
        self.assertIsNotNone(hit)
        self.assertEqual(hit["match_type"], "query_similar")
        self.assertGreaterEqual(hit["score"], 0.2)

    def test_unrelated_query_returns_none(self):
        s = ExperienceStore(path=self.path)
        s.add_experience(self._rec("SANDBOX_FAILED", "清理磁盘空间"))
        self.assertIsNone(s.match_experience(query="写一份竞品分析报告"))

    def test_corrupted_file_degrades_to_empty(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("{ this is not json")
        s = ExperienceStore(path=self.path)  # 不抛异常
        self.assertEqual(s.snapshot()["experiences"], 0)
        s.add_experience(self._rec("SANDBOX_FAILED", "清理磁盘"))  # 仍可写
        self.assertEqual(s.snapshot()["experiences"], 1)

    def test_mark_used_increments(self):
        s = ExperienceStore(path=self.path)
        s.add_experience(dict(self._rec("SANDBOX_FAILED", "清理磁盘"), id="e1"))
        s.mark_used("e1")
        s.mark_used("e1", success=True)
        with open(self.path, encoding="utf-8") as fh:
            data = json.load(fh)
        e = data["experiences"][0]
        self.assertEqual(e["used_count"], 2)
        self.assertEqual(e["success_count"], 1)

    def test_experience_capacity_guard_keeps_high_value(self):
        """长期运行护栏：超上限淘汰低频陈旧经验，保留高复用 + 刚写入的新经验。"""
        from agentskills.reflection import MAX_EXPERIENCES
        s = ExperienceStore(path=self.path)
        # 写一条高价值经验（复用 10 次 + 成功 5 次）
        s.add_experience(dict(self._rec("SANDBOX_FAILED", "清理磁盘"),
                              id="high-value"))
        for _ in range(10):
            s.mark_used("high-value", success=True)
        # 灌入大量低价值经验触发淘汰
        for i in range(MAX_EXPERIENCES + 20):
            s.add_experience(dict(self._rec("QUALITY_VIOLATION", "任务%d" % i),
                                  id="low-%d" % i))
        self.assertLessEqual(s.snapshot()["experiences"], MAX_EXPERIENCES)
        ids = {e["id"] for e in s._data["experiences"]}
        self.assertIn("high-value", ids, "高复用经验绝不能被淘汰")
        self.assertIn("low-%d" % (MAX_EXPERIENCES + 19), ids, "刚写入的新经验必留")

    def test_procedure_capacity_guard(self):
        from agentskills.reflection import MAX_PROCEDURE_SKILLS
        s = ExperienceStore(path=self.path)
        for i in range(MAX_PROCEDURE_SKILLS + 10):
            s.add_procedure("skill-%d" % i, "查询任务%d" % i)
        self.assertLessEqual(len(s._data["procedures"]), MAX_PROCEDURE_SKILLS)


class ReflectionEngineTest(unittest.TestCase):
    """修正假设：LLM 产出 + 诚实降级规则式，绝不 Mock。"""

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="sectrust-refl-")
        self.path = os.path.join(self.d, "experience.json")

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_no_llm_uses_rule_and_never_mocks(self):
        eng = ReflectionEngine(store=ExperienceStore(path=self.path), llm=None)
        r = eng.reflect("清理磁盘", _err("SANDBOX_FAILED", "sandbox", "cmd exit 1"))
        self.assertEqual(r["generated_by"], "rule")
        self.assertEqual(r["category"], CAT_TOOL_FAILURE)
        self.assertTrue(r["correction"])  # 有内容
        self.assertNotIn("已修复", r["correction"])  # 不谎称已修复
        self.assertTrue(r["stored"])

    def test_rule_correction_by_category(self):
        eng = ReflectionEngine(store=ExperienceStore(path=self.path))
        # 参数缺失：从技术细节抠出字段名
        r = eng.reflect("发邮件", _err("MISSING_PARAM", tech="field recipient empty"))
        self.assertIn("recipient", r["correction"])
        self.assertEqual(r["category"], CAT_MISSING_PARAM)
        # 权限：审批
        r2 = eng.reflect("发布", _err("APPROVAL_PENDING", "approval"))
        self.assertIn("审批", r2["correction"])
        # 权限：凭证
        r3 = eng.reflect("调API", _err("CREDENTIAL_UNAVAILABLE", "credential"))
        self.assertIn("凭证", r3["correction"])

    def test_llm_produces_correction(self):
        ans = '{"correction": "改用备选技能 disk-cleanup", "action": "switch_tool"}'
        eng = ReflectionEngine(store=ExperienceStore(path=self.path),
                               llm=_FakeLLM(ans))
        r = eng.reflect("清理磁盘", _err("SANDBOX_FAILED", "sandbox"))
        self.assertEqual(r["generated_by"], "llm")
        self.assertEqual(r["correction"], "改用备选技能 disk-cleanup")

    def test_llm_unavailable_degrades_to_rule(self):
        eng = ReflectionEngine(store=ExperienceStore(path=self.path),
                               llm=_FakeLLM(raise_exc=RuntimeError("no_llm_available")))
        r = eng.reflect("清理磁盘", _err("SANDBOX_FAILED", "sandbox"))
        self.assertEqual(r["generated_by"], "rule")

    def test_llm_garbage_degrades_to_rule(self):
        eng = ReflectionEngine(store=ExperienceStore(path=self.path),
                               llm=_FakeLLM("我觉得随便换一个吧"))
        r = eng.reflect("清理磁盘", _err("SANDBOX_FAILED", "sandbox"))
        self.assertEqual(r["generated_by"], "rule")

    def test_closed_loop_reflect_then_reuse(self):
        """方向一核心闭环：失败反思落库 → 下次相似 query 命中修正方案。"""
        store = ExperienceStore(path=self.path)
        eng = ReflectionEngine(store=store, llm=None)
        eng.reflect("帮我清理电脑释放磁盘空间",
                    _err("SANDBOX_FAILED", "sandbox", "cmd exit 1"))
        hit = eng.reuse("清理磁盘")
        self.assertIsNotNone(hit, "相似 query 必须能复用经验（闭环）")
        self.assertEqual(hit["match_type"], "query_similar")
        self.assertEqual(hit["category"], CAT_TOOL_FAILURE)
        self.assertTrue(hit["correction"])
        # 完全相同错误 → 精确命中
        hit2 = eng.reuse("另一个不相干请求",
                         error_info=_err("SANDBOX_FAILED", "sandbox"))
        self.assertEqual(hit2["match_type"], "error_exact")

    def test_reuse_no_experience_returns_none(self):
        eng = ReflectionEngine(store=ExperienceStore(path=self.path))
        self.assertIsNone(eng.reuse("任何请求"))


# ==========================================================================
# 方向二 · 分层记忆
# ==========================================================================
class LayeredMemoryTest(unittest.TestCase):
    """工作 / 情景 / 程序三层记忆 + 经验必须能被后续 query 检索（闭环）。"""

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="sectrust-mem-")
        self.path = os.path.join(self.d, "memory.json")

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_episodic_success_and_failure(self):
        mem = LayeredMemory(store=ExperienceStore(path=self.path))
        mem.remember_episode("disk-cleanup", "清理磁盘", True,
                             context={"task_type": "cleanup", "role": "ops"})
        mem.remember_episode("competitor-report", "竞品分析", False,
                             error_info=_err("SANDBOX_FAILED", "sandbox"),
                             context={"task_type": "report"})
        self.assertEqual(len(mem.recall_episodes(ok=True)), 1)
        self.assertEqual(len(mem.recall_episodes(ok=False)), 1)
        self.assertEqual(mem.recall_episodes(skill="disk-cleanup")[0]["ok"], True)

    def test_procedural_memory_distilled_from_success(self):
        mem = LayeredMemory(store=ExperienceStore(path=self.path))
        mem.remember_episode("disk-cleanup", "帮我清理电脑释放磁盘空间", True,
                             context={"task_type": "cleanup"})
        # 相似 query 应召回历史上成功用过的技能（程序记忆）
        procs = mem.recall_procedures(query="清理磁盘空间")
        self.assertTrue(any(p["skill"] == "disk-cleanup" for p in procs))

    def test_procedural_not_distilled_from_failure(self):
        mem = LayeredMemory(store=ExperienceStore(path=self.path))
        mem.remember_episode("disk-cleanup", "清理磁盘", False,
                             error_info=_err("SANDBOX_FAILED", "sandbox"))
        self.assertEqual(mem.recall_procedures(query="清理磁盘"), [])

    def test_procedural_memory_records_tool_combination(self):
        """方向二深化：程序记忆除参数模式外，还提炼「工具组合」(allowed_tools)。"""
        mem = LayeredMemory(store=ExperienceStore(path=self.path))
        mem.remember_episode("disk-cleanup", "帮我清理电脑释放磁盘空间", True,
                             context={"task_type": "cleanup"},
                             tools=["web_search", "file_read"])
        procs = mem.recall_procedures(query="清理磁盘空间")
        self.assertTrue(procs)
        top = procs[0]
        self.assertEqual(top["skill"], "disk-cleanup")
        # 工具组合随程序记忆一起召回（只记名，不含任何参数值）
        self.assertIn("web_search", top["tools"])
        self.assertIn("file_read", top["tools"])
        # 情景轨迹里也结构化记住了本次工具组合
        ep = mem.recall_episodes(skill="disk-cleanup", ok=True)[0]
        self.assertEqual(ep["tools"], ["file_read", "web_search"])

    def test_tool_combination_is_name_only_no_values(self):
        """工具组合只记工具名，绝不落敏感参数值。"""
        mem = LayeredMemory(store=ExperienceStore(path=self.path))
        mem.remember_episode("s", "查询订单", True,
                             tools=["web_search", "api_call"])
        top = mem.recall_procedures(query="查询订单")[0]
        for t in top["tools"]:
            self.assertNotIn("=", t)
            self.assertNotIn("secret", t.lower())

    def test_working_memory_hint_respects_redaction(self):
        class _State:
            def get_cached_params(self, skill, sid):
                return {"PROJECT_PATH": "/home/x", "API_KEY": "<redacted>"}
        mem = LayeredMemory(store=ExperienceStore(path=self.path))
        hint = mem.working_hint("disk-cleanup", _State())
        self.assertIn("PROJECT_PATH=/home/x", hint)
        self.assertIn("API_KEY", hint)
        self.assertNotIn("<redacted>=<redacted>", hint)  # 敏感键不显值

    def test_experience_retrievable_via_same_store(self):
        """闭环要点：方向一经验与方向二记忆共享同一 store，经验可被检索统计。"""
        store = ExperienceStore(path=self.path)
        ReflectionEngine(store=store).reflect(
            "清理磁盘", _err("SANDBOX_FAILED", "sandbox"))
        mem = LayeredMemory(store=store)
        self.assertEqual(mem.snapshot()["experiences"], 1)
        # 程序记忆 + 情景记忆 + 经验共库持久化
        mem.remember_episode("disk-cleanup", "清理磁盘", True)
        store2 = ExperienceStore(path=self.path)
        self.assertEqual(store2.snapshot()["experiences"], 1)
        self.assertEqual(store2.snapshot()["episodes"], 1)

    def test_episode_cap_500(self):
        mem = LayeredMemory(store=ExperienceStore(path=self.path))
        for i in range(505):
            mem.remember_episode("s", "q%d" % i, True)
        self.assertLessEqual(len(mem.recall_episodes(limit=1000)), 500)


class MatchTokenTest(unittest.TestCase):
    def test_cjk_bigrams_and_latin(self):
        t = _match_tokens("帮我清理磁盘 disk space")
        self.assertIn("清理", t)
        self.assertIn("磁盘", t)
        self.assertIn("disk", t)
        self.assertIn("space", t)


# ==========================================================================
# 方向三 · Confidence Loop（模型自适应升级）
# ==========================================================================
class ConfidenceLoopTest(unittest.TestCase):
    """排序一致性 proxy + 小模型优先 + 不够再升级，与竞争力 gate 同哲学。"""

    def _cands(self):
        return [SkillCandidate(name="a", scores={"rrf": 0.016}),
                SkillCandidate(name="b", scores={"rrf": 0.012}),
                SkillCandidate(name="c", scores={"rrf": 0.010})]

    def test_consistency_same_reversed_swap(self):
        h = [SkillCandidate(name=n) for n in ("a", "b", "c")]
        same = [SkillCandidate(name=n) for n in ("a", "b", "c")]
        rev = [SkillCandidate(name=n) for n in ("c", "b", "a")]
        swap = [SkillCandidate(name=n) for n in ("b", "a", "c")]
        self.assertEqual(ConfidenceLoop._consistency(h, same), 1.0)
        self.assertEqual(ConfidenceLoop._consistency(h, rev), 0.0)
        self.assertAlmostEqual(ConfidenceLoop._consistency(h, swap), 2 / 3)

    def test_unconfigured_degrades_single_rerank(self):
        loop = ConfidenceLoop()  # 无 weak/strong
        self.assertFalse(loop.configured)
        llm = _FakeLLM('[{"name":"a"}]')
        ranked, meta = loop.decide("q", self._cands(),
                                   {c.name: "d" for c in self._cands()},
                                   llm=llm, min_competitive=0)
        self.assertFalse(meta["confidence_loop"]["applied"])

    def test_no_llm_degrades(self):
        loop = ConfidenceLoop(weak_model="w", strong_model="s")
        ranked, meta = loop.decide("q", self._cands(),
                                   {c.name: "d" for c in self._cands()}, llm=None)
        self.assertFalse(meta["confidence_loop"]["applied"])

    def test_gate_not_fired_no_upgrade(self):
        """竞争力 gate 未触发重排 → 没花 LLM，不该升级（能省则省）。"""
        loop = ConfidenceLoop(weak_model="w", strong_model="s", min_competitive=2)
        cands = [SkillCandidate(name="top", scores={"rrf": 0.0164}),
                 SkillCandidate(name="n1", scores={"rrf": 0.0078}),
                 SkillCandidate(name="n2", scores={"rrf": 0.0076})]
        llm = _FakeLLM('[{"name":"top"}]')
        ranked, meta = loop.decide("q", cands, {c.name: "d" for c in cands},
                                   llm=llm)  # 默认 min_competitive=2 → gate 跳过
        self.assertFalse(meta["confidence_loop"]["applied"])
        self.assertEqual(llm.calls, [])  # gate 未过，根本没调 LLM

    def test_high_consistency_no_upgrade(self):
        """小模型排序与 heuristic 一致 → 不升级（省强模型）。"""
        loop = ConfidenceLoop(weak_model="w", strong_model="s",
                              min_competitive=0, consistency_threshold=0.5)
        # 无论 model 都返回与输入一致的顺序 [a,b,c]
        llm = _FakeLLM(lambda m: '[{"name":"a"},{"name":"b"},{"name":"c"}]')
        ranked, meta = loop.decide("q", self._cands(),
                                   {c.name: "d" for c in self._cands()}, llm=llm)
        self.assertTrue(meta["confidence_loop"]["applied"])
        self.assertFalse(meta["confidence_loop"]["upgraded"])
        self.assertEqual(llm.calls, ["w"])  # 只调小模型

    def test_low_consistency_upgrades_to_strong(self):
        """小模型反序 → 一致性 0 < 阈值 → 升级强模型，用强模型结果。"""
        loop = ConfidenceLoop(weak_model="w", strong_model="s",
                              min_competitive=0, consistency_threshold=0.5)
        # weak 反序 [c,b,a]；strong 原序 [a,b,c]
        def answer(model):
            if model == "w":
                return '[{"name":"c"},{"name":"b"},{"name":"a"}]'
            return '[{"name":"a"},{"name":"b"},{"name":"c"}]'
        llm = _FakeLLM(answer)
        ranked, meta = loop.decide("q", self._cands(),
                                   {c.name: "d" for c in self._cands()}, llm=llm)
        cl = meta["confidence_loop"]
        self.assertTrue(cl["applied"])
        self.assertTrue(cl["upgraded"])
        self.assertEqual(cl["weak_consistency"], 0.0)
        self.assertEqual(cl["model"], "s")
        self.assertEqual(llm.calls, ["w", "s"])  # 小模型 + 升级强模型
        self.assertEqual([c.name for c in ranked], ["a", "b", "c"])  # 用强模型结果

    def test_threshold_tunable(self):
        """阈值调高 → 一次交换(0.667)也不够 → 升级；调低则不升级。"""
        def answer(model):
            if model == "w":
                return '[{"name":"b"},{"name":"a"},{"name":"c"}]'  # 0.667
            return '[{"name":"a"},{"name":"b"},{"name":"c"}]'
        # 阈值 0.9：0.667 < 0.9 → 升级
        loop_hi = ConfidenceLoop(weak_model="w", strong_model="s",
                                 min_competitive=0, consistency_threshold=0.9)
        _, meta_hi = loop_hi.decide("q", self._cands(),
                                    {c.name: "d" for c in self._cands()},
                                    llm=_FakeLLM(answer))
        self.assertTrue(meta_hi["confidence_loop"]["upgraded"])
        # 阈值 0.5：0.667 >= 0.5 → 不升级
        loop_lo = ConfidenceLoop(weak_model="w", strong_model="s",
                                 min_competitive=0, consistency_threshold=0.5)
        _, meta_lo = loop_lo.decide("q", self._cands(),
                                    {c.name: "d" for c in self._cands()},
                                    llm=_FakeLLM(answer))
        self.assertFalse(meta_lo["confidence_loop"]["upgraded"])


# ==========================================================================
# 接线集成 · SkillRouter
# ==========================================================================
class RouterReflectionIntegrationTest(unittest.TestCase):
    """经验复用预填 feedback（不改 chosen）+ Confidence Loop 字段暴露。"""

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="sectrust-router-")
        self.lib = _fixture_library(self.d)
        self.path = os.path.join(self.d, "experience.json")

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_experience_hint_prefilled_but_chosen_unchanged(self):
        store = ExperienceStore(path=self.path)
        ReflectionEngine(store=store).reflect(
            "帮我清理电脑释放磁盘空间", _err("SANDBOX_FAILED", "sandbox", "exit 1"))
        with_ref = SkillRouter(library=self.lib,
                               reflection=ReflectionEngine(store=store))
        no_ref = SkillRouter(library=self.lib)
        out = with_ref.select("清理磁盘空间", k=3)
        base = no_ref.select("清理磁盘空间", k=3)
        self.assertTrue(out["experience_hint"])
        self.assertIn("[经验复用", out["feedback"])
        self.assertEqual(out["experience_match"]["match_type"], "query_similar")
        # 铁律：经验是提示不改道 —— chosen 与无经验时一致，不绕过门禁
        self.assertEqual(out["chosen"], base["chosen"])
        self.assertEqual(out["candidates"], base["candidates"])

    def test_no_reflection_behavior_unchanged(self):
        out = SkillRouter(library=self.lib).select("清理电脑", k=3)
        self.assertEqual(out["experience_hint"], "")
        self.assertIsNone(out["experience_match"])
        self.assertEqual(out["confidence_loop"], {"applied": False})

    def test_confidence_loop_field_exposed(self):
        # 用假 loop 验证 router 确实调用 decide 并暴露字段
        class _Loop:
            configured = True
            def decide(self, query, candidates, descriptions, llm, k=5,
                       conversation=None, min_competitive=None):
                meta = {"applied": True, "model": "s", "reason": "ok",
                        "chosen": [], "reranker": "llm-v1",
                        "confidence_loop": {"applied": True, "upgraded": True,
                                            "model": "s"}}
                return candidates, meta
        out = SkillRouter(library=self.lib, confidence_loop=_Loop()).select(
            "清理电脑", k=3)
        self.assertTrue(out["confidence_loop"]["applied"])
        self.assertTrue(out["confidence_loop"]["upgraded"])

    def test_build_runtime_assembly(self):
        rt = build_runtime(reflection_path=self.path, llm=None,
                           weak_model=None, strong_model=None)
        self.assertIsNotNone(rt["reflection"])
        self.assertIsNotNone(rt["memory"])
        self.assertIsNone(rt["confidence_loop"])  # 未配 weak/strong → 不装配
        rt2 = build_runtime(reflection_path=self.path, llm=None,
                            weak_model="w", strong_model="s")
        self.assertTrue(rt2["confidence_loop"].configured)


# ==========================================================================
# 接线集成 · daemon（端到端闭环）
# ==========================================================================
FAIL_RES = {"ok": False, "stage": "sandbox", "reason": "脚本执行失败 exit 1",
            "error_info": _err("SANDBOX_FAILED", "sandbox", "cmd exit 1"),
            "execution_mode": "sandbox", "feedback_hint": "", "approval_id": ""}
OK_RES = {"ok": True, "stage": "ok", "reason": "", "execution_mode": "l2_only",
          "feedback_hint": "", "approval_id": "", "body_bytes": 10}


class DaemonWiringTest(unittest.TestCase):
    """daemon 失败→反思落库→下次相似 query 命中（闭环）+ 默认降级不破坏。"""

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="sectrust-daemon-")
        self.db = os.path.join(self.d, "task.db")

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def _daemon(self):
        return ExecutionDaemon(db_path=self.db,
                               workspace_root=os.path.join(self.d, "ws"))

    def test_failure_reflects_and_records_episode(self):
        from agentskills.guard_chain import SkillExecutionGuard
        with mock.patch.object(SkillExecutionGuard, "execute", return_value=FAIL_RES):
            ok, result, err = self._daemon()._execute(
                "t-fail", {"op": "skill", "query": "帮我清理电脑释放磁盘空间",
                           "skill_k": 3})
        self.assertFalse(ok)
        self.assertEqual(result["error_info"]["error_code"], "SANDBOX_FAILED")
        self.assertTrue(result["self_correction_hint"])
        # 方向一：反思落库（无 LLM → 规则式降级，绝不 Mock）
        self.assertEqual(result["reflection"]["category"], CAT_TOOL_FAILURE)
        self.assertEqual(result["reflection"]["generated_by"], "rule")
        self.assertTrue(result["reflection"]["experience_id"])
        # 方向二：失败情景记忆
        self.assertIsNotNone(result["memory_episode"])
        self.assertFalse(result["memory_episode"]["ok"])
        # 经验库确实落盘（task.db 同目录）
        exp_path = os.path.join(self.d, "experience.json")
        self.assertTrue(os.path.exists(exp_path))
        with open(exp_path, encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertEqual(len(data["experiences"]), 1)
        self.assertEqual(data["experiences"][0]["error_code"], "SANDBOX_FAILED")

    def test_closed_loop_second_similar_query_hits_experience(self):
        """端到端闭环：第一次失败反思 → 第二次相似 query select 命中经验复用。"""
        from agentskills.guard_chain import SkillExecutionGuard
        # 第一次：失败 → 落经验
        with mock.patch.object(SkillExecutionGuard, "execute", return_value=FAIL_RES):
            self._daemon()._execute(
                "t1", {"op": "skill", "query": "帮我清理电脑释放磁盘空间",
                       "skill_k": 3})
        # 第二次：新 daemon 实例读同一份经验库 → 相似 query 命中
        with mock.patch.object(SkillExecutionGuard, "execute", return_value=OK_RES):
            ok, result, err = self._daemon()._execute(
                "t2", {"op": "skill", "query": "清理磁盘空间", "skill_k": 3})
        self.assertTrue(ok, err)
        self.assertTrue(result.get("experience_hint"),
                        "第二次相似 query 必须命中经验复用（闭环）")
        self.assertIn("[经验复用", result["skill_match"]["feedback"])
        self.assertEqual(result["skill_match"]["experience_match"]["match_type"],
                         "query_similar")
        # 成功执行 → 程序记忆提炼（含工具组合维度）
        self.assertTrue(result["memory_episode"]["ok"])
        self.assertIn("tools", result["memory_episode"])
        # 经验有效性反馈闭环：第二次复用经验且执行成功 → used_count/success_count 记账
        exp_path = os.path.join(self.d, "experience.json")
        with open(exp_path, encoding="utf-8") as fh:
            exps = json.load(fh)["experiences"]
        self.assertTrue(exps)
        self.assertGreaterEqual(exps[0]["used_count"], 1)   # 至少被复用一次
        self.assertGreaterEqual(exps[0]["success_count"], 1)  # 复用后成功如实记账

    def test_confidence_loop_off_by_default_no_env(self):
        """未配 weak/strong 环境变量 → Confidence Loop 不 applied（行为不变）。"""
        from agentskills.guard_chain import SkillExecutionGuard
        old_w = os.environ.pop("SECTRUST_CONFIDENCE_WEAK_MODEL", None)
        old_s = os.environ.pop("SECTRUST_CONFIDENCE_STRONG_MODEL", None)
        try:
            with mock.patch.object(SkillExecutionGuard, "execute", return_value=OK_RES):
                ok, result, err = self._daemon()._execute(
                    "t", {"op": "skill", "query": "清理电脑", "skill_k": 3})
            self.assertTrue(ok, err)
            self.assertFalse(result["skill_match"]["confidence_loop"]["applied"])
        finally:
            if old_w is not None:
                os.environ["SECTRUST_CONFIDENCE_WEAK_MODEL"] = old_w
            if old_s is not None:
                os.environ["SECTRUST_CONFIDENCE_STRONG_MODEL"] = old_s

    def test_confidence_loop_wired_when_env_configured(self):
        """配了 weak/strong → build_runtime 构造 loop 并传入 router，decide 被调用。"""
        from agentskills.guard_chain import SkillExecutionGuard
        fake_runtime = {"reflection": None, "memory": None, "store": None,
                        "confidence_loop": None}
        seen = {}

        class _Loop:
            configured = True
            def decide(self, query, candidates, descriptions, llm, k=5,
                       conversation=None, min_competitive=None):
                seen["called"] = True
                meta = {"applied": True, "model": "s", "reason": "ok",
                        "chosen": [], "reranker": "llm-v1",
                        "confidence_loop": {"applied": True, "upgraded": True,
                                            "model": "s"}}
                return candidates, meta
        fake_runtime["confidence_loop"] = _Loop()
        old_w = os.environ.get("SECTRUST_CONFIDENCE_WEAK_MODEL")
        old_s = os.environ.get("SECTRUST_CONFIDENCE_STRONG_MODEL")
        os.environ["SECTRUST_CONFIDENCE_WEAK_MODEL"] = "w"
        os.environ["SECTRUST_CONFIDENCE_STRONG_MODEL"] = "s"
        try:
            with mock.patch("agentskills.reflection.build_runtime",
                            return_value=fake_runtime), \
                 mock.patch.object(SkillExecutionGuard, "execute", return_value=OK_RES):
                ok, result, err = self._daemon()._execute(
                    "t", {"op": "skill", "query": "清理电脑", "skill_k": 3})
            self.assertTrue(ok, err)
            self.assertTrue(seen.get("called"), "ConfidenceLoop.decide 必须被调用")
            self.assertTrue(result["skill_match"]["confidence_loop"]["upgraded"])
        finally:
            if old_w is None:
                os.environ.pop("SECTRUST_CONFIDENCE_WEAK_MODEL", None)
            else:
                os.environ["SECTRUST_CONFIDENCE_WEAK_MODEL"] = old_w
            if old_s is None:
                os.environ.pop("SECTRUST_CONFIDENCE_STRONG_MODEL", None)
            else:
                os.environ["SECTRUST_CONFIDENCE_STRONG_MODEL"] = old_s

    def test_name_compat_path_untouched(self):
        """旧 name 激活路径不进检索，完全不受反思接线影响。"""
        ok, result, err = self._daemon()._execute(
            "t", {"op": "skill", "name": "verification-before-completion"})
        self.assertTrue(ok, err)
        self.assertEqual(result.get("name"), "verification-before-completion")
        self.assertNotIn("skill_match", result)
        self.assertNotIn("reflection", result)


# ==========================================================================
# 前端接线集成 · 任务详情「智能运行时」面板 DOM stub 渲染
# 项目纪律：JS 改完必须 node --check + DOM stub 验证，不能只验 200 / 只验语法。
# ==========================================================================
_FRONTEND_STUB_CJS = r"""
const fs = require('fs');
const vm = require('vm');
const js = fs.readFileSync(process.argv[2], 'utf8');
function mkEl() {
  const store = { innerHTML: '', textContent: '', value: '', style: {}, dataset: {} };
  const classList = { add(){}, remove(){}, toggle(){}, contains(){return false;},
                      length:0, item(){return null;} };
  return new Proxy(store, {
    get(t,k){
      if (k in t) return t[k];
      if (k==='classList') return classList;
      if (k==='style'||k==='dataset') return t[k]||{};
      if (k==='querySelectorAll') return ()=>[];
      if (k==='querySelector') return ()=>mkEl();
      if (['addEventListener','removeEventListener','appendChild','removeChild',
           'setAttribute','removeAttribute','getAttribute','focus','click','blur',
           'insertAdjacentHTML','scrollIntoView','remove','replaceChild'].includes(k))
        return ()=>{};
      if (k==='getBoundingClientRect')
        return ()=>({top:0,left:0,width:0,height:0,bottom:0,right:0});
      if (k==='children'||k==='childNodes') return [];
      if (['firstChild','lastChild','parentNode','parentElement','nextSibling',
           'previousSibling'].includes(k)) return null;
      if (['offsetWidth','offsetHeight','clientWidth','clientHeight','scrollHeight',
           'scrollTop'].includes(k)) return 0;
      if (k==='length') return 0;
      if (k==='item') return ()=>null;
      if (typeof k==='symbol') return undefined;
      return mkEl();
    },
    set(t,k,v){ t[k]=v; return true; }, has(){ return true; },
  });
}
const els = {};
const document = {
  getElementById(id){ return els[id]||(els[id]=mkEl()); },
  querySelector(){ return mkEl(); }, querySelectorAll(){ return []; },
  createElement(){ return mkEl(); }, addEventListener(){}, removeEventListener(){},
  body:mkEl(), head:mkEl(), documentElement:mkEl(), cookie:'', title:'',
};
// 带完整智能运行时字段的失败任务：经验复用 + 反思落库 + 分层记忆 + 置信升级
const runtimeTask = {
  task_id:'rt1', status:'failed', op:'skill', name:'disk-cleanup', runner_id:'r1',
  payload:{ role:'writer' },
  result:{
    op:'skill', query:'清理磁盘',
    skill_match:{ chosen:'disk-cleanup',
      experience_match:{ match_type:'query_similar', score:0.667,
                         category:'tool_failure', experience_id:'exp-abc' } },
    experience_hint:'[经验复用·query_similar] 检查沙箱依赖',
    error_info:{ error_code:'SANDBOX_FAILED', stage:'sandbox',
                 human_readable_message:'脚本执行失败', technical_detail:'exit 1' },
    self_correction_hint:'下一步检查依赖',
    reflection:{ category:'tool_failure', generated_by:'rule',
                 experience_id:'exp-abc', correction:'检查沙箱依赖与命令路径' },
    memory_episode:{ skill:'disk-cleanup', ok:false, tools:['web_search','file_read'],
                     procedures:[] },
    confidence_loop:{ applied:true, upgraded:true, weak_model:'small', model:'strong',
                      weak_consistency:0.0, consistency:1.0, threshold:0.5 },
  },
};
async function fetch(){ return { ok:true, status:200, json:async()=>({}), text:async()=>'{}' }; }
const location={ hash:'',href:'',pathname:'/',search:'',assign(){},replace(){},reload(){} };
const history={ pushState(){},replaceState(){},back(){} };
const navigator={ userAgent:'node', clipboard:{ writeText:async()=>{} } };
const localStorage={ getItem(){return null;},setItem(){},removeItem(){},clear(){} };
const ctx={ console,document,fetch,location,history,navigator,localStorage,
  setTimeout(){return 0;},clearTimeout(){},setInterval(){return 0;},clearInterval(){},
  requestAnimationFrame(){return 0;},cancelAnimationFrame(){},
  alert(){},confirm(){return false;},prompt(){return '';},
  Notification:function(){}, URL,URLSearchParams,Date,Math,JSON,Intl };
ctx.window=ctx; ctx.globalThis=ctx;
vm.createContext(ctx);
let initErr='';
try { vm.runInContext(js, ctx, { timeout:8000 }); }
catch(e){ initErr=e.message; }
(async()=>{
  if (typeof ctx.showTaskDetail!=='function'){
    console.log('FAIL: showTaskDetail 未定义 | initErr='+initErr); process.exit(1);
  }
  ctx.jget = async()=>runtimeTask;
  await ctx.showTaskDetail('rt1');
  const html = els['taskDetail'] ? els['taskDetail'].innerHTML : '';
  const checks = ['智能运行时','经验复用','query_similar','0.667','自我反思·落库',
                  'tool_failure','exp-abc','规则式降级','分层记忆','disk-cleanup',
                  'web_search','置信循环','已升级更强模型','strong'];
  const missing = checks.filter(k=>!html.includes(k));
  console.log('initErr(可容忍):', initErr||'(none)');
  console.log('rendered chars:', html.length, '| missing:', JSON.stringify(missing));
  if (missing.length){ console.log('FAIL: 智能运行时面板缺少字段'); process.exit(1); }
  // 干净任务：无任何智能运行时字段 → 面板不应渲染
  ctx.jget = async()=>({ task_id:'clean', status:'done', op:'llm', name:'',
                         payload:{}, result:{ op:'llm', output:'ok' } });
  await ctx.showTaskDetail('clean');
  const clean = els['taskDetail'] ? els['taskDetail'].innerHTML : '';
  if (clean.includes('智能运行时') || clean.includes('置信循环')){
    console.log('FAIL: 干净任务不应显示智能运行时面板'); process.exit(1);
  }
  console.log('OK: 智能运行时面板渲染通过（富任务字段齐全 + 干净任务不误显）');
})();
"""


class FrontendRuntimePanelTest(unittest.TestCase):
    """前端「智能运行时」面板：node --check + DOM stub 实渲，不能只验 200。"""

    @unittest.skipUnless(shutil.which("node"), "node 不可用，跳过前端渲染验证")
    def test_runtime_panel_renders(self):
        src_path = os.path.join(ROOT, "console_web.py")
        with open(src_path, "r", encoding="utf-8") as fh:
            src = fh.read()
        m = re.search(r"<script>(.*?)</script>", src, re.S)
        self.assertIsNotNone(m, "console_web.py 应含内嵌 <script>")
        js = m.group(1)
        # 新增的关键渲染点都在
        for token in ("智能运行时", "经验复用", "置信循环", "分层记忆",
                      "自我反思·落库", "🔁 经验复用", "⬆️ 模型升级"):
            self.assertIn(token, js, "内嵌 JS 缺少渲染点：%s" % token)
        d = tempfile.mkdtemp(prefix="sectrust-rtjs-")
        try:
            js_path = os.path.join(d, "_rt_check.js")
            cjs_path = os.path.join(d, "_rt_dom_stub.cjs")
            with open(js_path, "w", encoding="utf-8") as fh:
                fh.write(js)
            with open(cjs_path, "w", encoding="utf-8") as fh:
                fh.write(_FRONTEND_STUB_CJS)
            chk = subprocess.run(["node", "--check", js_path],
                                 capture_output=True, text=True, timeout=30)
            self.assertEqual(chk.returncode, 0,
                             "node --check 失败：%s" % chk.stderr[:500])
            run = subprocess.run(["node", cjs_path, js_path],
                                 capture_output=True, text=True, timeout=30)
            self.assertEqual(run.returncode, 0,
                             "DOM stub 渲染失败：%s\n%s"
                             % (run.stdout[-1000:], run.stderr[-1000:]))
            self.assertIn("OK: 智能运行时面板渲染通过", run.stdout)
        finally:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
