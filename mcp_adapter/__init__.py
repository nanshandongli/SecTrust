"""
mcp_adapter —— 连接外部 MCP Server 的客户端/治理适配器（零依赖）。

与平台自身 `mcp_server.py`（被连接的 Server，暴露本平台任务登记处）职责相反：
本包是**去连接别人**的 Host，提供协议帧、双传输、Server 清册、TTL 熔断与
六道安全策略门，并把 EvidenceGate 挂到调用链上（B3）。

    from mcp_adapter import MCPServerRegistry, ServerSpec, SecureProxy
    reg = MCPServerRegistry()
    reg.register(ServerSpec(name="github", transport={"type": "stdio", ...},
                            allowed_domains=["api.github.com"]))
    proxy = SecureProxy(reg, approvals=ApprovalCenter(...))
    r = proxy.call_tool("github", "search_repositories", {"query": "mcp"})
    if r.ok:
        print(r.text)
    else:
        print(r.stage, r.reason)   # 如实拒绝，不伪造

诚实边界见各模块 docstring：无凭证不可用、fixture 明确标注、loopback 不冒充
真跨网、认证/语义审核不自证。
"""

from .protocol import (  # noqa: F401
    CallToolResult, FixtureData, LATEST_PROTOCOL_VERSION, Prompt, PromptArgument,
    Resource, RpcError, SUPPORTED_PROTOCOL_VERSIONS, Tool,
    make_error, make_notification, make_request, make_result, negotiated_version,
    parse_message, result_of, server_capabilities, text_content,
    INITIALIZE, NOTIFY_INITIALIZED, PING, TOOLS_LIST, TOOLS_CALL,
    RESOURCES_LIST, RESOURCES_READ, PROMPTS_LIST, PROMPTS_GET,
    CREDENTIAL_UNAVAILABLE, DOMAIN_BLOCKED, APPROVAL_REQUIRED,
    SECRET_SCAN_HIT, CIRCUIT_OPEN, FIXTURE_MODE,
)
from .transport import (  # noqa: F401
    HttpTransport, StdioTransport, Transport, TransportError,
)
from .registry import (  # noqa: F401
    MCPServerRegistry, RegistryError, ServerSpec,
)
from .breaker import TTLCircuitBreaker  # noqa: F401
from .proxy import (  # noqa: F401
    CredentialVault, ProxyResult, SecureProxy, scan_secrets,
)

__all__ = [
    "CallToolResult", "FixtureData", "LATEST_PROTOCOL_VERSION", "Prompt",
    "PromptArgument", "Resource", "RpcError", "SUPPORTED_PROTOCOL_VERSIONS",
    "Tool", "make_error", "make_notification", "make_request", "make_result",
    "negotiated_version", "parse_message", "result_of", "server_capabilities",
    "text_content", "INITIALIZE", "NOTIFY_INITIALIZED", "PING", "TOOLS_LIST",
    "TOOLS_CALL", "RESOURCES_LIST", "RESOURCES_READ", "PROMPTS_LIST",
    "PROMPTS_GET", "CREDENTIAL_UNAVAILABLE", "DOMAIN_BLOCKED",
    "APPROVAL_REQUIRED", "SECRET_SCAN_HIT", "CIRCUIT_OPEN", "FIXTURE_MODE",
    "HttpTransport", "StdioTransport", "Transport", "TransportError",
    "MCPServerRegistry", "RegistryError", "ServerSpec", "TTLCircuitBreaker",
    "CredentialVault", "ProxyResult", "SecureProxy", "scan_secrets",
]
