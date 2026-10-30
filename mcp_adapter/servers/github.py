"""
github.py —— GitHub MCP Server 样板（Tools / Resources / Prompts 三原语）。

它是**被连接的对端**，既可独立进程跑 stdio，也可作为 HTTP handler 复用同一套
`handle()` 纯函数；`mcp_adapter` 的 SecureProxy 通过双传输去连它。

三原语：
  · Tools     search_repositories / get_file_contents / list_issues / create_issue
  · Resources repos://sample/readme, repos://sample/metadata（Server 自带示例资源）
  · Prompts   repo_summary（带 owner/repo/focus 参数，产出要求"结论必须带来源"）

数据模式（**绝不伪造**）：
  live        设了 GITHUB_TOKEN → 真调 https://api.github.com，失败如实 isError；
  fixture     未设 token 但显式 SECTRUST_MCP_GITHUB_FIXTURE=1 → 返回**明确标注**
              `_fixture=true` 的离线数据，供测试/演示；写操作在 fixture 下拒绝执行；
  unavailable 未设 token 且未开 fixture → 明确返回 credential_unavailable 错误，
              **一条假数据都不给**。

诚实边界：本样板是 MCP 协议与治理链路的参考实现，不是 GitHub 官方 Server；
live 模式只覆盖 4 个工具的 REST 子集，不宣称覆盖 GitHub API 全量。
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, List, Optional

from ..protocol import (CallToolResult, LATEST_PROTOCOL_VERSION, Prompt,
                        PromptArgument, Resource, Tool, make_error, make_result,
                        negotiated_version, server_capabilities, text_content,
                        INVALID_PARAMS, METHOD_NOT_FOUND, INTERNAL_ERROR,
                        INITIALIZE, NOTIFY_INITIALIZED, PING, TOOLS_LIST,
                        TOOLS_CALL, RESOURCES_LIST, RESOURCES_TEMPLATES_LIST,
                        RESOURCES_READ, PROMPTS_LIST, PROMPTS_GET)

SERVER_NAME = "sectrust-github-mcp-sample"
SERVER_VERSION = "1.0.0"
API_ROOT = "https://api.github.com"
FIXTURE_ENV = "SECTRUST_MCP_GITHUB_FIXTURE"
TOKEN_ENV = "GITHUB_TOKEN"

FIXTURE_NOTE = "离线 fixture 数据（SECTRUST_MCP_GITHUB_FIXTURE=1），非真实 GitHub API 响应"

FIXTURE_README = """# 示例仓库 README（Server 内置 fixture 资源）

这是 MCP Server 自带的示例 Resources，用 `resources/read` 读取，与 GitHub
token 无关。真实接入时 resources 应对接对端的文档/配置端点。

