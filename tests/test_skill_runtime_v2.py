"""Skill 运行时 v2 优化（P0→P4）回归测试。

对齐用户拍板："白名单边界要更松，保证速度。接入策略选你的（渐进式：
保留硬编码 fallback + skill_hint/query 触发检索）。直接落地。"

覆盖：
  P0 接通主链路   —— create_task 白名单透传 op=skill；daemon query 路径走
                      SkillRouter + SkillExecutionGuard；name 兼容路径不破；
                      skill_match Trace 有上下文才写、缺上下文不硬造孤儿轨迹。
  P1 状态治理     —— 连续 3 次失败熔断；L1 剔除 circuit_open；feedback_hint；
                      reset 恢复；驳回（安全策略）不计入执行故障熔断。
  P2 语义召回     —— "清理电脑" 跨词召回 "disk-cleanup" 排名第一；
                      embedding_status 诚实（hashed_fallback / 坏 provider 降级）；
                      L1 只读 frontmatter，不夹带正文。
  P3 分级安全     —— 更松的 white 准入（medium + 只读工具）；scripts 强制 gray；
                      副作用/高危强制 black；声明 white 硬条件不满足自动降级；
                      white waived_gates 留痕 + 运行期越界降级拒绝；
                      MCP 入向四门并行（wall < serial）+ guard_decision Trace。
  P4 可观测仪表盘 —— /api/governance/skills/* 401/403/200 与数据结构；
                      内嵌 JS node --check + DOM stub 渲染三视图。

全部离线：临时目录 + 回环 http.server，不触网、不需要 GITHUB_TOKEN。
零第三方依赖；pytest 与 unittest 均可运行。
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import console_api  # noqa: E402
import planner  # noqa: E402
from agentskills.guard_chain import SkillExecutionGuard  # noqa: E402
from agentskills.retrieval import SkillRetriever  # noqa: E402
from agentskills.router import SkillRouter  # noqa: E402
from agentskills.skill_md import StandardSkillLibrary  # noqa: E402
from agentskills.state import SkillStateStore  # noqa: E402
from agentskills.tier import classify, escalate_on_action, gate_plan  # noqa: E402
from execution.daemon import ExecutionDaemon  # noqa: E402
from mcp_adapter.breaker import TTLCircuitBreaker  # noqa: E402
from mcp_adapter.proxy import SecureProxy  # noqa: E402
from mcp_adapter.registry import MCPServerRegistry, ServerSpec  # noqa: E402
from trace import TraceStore  # noqa: E402


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _write_skill(root, name, description, extra=""):
    """在 root/<name>/SKILL.md 写一个标准格式技能（含可识别的正文标记）。"""
    d = os.path.join(root, name)
    os.makedirs(d, exist_ok=True)
    text = ("---\nname: %s\ndescription: %s\nversion: 1.0.0\nlicense: MIT\n"
            "allowed-tools: [web_search, file_read]\nrisk_level: low\n"
            "cost_tier: low\n%s---\n\n# %s\n\n%s\n"
            % (name, description, extra, name, "BODY_SENTINEL_MARKER_9527"))
    with open(os.path.join(d, "SKILL.md"), "w", encoding="utf-8") as fh:
        fh.write(text)
    return d


def _fixture_library(tmp):
    """两个自建技能：一个磁盘清理（与"清理电脑"跨词），一个竞品报告（无关）。"""
    root = os.path.join(tmp, "lib")
    _write_skill(root, "disk-cleanup",
                 "磁盘释放工具：清理电脑垃圾文件、释放磁盘空间、系统优化瘦身。")
    _write_skill(root, "competitor-report",
                 "竞品分析报告：梳理竞争对手功能对比、输出竞品矩阵与调研文稿。")
    return StandardSkillLibrary(roots=[root]), root


class _FakeRegistry:
    """create_task 需要的最小 registry（只登记，不落真库）。"""

    def __init__(self):
        self.saved = []

    def list_tasks(self, limit=10000):
        return [{"task_id": s[0]} for s in self.saved]

    def register_task(self, tid, payload, depends_on=None):
        self.saved.append((tid, dict(payload), depends_on))


class _FakeTrace:
    """记录 record() 调用，验证 Trace 写/不写，不依赖真实 sqlite。"""

    def __init__(self):
        self.events = []

    def record(self, trace_id, kind, **kw):
        self.events.append((trace_id, kind, kw))


# ==========================================================================
# P0 · 接通 Skill 运行时主链路
# ==========================================================================
class P0MainChainTest(unittest.TestCase):
    """渐进式接入：query/skill_hint 切 op=skill；name 兼容路径不变。"""

    def test_whitelist_constants_stay_in_sync(self):
        # 铁律：console_api 与 planner 各自白名单构造 payload，两处必须一致，
        # 否则新增字段会在某一路被静默丢弃。
        self.assertEqual(tuple(console_api.SKILL_PAYLOAD_KEYS),
                         tuple(planner.SKILL_PAYLOAD_KEYS))

    def test_create_task_switches_to_skill_op_and_whitelists(self):
        reg = _FakeRegistry()
        res = console_api.create_task(
            reg, None, "帮我清理电脑释放磁盘", "auditor",
            skill_payload={"query": "清理电脑释放磁盘", "skill_k": 3,
                           "security_tier": "", "script": "",
                           "unknown_field": "must_be_dropped"})
        self.assertTrue(res["ok"])
        p = res["payload"]
        self.assertEqual(p["op"], "skill")
        self.assertEqual(p["query"], "清理电脑释放磁盘")
        self.assertEqual(p["skill_k"], 3)
        self.assertNotIn("unknown_field", p, "白名单外字段必须被丢弃")
        self.assertFalse(p.get("security_tier"))  # 空值不透传

    def test_create_task_without_skill_payload_keeps_llm(self):
        reg = _FakeRegistry()
        res = console_api.create_task(reg, None, "普通写报告任务", "auditor")
        self.assertTrue(res["ok"])
        self.assertEqual(res["payload"]["op"], "llm",
                         "不声明检索意图时保持既有 op=llm（渐进接入）")

    def test_daemon_skill_query_e2e_white_fast_path(self):
        """query → SkillRouter 选技能 → SkillExecutionGuard white L2 快速通道。"""
        d = tempfile.mkdtemp(prefix="sectrust-p0-")
        try:
            daemon = ExecutionDaemon(db_path=os.path.join(d, "task.db"),
                                     workspace_root=os.path.join(d, "ws"))
            ok, result, err = daemon._execute(
                "t-skill-query",
                {"op": "skill", "query": "帮我清理电脑释放磁盘空间", "skill_k": 3})
            self.assertTrue(ok, err)
            self.assertIsNone(err)
            sel = result["skill_match"]
            self.assertTrue(sel["ok"])
            self.assertTrue(sel["chosen"])
            self.assertIn("candidates", sel)
            self.assertIn("embedding_status", sel)
            g = result["guard"]
            self.assertTrue(g["ok"])
            self.assertEqual(g["tier"], "white")
            self.assertEqual(g["execution_mode"], "l2_only")
            self.assertEqual([s["stage"] for s in g["stages"]],
                             ["tier", "install", "l2", "pii_check", "ok"])
            waived = g["audit"]["waived_gates"]
            for gate in ("approval", "sandbox", "secret_scan", "evidence",
                         "artifacts", "quality"):
                self.assertIn(gate, waived, "white 免检依据必须留痕")
            self.assertGreater(g["body_bytes"], 0)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_daemon_skill_by_name_compat_path_unchanged(self):
        """旧 loader.activate(name) 路径必须继续可用，不被 query 路径破坏。"""
        d = tempfile.mkdtemp(prefix="sectrust-p0name-")
        try:
            daemon = ExecutionDaemon(db_path=os.path.join(d, "task.db"),
                                     workspace_root=os.path.join(d, "ws"))
            ok, result, err = daemon._execute(
                "t-skill-name",
                {"op": "skill", "name": "verification-before-completion"})
            self.assertTrue(ok, err)
            self.assertEqual(result.get("name"), "verification-before-completion")
            self.assertGreater(result.get("body_bytes", 0), 0)
            self.assertNotIn("skill_match", result, "name 路径不走检索")
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_skill_match_trace_written_only_with_context(self):
        """有 trace_store/trace_id 才写 skill_match；缺上下文不硬造孤儿轨迹。"""
        d = tempfile.mkdtemp(prefix="sectrust-p0trace-")
        try:
            lib, _ = _fixture_library(d)
            ft = _FakeTrace()
            out = SkillRouter(library=lib, trace_store=ft,
                              trace_id="tr-1").select("清理电脑", k=3)
            self.assertTrue(out["trace_written"])
            kinds = [e[1] for e in ft.events]
            self.assertIn("skill_match", kinds)
            detail = [e[2]["detail"] for e in ft.events
                      if e[1] == "skill_match"][0]
            self.assertIn("candidates", detail)
            self.assertIn("embedding_status", detail)

            ft2 = _FakeTrace()
            out2 = SkillRouter(library=lib, trace_store=ft2,
                               trace_id="").select("清理电脑", k=3)
            self.assertFalse(out2["trace_written"])
            self.assertEqual(ft2.events, [], "缺 trace_id 不得写轨迹")
        finally:
            shutil.rmtree(d, ignore_errors=True)


# ==========================================================================
# P1 · 运行时状态治理 + 自适应熔断 + 反馈闭环
# ==========================================================================
class P1StateGovernanceTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="sectrust-p1-")
        self.breaker = TTLCircuitBreaker(
            path=os.path.join(self.d, "b.json"), threshold=3, ttl=60.0)
        self.state = SkillStateStore(breaker=self.breaker)

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_three_consecutive_failures_open_circuit(self):
        last = {}
        for i in range(3):
            last = self.state.record("competitor-analysis", False,
                                     stage="sandbox", reason="脚本第%d次失败" % (i + 1))
        self.assertEqual(last["status"], "circuit_open")
        self.assertTrue(last["breaker_open"])
        self.assertEqual(last["consecutive_failures"], 3)
        hist = self.state.history_map()
        self.assertTrue(hist["competitor-analysis"]["circuit_open"])

    def test_rejected_is_not_counted_as_execution_failure(self):
        """门禁驳回 = 安全策略生效，不计入连续失败熔断。"""
        for _ in range(3):
            out = self.state.record("x", False, stage="approval",
                                    reason="需人工审批", rejected=True)
        self.assertEqual(out["status"], "rejected")
        self.assertEqual(out["consecutive_failures"], 0)
        self.assertFalse(out["breaker_open"])
        self.assertFalse(self.state.history_map()["x"]["circuit_open"])

    def test_router_filters_circuit_open_and_reset_restores(self):
        lib = StandardSkillLibrary()
        router = SkillRouter(library=lib, state=self.state)
        before = router.select("竞品分析报告", k=5)
        self.assertEqual(before["chosen"], "competitor-analysis")
        for _ in range(3):
            self.state.record("competitor-analysis", False, stage="sandbox",
                              reason="故意失败")
        after = router.select("竞品分析报告", k=5)
        cm = {c["name"]: c for c in after["candidates"]}
        self.assertEqual(cm["competitor-analysis"]["filtered_by"], "circuit_open")
        self.assertNotEqual(after["chosen"], "competitor-analysis",
                            "熔断技能不得再被选中（L1 阶段剔除省 Token）")
        self.state.reset("competitor-analysis")
        restored = router.select("竞品分析报告", k=5)
        self.assertEqual(restored["chosen"], "competitor-analysis")

    def test_feedback_hint_self_correction_and_reset_clears(self):
        for _ in range(3):
            self.state.record("competitor-analysis", False, stage="sandbox",
                              reason="脚本故意失败")
        hint = self.state.hint_for("competitor-analysis")
        self.assertIn("连续失败 3 次", hint)
        self.assertIn("不因上次失败", hint, "提示必须声明备选仍过门禁")
        # router 把状态层的自我纠正提示回灌到 select 结果（用可控 fake state
        # 精确验证接线；真实熔断场景下 chosen 是无失败历史的备选，feedback 为空
        # 是正确行为，不应断言非空）。
        class _StateWithHint:
            def history_map(self, sid):
                return {}
            def hint_for(self, name, sid):
                return "SELF_CORRECT_HINT"
        out = SkillRouter(library=StandardSkillLibrary(),
                          state=_StateWithHint()).select("竞品分析报告", k=5)
        self.assertEqual(out["feedback"], "SELF_CORRECT_HINT")
        self.assertEqual(self.state.reset("competitor-analysis")["status"], "ok")
        self.assertEqual(self.state.hint_for("competitor-analysis"), "")


# ==========================================================================
# P2 · 语义召回 / Rerank / 诚实标注
# ==========================================================================
class P2SemanticRetrievalTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="sectrust-p2-")
        self.lib, _ = _fixture_library(self.d)

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_cross_word_semantic_recall_ranks_first(self):
        """用户说"清理电脑"，技能叫"磁盘释放工具"——纯词项会漏，三路召回+RRF 不漏。"""
        retriever = SkillRetriever(
            self.lib.load_l1_metadata(include_broken=True), embedding=False)
        out = SkillRouter(library=self.lib, retriever=retriever).select(
            "帮我清理电脑释放磁盘空间", k=2)
        self.assertEqual(out["chosen"], "disk-cleanup")
        self.assertEqual(out["reranker"], "heuristic-v1")
        top = [c for c in out["candidates"] if c["rank"] == 1]
        self.assertEqual(top[0]["name"], "disk-cleanup")
        # 打分瀑布可解释：三路信号 + RRF + rerank 都在
        for key in ("bm25", "vector", "entity", "rrf", "rerank"):
            self.assertIn(key, top[0]["scores"])
        self.assertLess(out["top1_top2_gap"] if len(out["candidates"]) > 1 else 1,
                        1.0)

    def test_embedding_status_hashed_fallback_is_honest(self):
        """无真 embedding provider 时如实标 hashed_fallback，不冒充真向量。"""
        items = self.lib.load_l1_metadata(include_broken=True)
        r = SkillRetriever(items, embedding=False)
        self.assertEqual(r.status, "hashed_fallback")
        out = r.recall("清理电脑", k=2)
        self.assertTrue(out)
        self.assertTrue(all(c.embedding_status == "hashed_fallback"
                            for c in out))

    def test_broken_provider_degrades_to_hashed_not_mixed_space(self):
        """显式坏 provider：整体降级哈希索引，绝不混哈希与真向量空间。"""
        class _Broken:
            name = "broken"
            def embed(self, text):
                raise RuntimeError("provider down")
        items = self.lib.load_l1_metadata(include_broken=True)
        r = SkillRetriever(items, embedding=_Broken())
        self.assertEqual(r.status, "hashed_fallback")
        self.assertIsNone(r._provider)
        # 降级后仍能召回（不因 provider 挂掉归零）
        self.assertTrue(r.recall("清理电脑", k=2))

    def test_l1_metadata_never_carries_body(self):
        """L1 只读 frontmatter：正文标记绝不能出现在任何 L1 元数据里。"""
        items = self.lib.load_l1_metadata(include_broken=True)
        blob = json.dumps(items, ensure_ascii=False)
        self.assertNotIn("BODY_SENTINEL_MARKER_9527", blob)
        for it in items:
            self.assertNotIn("body", it)
            self.assertIn("description", it)


# ==========================================================================
# P3 · 分级安全（更松白名单）+ MCP 并行门 + guard_decision Trace
# ==========================================================================
class P3TierSafetyTest(unittest.TestCase):
    @staticmethod
    def _spec(**over):
        base = {"name": "s", "risk_level": "low", "allowed_tools": [],
                "has_scripts": False, "manifest": {}, "security_tier": "auto"}
        base.update(over)
        return base

    def test_medium_and_readonly_tools_can_be_white(self):
        """更松点1/3：medium + 只读意图工具（web_search/file_read）可进 white。"""
        dec = classify(self._spec(risk_level="medium",
                                  allowed_tools=["web_search", "file_read"]))
        self.assertEqual(dec.tier, "white")
        self.assertFalse(dec.downgraded)
        self.assertTrue(dec.checks["risk_acceptable"])
        self.assertEqual(gate_plan("white")["execution_mode"], "l2_only")
        self.assertFalse(gate_plan("white")["sandbox"])

    def test_scripts_forced_gray(self):
        """绝对底线①：带 scripts/ 强制 >= gray，沙箱不可免。"""
        dec = classify(self._spec(has_scripts=True))
        self.assertEqual(dec.tier, "gray")
        self.assertTrue(gate_plan("gray")["sandbox"])

    def test_side_effect_tool_forced_black(self):
        """绝对底线②：写/执行/发布类工具声明即 black。"""
        dec = classify(self._spec(allowed_tools=["web_search", "file_write"]))
        self.assertEqual(dec.tier, "black")
        self.assertIn("file_write", dec.side_effect_tools)
        self.assertTrue(gate_plan("black")["secret_scan"])

    def test_high_risk_or_approval_forced_black(self):
        for over in ({"risk_level": "high"},
                     {"manifest": {"requires_approval": True}}):
            self.assertEqual(classify(self._spec(**over)).tier, "black")

    def test_declared_white_downgrades_when_hard_condition_fails(self):
        """声明 white 但含 exec → 自动降级 black 并记原因，绝不静默放行。"""
        dec = classify(self._spec(allowed_tools=["exec"],
                                  security_tier="white"))
        self.assertEqual(dec.tier, "black")
        self.assertTrue(dec.downgraded)
        self.assertTrue(any("自动降级" in r for r in dec.reasons))

    def test_white_escalates_on_runtime_action(self):
        self.assertTrue(escalate_on_action("white", "run_script")[0])
        self.assertTrue(escalate_on_action("white", "write_files")[0])
        self.assertFalse(escalate_on_action("gray", "run_script")[0])
        self.assertFalse(escalate_on_action("white", "read")[0])

    def test_guard_white_waived_gates_and_script_request_rejected(self):
        d = tempfile.mkdtemp(prefix="sectrust-p3white-")
        try:
            root = os.path.join(d, "lib")
            _write_skill(root, "clean-skill", "清理磁盘释放空间的纯提示词技能。")
            guard = SkillExecutionGuard(library=StandardSkillLibrary(roots=[root]))
            okres = guard.execute("clean-skill", "")
            self.assertTrue(okres["ok"])
            self.assertEqual(okres["tier"], "white")
            self.assertEqual(okres["execution_mode"], "l2_only")
            for gate in ("approval", "sandbox", "secret_scan", "evidence",
                         "artifacts", "quality"):
                self.assertIn(gate, okres["audit"]["waived_gates"])
            # white 是可撤销的信任：请求执行脚本立即降级并拒绝，不偷偷跑
            bad = guard.execute("clean-skill", "scripts/run.py")
            self.assertFalse(bad["ok"])
            self.assertEqual(bad["stage"], "tier_escalation")
            self.assertEqual(bad["audit"]["rule_id"], "tier.white_script")
            self.assertEqual(bad["audit"]["decision"], "hold")
        finally:
            shutil.rmtree(d, ignore_errors=True)


class P3McpParallelTest(unittest.TestCase):
    """MCP 入向四门并行 fan-out + guard_decision Trace（离线 stdio fixture）。"""

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="sectrust-p3mcp-")
        self._env = mock.patch.dict(os.environ, {
            "SECTRUST_MCP_GITHUB_FIXTURE": "1", "GITHUB_TOKEN": ""})
        self._env.start()
        spec = ServerSpec(
            name="github-stdio",
            transport={"type": "stdio",
                       "command": [sys.executable, "-m",
                                   "mcp_adapter.servers.github"],
                       "cwd": ROOT},
            description="offline fixture",
            allowed_domains=["api.github.com", "github.com"],
            requires_approval_tools=["create_issue"],
            credential_ref="GITHUB_TOKEN", risk_level="medium")
        reg = MCPServerRegistry(path=os.path.join(self.d, "servers.json"))
        reg.register(spec)
        self.trace = TraceStore(db_path=os.path.join(self.d, "trace.db"))
        self.tid = self.trace.start_trace(title="v2 parallel", kind="test",
                                          started_by="tester")
        self.proxy = SecureProxy(
            reg, breaker=TTLCircuitBreaker(path=os.path.join(self.d, "b.json")),
            trace_store=self.trace, trace_id=self.tid)

    def tearDown(self):
        self.trace.finish(self.tid)
        self.trace.close()
        self._env.stop()
        shutil.rmtree(self.d, ignore_errors=True)

    def test_parallel_gates_wall_time_beats_serial(self):
        base = self.proxy.call_tool(
            "github-stdio", "search_repositories", {"query": "mcp", "limit": 2})
        self.assertTrue(base.ok, base.reason)
        self.assertEqual(base.audit["decision"], "allow")
        self.assertEqual(base.audit["parallel_gates"], 4)
        self.assertEqual(base.audit["tier"], "black")
        self.assertEqual(base.audit["cost_estimate"], None, "无计费器不编数字")

        def slow_domain(spec, params):
            time.sleep(0.06)
            return True, "ok", ["github.com"]

        def slow_cred(spec, params):
            time.sleep(0.06)
            return True, "ok", {}, {}, {"state": "unavailable"}

        def slow_scan(text):
            time.sleep(0.06)
            return []

        with mock.patch.object(self.proxy, "_domain_gate", slow_domain), \
             mock.patch.object(self.proxy, "_credential_gate", slow_cred), \
             mock.patch("mcp_adapter.proxy.scan_secrets", slow_scan):
            t0 = time.time()
            r = self.proxy.call_tool("github-stdio", "search_repositories",
                                     {"query": "x"})
            wall = (time.time() - t0) * 1000
        self.assertTrue(r.ok)
        a = r.audit
        self.assertLess(a["parallel_wall_ms"], a["serial_sum_ms"],
                        "并行墙钟必须小于串行累加")
        self.assertGreaterEqual(a["serial_sum_ms"], 150.0)   # 3 门 x 60ms
        self.assertLess(a["parallel_wall_ms"], 150.0)        # 并行 < 串行
        # wall 含 stdio 子进程握手/协议往返开销（约 0.7~0.9s），不能拿它当
        # 并行门耗时；并行收益只看 audit 的 parallel_wall_ms vs serial_sum_ms。
        self.assertLess(wall, 3000.0)                        # 整体不灾难性退化

        evs = [e for e in self.trace.events(self.tid)
               if e["kind"] == "guard_decision"]
        self.assertGreaterEqual(len(evs), 2)
        detail = evs[-1]["detail"]
        for key in ("stages", "blocked_by", "rule_id", "decision",
                    "parallel_gates", "parallel_wall_ms", "serial_sum_ms"):
            self.assertIn(key, detail)

    def test_blocked_by_and_rule_id_recorded_on_denial(self):
        """域名门拒绝 → blocked_by/rule_id 精确落审计，decision=reject。"""
        def deny_domain(spec, params):
            return False, "域名不在白名单", []

        with mock.patch.object(self.proxy, "_domain_gate", deny_domain):
            r = self.proxy.call_tool("github-stdio", "search_repositories",
                                     {"query": "x"})
        self.assertFalse(r.ok)
        self.assertEqual(r.stage, "domain")
        a = r.audit
        self.assertEqual(a["decision"], "reject")
        self.assertEqual(a["blocked_by"], "domain:domain.not_allowed")
        self.assertEqual(a["rule_id"], "domain.not_allowed")
        # 入向密钥扫描是安全底线，不因别的门拒绝就被短路跳过
        scan_stage = [s for s in a["stages"]
                      if s["stage"] == "secret_scan_in"]
        self.assertEqual(len(scan_stage), 1)
        self.assertIsNotNone(scan_stage[0]["ok"])
        evs = [e for e in self.trace.events(self.tid)
               if e["kind"] == "guard_decision"]
        self.assertTrue(any(e["detail"]["decision"] == "reject" for e in evs))


# ==========================================================================
# P4 · 技能执行可观测仪表盘（端点鉴权 / 数据结构 / 前端渲染）
# ==========================================================================
class _FakeRegistryHandler:
    pass


class P4DashboardEndpointTest(unittest.TestCase):
    """离线回环 http.server，不起真 daemon；覆盖 401/403/200 与数据结构。"""

    @classmethod
    def setUpClass(cls):
        import console_web
        cls._cw = console_web
        cls._old_cwd = os.getcwd()
        cls.tmp = tempfile.mkdtemp(prefix="sectrust-p4-")
        os.chdir(cls.tmp)   # SkillStateStore 默认落盘到此，不污染工作区
        os.makedirs(os.path.join(cls.tmp, "data"), exist_ok=True)

        saved = {}
        for attr in ("db_path", "sessions", "trace", "registry"):
            saved[attr] = getattr(console_web.Handler, attr, None)
        cls._saved = saved
        console_web.Handler.db_path = None      # 禁止请求路径拉起真 daemon
        console_web.Handler.sessions = console_api.SessionStore()
        console_web.Handler.registry = _FakeRegistry()
        cls.trace = TraceStore(db_path=os.path.join(cls.tmp, "trace.db"))
        console_web.Handler.trace = cls.trace
        tid = cls.trace.start_trace(title="skillops fixture", kind="test",
                                    started_by="tester", trace_id="tr-skillops-1")
        cls.trace.record(tid, "skill_match", task_id="", actor="agentskills",
                         summary="L1 选技能：清理电脑 → disk-cleanup",
                         detail={"query": "清理电脑",
                                 "embedding_status": "hashed_fallback",
                                 "reranker": "heuristic-v1",
                                 "candidates": [
                                     {"name": "disk-cleanup", "rank": 1,
                                      "chosen": True, "tier": "white"}]},
                         content=None)
        cls.trace.record(tid, "guard_decision", task_id="", actor="agentskills",
                         summary="门禁 white → allow",
                         detail={"target": "skill:disk-cleanup", "tier": "white",
                                 "decision": "allow", "blocked_by": "",
                                 "rule_id": "", "stages": []},
                         content=None)

        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), console_web.Handler)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.trace.close()
        for attr, val in cls._saved.items():
            setattr(cls._cw.Handler, attr, val)
        os.chdir(cls._old_cwd)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _req(self, path, payload=None, token=None):
        url = "http://127.0.0.1:%d%s" % (self.port, path)
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(
            url, data=data, method=("POST" if data is not None else "GET"))
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if token:
            req.add_header("X-Session-Token", token)
        try:
            with self._opener.open(req, timeout=10) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8"))

    def _login(self, role="auditor"):
        st, d = self._req("/api/login", {"user": "v2-" + role, "role": role})
        self.assertEqual(st, 200, d)
        return d["token"]

    def test_get_requires_login(self):
        for path in ("/api/governance/skills/state",
                     "/api/governance/skills/decisions",
                     "/api/governance/skills/guard",
                     "/api/governance/skills/summary"):
            st, d = self._req(path)
            self.assertEqual(st, 401, path)
            self.assertTrue(d.get("need_auth"))

    def test_get_data_structures(self):
        tok = self._login("auditor")
        st, state = self._req("/api/governance/skills/state", token=tok)
        self.assertEqual(st, 200)
        for key in ("skills", "threshold", "ttl", "breaker_path"):
            self.assertIn(key, state)

        st, dec = self._req("/api/governance/skills/decisions", token=tok)
        self.assertEqual(st, 200)
        self.assertEqual(dec["trace_id"], "tr-skillops-1")
        self.assertTrue(dec["events"])
        self.assertEqual(dec["events"][0]["kind"], "skill_match")

        st, guard = self._req("/api/governance/skills/guard?limit=30", token=tok)
        self.assertEqual(st, 200)
        for key in ("items", "by_blocked_by", "total"):
            self.assertIn(key, guard)
        self.assertGreaterEqual(guard["total"], 1)
        self.assertIn("-", guard["by_blocked_by"],
                      "blocked_by 为空时按 '-' 聚合（见 _skill_observability）")

        st, summary = self._req("/api/governance/skills/summary", token=tok)
        self.assertEqual(st, 200)
        for key in ("skills", "threshold", "ttl", "trace_kinds"):
            self.assertIn(key, summary)

    def test_run_requires_login_and_auditor_creates_skill_task(self):
        body = {"title": "清理电脑释放磁盘", "query": "清理电脑释放磁盘",
                "role": "auditor", "skill_k": 3}
        st, d = self._req("/api/governance/skills/run", body)
        self.assertEqual(st, 401)
        tok = self._login("auditor")
        st, d = self._req("/api/governance/skills/run", body, tok)
        self.assertEqual(st, 200, d)
        self.assertTrue(d["ok"])
        self.assertEqual(d["payload"]["op"], "skill")
        self.assertEqual(d["payload"]["query"], "清理电脑释放磁盘")

    def test_reset_requires_admin_privilege(self):
        tok = self._login("auditor")
        st, d = self._req("/api/governance/skills/state/reset",
                          {"skill": "disk-cleanup"}, tok)
        self.assertEqual(st, 403)
        self.assertEqual(d.get("need_permission"), "governance.emergency")
        adm = self._login("admin")
        st, d = self._req("/api/governance/skills/state/reset",
                          {"skill": "disk-cleanup"}, adm)
        self.assertEqual(st, 200, d)
        self.assertTrue(d["ok"])
        self.assertEqual(d["skill"], "disk-cleanup")


# 内嵌 JS 的 DOM stub 验证脚本：从 console_web.py 抽出的 <script> 由 argv[2] 传入，
# 在 vm 沙箱里用 fake fetch 数据渲染 loadSkillOps，检查三视图关键内容都出现。
# 项目纪律：页面验证 = node --check + DOM stub，不能只验 HTTP 200。
_DOM_STUB_CJS = r"""
const fs = require('fs');
const vm = require('vm');
const js = fs.readFileSync(process.argv[2], 'utf8');
function mkEl() {
  const store = { innerHTML: '', textContent: '', value: '', style: {}, dataset: {} };
  const classList = { add() {}, remove() {}, toggle() {}, contains() { return false; },
                      length: 0, item() { return null; } };
  return new Proxy(store, {
    get(t, k) {
      if (k in t) return t[k];
      if (k === 'classList') return classList;
      if (k === 'style' || k === 'dataset') return t[k] || {};
      if (k === 'querySelectorAll') return () => [];
      if (k === 'querySelector') return () => mkEl();
      if (['addEventListener','removeEventListener','appendChild','removeChild',
           'setAttribute','removeAttribute','getAttribute','focus','click','blur',
           'insertAdjacentHTML','scrollIntoView','remove','replaceChild'].includes(k))
        return () => {};
      if (k === 'getBoundingClientRect')
        return () => ({top:0,left:0,width:0,height:0,bottom:0,right:0});
      if (k === 'children' || k === 'childNodes') return [];
      if (['firstChild','lastChild','parentNode','parentElement','nextSibling','previousSibling'].includes(k)) return null;
      if (['offsetWidth','offsetHeight','clientWidth','clientHeight','scrollHeight','scrollTop'].includes(k)) return 0;
      if (k === 'length') return 0;
      if (k === 'item') return () => null;
      if (typeof k === 'symbol') return undefined;
      return mkEl();
    },
    set(t, k, v) { t[k] = v; return true; },
    has() { return true; },
  });
}
const els = {};
const document = {
  getElementById(id) { return els[id] || (els[id] = mkEl()); },
  querySelector() { return mkEl(); },
  querySelectorAll() { return []; },
  createElement() { return mkEl(); },
  addEventListener() {}, removeEventListener() {},
  body: mkEl(), head: mkEl(), documentElement: mkEl(), cookie: '', title: '',
};
const fake = {
  '/api/governance/skills/state': {
    skills: {
      'competitor-analysis': {status:'circuit_open', success_rate:0.2,
                              consecutive_failures:3, last_stage:'sandbox'},
      'disk-cleanup': {status:'ok', success_rate:1,
                       consecutive_failures:0, last_stage:''},
    }, threshold: 3, ttl: 60,
  },
  '/api/governance/skills/decisions': {
    trace_id: 'tr-1', events: [{ kind:'skill_match', detail: {
      query:'清理电脑', embedding_status:'hashed_fallback',
      reranker:'heuristic-v1', top1_top2_gap:0.02, needs_human:true,
      candidates: [
        {name:'磁盘释放工具', rank:1, chosen:true, tier:'white',
         scores:{bm25:0.1,vector:0.4,entity:0.3,rrf:0.008,rerank:0.52}, reason:'首位'},
        {name:'registry-first', rank:2, chosen:false, tier:'white',
         scores:{rrf:0.007,rerank:0.50}, reason:'', filtered_by:''},
      ],
    }}],
  },
  '/api/governance/skills/guard?limit=30': {
    items: [{ detail: { target:'skill:competitor-analysis', decision:'reject',
      blocked_by:'sandbox:sandbox.fail', stages: [
        {stage:'tier',ok:true,note:'',rule_id:'',parallel:false},
        {stage:'sandbox',ok:false,note:'脚本失败',rule_id:'sandbox.fail',parallel:false},
      ]}}],
    by_blocked_by: {'sandbox:sandbox.fail':1}, total:1,
  },
};
async function fetch(u) {
  const data = fake[String(u)] || fake[String(u).split('?')[0]] || {};
  return {ok:true, status:200, json: async () => data, text: async () => JSON.stringify(data)};
}
const location = {hash:'', href:'', pathname:'/', search:'', assign(){}, replace(){}, reload(){}};
const history = {pushState(){}, replaceState(){}, back(){}};
const navigator = {userAgent:'node', clipboard:{writeText: async () => {}}};
const localStorage = {getItem(){return null;}, setItem(){}, removeItem(){}, clear(){}};
const ctx = {
  console, document, fetch, location, history, navigator, localStorage,
  setTimeout(){return 0;}, clearTimeout(){}, setInterval(){return 0;}, clearInterval(){},
  requestAnimationFrame(){return 0;}, cancelAnimationFrame(){},
  alert(){}, confirm(){return false;}, prompt(){return '';},
  Notification: function(){}, URL, URLSearchParams, Date, Math, JSON, Intl,
  unhandledRejection: null,
};
ctx.window = ctx;
ctx.globalThis = ctx;
vm.createContext(ctx);
let initErr = '';
try { vm.runInContext(js, ctx, {timeout: 8000}); }
catch (e) { initErr = e.message; }
(async () => {
  if (typeof ctx.loadSkillOps !== 'function') {
    console.log('FAIL: loadSkillOps 未定义 | initErr=' + initErr);
    process.exit(1);
  }
  await ctx.loadSkillOps();
  const html = els['view-skillops'] ? els['view-skillops'].innerHTML : '';
  const body = els['skopsBody'] ? els['skopsBody'].innerHTML : '';
  const all = html + body;
  const checks = ['状态热力', '打分瀑布', '门禁泳道', '试跑', 'circuit_open',
                  '磁盘释放工具', 'heuristic-v1', 'sandbox.fail'];
  const missing = checks.filter(k => !all.includes(k));
  console.log('initErr(可容忍):', initErr || '(none)');
  console.log('rendered chars:', all.length);
  console.log('missing:', JSON.stringify(missing));
  if (missing.length) { console.log('FAIL: 视图缺少关键内容'); process.exit(1); }
  console.log('OK: DOM stub 渲染通过（三视图 + 试跑 + 熔断状态 + 命中规则均可见）');
})();
"""


# 治理视图 DOM stub：验证「合规治理」面板真能渲染七块（熔断/RBAC/资产/合规/互联/KB出域/ETSI）。
_DOM_STUB_GOV_CJS = r"""
const fs = require('fs');
const vm = require('vm');
const js = fs.readFileSync(process.argv[2], 'utf8');
function mkEl() {
  const store = { innerHTML: '', textContent: '', value: '', style: {}, dataset: {} };
  const classList = { add() {}, remove() {}, toggle() {}, contains() { return false; },
                      length: 0, item() { return null; } };
  return new Proxy(store, {
    get(t, k) {
      if (k in t) return t[k];
      if (k === 'classList') return classList;
      if (k === 'style' || k === 'dataset') return t[k] || {};
      if (k === 'querySelectorAll') return () => [];
      if (k === 'querySelector') return () => mkEl();
      if (['addEventListener','removeEventListener','appendChild','removeChild',
           'setAttribute','removeAttribute','getAttribute','focus','click','blur',
           'insertAdjacentHTML','scrollIntoView','remove','replaceChild'].includes(k))
        return () => {};
      if (k === 'getBoundingClientRect')
        return () => ({top:0,left:0,width:0,height:0,bottom:0,right:0});
      if (k === 'children' || k === 'childNodes') return [];
      if (['firstChild','lastChild','parentNode','parentElement','nextSibling','previousSibling'].includes(k)) return null;
      if (['offsetWidth','offsetHeight','clientWidth','clientHeight','scrollHeight','scrollTop'].includes(k)) return 0;
      if (k === 'length') return 0;
      if (k === 'item') return () => null;
      if (typeof k === 'symbol') return undefined;
      return mkEl();
    },
    set(t, k, v) { t[k] = v; return true; },
    has() { return true; },
  });
}
const els = {};
const document = {
  getElementById(id) { return els[id] || (els[id] = mkEl()); },
  querySelector() { return mkEl(); },
  querySelectorAll() { return []; },
  createElement() { return mkEl(); },
  addEventListener() {}, removeEventListener() {},
  body: mkEl(), head: mkEl(), documentElement: mkEl(), cookie: '', title: '',
};
const fake = {
  '/api/governance/rbac': {
    permissions: ['task.create','governance.read','governance.emergency','asset.write',
                  'kb.label','egress.request','interop.write','compliance.export'],
    roles: {
      auditor: ['task.create','governance.read','kb.label','egress.request'],
      admin: ['governance.read','governance.emergency','asset.write','interop.write'],
    },
  },
  '/api/governance/circuit': {
    open: false, tripped_by: 'root', tripped_at: null, reason: '-',
    reset_by: null, reset_at: null, history: [
      {event:'trip', by:'root', at:1793281815, reason:'应急演练'},
      {event:'reset', by:'root', at:1793281816, reason:'解除熔断'}],
    path: 'data/circuit_breaker.json',
  },
  '/api/governance/assets': {
    summary: {total: 5, by_kind: {agent: 3, model: 2},
              by_classification: {internal: 5}, discovered_at: 1793000000},
    assets: [
      {id:'agent:auditor', kind:'agent', classification:'internal', meta:{desc:'审计岗位'}},
      {id:'model:gpt-4o-mini', kind:'model', classification:'internal', meta:{desc:'默认模型'}},
    ],
  },
  '/api/governance/compliance': {
    title: 'SecTrust 协作审计台 · 合规自评报告', generated_at: 1793317115,
    baseline: '国标委〔2026〕41号', total_evaluated: 10,
    summary: {implemented: 9, partial: 1, designed: 0, exempt: 1},
    capabilities: [
      {id:'asset_inventory', name:'资产盘点与管理', standard:'国标委〔2026〕41号·六项之一',
       status:'implemented', evidence:'governance.asset_registry', note:'已登记+自动发现'},
      {id:'identity_authz', name:'身份认证与权限', standard:'国标委〔2026〕41号·六项之一',
       status:'implemented', evidence:'governance.rbac', note:'RBAC 强校验'},
    ],
  },
  '/api/governance/interop': {
    descriptors: [{agent_id:'agent:admin', display_name:'平台管理员',
                   schema_version:'gbz185-2026.draft'}],
  },
  '/api/governance/kb/labels': {labels: {}, updated_at: null, path:'data/kb_labels.json'},
  '/api/governance/egress': {requests: {}, denials: [], updated_at: null,
                             path:'data/egress_approvals.json'},
  '/api/governance/interop/peers': {peers: [], discovered: {}, audits: [],
                                    path:'data/interop_federation.json'},
  '/api/governance/etsi': {
    standard: 'ETSI TS 104 033',
    hardening_manifest: {standard:'ETSI TS 104 033', scope:'应用层就绪 + 部署层建议',
                         systemd_unit:'# /etc/systemd/system/sectrust.service'},
  },
};
async function fetch(u) {
  const key = String(u).split('?')[0];
  const data = fake[key] || {};
  return {ok:true, status:200, json: async () => data, text: async () => JSON.stringify(data)};
}
const location = {hash:'', href:'', pathname:'/', search:'', assign(){}, replace(){}, reload(){}};
const history = {pushState(){}, replaceState(){}, back(){}};
const navigator = {userAgent:'node', clipboard:{writeText: async () => {}}};
const localStorage = {getItem(){return null;}, setItem(){}, removeItem(){}, clear(){}};
const ctx = {
  console, document, fetch, location, history, navigator, localStorage,
  setTimeout(){return 0;}, clearTimeout(){}, setInterval(){return 0;}, clearInterval(){},
  requestAnimationFrame(){return 0;}, cancelAnimationFrame(){},
  alert(){}, confirm(){return false;}, prompt(){return '';},
  Notification: function(){}, URL, URLSearchParams, Date, Math, JSON, Intl,
};
ctx.window = ctx;
ctx.globalThis = ctx;
vm.createContext(ctx);
let initErr = '';
try { vm.runInContext(js, ctx, {timeout: 8000}); }
catch (e) { initErr = e.message; }
(async () => {
  if (typeof ctx.loadGovernance !== 'function') {
    console.log('FAIL: loadGovernance 未定义 | initErr=' + initErr);
    process.exit(1);
  }
  await ctx.loadGovernance();
  const html = els['view-governance'] ? els['view-governance'].innerHTML : '';
  const body = els['govBody'] ? els['govBody'].innerHTML : '';
  const all = html + body;
  const checks = ['全局紧急熔断','角色权限矩阵','资产盘点与管理','合规自评矩阵',
                  '互联描述符与联邦','知识库分级与数据出域审批','ETSI TS 104 033',
                  'fail-closed','default-deny','agent:admin','已实现','应急演练'];
  const missing = checks.filter(k => !all.includes(k));
  console.log('initErr(可容忍):', initErr || '(none)');
  console.log('rendered chars:', all.length);
  console.log('missing:', JSON.stringify(missing));
  if (missing.length) { console.log('FAIL: 治理视图缺少关键内容'); process.exit(1); }
  console.log('OK: 治理视图渲染通过（七块面板 + 诚实边界 + 样例数据均可见）');
})();
"""

# 任务详情 DOM stub：验证 showTaskDetail 在带 cached_params / error_info /
# self_correction_hint 的任务上能渲染三块（会话记忆 / 结构化错误 / 自我反思），
# 且在干净任务上不误显示错误块。
_DOM_STUB_TASKDETAIL_CJS = r"""
const fs = require('fs');
const vm = require('vm');
const js = fs.readFileSync(process.argv[2], 'utf8');
function mkEl() {
  const store = { innerHTML: '', textContent: '', value: '', style: {}, dataset: {} };
  const classList = { add() {}, remove() {}, toggle() {}, contains() { return false; },
                      length: 0, item() { return null; } };
  return new Proxy(store, {
    get(t, k) {
      if (k in t) return t[k];
      if (k === 'classList') return classList;
      if (k === 'style' || k === 'dataset') return t[k] || {};
      if (k === 'querySelectorAll') return () => [];
      if (k === 'querySelector') return () => mkEl();
      if (['addEventListener','removeEventListener','appendChild','removeChild',
           'setAttribute','removeAttribute','getAttribute','focus','click','blur',
           'insertAdjacentHTML','scrollIntoView','remove','replaceChild'].includes(k))
        return () => {};
      if (k === 'getBoundingClientRect')
        return () => ({top:0,left:0,width:0,height:0,bottom:0,right:0});
      if (k === 'children' || k === 'childNodes') return [];
      if (['firstChild','lastChild','parentNode','parentElement','nextSibling','previousSibling'].includes(k)) return null;
      if (['offsetWidth','offsetHeight','clientWidth','clientHeight','scrollHeight','scrollTop'].includes(k)) return 0;
      if (k === 'length') return 0;
      if (k === 'item') return () => null;
      if (typeof k === 'symbol') return undefined;
      return mkEl();
    },
    set(t, k, v) { t[k] = v; return true; },
    has() { return true; },
  });
}
const els = {};
const document = {
  getElementById(id) { return els[id] || (els[id] = mkEl()); },
  querySelector() { return mkEl(); },
  querySelectorAll() { return []; },
  createElement() { return mkEl(); },
  addEventListener() {}, removeEventListener() {},
  body: mkEl(), head: mkEl(), documentElement: mkEl(), cookie: '', title: '',
};
// 带会话记忆 + 结构化错误 + 自我反思的任务
const richTask = {
  task_id: 't1', status: 'failed', op: 'skill', name: 'disk-cleanup', runner_id: 'r1',
  instruction: '清理磁盘', payload: { role: 'writer', intended_colleague: 'zhipu' },
  result: {
    op: 'skill', query: '清理磁盘',
    skill_match: { cached_params: { API_KEY: '<redacted>', PROJECT_PATH: '/home/x' },
                   chosen: 'disk-cleanup' },
    executed_by: '商汤 SenseNova',
    error_info: { error_code: 'APPROVAL_PENDING',
                  human_readable_message: '需要审批才能执行',
                  technical_detail: 'approval_id=abc123 stage=approval', stage: 'approval' },
    self_correction_hint: '请向用户索取审批单号后重试',
    feedback_hint: '',
  },
};
async function fetch() {
  return { ok: true, status: 200, json: async () => ({}), text: async () => '{}' };
}
const location = { hash: '', href: '', pathname: '/', search: '', assign(){}, replace(){}, reload(){} };
const history = { pushState(){}, replaceState(){}, back(){} };
const navigator = { userAgent: 'node', clipboard: { writeText: async () => {} } };
const localStorage = { getItem(){ return null; }, setItem(){}, removeItem(){}, clear(){} };
const ctx = {
  console, document, fetch, location, history, navigator, localStorage,
  setTimeout(){ return 0; }, clearTimeout(){}, setInterval(){ return 0; }, clearInterval(){},
  requestAnimationFrame(){ return 0; }, cancelAnimationFrame(){},
  alert(){}, confirm(){ return false; }, prompt(){ return ''; },
  Notification: function(){}, URL, URLSearchParams, Date, Math, JSON, Intl,
};
ctx.window = ctx;
ctx.globalThis = ctx;
vm.createContext(ctx);
let initErr = '';
try { vm.runInContext(js, ctx, { timeout: 8000 }); }
catch (e) { initErr = e.message; }
(async () => {
  if (typeof ctx.showTaskDetail !== 'function') {
    console.log('FAIL: showTaskDetail 未定义 | initErr=' + initErr);
    process.exit(1);
  }
  ctx.jget = async () => richTask;
  await ctx.showTaskDetail('t1');
  const rich = els['taskDetail'] ? els['taskDetail'].innerHTML : '';
  const richChecks = ['会话记忆', 'API_KEY', '（已脱敏）', 'PROJECT_PATH', '/home/x',
                      '结构化错误', 'APPROVAL_PENDING', '需要审批才能执行',
                      'AI 自我反思建议', '请向用户索取审批单号'];
  const richMissing = richChecks.filter(k => !rich.includes(k));
  console.log('initErr(可容忍):', initErr || '(none)');
  console.log('rich rendered chars:', rich.length);
  console.log('rich missing:', JSON.stringify(richMissing));
  if (richMissing.length) { console.log('FAIL: 详情页缺少会话记忆/错误/反思内容'); process.exit(1); }
  // 干净任务：无 error_info / cached_params，不应显示错误块
  ctx.jget = async () => ({ task_id: 't2', status: 'done', op: 'llm', name: '',
                            payload: {}, result: { op: 'llm', output: '你好' } });
  await ctx.showTaskDetail('t2');
  const clean = els['taskDetail'] ? els['taskDetail'].innerHTML : '';
  if (clean.includes('结构化错误') || clean.includes('会话记忆')) {
    console.log('FAIL: 干净任务不应显示结构化错误/会话记忆块');
    process.exit(1);
  }
  console.log('OK: 任务详情页渲染通过（富任务三块齐全 + 干净任务不误显）');
})();
"""

# 任务列表（任务中心表格）DOM stub：验证 loadTasks 在带 cached_params / error_info /
# self_correction_hint 的任务上，结果列附加「🧠 记忆 / ⛔ 错误 / 💡 反思」紧凑徽章，
# 干净任务不误显。
_DOM_STUB_TASKLIST_CJS = r"""
const fs = require('fs');
const vm = require('vm');
const js = fs.readFileSync(process.argv[2], 'utf8');
function mkEl() {
  const store = { innerHTML: '', textContent: '', value: '', style: {}, dataset: {} };
  const classList = { add() {}, remove() {}, toggle() {}, contains() { return false; },
                      length: 0, item() { return null; } };
  return new Proxy(store, {
    get(t, k) {
      if (k in t) return t[k];
      if (k === 'classList') return classList;
      if (k === 'style' || k === 'dataset') return t[k] || {};
      if (k === 'querySelectorAll') return () => [];
      if (k === 'querySelector') return () => mkEl();
      if (['addEventListener','removeEventListener','appendChild','removeChild',
           'setAttribute','removeAttribute','getAttribute','focus','click','blur',
           'insertAdjacentHTML','scrollIntoView','remove','replaceChild'].includes(k))
        return () => {};
      if (k === 'getBoundingClientRect')
        return () => ({top:0,left:0,width:0,height:0,bottom:0,right:0});
      if (k === 'children' || k === 'childNodes') return [];
      if (['firstChild','lastChild','parentNode','parentElement','nextSibling','previousSibling'].includes(k)) return null;
      if (['offsetWidth','offsetHeight','clientWidth','clientHeight','scrollHeight','scrollTop'].includes(k)) return 0;
      if (k === 'length') return 0;
      if (k === 'item') return () => null;
      if (typeof k === 'symbol') return undefined;
      return mkEl();
    },
    set(t, k, v) { t[k] = v; return true; },
    has() { return true; },
  });
}
const els = {};
const document = {
  getElementById(id) { return els[id] || (els[id] = mkEl()); },
  querySelector() { return mkEl(); },
  querySelectorAll() { return []; },
  createElement() { return mkEl(); },
  addEventListener() {}, removeEventListener() {},
  body: mkEl(), head: mkEl(), documentElement: mkEl(), cookie: '', title: '',
};
const rich = { task_id:'t1', status:'failed', op:'skill', runner_id:'r1',
  created_at:'2026-10-30 21:00:00',
  payload:{ result:{ op:'skill',
    skill_match:{ cached_params:{ API_KEY:'<redacted>', PROJECT_PATH:'/home/x' }, chosen:'disk-cleanup' },
    error_info:{ error_code:'APPROVAL_PENDING', human_readable_message:'需要审批才能执行', stage:'approval' },
    self_correction_hint:'请向用户索取审批单号后重试' } } };
