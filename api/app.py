"""
HTTP API 服务（api/app.py）—— 把"跨进程 SQLite 内核"暴露为可跨机访问的 REST 接口。

设计目标（对应产品化升级路线 1：放弃同机 SQLite 前提）：
- 任何 runner / 外部 agent 可通过 HTTP 读写任务登记处、审批中心、长期记忆、扫描模板；
- 鉴权：Bearer 令牌（OAuth 2.1，见 security/oauth）；未配置则默认开放（仅限本地/内网）；
- 可观测性：每个请求自动打点（计数 + 延迟），详见 security/observability；
- 零依赖：仅用标准库 http.server；Redis/MQ 等分布式能力见 transport/。

⚠️ 传输层 TLS 由反向代理/网关负责；本服务只做令牌校验与业务处理。
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse, parse_qs

from approval import ApprovalCenter
from memory import MemoryStore, _memory_db_path
from task_registry import TaskRegistry, DEFAULT_DB_PATH
from templates import execute_scan, list_templates, request_scan

from security.oauth import (TokenValidator, ResourceServerValidator, extract_bearer,
                            protected_resource_metadata, www_authenticate)
from security import observability as obs

# MCP 2026-07-28：HTTP 传输下每个响应都应带 MCP-Protocol-Version
try:
    from mcp_server import LATEST_PROTOCOL_VERSION as MCP_PROTOCOL_VERSION
except Exception:  # noqa: BLE001  （避免 api 层硬依赖 mcp_server）
    MCP_PROTOCOL_VERSION = "2026-07-28"


class RegistryAPI:
    """REST 服务：包裹所有本地 store，提供统一 HTTP 入口。"""

    def __init__(self, db_path: Optional[str] = None, memory_db_path: Optional[str] = None,
                 host: str = "0.0.0.0", port: int = 8080,
                 require_auth: bool = False, token_validator: Optional[TokenValidator] = None):
        self.db_path = db_path or os.getenv("DB_PATH") or DEFAULT_DB_PATH
        self.memory_db_path = memory_db_path or os.getenv("MEMORY_DB_PATH") or _memory_db_path()
        self.host = host
        self.port = port
        self.require_auth = require_auth or os.getenv("API_AUTH_ENABLED") == "1"
        self.validator = token_validator or self._build_validator()
        self._server = None
        self.metrics = obs.get_metrics()

    @staticmethod
    def _build_validator() -> TokenValidator:
        """按配置决定鉴权口径：

        · 配置了 MCP_RESOURCE_URL / OAUTH_AUTHORIZATION_SERVERS → **资源服务器严格模式**：
          令牌必须带 aud 且等于本服务器（RFC 8707）。这是产品化/多租户场景应有姿态。
        · 否则 → 宽松模式（本地/单机，静态令牌可用），避免升级即打断既有部署。

        两种模式下 .well-known 元数据端点都可用，客户端总有一致的发现路径。
        """
        strict = bool(os.getenv("MCP_RESOURCE_URL") or os.getenv("OAUTH_AUTHORIZATION_SERVERS"))
        return ResourceServerValidator(require_resource=strict)

    # ---------- 路由表 ----------
    _ROUTES = [
        ("GET", r"^/\.well-known/oauth-protected-resource$", "_oauth_metadata"),
        ("GET", r"^/health$", "_health"),
        ("POST", r"^/scan/deep$", "_scan_deep"),
        ("POST", r"^/tasks$", "_task_create"),
        ("GET", r"^/tasks$", "_task_list"),
        ("GET", r"^/tasks/([^/]+)$", "_task_get"),
        ("POST", r"^/tasks/([^/]+)/status$", "_task_status"),
        ("POST", r"^/tasks/([^/]+)/progress$", "_task_progress"),
        ("POST", r"^/runners/beat$", "_runner_beat"),
        ("GET", r"^/runners$", "_runner_list"),
        ("POST", r"^/approvals/request$", "_approval_request"),
        ("POST", r"^/approvals/([^/]+)/decide$", "_approval_decide"),
        ("GET", r"^/approvals$", "_approval_list"),
        ("GET", r"^/templates$", "_template_list"),
        ("POST", r"^/memory$", "_memory_set"),
        ("GET", r"^/memory/([^/]+)/([^/]+)$", "_memory_get"),
        ("GET", r"^/memory$", "_memory_list"),
        ("DELETE", r"^/memory/([^/]+)/([^/]+)$", "_memory_forget"),
        ("POST", r"^/scan/request$", "_scan_request"),
        ("POST", r"^/scan/execute$", "_scan_execute"),
    ]

    def _dispatch(self, method: str, path: str) -> Optional[Tuple[str, Tuple]]:
        for m, pat, fn in self._ROUTES:
            if m != method:
                continue
            mt = re.match(pat, path)
            if mt:
                return fn, mt.groups()
        return None

    # ---------- 业务处理 ----------
    def _oauth_metadata(self, args, body, q):
        """RFC 9728 Protected Resource Metadata（免鉴权）。

        客户端靠它自动发现授权服务器，从而免掉"手工配置 token endpoint"这一步——
        这也是 MCP 2026-07-28 把"服务器即 OAuth 资源服务器"定为强制之后的落地前提。
        """
        return 200, protected_resource_metadata()

    def _health(self, args, body, q):
        return 200, {"status": "ok", "service": "sectrust",
                     "auth": "on" if self.require_auth else "off",
                     "mcpProtocolVersion": MCP_PROTOCOL_VERSION}

    def _scan_deep(self, args, body, q):
        """深度凭证扫描（scanner 引擎）：返回统计、闸门与可选 SARIF。

        与 /scan/execute（合规模板·广度）互补：这里是 78 条结构化检测器 + 熵兜底。
        仍受审批闸门约束：传 approval_id 且已批准才执行，默认拒绝。
        """
        from approval import ApprovalCenter
        target = body.get("target") or body.get("repo_dir")
        if not target:
            return 400, {"error": "missing target"}
        aid = body.get("approval_id")
        if aid:
            ac = ApprovalCenter(db_path=self.db_path)
            rec = ac.get(aid) if hasattr(ac, "get") else None
            ac.close()
            if not rec or rec.get("status") != "approved":
                return 403, {"error": "approval required", "approval_id": aid,
                             "status": (rec or {}).get("status", "not_found")}
        elif self.require_auth is False and os.getenv("ALLOW_UNAPPROVED_SCAN") != "1":
            # 未开鉴权（本地模式）时，仍要求显式开关，避免"随手一扫"成为默认
            return 403, {"error": "approval required",
                         "hint": "先 POST /approvals/request 并取得批准，或设置 ALLOW_UNAPPROVED_SCAN=1"}

        from scanner.engine import scan_path
        res = scan_path(
            target,
            providers=body.get("providers"),
            categories=body.get("categories"),
            severities=body.get("severities"),
            high_entropy=bool(body.get("high_entropy", True)),
            only_changed=bool(body.get("only_changed", False)),
            since=body.get("since", "HEAD~1"),
            baseline_path=body.get("baseline_path"),
            write_baseline=bool(body.get("write_baseline", False)),
            fail_on=body.get("fail_on", "high"),
        )
        return 200, res

    def _task_create(self, args, body, q):
        reg = TaskRegistry(db_path=self.db_path)
        tid = reg.register_task(body.get("task_id"), payload=body.get("payload") or {})
        reg.close()
        return 200, {"task_id": tid}

    def _task_list(self, args, body, q):
        reg = TaskRegistry(db_path=self.db_path)
        rows = reg.list_tasks(
            status=q.get("status", [None])[0],
            runner_id=q.get("runner_id", [None])[0],
            limit=int(q.get("limit", ["100"])[0]),
        )
        reg.close()
        return 200, rows

    def _task_get(self, args, body, q):
        reg = TaskRegistry(db_path=self.db_path)
        row = reg.get_task(args[0])
        reg.close()
        if row is None:
            return 404, {"error": "not found"}
        return 200, row

    def _task_status(self, args, body, q):
        reg = TaskRegistry(db_path=self.db_path)
        ok = reg.update_task_status(args[0], body.get("status"))
        reg.close()
        return 200, {"updated": ok}

    def _task_progress(self, args, body, q):
        reg = TaskRegistry(db_path=self.db_path)
        reg.set_progress(args[0], body.get("progress"), body.get("note"))
        reg.close()
        return 200, {"updated": True}

    def _runner_beat(self, args, body, q):
        reg = TaskRegistry(db_path=self.db_path, runner_id=body.get("runner_id", "api"))
        reg.beat()
        reg.close()
        return 200, {"beat": True}

    def _runner_list(self, args, body, q):
        reg = TaskRegistry(db_path=self.db_path)
        rows = reg.list_runners()
        reg.close()
        return 200, rows

    def _approval_request(self, args, body, q):
        ac = ApprovalCenter(db_path=self.db_path)
        aid = ac.request(body.get("action", "unknown"), body.get("target", ""),
                         task_id=body.get("task_id"), channel=body.get("channel", "api"),
                         user_id=body.get("user_id", "api"))
        ac.close()
        return 200, {"approval_id": aid, "status": "pending"}

    def _approval_decide(self, args, body, q):
        ac = ApprovalCenter(db_path=self.db_path)
        ac.decide(args[0], body.get("decision", "approve"),
                  by=body.get("by", "api"), note=body.get("note", ""))
        ac.close()
        return 200, {"approval_id": args[0], "decision": body.get("decision")}

    def _approval_list(self, args, body, q):
        ac = ApprovalCenter(db_path=self.db_path)
        rows = ac.list_approvals(status=q.get("status", [None])[0],
                                 limit=int(q.get("limit", ["100"])[0]))
        ac.close()
        return 200, rows

    def _template_list(self, args, body, q):
        return 200, list_templates()

    def _memory_set(self, args, body, q):
        ms = MemoryStore(db_path=self.memory_db_path)
        ms.remember(body.get("category", "preference"), body.get("key", ""),
                    body.get("value"), note=body.get("note", ""))
        ms.close()
        return 200, {"stored": True}

    def _memory_get(self, args, body, q):
        ms = MemoryStore(db_path=self.memory_db_path)
        row = ms.get_record(args[0], args[1])
        ms.close()
        if row is None:
            return 404, {"error": "not found"}
        return 200, row

    def _memory_list(self, args, body, q):
        ms = MemoryStore(db_path=self.memory_db_path)
        if q.get("query"):
            rows = ms.search(q["query"][0])
        elif q.get("top"):
            rows = ms.top_used(int(q["top"][0]))
        elif q.get("category"):
            rows = ms.list_category(q["category"][0])
        else:
            rows = ms.list_category(None)
        ms.close()
        return 200, rows

    def _memory_forget(self, args, body, q):
        ms = MemoryStore(db_path=self.memory_db_path)
        ms.forget(args[0], args[1])
        ms.close()
        return 200, {"forgotten": True}

    def _scan_request(self, args, body, q):
        req = request_scan(
            body.get("template_id"), body.get("repo_dir", "."),
            db_path=self.db_path, channel=body.get("channel", "api"),
            user_id=body.get("user_id", "api"), repo_label=body.get("repo_label"),
        )
        return 200, req

    def _scan_execute(self, args, body, q):
        res = execute_scan(
            body.get("task_id"), body.get("approval_id"), body.get("repo_dir", "."),
            db_path=self.db_path, sandbox_backend=body.get("sandbox_backend", "auto"),
            template_id=body.get("template_id"),
        )
        return 200, res

    # ---------- HTTP 处理器 ----------
    def make_handler(self):
        api = self

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # 静默默认访问日志（统一走 obs.log_event）
                pass

            def _send(self, code, payload, headers: Optional[Dict[str, str]] = None):
                data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                # MCP 2026-07-28：HTTP 传输下必须带 MCP-Protocol-Version 头
                self.send_header("MCP-Protocol-Version", MCP_PROTOCOL_VERSION)
                self.end_headers()
                self.wfile.write(data)

            def _auth_ok(self) -> bool:
                if not api.require_auth:
                    return True
                token = extract_bearer(self.headers.get("Authorization"))
                res = api.validator.validate(token)
                return res.get("active", False)

            def _read_body(self) -> Dict:
                length = int(self.headers.get("Content-Length", 0))
                if not length:
                    return {}
                try:
                    return json.loads(self.rfile.read(length).decode("utf-8"))
                except Exception:  # noqa: BLE001
                    return {}

            def _route(self):
                t0 = time.time()
                method = self.command
                parsed = urlparse(self.path)
                q = parse_qs(parsed.query)
                route = api._dispatch(method, parsed.path)

                # RFC 9728：元数据端点必须**免鉴权**可达——客户端正是靠它来发现
                # 该去哪儿换取令牌。若它也要求令牌，就形成鸡生蛋死锁。
                if not self._auth_ok() and route is not None and route[0] != "_oauth_metadata":
                    self._send(401, {"error": "unauthorized"},
                               headers={"WWW-Authenticate": www_authenticate()})
                    return
                if not self._auth_ok() and route is None:
                    self._send(401, {"error": "unauthorized"},
                               headers={"WWW-Authenticate": www_authenticate(
                                   error="invalid_request", description="unknown route")})
                    return
                if route is None:
                    self._send(404, {"error": "not found"})
                    return
                fn, groups = route
                body = self._read_body() if method in ("POST", "PUT", "DELETE") else {}
                try:
                    with obs.span(fn, method=method, path=parsed.path):
                        code, payload = getattr(api, fn)(groups, body, q)
                    status_tag = "2xx" if code < 300 else ("4xx" if code < 500 else "5xx")
                    api.metrics.inc("http_requests_total", tags={"method": method,
                                                               "status": status_tag})
                    if code >= 400:
                        obs.log_event("warn", "api_error", code=code, path=parsed.path)
                    self._send(code, payload)
                except Exception as e:  # noqa: BLE001
                    obs.log_event("error", "api_exception", error=str(e), path=parsed.path)
                    api.metrics.inc("http_requests_total", tags={"method": method, "status": "5xx"})
                    self._send(500, {"error": str(e)})
                finally:
                    try:
                        api.metrics.observe("http_request_duration_seconds",
                                           time.time() - t0, {"method": method})
                    except Exception:  # noqa: BLE001
                        pass

            def do_GET(self):
                self._route()

            def do_POST(self):
                self._route()

            def do_DELETE(self):
                self._route()

        return _Handler

    def serve(self):
        self._server = HTTPServer((self.host, self.port), self.make_handler())
        obs.log_event("info", "API 服务启动", host=self.host, port=self.port,
                      auth="on" if self.require_auth else "off")
        try:
            self._server.serve_forever()
        except OSError:
            # Windows 上跨线程 shutdown 后 select 可能抛 10038，属正常收尾噪声
            pass

    def shutdown(self):
        if self._server:
            try:
                self._server.shutdown()
            except Exception:  # noqa: BLE001
                pass
            try:
                self._server.server_close()
            except Exception:  # noqa: BLE001
                pass


def main():
    import argparse
    ap = argparse.ArgumentParser(description="AI 同事 注册中心 REST API")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=int(os.getenv("API_PORT", "8080")))
    ap.add_argument("--db-path", default=None)
    ap.add_argument("--auth", action="store_true", help="开启 Bearer 令牌鉴权")
    args = ap.parse_args()
    api = RegistryAPI(host=args.host, port=args.port, db_path=args.db_path,
                      require_auth=args.auth or os.getenv("API_AUTH_ENABLED") == "1")
    try:
        api.serve()
    except KeyboardInterrupt:
        api.shutdown()


if __name__ == "__main__":
    main()
