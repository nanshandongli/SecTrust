"""B2 · MCP 适配器 + GitHub 样板三原语全链路回归测试。

全部离线：stdio 走本地子进程、HTTP 走 127.0.0.1 loopback fixture，不触网、
不需要 GITHUB_TOKEN。覆盖用户 P0 第二件事的每项硬要求：
  · Tools / Resources / Prompts 三原语全链路（两种传输各跑一遍）；
  · stdio / HTTP 双传输 + MCPServerRegistry（登记 ≠ 在线，probe 真握手）；
  · 安全代理六道门：审批 / 域名白名单 / 凭证代理注入 / 密钥扫描 / TTL 熔断；
  · 无 token 明确不可用不造假；fixture 数据明确标注，不冒充真跨网。
"""

import hashlib
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from approval import ApprovalCenter  # noqa: E402
from mcp_adapter.protocol import (  # noqa: E402
    CallToolResult, Prompt, PromptArgument, Resource, RpcError, Tool,
    make_error, make_notification, make_request, make_result,
    negotiated_version, parse_message, result_of, text_content,
    INITIALIZE, NOTIFY_INITIALIZED, TOOLS_CALL,
)
from mcp_adapter.transport import (  # noqa: E402
    HttpTransport, StdioTransport, TransportError,
)
from mcp_adapter.registry import (  # noqa: E402
    MCPServerRegistry, RegistryError, ServerSpec,
)
from mcp_adapter.breaker import TTLCircuitBreaker  # noqa: E402
from mcp_adapter.proxy import (  # noqa: E402
    CredentialVault, SecureProxy, scan_secrets,
)
from mcp_adapter.servers import github  # noqa: E402


@contextmanager
def fixture_mode():
    """离线 fixture 模式：无真实 GITHUB_TOKEN，显式开 fixture 开关。"""
    with mock.patch.dict(os.environ, {
            "SECTRUST_MCP_GITHUB_FIXTURE": "1", "GITHUB_TOKEN": ""}):
        yield


@contextmanager
def unavailable_mode():
    """无 token 且未开 fixture：必须明确不可用，一条假数据都不给。"""
    with mock.patch.dict(os.environ, {
            "SECTRUST_MCP_GITHUB_FIXTURE": "", "GITHUB_TOKEN": ""}):
        yield


def stdio_spec(name="github-stdio", **overrides):
    base = dict(
        name=name,
        transport={"type": "stdio",
                   "command": [sys.executable, "-m",
                               "mcp_adapter.servers.github"],
                   "cwd": ROOT},
        description="GitHub MCP 三原语样板（离线 stdio fixture）",
        allowed_domains=["api.github.com", "github.com"],
        requires_approval_tools=["create_issue"],
        credential_ref="GITHUB_TOKEN",
        risk_level="medium",
    )
    base.update(overrides)
    return ServerSpec(**base)


class FakeTransport:
    """离线假传输：记录调用与凭证注入点，可返回指定响应。"""

    def __init__(self, responses=None):
        self.responses = dict(responses or {})
        self.calls = []
        self.via = "fake(offline)"
        self.extra_env = {}
        self.headers = {}
        self.closed = False

    def request(self, method, params=None, timeout=None):
        self.calls.append((method, params or {}))
        if method == INITIALIZE:
            return {"protocolVersion": "2025-06-18", "capabilities": {},
                    "serverInfo": {"name": "fake", "version": "0"}}
        return self.responses.get(method, {})

    def notify(self, method, params=None):
        self.calls.append((method, params or {}))

    def close(self):
        self.closed = True


def make_factory(made, responses=None):
    def factory(server, extra_env=None, headers=None, timeout=15.0):
        t = FakeTransport(responses=responses)
        t.extra_env = dict(extra_env or {})
        t.headers = dict(headers or {})
        made.append(t)
        return t
    return factory


