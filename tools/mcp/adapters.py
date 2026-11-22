"""
Hub tools presented to a runtime (MCP-RT-*, MCP-APPR-1).

Phase 1 is lean. ADK (MCP-RT-2) and deepagent (MCP-RT-3) come next and go in
this file too, over the same hub.
"""

from __future__ import annotations

from typing import Any, Dict, Set

from declarative_agent_sdk.core.agent_logging import get_logger
from declarative_agent_sdk.tools.mcp.hub import McpHub

logger = get_logger(__name__)


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
        schema = tool.input_schema or {}
        properties: Dict[str, Any] = dict(schema.get("properties") or {})
        required = [str(r) for r in (schema.get("required") or [])]

        def handler(args: Dict[str, Any], _name: str = tool.name) -> str:
            return hub.call(_name, args)

        registry.add(tool.name, tool.description, properties, required, handler)
        if not tool.needs_approval:
            auto.add(tool.name)
    return auto