三原语自检清单：tools/list、resources/list、prompts/list 均应返回非空。
"""


def _mode() -> str:
    if os.environ.get(TOKEN_ENV, "").strip():
        return "live"
    if os.environ.get(FIXTURE_ENV, "").strip().lower() in ("1", "true", "yes"):
        return "fixture"
    return "unavailable"


# ---------------------------------------------------------------- 三原语声明
TOOLS: List[Tool] = [
    Tool("search_repositories", "按关键词搜索仓库（只读）", {
        "type": "object", "properties": {
            "query": {"type": "string", "description": "搜索关键词"},
            "limit": {"type": "integer", "description": "返回条数上限，默认 3"},
        }, "required": ["query"],
    }),
    Tool("get_file_contents", "读取仓库内某文件内容（只读）", {
        "type": "object", "properties": {
            "owner": {"type": "string"}, "repo": {"type": "string"},
            "path": {"type": "string"},
        }, "required": ["owner", "repo", "path"],
    }),
    Tool("list_issues", "列出仓库 issue（只读）", {
        "type": "object", "properties": {
            "owner": {"type": "string"}, "repo": {"type": "string"},
            "state": {"type": "string", "description": "open/closed/all"},
        }, "required": ["owner", "repo"],
    }),
    Tool("create_issue", "新建 issue（写操作，需审批闸门放行）", {
        "type": "object", "properties": {
            "owner": {"type": "string"}, "repo": {"type": "string"},
            "title": {"type": "string"}, "body": {"type": "string"},
        }, "required": ["owner", "repo", "title"],
    }),
]

RESOURCES: List[Resource] = [
    Resource("repos://sample/readme", "示例 README", "Server 内置 fixture 资源",
             "text/markdown"),
    Resource("repos://sample/metadata", "Server 元数据",
             "名称/版本/数据模式/能力声明", "application/json"),
]

PROMPTS: List[Prompt] = [
    Prompt("repo_summary", "生成仓库竞品/技术摘要（要求结论带来源）", [
        PromptArgument("owner", "仓库属主", True),
        PromptArgument("repo", "仓库名", True),
        PromptArgument("focus", "关注维度（如 性能/合规/生态）", False),
    ]),
]


def _fixture_result(structured: Dict, text: str = "") -> CallToolResult:
    return CallToolResult(
        content=text_content(text or json.dumps(structured, ensure_ascii=False,
                                                indent=2)),
        structured=structured, fixture=True, note=FIXTURE_NOTE)


def _unavailable(tool: str) -> CallToolResult:
    return CallToolResult(
        content=text_content(
            "credential_unavailable：工具 %s 需要 %s 环境变量，当前未配置；"
            "且未开启 %s=1 离线 fixture 模式。按纪律不返回任何伪造数据。"
            % (tool, TOKEN_ENV, FIXTURE_ENV)),
        is_error=True,
        structured={"error": "credential_unavailable", "tool": tool,
                    "mode": "unavailable"})


def _live_get(path: str, token: str) -> Dict:
    req = urllib.request.Request(API_ROOT + path, headers={
        "Authorization": "Bearer " + token,
        "Accept": "application/vnd.github+json",
        "User-Agent": "sectrust-mcp-adapter/1.0",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _live_post(path: str, token: str, payload: Dict) -> Dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(API_ROOT + path, data=data, method="POST",
                                 headers={
                                     "Authorization": "Bearer " + token,
                                     "Accept": "application/vnd.github+json",
                                     "User-Agent": "sectrust-mcp-adapter/1.0",
                                     "Content-Type": "application/json",
                                 })
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _require(args: Dict, *keys: str) -> Optional[str]:
    missing = [k for k in keys if not str(args.get(k) or "").strip()]
    return "缺少必需参数：%s" % "、".join(missing) if missing else None


def call_tool(name: str, args: Dict) -> CallToolResult:
    """工具分发。任何异常都转成 isError 结果，不让协议通道崩掉。"""
    mode = _mode()
    try:
        if name == "search_repositories":
            bad = _require(args, "query")
            if bad:
                return CallToolResult(content=text_content(bad), is_error=True)
            if mode == "unavailable":
                return _unavailable(name)
            n = max(1, min(int(args.get("limit") or 3), 20))
            if mode == "fixture":
                items = [{"full_name": "fixture-org/fixture-repo-%d" % i,
                          "html_url": "https://github.com/fixture-org/fixture-repo-%d" % i,
                          "stargazers_count": 100 + i,
                          "description": "[fixture] 示例仓库，非真实数据"}
                         for i in range(1, n + 1)]
                return _fixture_result({"mode": "fixture", "query": args["query"],
                                        "items": items, "note": FIXTURE_NOTE})
            q = urllib.parse.quote(str(args["query"]))
            data = _live_get("/search/repositories?q=%s&per_page=%d" % (q, n),
                             os.environ[TOKEN_ENV])
            return CallToolResult(
                content=text_content(json.dumps(data, ensure_ascii=False, indent=2)),
                structured={"mode": "live", "total_count": data.get("total_count"),
                            "items": data.get("items", [])[:n]})

        if name == "get_file_contents":
            bad = _require(args, "owner", "repo", "path")
            if bad:
                return CallToolResult(content=text_content(bad), is_error=True)
            if mode == "unavailable":
                return _unavailable(name)
            if mode == "fixture":
                return _fixture_result({
                    "mode": "fixture", "path": args["path"],
                    "content": FIXTURE_README, "note": FIXTURE_NOTE})
            owner = urllib.parse.quote(str(args["owner"]))
            repo = urllib.parse.quote(str(args["repo"]))
            path = urllib.parse.quote(str(args["path"]))
            data = _live_get("/repos/%s/%s/contents/%s" % (owner, repo, path),
                             os.environ[TOKEN_ENV])
            content = base64.b64decode(data.get("content") or "").decode(
                "utf-8", "replace")
            return CallToolResult(content=text_content(content[:50000]),
                                  structured={"mode": "live", "sha": data.get("sha")})

        if name == "list_issues":
            bad = _require(args, "owner", "repo")
            if bad:
                return CallToolResult(content=text_content(bad), is_error=True)
            if mode == "unavailable":
                return _unavailable(name)
            if mode == "fixture":
                return _fixture_result({
                    "mode": "fixture",
                    "items": [{"number": 1, "title": "[fixture] 示例 issue",
                               "state": "open", "note": FIXTURE_NOTE}],
                    "note": FIXTURE_NOTE})
            owner = urllib.parse.quote(str(args["owner"]))
            repo = urllib.parse.quote(str(args["repo"]))
            state = urllib.parse.quote(str(args.get("state") or "open"))
            data = _live_get("/repos/%s/%s/issues?state=%s" % (owner, repo, state),
                             os.environ[TOKEN_ENV])
            return CallToolResult(
                content=text_content(json.dumps(data, ensure_ascii=False, indent=2)),
                structured={"mode": "live", "count": len(data)})

        if name == "create_issue":
            bad = _require(args, "owner", "repo", "title")
            if bad:
                return CallToolResult(content=text_content(bad), is_error=True)
            if mode == "unavailable":
                return _unavailable(name)
            if mode == "fixture":
                # 写操作在 fixture 模式不假装成功——明确拒绝
                return CallToolResult(
                    content=text_content("fixture 模式不执行写操作：create_issue "
                                         "需要 live 模式（GITHUB_TOKEN）且经审批闸门放行；"
                                         "不返回伪造的 issue 编号。"),
                    is_error=True,
                    structured={"error": "write_not_supported_in_fixture",
                                "mode": "fixture"})
            owner = urllib.parse.quote(str(args["owner"]))
            repo = urllib.parse.quote(str(args["repo"]))
            data = _live_post("/repos/%s/%s/issues" % (owner, repo),
                              os.environ[TOKEN_ENV],
                              {"title": args["title"], "body": args.get("body", "")})
            return CallToolResult(
                content=text_content("已创建 issue #%s" % data.get("number")),
                structured={"mode": "live", "number": data.get("number"),
                            "html_url": data.get("html_url")})

        return CallToolResult(content=text_content("未知工具：%s" % name),
                              is_error=True)
    except (urllib.error.HTTPError, urllib.error.URLError, OSError,
            ValueError, KeyError) as e:
        return CallToolResult(
            content=text_content("%s 调用失败（%s 模式）：%s: %s"
                                 % (name, mode, type(e).__name__, e)),
            is_error=True, structured={"error": "upstream_failed", "mode": mode})


def read_resource(uri: str) -> Optional[Dict]:
    if uri == "repos://sample/readme":
        return {"contents": [{"uri": uri, "mimeType": "text/markdown",
                              "text": FIXTURE_README}],
                "_fixture": True, "_note": "Server 内置示例资源，非 GitHub 远端数据"}
    if uri == "repos://sample/metadata":
        meta = {"name": SERVER_NAME, "version": SERVER_VERSION,
                "mode": _mode(), "capabilities": server_capabilities(),
                "tools": [t.name for t in TOOLS],
                "resources": [r.uri for r in RESOURCES],
                "prompts": [p.name for p in PROMPTS]}
        return {"contents": [{"uri": uri, "mimeType": "application/json",
                              "text": json.dumps(meta, ensure_ascii=False, indent=2)}],
                "_fixture": True, "_note": "Server 自描述元数据"}
    return None


def get_prompt(name: str, args: Dict) -> Optional[Dict]:
    if name != "repo_summary":
        return None
    owner = str(args.get("owner") or "unknown")
    repo = str(args.get("repo") or "unknown")
    focus = str(args.get("focus") or "功能/性能/合规/生态")
    text = (
        "请为仓库 %s/%s 生成技术摘要，关注维度：%s。\n\n"
        "硬性要求（默认不信任 AI）：\n"
        "1. 每条结论必须附来源 URL 与抓取时间；缺来源的结论标注「待核实」，"
        "不得凭记忆编造版本号、价格或日期。\n"
        "2. 产出必须包含「功能对比表」「来源链接清单」两项工件；缺任一不得标记 done。\n"
        "3. 搜索失败如实写 unavailable，不伪造资料；写操作需人工审批。"
        % (owner, repo, focus))
    return {
        "description": "仓库技术摘要（结论必须带来源）",
        "messages": [{"role": "user", "content": {"type": "text", "text": text}}],
    }


# ---------------------------------------------------------------- 协议入口
def handle(req: Dict) -> Optional[Dict]:
    """纯函数：一个 JSON-RPC 请求 → 一个响应（通知返回 None）。"""
    method = req.get("method")
    rid = req.get("id")
    params = req.get("params") or {}

    if method == INITIALIZE:
        return make_result(rid, {
            "protocolVersion": negotiated_version(params.get("protocolVersion")),
            "capabilities": server_capabilities(),
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION,
                           "description": "GitHub MCP 三原语样板（Tools/Resources/Prompts）"},
        })
    if method in (NOTIFY_INITIALIZED, "initialized", "notifications/cancelled"):
        return None
    if method == PING:
        return make_result(rid, {})

    if method == TOOLS_LIST:
        return make_result(rid, {"tools": [t.to_dict() for t in TOOLS]})
    if method == TOOLS_CALL:
        result = call_tool(str(params.get("name") or ""),
                           dict(params.get("arguments") or {}))
        out = result.to_dict()
        if result.is_error:
            return make_result(rid, {"content": out["content"],
                                     "isError": True,
                                     "structuredContent": out.get("structuredContent")})
        return make_result(rid, out)

    if method == RESOURCES_LIST:
        return make_result(rid, {"resources": [r.to_dict() for r in RESOURCES]})
    if method == RESOURCES_TEMPLATES_LIST:
        return make_result(rid, {"resourceTemplates": []})
    if method == RESOURCES_READ:
        got = read_resource(str(params.get("uri") or ""))
        if got is None:
            return make_error(rid, INVALID_PARAMS, "未知资源 URI：%s"
                              % params.get("uri"))
        return make_result(rid, got)

    if method == PROMPTS_LIST:
        return make_result(rid, {"prompts": [p.to_dict() for p in PROMPTS]})
    if method == PROMPTS_GET:
        got = get_prompt(str(params.get("name") or ""),
                         dict(params.get("arguments") or {}))
        if got is None:
            return make_error(rid, INVALID_PARAMS, "未知 prompt：%s"
                              % params.get("name"))
        return make_result(rid, got)

    if rid is not None:
        return make_error(rid, METHOD_NOT_FOUND, "method not found: %s" % method)
    return None


# ---------------------------------------------------------------- 两种运行形态
def serve_stdio() -> None:
    """stdio 形态：stdout 是协议通道，诊断只走 stderr（绝不污染）。"""
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
    print("[%s] stdio 启动，mode=%s" % (SERVER_NAME, _mode()), file=sys.stderr)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError:
            continue
        resp = handle(req)
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()


def make_http_handler():
    """HTTP 形态：复用同一个 handle()，便于 loopback fixture 测试。"""
    from http.server import BaseHTTPRequestHandler

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler 约定命名
            try:
                length = int(self.headers.get("Content-Length") or 0)
                req = json.loads(self.rfile.read(length).decode("utf-8"))
                resp = handle(req)
            except (ValueError, OSError) as e:
                resp = make_error(None, INTERNAL_ERROR, str(e))
            body = (b"" if resp is None
                    else json.dumps(resp, ensure_ascii=False).encode("utf-8"))
            self.send_response(202 if resp is None else 200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)

        def log_message(self, *a):  # 不打访问日志，避免干扰测试
            pass

    return Handler


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="GitHub MCP 三原语样板 Server")
    ap.add_argument("--http", action="store_true", help="以 HTTP 形态运行（默认 stdio）")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args(argv)
    if args.http:
        from http.server import ThreadingHTTPServer
        srv = ThreadingHTTPServer(("127.0.0.1", args.port), make_http_handler())
        print("[%s] http://127.0.0.1:%d ，mode=%s"
              % (SERVER_NAME, args.port, _mode()), file=sys.stderr)
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            srv.server_close()
    else:
        serve_stdio()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