# ---------------------------------------------------------------- 协议帧
class TestProtocol(unittest.TestCase):
    def test_request_result_error_frames(self):
        req = make_request("tools/list", {"cursor": "a"}, rid=7)
        self.assertEqual(req["jsonrpc"], "2.0")
        self.assertEqual(req["id"], 7)
        self.assertEqual(parse_message(json.dumps(make_result(7, {"tools": []})))
                         ["result"], {"tools": []})
        with self.assertRaises(RpcError) as cm:
            result_of(make_error(7, -32601, "method not found"))
        self.assertEqual(cm.exception.code, -32601)

    def test_notification_has_no_id(self):
        self.assertNotIn("id", make_notification(NOTIFY_INITIALIZED))

    def test_parse_message_rejects_garbage(self):
        for raw in ("not json", '{"jsonrpc":"1.0"}', "[1,2,3]"):
            with self.subTest(raw=raw), self.assertRaises(RpcError):
                parse_message(raw)

    def test_negotiated_version(self):
        self.assertEqual(negotiated_version("2024-11-05"), "2024-11-05")
        self.assertEqual(negotiated_version("bogus"), "2025-06-18")

    def test_primitives_to_dict(self):
        self.assertEqual(Tool("t", "d").to_dict()["name"], "t")
        self.assertEqual(Resource("r://x").to_dict()["uri"], "r://x")
        p = Prompt("p", "d", [PromptArgument("a", required=True)])
        self.assertTrue(p.to_dict()["arguments"][0]["required"])

    def test_call_tool_result_fixture_marker(self):
        r = CallToolResult(content=text_content("x"), fixture=True,
                           note="离线 fixture")
        d = r.to_dict()
        self.assertTrue(d["_fixture"])
        self.assertIn("fixture", d["_note"])
        self.assertEqual(r.text, "x")


# ---------------------------------------------------------------- 样板 handle（纯函数）
class TestGithubServerHandle(unittest.TestCase):
    def _handle(self, method, params=None, rid=1):
        return github.handle({"jsonrpc": "2.0", "id": rid,
                              "method": method, "params": params or {}})

    def test_initialize_and_three_primitive_lists(self):
        init = self._handle(INITIALIZE, {"protocolVersion": "2025-06-18"})
        self.assertIn("capabilities", init["result"])
        self.assertEqual(init["result"]["serverInfo"]["name"],
                         "sectrust-github-mcp-sample")
        self.assertEqual(len(self._handle("tools/list")["result"]["tools"]), 4)
        self.assertEqual(len(self._handle("resources/list")["result"]["resources"]), 2)
        self.assertEqual(len(self._handle("prompts/list")["result"]["prompts"]), 1)
        self.assertEqual(self._handle("ping")["result"], {})

    def test_tools_call_fixture_marked(self):
        with fixture_mode():
            r = github.call_tool("search_repositories", {"query": "mcp", "limit": 2})
        self.assertFalse(r.is_error)
        self.assertTrue(r.fixture)
        self.assertEqual(r.structured["mode"], "fixture")
        self.assertEqual(len(r.structured["items"]), 2)
        self.assertIn("fixture", r.structured["note"])

    def test_resources_read_and_prompts_get(self):
        r = self._handle("resources/read", {"uri": "repos://sample/readme"})
        self.assertTrue(r["result"]["_fixture"])
        self.assertIn("README", r["result"]["contents"][0]["text"])
        p = self._handle("prompts/get", {
            "name": "repo_summary",
            "arguments": {"owner": "o", "repo": "r", "focus": "性能"}})
        self.assertIn("来源", p["result"]["messages"][0]["content"]["text"])

    def test_unavailable_mode_never_fakes_data(self):
        with unavailable_mode():
            for tool, args in (("search_repositories", {"query": "x"}),
                               ("get_file_contents", {"owner": "o", "repo": "r",
                                                      "path": "README.md"}),
                               ("list_issues", {"owner": "o", "repo": "r"}),
                               ("create_issue", {"owner": "o", "repo": "r",
                                                 "title": "t"})):
                with self.subTest(tool=tool):
                    r = github.call_tool(tool, args)
                    self.assertTrue(r.is_error)
                    self.assertIn("credential_unavailable", r.text)
                    self.assertNotIn("items", r.structured or {})

    def test_fixture_write_is_refused_not_faked(self):
        with fixture_mode():
            r = github.call_tool("create_issue",
                                 {"owner": "o", "repo": "r", "title": "t"})
        self.assertTrue(r.is_error)
        self.assertEqual(r.structured["error"], "write_not_supported_in_fixture")
        self.assertIn("不返回伪造", r.text)

    def test_unknown_method_and_notification(self):
        err = self._handle("no/such/method")
        self.assertEqual(err["error"]["code"], -32601)
        self.assertIsNone(self._handle(NOTIFY_INITIALIZED))


