"""
Hub tools presented to each runtime (MCP-RT-1..3, MCP-APPR-1).

All three runtimes share one hub per agent (D1); these adapters only change
the shape a tool is handed over in. The runtime frameworks are imported inside
the functions, so a lean agent never imports ADK or LangChain for MCP.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from declarative_agent_sdk.core.agent_logging import get_logger
from declarative_agent_sdk.tools.mcp.config import parse_mcp_servers
from declarative_agent_sdk.tools.mcp.hub import McpHub, McpTool

logger = get_logger(__name__)


def start_hub(mcp_servers: Optional[Dict[str, Any]], workspace: str,
              config_dir: Optional[str] = None) -> Optional[McpHub]:
    """
    The started hub for an agent's ``mcp_servers``, or None when it has none.

    A bad block raises McpConfigError (the agent does not build, and says
    why); a server that will not start is left out (MCP-LIFE-1).
    """
    if not mcp_servers:
        return None
    workspace = str(Path(workspace).resolve())
    configs = parse_mcp_servers(mcp_servers, os.environ, workspace, base_dir=config_dir)
    if not configs:
        return None
    return McpHub(configs, workspace).start()


def _schema(tool: McpTool) -> Dict[str, Any]:
    schema = dict(tool.input_schema or {})
    schema.setdefault("type", "object")
    schema.setdefault("properties", {})
    return schema


# --------------------------------------------------------------------- lean

def register_lean(hub: McpHub, registry: Any) -> Set[str]:
    """
    Add every hub tool to a LeanToolRegistry (MCP-RT-1).

    Returns the names that need no approval (MCP-APPR-1), for the loop's
    auto-approve set. A name the registry already has (a core tool) is left
    alone: core tools win (MCP-TOOL-2).
    """
    taken = set(registry.names())
    auto: Set[str] = set()
    for tool in hub.tools:
        if tool.name in taken:
            logger.warning(f"[mcp] {tool.name} clashes with an existing tool; skipped")
            continue
        schema = _schema(tool)
        properties: Dict[str, Any] = dict(schema.get("properties") or {})
        required = [str(r) for r in (schema.get("required") or [])]

        def handler(args: Dict[str, Any], _name: str = tool.name) -> str:
            return hub.call(_name, args)

        registry.add(tool.name, tool.description, properties, required, handler)
        if not tool.needs_approval:
            auto.add(tool.name)
    return auto


# ---------------------------------------------------------------------- adk

def adk_tools(hub: McpHub, approval_required: bool, taken: Set[str]) -> List[Any]:
    """
    Hub tools as ADK tools (MCP-RT-2).

    Each declares the server's JSON schema as-is, and asks for confirmation
    the way ADK's own FunctionTool does, when the agent requires approval and
    the server's ``approval`` says this tool needs it.
    """
    from google.adk.tools import BaseTool
    from google.genai import types

    class AdkMcpTool(BaseTool):
        def __init__(self, tool: McpTool, confirm: bool) -> None:
            super().__init__(name=tool.name, description=tool.description)
            self._tool = tool
            self._confirm = confirm

        def _get_declaration(self):
            return types.FunctionDeclaration(
                name=self.name, description=self.description,
                parameters_json_schema=_schema(self._tool))

        async def run_async(self, *, args: Dict[str, Any], tool_context: Any) -> Any:
            if self._confirm:
                if not tool_context.tool_confirmation:
                    tool_context.request_confirmation(
                        hint=(f"Please approve or reject the tool call {self.name}() by "
                              "responding with a FunctionResponse with an expected "
                              "ToolConfirmation payload."))
                    tool_context.actions.skip_summarization = True
                    return {"error": "This tool call requires confirmation, please "
                                     "approve or reject."}
                if not tool_context.tool_confirmation.confirmed:
                    return {"error": "This tool call is rejected."}
            return {"result": await hub.acall(self.name, args or {})}

    tools = []
    for tool in hub.tools:
        if tool.name in taken:
            logger.warning(f"[mcp] {tool.name} clashes with an existing tool; skipped")
            continue
        tools.append(AdkMcpTool(tool, confirm=approval_required and tool.needs_approval))
    return tools


# ---------------------------------------------------------------- deepagent

def langchain_tools(hub: McpHub, taken: Set[str]) -> "tuple[List[Any], Set[str]]":
    """
    Hub tools as LangChain StructuredTools (MCP-RT-3), and the names that need
    no approval, to leave out of deepagents' ``interrupt_on`` (MCP-APPR-1).
    """
    from langchain_core.tools import StructuredTool

    tools, auto = [], set()
    for tool in hub.tools:
        if tool.name in taken:
            logger.warning(f"[mcp] {tool.name} clashes with an existing tool; skipped")
            continue

        def call(_name: str = tool.name, **kwargs: Any) -> str:
            return hub.call(_name, kwargs)

        async def acall(_name: str = tool.name, **kwargs: Any) -> str:
            return await hub.acall(_name, kwargs)

        tools.append(StructuredTool.from_function(
            func=call, coroutine=acall, name=tool.name, description=tool.description,
            args_schema=_schema(tool), infer_schema=False))
        if not tool.needs_approval:
            auto.add(tool.name)
    return tools, auto