const cleanish = { task_id:'t2', status:'done', op:'llm', runner_id:'r2',
  created_at:'2026-10-30 21:01:00', payload:{ result:{ op:'llm', output:'ok' } } };
const clean = { task_id:'t3', status:'done', op:'llm', runner_id:'r2',
  created_at:'2026-10-30 21:02:00', payload:{ result:{ op:'llm', output:'你好' } } };
let mode='rich';
async function fetch() {
  return { ok: true, status: 200, json: async () => ({}), text: async () => '{}' };
}
const location = { hash: '', href: '', pathname: '/', search: '', assign(){}, replace(){}, reload(){} };
const history = { pushState(){}, replaceState(){}, back(){} };
const navigator = { userAgent: 'node', clipboard: { writeText: async () => {} } };
const localStorage = { getItem(){ return null; }, setItem(){}, removeItem(){}, clear(){} };
const ctx = {
  console, document, fetch, location, history, navigator, localStorage,
  setTimeout(){ return 0; }, clearTimeout(){}, setInterval(){ return 0; }, clearInterval(){},
  requestAnimationFrame(){ return 0; }, cancelAnimationFrame(){},
  addEventListener(){}, removeEventListener(){},
  alert(){}, confirm(){ return false; }, prompt(){ return ''; },
  Notification: function(){}, URL, URLSearchParams, Date, Math, JSON, Intl,
};
ctx.window = ctx;
ctx.globalThis = ctx;
vm.createContext(ctx);
let initErr = '';
try { vm.runInContext(js, ctx, { timeout: 8000 }); }
catch (e) { initErr = e.message; }
(async () => {
  if (typeof ctx.loadTasks !== 'function') {
    console.log('FAIL: loadTasks 未定义 | initErr=' + initErr);
    process.exit(1);
  }
  ctx.jget = async (url) => {
    if (url.indexOf('/api/dag') >= 0) return { tasks:{} };
    if (mode === 'rich') return [rich, cleanish];
    return [clean];
  };
  await ctx.loadTasks();
  const body = els['tasksBody'] ? els['tasksBody'].innerHTML : '';
  const checks = ['🧠 记忆 2', '⛔ APPROVAL_PENDING', '💡 反思', 't1', 't2'];
  const missing = checks.filter(k => !body.includes(k));
  console.log('initErr(可容忍):', initErr || '(none)');
  console.log('list body chars:', body.length);
  console.log('list missing:', JSON.stringify(missing));
  if (missing.length) { console.log('FAIL: 任务列表缺少会话记忆/错误/反思徽章'); process.exit(1); }
  // 干净模式：不应出现错误/记忆/反思徽章
  mode = 'clean';
  await ctx.loadTasks();
  const body2 = els['tasksBody'] ? els['tasksBody'].innerHTML : '';
  if (body2.includes('⛔') || body2.includes('🧠 记忆') || body2.includes('💡 反思')) {
    console.log('FAIL: 干净任务不应显示错误/记忆/反思徽章');
    process.exit(1);
  }
  if (!body2.includes('t3')) { console.log('FAIL: 干净任务未渲染'); process.exit(1); }
  console.log('OK: 任务列表渲染通过（富任务三徽章齐全 + 干净任务不误显）');
})();
"""


class P4FrontendRenderTest(unittest.TestCase):
    """抽内嵌 JS 做 node --check + DOM stub，验证仪表盘真能渲染。"""

    @unittest.skipUnless(shutil.which("node"), "node 不可用，跳过前端渲染验证")
    def test_skillops_dashboard_renders_three_views(self):
        src_path = os.path.join(ROOT, "console_web.py")
        with open(src_path, "r", encoding="utf-8") as fh:
            src = fh.read()
        m = re.search(r"<script>(.*?)</script>", src, re.S)
        self.assertIsNotNone(m, "console_web.py 应含内嵌 <script>")
        js = m.group(1)
        self.assertIn("loadSkillOps", js)
        self.assertIn("runSkillOps", js)

        d = tempfile.mkdtemp(prefix="sectrust-p4js-")
        try:
            js_path = os.path.join(d, "_skillops_check.js")
            cjs_path = os.path.join(d, "_dom_stub_check.cjs")
            with open(js_path, "w", encoding="utf-8") as fh:
                fh.write(js)
            with open(cjs_path, "w", encoding="utf-8") as fh:
                fh.write(_DOM_STUB_CJS)
            chk = subprocess.run(["node", "--check", js_path],
                                 capture_output=True, text=True, timeout=30)
            self.assertEqual(chk.returncode, 0,
                             "node --check 失败：%s" % chk.stderr[:500])
            run = subprocess.run(["node", cjs_path, js_path],
                                 capture_output=True, text=True, timeout=30)
            self.assertEqual(run.returncode, 0,
                             "DOM stub 渲染失败：%s\n%s"
                             % (run.stdout[-800:], run.stderr[-800:]))
            self.assertIn("OK: DOM stub 渲染通过", run.stdout)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    @unittest.skipUnless(shutil.which("node"), "node 不可用，跳过前端渲染验证")
    def test_governance_view_renders(self):
        src_path = os.path.join(ROOT, "console_web.py")
        with open(src_path, "r", encoding="utf-8") as fh:
            src = fh.read()
        m = re.search(r"<script>(.*?)</script>", src, re.S)
        self.assertIsNotNone(m, "console_web.py 应含内嵌 <script>")
        js = m.group(1)
        self.assertIn("loadGovernance", js)
        self.assertIn("govCircuitPanel", js)
        self.assertIn("govCompliancePanel", js)

        d = tempfile.mkdtemp(prefix="sectrust-govjs-")
        try:
            js_path = os.path.join(d, "_gov_check.js")
            cjs_path = os.path.join(d, "_gov_dom_stub.cjs")
            with open(js_path, "w", encoding="utf-8") as fh:
                fh.write(js)
            with open(cjs_path, "w", encoding="utf-8") as fh:
                fh.write(_DOM_STUB_GOV_CJS)
            chk = subprocess.run(["node", "--check", js_path],
                                 capture_output=True, text=True, timeout=30)
            self.assertEqual(chk.returncode, 0,
                             "node --check 失败：%s" % chk.stderr[:500])
            run = subprocess.run(["node", cjs_path, js_path],
                                 capture_output=True, text=True, timeout=30)
            self.assertEqual(run.returncode, 0,
                             "治理视图 DOM stub 渲染失败：%s\n%s"
                             % (run.stdout[-800:], run.stderr[-800:]))
            self.assertIn("OK: 治理视图渲染通过", run.stdout)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    @unittest.skipUnless(shutil.which("node"), "node 不可用，跳过前端渲染验证")
    def test_task_detail_renders_cache_and_error(self):
        """验证 showTaskDetail 渲染 cached_params / error_info / self_correction_hint。"""
        src_path = os.path.join(ROOT, "console_web.py")
        with open(src_path, "r", encoding="utf-8") as fh:
            src = fh.read()
        m = re.search(r"<script>(.*?)</script>", src, re.S)
        self.assertIsNotNone(m, "console_web.py 应含内嵌 <script>")
        js = m.group(1)
        self.assertIn("showTaskDetail", js)
        self.assertIn("会话记忆", js)
        self.assertIn("结构化错误", js)
        self.assertIn("AI 自我反思建议", js)

        d = tempfile.mkdtemp(prefix="sectrust-tdjs-")
        try:
            js_path = os.path.join(d, "_td_check.js")
            cjs_path = os.path.join(d, "_td_dom_stub.cjs")
            with open(js_path, "w", encoding="utf-8") as fh:
                fh.write(js)
            with open(cjs_path, "w", encoding="utf-8") as fh:
                fh.write(_DOM_STUB_TASKDETAIL_CJS)
            chk = subprocess.run(["node", "--check", js_path],
                                 capture_output=True, text=True, timeout=30)
            self.assertEqual(chk.returncode, 0,
                             "node --check 失败：%s" % chk.stderr[:500])
            run = subprocess.run(["node", cjs_path, js_path],
                                 capture_output=True, text=True, timeout=30)
            self.assertEqual(run.returncode, 0,
                             "任务详情 DOM stub 渲染失败：%s\n%s"
                             % (run.stdout[-800:], run.stderr[-800:]))
            self.assertIn("OK: 任务详情页渲染通过", run.stdout)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    @unittest.skipUnless(shutil.which("node"), "node 不可用，跳过前端渲染验证")
    def test_task_list_renders_cache_and_error(self):
        """验证 loadTasks 结果列附加 🧠记忆 / ⛔错误 / 💡反思 徽章。"""
        src_path = os.path.join(ROOT, "console_web.py")
        with open(src_path, "r", encoding="utf-8") as fh:
            src = fh.read()
        m = re.search(r"<script>(.*?)</script>", src, re.S)
        self.assertIsNotNone(m, "console_web.py 应含内嵌 <script>")
        js = m.group(1)
        self.assertIn("loadTasks", js)
        self.assertIn("🧠 记忆", js)
        self.assertIn("⛔", js)
        self.assertIn("💡 反思", js)

        d = tempfile.mkdtemp(prefix="sectrust-tljs-")
        try:
            js_path = os.path.join(d, "_tl_check.js")
            cjs_path = os.path.join(d, "_tl_dom_stub.cjs")
            with open(js_path, "w", encoding="utf-8") as fh:
                fh.write(js)
            with open(cjs_path, "w", encoding="utf-8") as fh:
                fh.write(_DOM_STUB_TASKLIST_CJS)
            chk = subprocess.run(["node", "--check", js_path],
                                 capture_output=True, text=True, timeout=30)
            self.assertEqual(chk.returncode, 0,
                             "node --check 失败：%s" % chk.stderr[:500])
            run = subprocess.run(["node", cjs_path, js_path],
                                 capture_output=True, text=True, timeout=30)
            self.assertEqual(run.returncode, 0,
                             "任务列表 DOM stub 渲染失败：%s\n%s"
                             % (run.stdout[-800:], run.stderr[-800:]))
            self.assertIn("OK: 任务列表渲染通过", run.stdout)
        finally:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
