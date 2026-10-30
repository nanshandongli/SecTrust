"""参考 MCP Server 实现（样板，可独立运行）。

    python -m mcp_adapter.servers.github                 # stdio
    python -m mcp_adapter.servers.github --http --port 8765   # HTTP

它们只用于验证适配器链路；真实接入请换成对端厂商的 Server，并在
MCPServerRegistry 中登记其域名白名单 / 审批 / 凭证策略。
"""

from . import github  # noqa: F401

__all__ = ["github"]