# ---------------------------------------------------------------- 双传输端到端
class TestStdioTransportE2E(unittest.TestCase):
    def test_full_three_primitives_over_subprocess(self):
        with fixture_mode():
            tr = StdioTransport(
                [sys.executable, "-m", "mcp_adapter.servers.github"], cwd=ROOT,
                timeout=15.0)
            try:
                init = tr.request(INITIALIZE, {"protocolVersion": "2025-06-18"})
                tr.notify(NOTIFY_INITIALIZED)
                tools = tr.request("tools/list")
                called = tr.request(TOOLS_CALL, {
                    "name": "search_repositories",
                    "arguments": {"query": "mcp", "limit": 2}})
                resources = tr.request("resources/list")
                read = tr.request("resources/read",
                                  {"uri": "repos://sample/readme"})
                prompts = tr.request("prompts/list")
                got = tr.request("prompts/get", {
                    "name": "repo_summary",
                    "arguments": {"owner": "o", "repo": "r"}})
            finally:
                tr.close()
        self.assertEqual(init["serverInfo"]["name"],
                         "sectrust-github-mcp-sample")
        self.assertEqual(len(tools["tools"]), 4)
        self.assertTrue(called.get("_fixture"), "fixture 数据必须明确标注")
        self.assertEqual(len(resources["resources"]), 2)
        self.assertIn("README", read["contents"][0]["text"])
        self.assertEqual(len(prompts["prompts"]), 1)
        self.assertIn("来源", got["messages"][0]["content"]["text"])
        self.assertEqual(tr.via, "stdio+json(loopback:subprocess)")

    def test_bad_command_raises_transport_error(self):
        tr = StdioTransport([os.path.join(ROOT, "no-such-interpreter-xyz")])
        with self.assertRaises(TransportError):
            tr.request("ping")


class TestHttpTransportE2E(unittest.TestCase):
    def test_full_three_primitives_over_loopback_http(self):
        srv = ThreadingHTTPServer(("127.0.0.1", 0), github.make_http_handler())
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        # addCleanup 是 LIFO：先注册 server_close，后注册 shutdown，
        # 实际执行顺序才是 shutdown() → server_close()（否则 selector 拿到
        # 已关闭的 socket，在 Windows 上抛 WinError 10038 线程告警）。
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        url = "http://127.0.0.1:%d/mcp" % port
        with fixture_mode():
            tr = HttpTransport(url, timeout=10.0)
            try:
                init = tr.request(INITIALIZE, {"protocolVersion": "2025-06-18"})
                tr.notify(NOTIFY_INITIALIZED)
                tools = tr.request("tools/list")
                called = tr.request(TOOLS_CALL, {
                    "name": "search_repositories",
                    "arguments": {"query": "mcp", "limit": 1}})
                read = tr.request("resources/read",
                                  {"uri": "repos://sample/metadata"})
                prompts = tr.request("prompts/list")
            finally:
                tr.close()
        self.assertEqual(init["serverInfo"]["name"],
                         "sectrust-github-mcp-sample")
        self.assertEqual(len(tools["tools"]), 4)
        self.assertTrue(called.get("_fixture"))
        self.assertEqual(json.loads(read["contents"][0]["text"])["name"],
                         "sectrust-github-mcp-sample")
        self.assertEqual(len(prompts["prompts"]), 1)
        self.assertEqual(tr.via, "http+json(loopback)")


