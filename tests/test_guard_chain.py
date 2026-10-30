"""B3 · EvidenceGate + 审批闸门挂到 Skill / MCP 调用链回归测试。

验证"默认不信任 AI"真的落地成拦截，而不是文档口号：
  · MCP 侧：写类工具必须带结构化证据，缺证据/坏证据/缺工件一律拒绝；
            挂到 SecureProxy 后 call_tool 真的返回 stage="evidence"；
  · Skill 侧：审批 → 沙箱实跑 → 实跑输出作证据 → 工件齐备 + 质量门，
            任一不过绝不 ok=True。
"""

import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from approval import ApprovalCenter  # noqa: E402
from trace import TraceStore, new_trace_id  # noqa: E402
from agentskills.evidence import EvidenceGate, Verdict  # noqa: E402
from agentskills.quality import QualityGate  # noqa: E402
from agentskills.skill_md import StandardSkillLibrary  # noqa: E402
from agentskills.guard_chain import SkillExecutionGuard, _declared_artifacts  # noqa: E402
from mcp_adapter.guard_chain import (  # noqa: E402
    MCPEvidenceAdapter, attach_evidence_gate,
)
from mcp_adapter.protocol import INITIALIZE, TOOLS_CALL  # noqa: E402
from mcp_adapter.proxy import SecureProxy  # noqa: E402
from mcp_adapter.registry import MCPServerRegistry, ServerSpec  # noqa: E402
from mcp_adapter.breaker import TTLCircuitBreaker  # noqa: E402
from sandbox import LocalSandbox  # noqa: E402

ARTIFACTS = {
    "竞品功能对比表": "| 维度 | A 产品 | B 产品 |\n|---|---|---|\n| 价格 | 见来源1 | 见来源2 |",
    "来源链接清单": "https://example.com/a （抓取时间 2026-10-30）；https://example.com/b",
    "成本估算": "未估算：无计费器接入，如实标注，不编造数字。",
}


def _evidence(**kw):
    base = {"command": "python -m pytest -q", "exit_code": 0,
            "stdout": "3 passed in 0.4s", "claim": "3 项测试通过",
            "ran_at": time.time(), "task_id": "mcp:test"}
    base.update(kw)
    return base


class FakeTransport:
    def __init__(self, responses):
        self.responses = dict(responses or {})
        self.via = "fake(offline)"
        self.calls = []

    def request(self, method, params=None, timeout=None):
        self.calls.append((method, params or {}))
        if method == INITIALIZE:
            return {"protocolVersion": "2025-06-18", "capabilities": {},
                    "serverInfo": {"name": "fake", "version": "0"}}
        return self.responses.get(method, {})

    def notify(self, method, params=None):
        self.calls.append((method, params or {}))

    def close(self):
        pass


