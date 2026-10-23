"""
MCP Server 测试（零依赖）
1) 进程内：直接调用 McpServer.handle() 验证协议层 + 各工具分发（含批准/拦截/报错路径）
2) 子进程 stdio 端到端：真实启动 mcp_server.py，经 stdin/stdout 交换 JSON-RPC，验证传输层
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from mcp_server import McpServer, LATEST_PROTOCOL_VERSION  # noqa: E402


def _make_repo(path):
    os.makedirs(path)
    with open(os.path.join(path, ".env"), "w", encoding="utf-8") as f:
        # AKIAIOSFODNN7EXAMPLE 是 AWS 官方文档示例密钥（占位符），不应被判泄露
        f.write('API_KEY=AKIAIOSFODNN7EXAMPLE\n')
    with open(os.path.join(path, "app.py"), "w", encoding="utf-8") as f:
        # 真实形态的（假）密钥，用于断言真阳性能被检出
        f.write('SECRET = "7fQ2xLp9VbN3mRkT"\n')


class McpInProcessTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "mcp.db")
        os.environ["DB_PATH"] = self.db
        self.repo = os.path.join(self.tmp, "repo")
        _make_repo(self.repo)
        self.srv = McpServer(db_path=self.db)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        shutil.rmtree(os.path.join(REPO_ROOT, ".scan_jail"), ignore_errors=True)
        os.environ.pop("DB_PATH", None)

    def _call(self, name, args=None):
        resp = self.srv.handle({
            "jsonrpc": "2.0", "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": args or {}},
        })
        self.assertFalse(resp["result"]["isError"], resp["result"]["content"][0]["text"])
        return json.loads(resp["result"]["content"][0]["text"])

    def test_initialize(self):
        resp = self.srv.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        # 未声明版本时回最新版（2026-07-28）；老客户端声明旧版本则回旧版本（见 test_negotiation）
        self.assertEqual(resp["result"]["protocolVersion"], LATEST_PROTOCOL_VERSION)
        self.assertIn("tools", resp["result"]["capabilities"])

    def test_negotiation_backward_compatible(self):
        """客户端声明 2024-11-05 → 服务端回 2024-11-05（不强行升级）。"""
        resp = self.srv.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                "params": {"protocolVersion": "2024-11-05"}})
        self.assertEqual(resp["result"]["protocolVersion"], "2024-11-05")

    def test_unsupported_version_error(self):
        resp = self.srv.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                "params": {"protocolVersion": "1999-01-01"}})
        self.assertIn("error", resp)
        self.assertIn("UnsupportedProtocolVersion", resp["error"]["message"])
        self.assertIn("2026-07-28", resp["error"]["data"]["supported"])

    def test_server_discover(self):
        """2026-07-28 新增：能力协商不再依赖 initialize 握手。"""
        resp = self.srv.handle({"jsonrpc": "2.0", "id": 1, "method": "server/discover"})
        r = resp["result"]
        self.assertEqual(r["protocolVersion"], LATEST_PROTOCOL_VERSION)
        self.assertIn("supportedProtocolVersions", r)
        self.assertIn("capabilities", r)
        # 已弃用特性不应被声明（Roots/Sampling/Logging 进入 12 个月弃用窗口）
        caps = r["capabilities"]
        self.assertNotIn("roots", caps)
        self.assertNotIn("sampling", caps)

    def test_tool_annotations(self):
        """工具必须带风险注解，让客户端在调用前就能判断。"""
        resp = self.srv.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        tools = {t["name"]: t for t in resp["result"]["tools"]}
        self.assertGreaterEqual(len(tools), 22)
        for name, t in tools.items():
            self.assertIn("annotations", t, f"{name} 缺少注解")
        self.assertTrue(tools["memory_forget"]["annotations"]["destructiveHint"])
        self.assertTrue(tools["list_templates"]["annotations"]["readOnlyHint"])
        self.assertTrue(tools["execute_scan"]["annotations"]["openWorldHint"])

    def test_tools_list(self):
        resp = self.srv.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        names = {t["name"] for t in resp["result"]["tools"]}
        for n in ("list_templates", "request_scan", "approve", "deny",
                  "execute_scan", "run_scan", "list_tasks", "get_task",
                  "list_runners", "list_approvals"):
            self.assertIn(n, names)

    def test_unknown_method_returns_error(self):
        resp = self.srv.handle({"jsonrpc": "2.0", "id": 9, "method": "bogus/method"})
        self.assertEqual(resp["error"]["code"], -32601)

    def test_tools_call_error_path(self):
        resp = self.srv.handle({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "request_scan",
                       "arguments": {"template_id": "nope", "repo_dir": self.repo}},
        })
        self.assertTrue(resp["result"]["isError"])

    def test_request_scan_then_list_tasks(self):
        out = self._call("request_scan",
                         {"template_id": "secret_leak", "repo_dir": self.repo})
        self.assertEqual(out["status"], "pending")
        tasks = self._call("list_tasks")
        self.assertTrue(any(t["task_id"] == out["task_id"] for t in tasks))
        approvals = self._call("list_approvals")
        self.assertTrue(any(a["approval_id"] == out["approval_id"] for a in approvals))

    def test_run_scan_auto_approve_e2e(self):
        out = self._call("run_scan", {
            "template_id": "secret_leak", "repo_dir": self.repo,
            "auto_approve": True, "sandbox_backend": "local",
        })
        self.assertEqual(out["status"], "done", out)
        rules = {f["rule"] for f in (out.get("report") or {}).get("findings", [])}
        self.assertIn("GENERIC_SECRET", rules)

    def test_get_task_not_found(self):
        resp = self.srv.handle({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "get_task", "arguments": {"task_id": "missing"}},
        })
        text = resp["result"]["content"][0]["text"]
        self.assertIn("not_found", text)


class McpStdioTransportTest(unittest.TestCase):
    """真实子进程 stdio 端到端：验证 JSON-RPC 传输层与 CLI 启动。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "mcp.db")
        self.repo = os.path.join(self.tmp, "repo")
        _make_repo(self.repo)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        shutil.rmtree(os.path.join(REPO_ROOT, ".scan_jail"), ignore_errors=True)

    def _rpc(self, method, params=None, rid=1):
        return {"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}

    def test_stdio_roundtrip(self):
        env = dict(os.environ)
        env["DB_PATH"] = self.db
        proc = subprocess.Popen(
            [sys.executable, "mcp_server.py"],
            cwd=REPO_ROOT, env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True,
        )
        reqs = [
            self._rpc("initialize", {}, rid=1),
            self._rpc("tools/list", {}, rid=2),
            self._rpc("tools/call", {
                "name": "run_scan",
                "arguments": {"template_id": "secret_leak", "repo_dir": self.repo,
                              "auto_approve": True, "sandbox_backend": "local"},
            }, rid=3),
        ]
        stdin_data = "".join(json.dumps(r) + "\n" for r in reqs)
        out, _ = proc.communicate(input=stdin_data, timeout=60)
        lines = [json.loads(l) for l in out.strip().splitlines() if l.strip()]
        by_id = {m.get("id"): m for m in lines}
        self.assertIn(1, by_id)
        self.assertEqual(by_id[1]["result"]["protocolVersion"], LATEST_PROTOCOL_VERSION)
        self.assertIn(2, by_id)
        self.assertIn("run_scan", {t["name"] for t in by_id[2]["result"]["tools"]})
        self.assertIn(3, by_id)
        result_text = by_id[3]["result"]["content"][0]["text"]
        result = json.loads(result_text)
        self.assertEqual(result["status"], "done", result)


class McpDagToolsTest(unittest.TestCase):
    """编排层 DAG 四工具（dag_add / dag_claim_ready / dag_complete / dag_status）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "mcp.db")
        os.environ["DB_PATH"] = self.db
        self.srv = McpServer(db_path=self.db)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        os.environ.pop("DB_PATH", None)

    def _call(self, name, args=None):
        resp = self.srv.handle({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": name, "arguments": args or {}},
        })
        self.assertFalse(resp["result"]["isError"], resp["result"]["content"][0]["text"])
        return json.loads(resp["result"]["content"][0]["text"])

    def test_dag_add_and_dependencies(self):
        r = self._call("dag_add", {"task_id": "a", "payload": {"name": "上游"}})
        self.assertTrue(r["ok"])
        r = self._call("dag_add", {"task_id": "b", "depends_on": ["a"]})
        self.assertTrue(r["ok"])
        # 重复 / 未知依赖 / 自依赖 / 混合未知依赖 全部被拒
        # （拒绝走 payload.error 约定，与 skill_load 等工具一致：isError 只留给未捕获异常）
        for bad in ({"task_id": "b", "depends_on": ["a"]},
                    {"task_id": "c", "depends_on": ["zzz"]},
                    {"task_id": "d", "depends_on": ["d"]},
                    {"task_id": "e", "depends_on": ["b", "nope"]}):
            resp = self.srv.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                    "params": {"name": "dag_add", "arguments": bad}})
            body = json.loads(resp["result"]["content"][0]["text"])
            self.assertEqual(body.get("error"), "dag_rejected", bad)

    def test_dag_claim_ready_and_complete_flow(self):
        self._call("dag_add", {"task_id": "a", "payload": {"name": "上游"}})
        self._call("dag_add", {"task_id": "b", "depends_on": ["a"]})
        # b 未就绪：认领只能拿到 a
        r = self._call("dag_claim_ready", {"runner_id": "w1"})
        self.assertEqual(r["claimed"]["task_id"], "a")
        self.assertEqual(r["claimed"]["status"], "running")
        # a 未完成时无就绪任务
        r = self._call("dag_claim_ready", {"runner_id": "w2"})
        self.assertIsNone(r["claimed"])
        # 完成 a -> b 就绪
        r = self._call("dag_complete", {"task_id": "a"})
        self.assertTrue(r["completed"])
        # 幂等：重复完成返回 False 不报错
        r = self._call("dag_complete", {"task_id": "a"})
        self.assertFalse(r["completed"])
        r = self._call("dag_claim_ready", {"runner_id": "w2"})
        self.assertEqual(r["claimed"]["task_id"], "b")

    def test_dag_status_views(self):
        self._call("dag_add", {"task_id": "a"})
        self._call("dag_add", {"task_id": "b", "depends_on": ["a"]})
        st = self._call("dag_status", {})
        self.assertEqual(st["stats"]["total"], 2)
        self.assertEqual(st["waves"], [["a"], ["b"]])
        self.assertEqual(st["ready"], ["a"])
        self.assertEqual(st["graph"], {"a": [], "b": ["a"]})
        st = self._call("dag_status", {"task_id": "b"})
        self.assertEqual(st["waiting_on"], ["a"])  # 未完成的上游

    def test_dag_tools_registered_with_annotations(self):
        resp = self.srv.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        tools = {t["name"]: t for t in resp["result"]["tools"]}
        for n in ("dag_add", "dag_claim_ready", "dag_complete", "dag_status"):
            self.assertIn(n, tools)
            self.assertIn("annotations", tools[n])  # 风险注解必须齐


if __name__ == "__main__":
    unittest.main(verbosity=2)
