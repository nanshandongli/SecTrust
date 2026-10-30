"""
protocol.py —— MCP JSON-RPC 2.0 协议面（零依赖）。

本模块只做"帧"的事：请求/响应/通知的构造与解析、标准错误码、MCP 三原语
（Tools / Resources / Prompts）的数据结构。不含任何 IO，便于离线单测。

与平台自身 `mcp_server.py` 的关系：那是**被连接的 Server**（暴露本平台的任务
登记处）；本包是**去连接别人的 Client/Host**。两者共用同一套 JSON-RPC 2.0
线格式，但职责相反，故各自独立实现，不互相 import。

诚实边界：
  · 只实现 MCP 规范里"只读 + 受控调用"所需的最小方法面，不假装支持
    sampling / roots / logging 等未实现的能力；
  · 应用层错误码（凭证不可用 / 域名拦截 / 需审批 / 密钥命中 / 熔断）使用
    -320xx 私有段，绝不与 JSON-RPC 标准码混淆，也绝不把失败伪装成成功结果。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple, Union

JSONRPC = "2.0"
# 支持的 MCP 协议版本（客户端声明其一则回它，否则回最新版）
SUPPORTED_PROTOCOL_VERSIONS: Tuple[str, ...] = (
    "2024-11-05", "2025-03-26", "2025-06-18",
)
LATEST_PROTOCOL_VERSION = SUPPORTED_PROTOCOL_VERSIONS[-1]

# ---------------------------------------------------------------- 方法常量
INITIALIZE = "initialize"
NOTIFY_INITIALIZED = "notifications/initialized"
PING = "ping"
TOOLS_LIST = "tools/list"
TOOLS_CALL = "tools/call"
RESOURCES_LIST = "resources/list"
RESOURCES_TEMPLATES_LIST = "resources/templates/list"
RESOURCES_READ = "resources/read"
PROMPTS_LIST = "prompts/list"
PROMPTS_GET = "prompts/get"

# ---------------------------------------------------------------- 错误码
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
# MCP 适配层私有错误码（应用层语义，不与标准码冲突）
CREDENTIAL_UNAVAILABLE = -32001
DOMAIN_BLOCKED = -32002
APPROVAL_REQUIRED = -32003
SECRET_SCAN_HIT = -32004
CIRCUIT_OPEN = -32005
FIXTURE_MODE = -32006   # 不是错误：标注"离线 fixture 数据"的语义码（由调用方解释）


class RpcError(Exception):
    """对端返回 JSON-RPC error，或本地协议校验失败。"""

    def __init__(self, code: int, message: str, data: Any = None):
        self.code = code
        self.message = message
        self.data = data
        super().__init__("[%s] %s" % (code, message))

    def to_dict(self) -> Dict:
        out = {"code": self.code, "message": self.message}
        if self.data is not None:
            out["data"] = self.data
        return out


class FixtureData(RpcError):
    """对端明确返回"离线 fixture 数据"——不是错误，但必须被上层如实标注。"""

    def __init__(self, note: str):
        super().__init__(FIXTURE_MODE, note)


# ---------------------------------------------------------------- 帧构造
def make_request(method: str, params: Optional[Dict] = None,
                 rid: Optional[int] = None) -> Dict:
    return {"jsonrpc": JSONRPC, "id": rid, "method": method,
            "params": params or {}}


def make_notification(method: str, params: Optional[Dict] = None) -> Dict:
    return {"jsonrpc": JSONRPC, "method": method, "params": params or {}}


def make_result(rid: Optional[int], result: Any) -> Dict:
    return {"jsonrpc": JSONRPC, "id": rid, "result": result}


def make_error(rid: Optional[int], code: int, message: str,
               data: Any = None) -> Dict:
    err: Dict = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": JSONRPC, "id": rid, "error": err}


def parse_message(raw: Union[str, bytes]) -> Dict:
    """解析一行/一段 JSON-RPC 消息，校验最小结构。

    失败一律 RpcError(PARSE_ERROR)，绝不返回"看起来像成功"的半成品。
    """
    try:
        text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        msg = json.loads(text)
    except (UnicodeDecodeError, ValueError, AttributeError) as e:
        raise RpcError(PARSE_ERROR, "非法 JSON 帧：%s" % e)
    if not isinstance(msg, dict):
        raise RpcError(INVALID_REQUEST, "JSON-RPC 消息必须是对象")
    if msg.get("jsonrpc") != JSONRPC:
        raise RpcError(INVALID_REQUEST, "缺少或非法的 jsonrpc 字段（应为 '2.0'）")
    return msg


def is_response_to(msg: Dict, rid: Optional[int]) -> bool:
    """该消息是否是 rid 的响应（通知/其他 id 的消息返回 False）。"""
    return (msg.get("id") == rid
            and ("result" in msg or "error" in msg))


def result_of(msg: Dict) -> Any:
    """从响应里取 result；若含 error 则抛 RpcError（含私有码透传）。"""
    if "error" in msg:
        err = msg["error"] or {}
        raise RpcError(int(err.get("code", INTERNAL_ERROR)),
                       str(err.get("message", "未知对端错误")),
                       err.get("data"))
    if "result" not in msg:
        raise RpcError(INVALID_REQUEST, "响应既无 result 也无 error")
    return msg["result"]


# ---------------------------------------------------------------- 三原语数据结构
@dataclass
class Tool:
    name: str
    description: str = ""
    input_schema: Dict = field(default_factory=lambda: {"type": "object",
                                                       "properties": {}})

    def to_dict(self) -> Dict:
        return {"name": self.name, "description": self.description,
                "inputSchema": self.input_schema}


@dataclass
class Resource:
    uri: str
    name: str = ""
    description: str = ""
    mime_type: str = "text/plain"

    def to_dict(self) -> Dict:
        return {"uri": self.uri, "name": self.name or self.uri,
                "description": self.description, "mimeType": self.mime_type}


@dataclass
class PromptArgument:
    name: str
    description: str = ""
    required: bool = False


@dataclass
class Prompt:
    name: str
    description: str = ""
    arguments: List[PromptArgument] = field(default_factory=list)

    def to_dict(self) -> Dict:
        return {"name": self.name, "description": self.description,
                "arguments": [{"name": a.name, "description": a.description,
                               "required": a.required} for a in self.arguments]}


@dataclass
class CallToolResult:
    """tools/call 的结构化结果（MCP content 块 + 可选结构化负载）。"""

    content: List[Dict] = field(default_factory=list)
    is_error: bool = False
    structured: Optional[Dict] = None
    fixture: bool = False
    note: str = ""

    @property
    def text(self) -> str:
        return "\n".join(str(c.get("text", ""))
                         for c in self.content if c.get("type") == "text")

    def to_dict(self) -> Dict:
        out: Dict = {"content": self.content, "isError": self.is_error}
        if self.structured is not None:
            out["structuredContent"] = self.structured
        if self.fixture:
            out["_fixture"] = True
            out["_note"] = self.note
        return out


def text_content(text: str) -> List[Dict]:
    return [{"type": "text", "text": text}]


def server_capabilities() -> Dict:
    """本适配器连接对端时**声明自己支持的**能力（诚实：只列已实现的）。"""
    return {
        "tools": {"listChanged": False},
        "resources": {"subscribe": False, "listChanged": False},
        "prompts": {"listChanged": False},
    }


def negotiated_version(client_version: Optional[str]) -> str:
    return (client_version if client_version in SUPPORTED_PROTOCOL_VERSIONS
            else LATEST_PROTOCOL_VERSION)