# ---------------------------------------------------------------- MCP 侧
class TestMCPEvidenceAdapter(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.gate = EvidenceGate(db_path=os.path.join(self.d, "ev.db"))

    def _adapter(self, **kw):
        return MCPEvidenceAdapter(gate=self.gate, **kw)

    def _write_result(self, structured=None):
        return {"content": [{"type": "text", "text": "created"}],
                "isError": False,
                "structuredContent": structured or {}}

    def test_read_only_tool_needs_no_evidence(self):
        r = self._adapter().verify_mcp_result(
            "s", "search_repositories", self._write_result())
        self.assertTrue(r["accepted"])
        self.assertFalse(r["checked"])
        self.assertIn("只读", r["note"])

    def test_write_without_evidence_rejected(self):
        r = self._adapter().verify_mcp_result("s", "create_issue",
                                              self._write_result())
        self.assertFalse(r["accepted"])
        self.assertTrue(r["checked"])
        self.assertTrue(any("缺少" in x for x in r["reasons"]))

    def test_write_with_valid_evidence_accepted_and_recorded(self):
        r = self._adapter().verify_mcp_result(
            "s", "create_issue",
            self._write_result({"evidence": _evidence(),
                                "artifacts": ["issue-12"]}))
        self.assertTrue(r["accepted"], r["reasons"])
        self.assertTrue(r["checks"]["accepted"])
        self.assertEqual(r["artifacts"], ["issue-12"])
        # evidence 显式给了 task_id="mcp:test"，审计落在该 id 下（可被回放）
        self.assertTrue(self.gate.history("mcp:test"))

    def test_bad_evidence_variants_rejected(self):
        bad_cases = [
            {"exit_code": 1, "stdout": "FAILED"},
            {"stdout": ""},
            {"ran_at": time.time() - 99999},
            {"claim": "应该能通过"},
            {"command": "全部搞定了"},
            {"expect": r"Ran 999", "stdout": "3 passed"},
            {"forbid": r"FAILED", "stdout": "OK\nFAILED 1"},
        ]
        for patch in bad_cases:
            with self.subTest(patch=patch):
                r = self._adapter().verify_mcp_result(
                    "s", "create_issue",
                    self._write_result({"evidence": _evidence(**patch)}))
                self.assertFalse(r["accepted"], patch)
                self.assertTrue(r["reasons"])

    def test_malformed_evidence_rejected(self):
        r = self._adapter().verify_mcp_result(
            "s", "create_issue",
            self._write_result({"evidence": {"exit_code": "not-an-int"}}))
        self.assertFalse(r["accepted"])
        self.assertTrue(any("非法" in x for x in r["reasons"]))

    def test_required_artifacts_missing_rejected(self):
        a = self._adapter(required_artifacts=["竞品功能对比表", "来源链接清单"])
        r = a.verify_mcp_result("s", "create_issue",
                                self._write_result({"evidence": _evidence()}))
        self.assertFalse(r["accepted"])
        self.assertTrue(any("缺少必检工件" in x for x in r["reasons"]))

    def test_quality_gate_rejects_forbidden_content(self):
        a = self._adapter(quality_gate=QualityGate(),
                          quality_rules={"forbidden": ["TODO"]})
        r = a.verify_mcp_result(
            "s", "create_issue",
            self._write_result({"evidence": _evidence(),
                                "artifacts": ["TODO 待补"]}))
        self.assertFalse(r["accepted"])
        self.assertFalse(r["quality"]["accepted"])

    def test_explicit_required_tools_override(self):
        a = self._adapter(required_tools={"search_repositories"})
        r = a.verify_mcp_result("s", "search_repositories",
                                self._write_result())
        self.assertTrue(r["checked"])
        self.assertFalse(r["accepted"])   # 显式要求 → 也要证据

    def test_default_task_id_when_not_supplied(self):
        ev = _evidence()
        ev.pop("task_id")
        r = self._adapter().verify_mcp_result(
            "s", "create_issue", self._write_result({"evidence": ev}))
        self.assertTrue(r["accepted"], r["reasons"])
        self.assertTrue(self.gate.history("mcp:s:create_issue"))


class TestMCPEvidenceAttachedToProxy(unittest.TestCase):
    """证据门真的挂在 SecureProxy.call_tool 链上，不是只做单元。"""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.reg = MCPServerRegistry(path=os.path.join(self.d, "servers.json"))
        self.reg.register(ServerSpec(
            name="github-stdio",
            transport={"type": "stdio", "command": [sys.executable, "-m",
                       "mcp_adapter.servers.github"], "cwd": ROOT},
            allowed_domains=["api.github.com", "github.com"],
            requires_approval_tools=["create_issue"],
            credential_ref="GITHUB_TOKEN", risk_level="medium"))
        self.breaker = TTLCircuitBreaker(
            path=os.path.join(self.d, "breaker.json"))
        self.approvals = ApprovalCenter(
            db_path=os.path.join(self.d, "ap.db"), tracer=False)
        self.addCleanup(self.approvals.close)
        self.made = []

    def _proxy(self, tool_result):
        def factory(server, extra_env=None, headers=None, timeout=15.0):
            t = FakeTransport({TOOLS_CALL: tool_result})
            self.made.append(t)
            return t
        return SecureProxy(self.reg, breaker=self.breaker,
                           approvals=self.approvals,
                           transport_factory=factory)

    def _approve_write(self, proxy):
        r = proxy.call_tool("github-stdio", "create_issue",
                            {"owner": "o", "repo": "r", "title": "t"})
        self.assertEqual(r.stage, "approval")
        self.assertTrue(self.approvals.decide(r.approval_id, "approve"))
        return r.approval_id

    def test_write_without_evidence_blocked_at_evidence_stage(self):
        proxy = self._proxy({"content": [{"type": "text", "text": "created"}],
                             "isError": False, "structuredContent": {}})
        adapter = attach_evidence_gate(
            proxy, gate=EvidenceGate(db_path=os.path.join(self.d, "ev.db")))
        aid = self._approve_write(proxy)
        r = proxy.call_tool("github-stdio", "create_issue",
                            {"owner": "o", "repo": "r", "title": "t"},
                            approval_id=aid)
        self.assertFalse(r.ok)
        self.assertEqual(r.stage, "evidence")
        self.assertIsNotNone(r.evidence)
        self.assertFalse(r.evidence["accepted"])
        self.assertTrue(adapter.needs_evidence("create_issue"))

    def test_write_with_valid_evidence_passes(self):
        proxy = self._proxy({
            "content": [{"type": "text", "text": "created issue"}],
            "isError": False,
            "structuredContent": {"number": 12, "evidence": _evidence()}})
        attach_evidence_gate(
            proxy, gate=EvidenceGate(db_path=os.path.join(self.d, "ev.db")))
        aid = self._approve_write(proxy)
        r = proxy.call_tool("github-stdio", "create_issue",
                            {"owner": "o", "repo": "r", "title": "t"},
                            approval_id=aid)
        self.assertTrue(r.ok, r.reason)
        self.assertEqual(r.stage, "ok")
        self.assertTrue(r.evidence["accepted"])
        self.assertEqual(r.data["structuredContent"]["number"], 12)

    def test_read_tool_not_blocked_by_evidence_gate(self):
        proxy = self._proxy({"content": [{"type": "text", "text": "ok"}],
                             "isError": False, "structuredContent": {}})
        attach_evidence_gate(proxy)
        r = proxy.call_tool("github-stdio", "search_repositories",
                            {"query": "mcp"})
        self.assertTrue(r.ok, r.reason)
        self.assertFalse(r.evidence["checked"])


# ---------------------------------------------------------------- Skill 侧
class TestSkillExecutionGuard(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.lib = StandardSkillLibrary()
        self.gate = EvidenceGate(db_path=os.path.join(self.d, "ev.db"))
        self.approvals = ApprovalCenter(
            db_path=os.path.join(self.d, "ap.db"), tracer=False)
        self.addCleanup(self.approvals.close)
        self.guard = SkillExecutionGuard(
            library=self.lib, evidence_gate=self.gate,
            quality_gate=QualityGate(), approvals=self.approvals)

    def _run(self, **kw):
        kw.setdefault("sandbox", LocalSandbox())
        return self.guard.execute("competitor-analysis",
                                  "scripts/collect_outline.py",
                                  args=["demo"], **kw)

    def test_declared_artifacts_parsing(self):
        self.assertEqual(_declared_artifacts('{"required_artifacts": ["a","b"]}'),
                         ["a", "b"])
        self.assertEqual(_declared_artifacts("not json"), [])

    def test_happy_path_ok(self):
        r = self._run(artifacts=dict(ARTIFACTS))
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["stage"], "ok")
        self.assertTrue(r["sandboxed"])
        self.assertEqual(r["containment"], "advisory")
        self.assertTrue(r["evidence"]["accepted"])
        self.assertTrue(r["quality"]["accepted"])
        self.assertEqual(len(r["declared_artifacts"]), 3)
        self.assertTrue(all(r["artifacts_checked"].values()))
        self.assertTrue(self.gate.history("skill:competitor-analysis"))

    def test_missing_artifacts_blocked(self):
        r = self._run(artifacts={"竞品功能对比表": "有内容"})
        self.assertFalse(r["ok"])
        self.assertEqual(r["stage"], "artifacts")
        self.assertIn("缺少必检工件", r["reason"])

    def test_empty_artifact_blocked(self):
        arts = dict(ARTIFACTS)
        arts["成本估算"] = "   "
        r = self._run(artifacts=arts)
        self.assertFalse(r["ok"])
        self.assertEqual(r["stage"], "artifacts")

    def test_todo_in_artifact_blocked_by_quality_gate(self):
        arts = dict(ARTIFACTS)
        arts["竞品功能对比表"] = "TODO 待补充完整对比表"
        r = self._run(artifacts=arts)
        self.assertFalse(r["ok"])
        self.assertEqual(r["stage"], "quality")
        self.assertFalse(r["quality"]["accepted"])

    def test_unfilled_slot_blocked(self):
        arts = dict(ARTIFACTS)
        arts["来源链接清单"] = "来源 <待补:url> 未填"
        r = self._run(artifacts=arts)
        self.assertFalse(r["ok"])
        self.assertEqual(r["stage"], "quality")

    def test_approval_gate_then_allow(self):
        with mock.patch.object(self.guard, "_needs_approval", return_value=True):
            r = self._run(artifacts=dict(ARTIFACTS))
            self.assertFalse(r["ok"])
            self.assertEqual(r["stage"], "approval")
            self.assertTrue(r["approval_id"])
            aid = r["approval_id"]
            self.assertTrue(self.approvals.decide(aid, "approve"))
            r2 = self._run(artifacts=dict(ARTIFACTS), approval_id=aid)
        self.assertTrue(r2["ok"], r2)

    def test_approval_gate_fail_closed_without_center(self):
        g = SkillExecutionGuard(library=self.lib, evidence_gate=self.gate,
                                quality_gate=QualityGate(), approvals=None)
        with mock.patch.object(g, "_needs_approval", return_value=True):
            r = g.execute("competitor-analysis", "scripts/collect_outline.py",
                          args=["demo"], sandbox=LocalSandbox())
        self.assertFalse(r["ok"])
        self.assertEqual(r["stage"], "approval")
        self.assertIn("fail-closed", r["reason"])

    def test_unapproved_id_rejected(self):
        with mock.patch.object(self.guard, "_needs_approval", return_value=True):
            r = self._run(artifacts=dict(ARTIFACTS),
                          approval_id="ap-not-approved")
        self.assertFalse(r["ok"])
        self.assertEqual(r["stage"], "approval")

    def test_unknown_skill_blocked(self):
        r = self.guard.execute("no-such-skill", "scripts/x.py")
        self.assertFalse(r["ok"])
        self.assertEqual(r["stage"], "registry")

    def test_install_validation_failure_blocked(self):
        with mock.patch.object(self.lib, "install_validate",
                               return_value={"ok": False,
                                             "problems": ["缺少 manifest"]}):
            r = self._run(artifacts=dict(ARTIFACTS))
        self.assertFalse(r["ok"])
        self.assertEqual(r["stage"], "install")

    def test_sandbox_failure_blocked(self):
        with mock.patch.object(self.lib, "run_script",
                               return_value={"ok": False, "error": "exit=1",
                                             "sandboxed": True, "backend": "local",
                                             "containment": "advisory",
                                             "stdout": ""}):
            r = self._run(artifacts=dict(ARTIFACTS))
        self.assertFalse(r["ok"])
        self.assertEqual(r["stage"], "sandbox")

    def test_evidence_gate_rejection_blocked(self):
        with mock.patch.object(self.gate, "verify",
                               return_value=Verdict(accepted=False,
                                                    reasons=["证据过期"])):
            r = self._run(artifacts=dict(ARTIFACTS))
        self.assertFalse(r["ok"])
        self.assertEqual(r["stage"], "evidence")
        self.assertIn("证据过期", r["reason"])

    def test_script_path_traversal_still_blocked(self):
        r = self.guard.execute("competitor-analysis", "../evil.py",
                               sandbox=LocalSandbox())
        self.assertFalse(r["ok"])
        self.assertEqual(r["stage"], "sandbox")


# ---------------------------------------------------------------- 审计轨迹
class TestAuditTrail(unittest.TestCase):
    """工单第七节：每次 Skill / MCP 调用都写审计轨迹（含被拒调用）。"""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.trace = TraceStore(db_path=os.path.join(self.d, "trace.db"))
        self.tid = new_trace_id()
        self.trace.start_trace(title="B2/B3 审计验证", trace_id=self.tid)
        self.addCleanup(self.trace.close)

    def _mcp_proxy(self, **kw):
        reg = MCPServerRegistry(path=os.path.join(self.d, "servers.json"))
        reg.register(ServerSpec(
            name="github-stdio",
            transport={"type": "stdio", "command": [sys.executable, "-m",
                       "mcp_adapter.servers.github"], "cwd": ROOT},
            allowed_domains=["api.github.com", "github.com"],
            requires_approval_tools=["create_issue"],
            credential_ref="GITHUB_TOKEN", risk_level="medium"))
        breaker = TTLCircuitBreaker(path=os.path.join(self.d, "breaker.json"))
        approvals = ApprovalCenter(db_path=os.path.join(self.d, "ap.db"),
                                   tracer=False)
        self.addCleanup(approvals.close)

        def factory(server, extra_env=None, headers=None, timeout=15.0):
            return FakeTransport({TOOLS_CALL: {
                "content": [{"type": "text", "text": "ok"}],
                "isError": False, "structuredContent": {}}})
        return SecureProxy(reg, breaker=breaker, approvals=approvals,
                           transport_factory=factory, **kw)

    def test_mcp_calls_written_to_trace_including_rejections(self):
        p = self._mcp_proxy(trace_store=self.trace, trace_id=self.tid)
        ok = p.call_tool("github-stdio", "search_repositories",
                         {"query": "mcp"})
        self.assertTrue(ok.ok, ok.reason)
        self.assertTrue(ok.audit["trace_written"])
        self.assertEqual(len(ok.audit["params_digest"]), 16)
        self.assertIsNone(ok.audit["cost_estimate"])
        self.assertIn("未估算", ok.audit["cost_note"])

        # 被审批门拒绝的调用同样留痕（试图越权本身要可追责）
        bad = p.call_tool("github-stdio", "create_issue",
                          {"owner": "o", "repo": "r", "title": "t"})
        self.assertFalse(bad.ok)
        self.assertTrue(bad.audit["trace_written"])

        events = [e for e in self.trace.events(self.tid)
                  if e["kind"] == "mcp_call"]
        self.assertEqual(len(events), 2)
        self.assertTrue(all(e["detail"].get("params_digest") for e in events))

    def test_no_trace_context_is_honest_not_faked(self):
        p = self._mcp_proxy()
        r = p.call_tool("github-stdio", "search_repositories",
                        {"query": "mcp"})
        self.assertTrue(r.ok)
        self.assertFalse(r.audit["trace_written"])
        self.assertIn("未硬造孤儿轨迹", r.audit["trace_note"])

    def test_skill_runs_written_to_trace_including_rejections(self):
        gate = EvidenceGate(db_path=os.path.join(self.d, "ev.db"))
        guard = SkillExecutionGuard(
            library=StandardSkillLibrary(), evidence_gate=gate,
            quality_gate=QualityGate(), trace_store=self.trace,
            trace_id=self.tid)
        ok = guard.execute("competitor-analysis", "scripts/collect_outline.py",
                           args=["demo"], artifacts=dict(ARTIFACTS),
                           sandbox=LocalSandbox())
        self.assertTrue(ok["ok"], ok)
        self.assertTrue(ok["audit"]["trace_written"])
        self.assertIsNone(ok["audit"]["cost_estimate"])

        bad = guard.execute("competitor-analysis", "scripts/collect_outline.py",
                            args=["demo"], artifacts={}, sandbox=LocalSandbox())
        self.assertFalse(bad["ok"])
        self.assertTrue(bad["audit"]["trace_written"])

        events = [e for e in self.trace.events(self.tid)
                  if e["kind"] == "skill_run"]
        self.assertEqual(len(events), 2)
        self.assertTrue(all(isinstance(e["detail"], dict) for e in events))


if __name__ == "__main__":
    unittest.main(verbosity=2)
