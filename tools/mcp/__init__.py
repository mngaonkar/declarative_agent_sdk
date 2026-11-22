"""
MCP tools for agents: the ``mcp_servers`` block of agent.yaml. See
docs/design/mcp.md.

Importing this package does not import the MCP client library; only
``McpHub.start`` does, so agents without MCP servers do not need the extra.
"""

from declarative_agent_sdk.tools.mcp.config import (
    McpConfigError,
    McpServerConfig,
    parse_mcp_servers,
)
from declarative_agent_sdk.tools.mcp.hub import McpError, McpHub, McpTool, tool_name
from declarative_agent_sdk.tools.mcp.adapters import (
    adk_tools,
    langchain_tools,
    register_lean,
    start_hub,
)

__all__ = [
    "McpConfigError",
    "McpError",
    "McpHub",
    "McpServerConfig",
    "McpTool",
    "parse_mcp_servers",
    "adk_tools",
    "langchain_tools",
    "register_lean",
    "start_hub",
    "tool_name",
]
