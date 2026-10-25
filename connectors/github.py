"""
connectors/github.py —— GitHub 连接器（urllib REST，零依赖）。

为什么选 GitHub 做第一个真连接器：token 即用（无需 OAuth 跳转），
且"在真实工具里落地"的效果立即可见——建 issue、评论、列 PR，
活儿干在 GitHub 上，而不是聊天草稿里（这正是 Grok Bot 的核心卖点之一）。

凭据：GITHUB_TOKEN 环境变量或构造参数。**只进 Header，不落库不打印**。
传输层可注入（测试用 FakeTransport），默认 urllib。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from .base import Connector, ConnectorError


class GitHubConnector(Connector):
    name = "github"

    def __init__(self, token: str = None, base_url: str = None, transport=None):
        super().__init__()
        self.token = token if token is not None else os.environ.get("GITHUB_TOKEN", "")
        self.base_url = (base_url or os.environ.get(
            "GITHUB_BASE_URL", "https://api.github.com")).rstrip("/")
        # transport: callable(method, url, headers, body_bytes|None) -> (status, bytes)
        self._transport = transport

    # ---- 契约实现 ----
    def _configured(self) -> bool:
        return bool(self.token) or self._transport is not None

    def _actions(self) -> list:
        return [
            {"name": "get_repo", "description": "读取仓库信息",
             "params": {"owner": "str", "repo": "str"}},
            {"name": "list_issues", "description": "列 issue（默认 open）",
             "params": {"owner": "str", "repo": "str", "state": "open|closed|all"}},
            {"name": "create_issue", "description": "建 issue（ consequential：写操作）",
             "params": {"owner": "str", "repo": "str", "title": "str", "body": "str"}},
            {"name": "comment_issue", "description": "评论 issue（consequential：写操作）",
             "params": {"owner": "str", "repo": "str", "number": "int", "body": "str"}},
        ]

    # ---- 传输 ----
    def _request(self, method: str, path: str, body: dict = None):
        url = self.base_url + path
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "SecTrust-connector/0.8",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["Authorization"] = "Bearer %s" % self.token
        if self._transport is not None:
            status, raw = self._transport(method, url, headers, data)
            decoded = self._decode(raw)
            if status >= 400:
                # 注入传输层同样必须校验状态码——否则 404 会被当成成功数据
                raise ConnectorError("GitHub HTTP %s: %s"
                                     % (status, decoded.get("message", str(decoded)[:200])))
            return status, decoded
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, self._decode(resp.read())
        except urllib.error.HTTPError as e:
            raw = e.read()
            raise ConnectorError("GitHub HTTP %s: %s"
                                 % (e.code, self._decode(raw).get("message", raw[:200])))
        except urllib.error.URLError as e:
            raise ConnectorError("GitHub 网络错误: %s" % e.reason)

    @staticmethod
    def _decode(raw):
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8", "replace")
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"_raw": raw[:500]}

    # ---- action 派发 ----
    def _call(self, action: str, **params):
        owner, repo = params["owner"], params["repo"]
        if action == "get_repo":
            status, data = self._request("GET", "/repos/%s/%s" % (owner, repo))
            return {"status": status, "repo": data}
        if action == "list_issues":
            state = params.get("state", "open")
            status, data = self._request(
                "GET", "/repos/%s/%s/issues?state=%s" % (owner, repo, state))
            return {"status": status,
                    "issues": [{"number": i.get("number"), "title": i.get("title"),
                                "state": i.get("state")} for i in data]
                    if isinstance(data, list) else data}
        if action == "create_issue":
            status, data = self._request(
                "POST", "/repos/%s/%s/issues" % (owner, repo),
                body={"title": params["title"], "body": params.get("body", "")})
            return {"status": status, "number": data.get("number"),
                    "url": data.get("html_url")}
        if action == "comment_issue":
            status, data = self._request(
                "POST", "/repos/%s/%s/issues/%s/comments" % (owner, repo, params["number"]),
                body={"body": params["body"]})
            return {"status": status, "url": data.get("html_url")}
        raise ConnectorError("未实现的 action: %s" % action)   # 基类已挡，防御性保留