# ---------------------------------------------------------------- Registry
class TestRegistry(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.path = os.path.join(self.d, "servers.json")

    def test_register_validate_get_list_unregister(self):
        reg = MCPServerRegistry(path=self.path)
        ok, err = reg.register(stdio_spec())
        self.assertTrue(ok, err)
        self.assertEqual(len(reg.list()), 1)
        self.assertEqual(reg.get("github-stdio").risk_level, "medium")
        self.assertTrue(reg.unregister("github-stdio"))
        self.assertFalse(reg.unregister("github-stdio"))

    def test_invalid_specs_rejected(self):
        reg = MCPServerRegistry(path=self.path)
        for bad in (stdio_spec(name="bad name!"),
                    ServerSpec(name="x", transport={"type": "ftp", "url": "x"}),
                    ServerSpec(name="x", transport={"type": "stdio", "command": []}),
                    ServerSpec(name="x", transport={"type": "http", "url": "ftp://x"}),
                    ServerSpec(name="x", transport={"type": "stdio",
                                                   "command": ["python"]},
                               credential_ref="bad-ref")):
            with self.subTest(bad=bad.name):
                ok, err = reg.register(bad)
                self.assertFalse(ok)
                self.assertTrue(err)

    def test_disabled_server_cannot_connect(self):
        reg = MCPServerRegistry(path=self.path)
        reg.register(stdio_spec())
        reg.set_enabled("github-stdio", False)
        with self.assertRaises(RegistryError):
            reg.build_transport("github-stdio")

    def test_probe_real_handshake(self):
        reg = MCPServerRegistry(path=self.path)
        reg.register(stdio_spec())
        with fixture_mode():
            r = reg.probe("github-stdio", timeout=15.0)
        self.assertTrue(r["healthy"], r)
        self.assertEqual(r["protocol_version"], "2025-06-18")
        self.assertEqual(r["via"], "stdio+json(loopback:subprocess)")

    def test_probe_offline_server_reports_error(self):
        reg = MCPServerRegistry(path=self.path)
        reg.register(stdio_spec(transport={"type": "http",
                                           "url": "http://127.0.0.1:1/mcp"}))
        r = reg.probe("github-stdio", timeout=2.0)
        self.assertFalse(r["healthy"])
        self.assertTrue(r["error"])

    def test_corrupt_file_backed_up_not_overwritten(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{not json")
        reg = MCPServerRegistry(path=self.path)
        with self.assertRaises(RegistryError):
            reg.list()
        self.assertTrue(os.path.exists(self.path + ".corrupt"))


# ---------------------------------------------------------------- TTL 熔断
class TestBreaker(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.path = os.path.join(self.d, "breaker.json")

    def test_open_half_open_reset_cycle(self):
        b = TTLCircuitBreaker(path=self.path, threshold=2, ttl=0.25)
        ok, why, _ = b.allow("s1")
        self.assertTrue(ok, why)
        b.record_failure("s1", "boom")
        ok, why, _ = b.allow("s1")
        self.assertTrue(ok)
        b.record_failure("s1", "boom")
        ok, why, st = b.allow("s1")
        self.assertFalse(ok)
        self.assertTrue(st["open"])
        self.assertIn("TTL 熔断", why)
        time.sleep(0.32)
        ok, why, st = b.allow("s1")
        self.assertTrue(ok, why)
        self.assertEqual(why, "half_open")
        b.record_success("s1")
        self.assertEqual(b.state("s1")["failures"], 0)

    def test_corrupt_file_raises_and_backs_up(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("oops")
        with self.assertRaises(RuntimeError):
            TTLCircuitBreaker(path=self.path).state("s")
        self.assertTrue(os.path.exists(self.path + ".corrupt"))


# ---------------------------------------------------------------- 安全代理六道门
class TestSecureProxy(unittest.TestCase):
    def setUp(self):
        self._patcher = mock.patch.dict(os.environ, {
            "SECTRUST_MCP_GITHUB_FIXTURE": "1", "GITHUB_TOKEN": ""})
        self._patcher.start()
        self.addCleanup(self._patcher.stop)
        self.d = tempfile.mkdtemp()
        self.reg = MCPServerRegistry(path=os.path.join(self.d, "servers.json"))
        self.reg.register(stdio_spec())
        self.breaker = TTLCircuitBreaker(
            path=os.path.join(self.d, "breaker.json"), threshold=3, ttl=30)
        self.approvals = ApprovalCenter(
            db_path=os.path.join(self.d, "ap.db"), tracer=False)
        self.addCleanup(self.approvals.close)
        self.proxy = SecureProxy(self.reg, breaker=self.breaker,
                                 approvals=self.approvals)

    def test_real_stdio_call_tool_and_reads(self):
        r = self.proxy.call_tool("github-stdio", "search_repositories",
                                 {"query": "mcp", "limit": 2})
        self.assertTrue(r.ok, r.reason)
        self.assertTrue(r.fixture)
        self.assertEqual(r.via, "stdio+json(loopback:subprocess)")
        self.assertIn("fixture", r.text)
        self.assertTrue(self.proxy.list_tools("github-stdio").ok)
        rr = self.proxy.read_resource("github-stdio", "repos://sample/readme")
        self.assertTrue(rr.ok, rr.reason)
        pp = self.proxy.get_prompt("github-stdio", "repo_summary",
                                   {"owner": "o", "repo": "r"})
        self.assertTrue(pp.ok, pp.reason)
        self.assertIn("来源", pp.text)

    def test_approval_gate_blocks_write_then_allows_after_approve(self):
        r = self.proxy.call_tool("github-stdio", "create_issue",
                                 {"owner": "o", "repo": "r", "title": "t"})
        self.assertFalse(r.ok)
        self.assertEqual(r.stage, "approval")
        self.assertTrue(r.approval_id)
        self.assertEqual(self.approvals.status(r.approval_id), "pending")
        self.assertTrue(self.approvals.decide(r.approval_id, "approve"))
        # 审批放行后进入传输；fixture 模式写操作被对端如实拒绝（不伪造 issue 号）
        r2 = self.proxy.call_tool("github-stdio", "create_issue",
                                  {"owner": "o", "repo": "r", "title": "t"},
                                  approval_id=r.approval_id)
        self.assertFalse(r2.ok)
        self.assertEqual(r2.stage, "upstream")
        self.assertTrue(r2.data["isError"])
        self.assertIn("fixture", r2.text)

    def test_approval_gate_fail_closed_without_approval_center(self):
        p = SecureProxy(self.reg, breaker=self.breaker, approvals=None)
        r = p.call_tool("github-stdio", "create_issue",
                        {"owner": "o", "repo": "r", "title": "t"})
        self.assertFalse(r.ok)
        self.assertEqual(r.stage, "approval")
        self.assertIn("fail-closed", r.reason)

    def test_domain_gate_blocks_out_of_whitelist_url(self):
        r = self.proxy.call_tool("github-stdio", "search_repositories",
                                 {"query": "see https://evil.example.com/x"})
        self.assertFalse(r.ok)
        self.assertEqual(r.stage, "domain")
        self.assertIn("evil.example.com", r.reason)

    def test_credential_gate_rejects_param_carried_token(self):
        r = self.proxy.call_tool("github-stdio", "search_repositories",
                                 {"query": "mcp", "token": "abc"})
        self.assertFalse(r.ok)
        self.assertEqual(r.stage, "credential")
        self.assertIn("凭证字段", r.reason)

    def test_inbound_secret_scan_blocks(self):
        # 高熵串：全 a/b 之类的填充值会被 scanner 抑制词正确判为示例值
        tok = "ghp_" + hashlib.sha256(b"sectrust-inbound").hexdigest()[:36]
        r = self.proxy.call_tool("github-stdio", "search_repositories",
                                 {"query": tok})
        self.assertFalse(r.ok)
        self.assertEqual(r.stage, "secret_scan_in")
        self.assertTrue(r.findings)
        self.assertTrue(any(f["rule_id"] == "github-pat-classic"
                            for f in r.findings))

    def test_outbound_secret_scan_blocks_without_deleting(self):
        made = []
        tok = "ghp_" + hashlib.sha256(b"sectrust-outbound").hexdigest()[:36]
        factory = make_factory(made, responses={TOOLS_CALL: {
            "content": [{"type": "text", "text": "leak " + tok}],
            "isError": False}})
        p = SecureProxy(self.reg, breaker=self.breaker, approvals=self.approvals,
                        transport_factory=factory)
        r = p.call_tool("github-stdio", "search_repositories", {"query": "mcp"})
        self.assertFalse(r.ok)
        self.assertEqual(r.stage, "secret_scan_out")
        self.assertTrue(r.findings)
        self.assertNotIn(tok, json.dumps(r.findings, ensure_ascii=False))

    def test_breaker_gate_blocks_after_failures(self):
        for _ in range(3):
            self.breaker.record_failure("github-stdio", "连不上")
        r = self.proxy.call_tool("github-stdio", "search_repositories",
                                 {"query": "mcp"})
        self.assertFalse(r.ok)
        self.assertEqual(r.stage, "breaker")
        self.assertIn("TTL 熔断", r.reason)

    def test_credential_proxy_injection_never_enters_params(self):
        made = []
        factory = make_factory(made, responses={TOOLS_CALL: {
            "content": [{"type": "text", "text": "ok"}], "isError": False}})
        vault = CredentialVault(env={"GITHUB_TOKEN": "ghp_injected_value"})
        p = SecureProxy(self.reg, breaker=self.breaker,
                        approvals=self.approvals, vault=vault,
                        transport_factory=factory)
        r = p.call_tool("github-stdio", "search_repositories", {"query": "mcp"})
        self.assertTrue(r.ok, r.reason)
        self.assertEqual(made[0].extra_env.get("GITHUB_TOKEN"),
                         "ghp_injected_value")
        # 凭证只进 env，绝不出现在请求 params 里
        for method, params in made[0].calls:
            self.assertNotIn("token", json.dumps(params, ensure_ascii=False).lower())
        self.assertEqual(r.audit["credential"]["state"], "injected_env")

    def test_missing_credential_is_honest_not_faked(self):
        made = []
        factory = make_factory(made, responses={TOOLS_CALL: {
            "content": [{"type": "text", "text": "ok"}], "isError": False}})
        p = SecureProxy(self.reg, breaker=self.breaker,
                        approvals=self.approvals,
                        vault=CredentialVault(env={}),
                        transport_factory=factory)
        r = p.call_tool("github-stdio", "search_repositories", {"query": "mcp"})
        self.assertTrue(r.ok, r.reason)
        self.assertEqual(r.audit["credential"]["state"], "unavailable")
        self.assertEqual(made[0].extra_env, {})

    def test_upstream_iserror_is_not_wrapped_as_success(self):
        made = []
        factory = make_factory(made, responses={TOOLS_CALL: {
            "content": [{"type": "text", "text": "credential_unavailable"}],
            "isError": True,
            "structuredContent": {"error": "credential_unavailable"}}})
        p = SecureProxy(self.reg, breaker=self.breaker,
                        approvals=self.approvals, transport_factory=factory)
        r = p.call_tool("github-stdio", "search_repositories", {"query": "mcp"})
        self.assertFalse(r.ok)
        self.assertEqual(r.stage, "upstream")
        self.assertTrue(r.data["isError"])

    def test_unregistered_server_rejected(self):
        r = self.proxy.call_tool("nope", "search_repositories", {"query": "x"})
        self.assertFalse(r.ok)
        self.assertEqual(r.stage, "registry")

    def test_scan_secrets_clean_text(self):
        self.assertEqual(scan_secrets("普通文本：竞品分析报告，无凭证"), [])


class TestCredentialVault(unittest.TestCase):
    def test_resolve_present_and_absent(self):
        v = CredentialVault(env={"GITHUB_TOKEN": "abc"})
        ok, val, why = v.resolve("GITHUB_TOKEN")
        self.assertTrue(ok)
        self.assertEqual(val, "abc")
        ok2, val2, why2 = CredentialVault(env={}).resolve("GITHUB_TOKEN")
        self.assertTrue(ok2)
        self.assertEqual(val2, "")
        self.assertIn("credential_unavailable", why2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
