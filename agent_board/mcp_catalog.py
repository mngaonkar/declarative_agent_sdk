"""
The board's MCP catalog (docs/design/mcp.md, MCP-CAT-*).

Server definitions live on the board; attaching one to an agent writes it into
that agent's agent.yaml under ``board_mcp_servers``, a key the board owns
entirely. Hand-written ``mcp_servers`` are never touched. Secrets stay
``${NAME}`` references here and are only ever written into agents' .env files.
"""

from __future__ import annotations

import copy
import os
from typing import Any, Dict, List, Optional

import yaml

from declarative_agent_sdk.agent_board import config as agent_config
from declarative_agent_sdk.tools.mcp.config import VAR_RE, McpConfigError, parse_mcp_servers

BOARD_KEY = "board_mcp_servers"
SPEC_KEYS = ("transport", "command", "args", "env", "cwd", "url", "headers", "tools",
             "approval", "timeout", "startup_timeout")
RUNNERS = ("npx", "uvx")

# MCP-CAT-4: starting points for a new entry.
PRESETS: List[Dict[str, Any]] = [
    {"name": "fetch", "label": "Fetch web pages",
     "spec": {"transport": "stdio", "command": "uvx", "args": ["mcp-server-fetch"],
              "startup_timeout": 120}},
    {"name": "time", "label": "Time and time zones",
     "spec": {"transport": "stdio", "command": "uvx", "args": ["mcp-server-time"],
              "startup_timeout": 120}},
    {"name": "git", "label": "Git, in the agent's workspace",
     "spec": {"transport": "stdio", "command": "uvx",
              "args": ["mcp-server-git", "--repository", "${WORKSPACE}"],
              "approval": "destructive", "startup_timeout": 120}},
    {"name": "filesystem", "label": "Files in the agent's workspace",
     "spec": {"transport": "stdio", "command": "npx",
              "args": ["-y", "@modelcontextprotocol/server-filesystem", "${WORKSPACE}"],
              "approval": "destructive", "startup_timeout": 120}},
    {"name": "github", "label": "GitHub (needs a token)",
     "spec": {"transport": "http", "url": "https://api.githubcopilot.com/mcp/",
              "headers": {"Authorization": "Bearer ${GITHUB_TOKEN}"},
              "approval": "destructive"}},
    {"name": "browser", "label": "Web browser (Playwright)",
     "spec": {"transport": "stdio", "command": "npx",
              "args": ["-y", "@playwright/mcp@latest", "--headless"],
              "startup_timeout": 180}},
]


def clean_spec(raw: Any) -> Dict[str, Any]:
    """Only the known keys, with empty values dropped."""
    if not isinstance(raw, dict):
        raise ValueError("an MCP server's settings must be a mapping")
    spec = {}
    for key in SPEC_KEYS:
        value = raw.get(key)
        if value in (None, "", [], {}):
            continue
        spec[key] = value
    return spec


def referenced_vars(spec: Dict[str, Any]) -> List[str]:
    """The ${NAME}s an entry uses, apart from ${WORKSPACE} (MCP-CAT-2)."""
    names = set(VAR_RE.findall(yaml.safe_dump(spec)))
    names.discard("WORKSPACE")
    return sorted(names)


def check(name: str, spec: Dict[str, Any]) -> Dict[str, Any]:
    """
    Refuse a bad entry with the MCP parser's own message (MCP-CAT-1).

    ${NAME}s are filled with a placeholder for the check only: their values
    belong to each agent's .env, not to the catalog.
    """
    placeholders = {var: "placeholder" for var in referenced_vars(spec)}
    try:
        parse_mcp_servers({name: spec}, placeholders, "/workspace")
    except McpConfigError as exc:
        raise ValueError(str(exc)) from exc
    return spec


def runner_for(spec: Dict[str, Any]) -> Optional[str]:
    """npx / uvx when the command is one of them (MCP-CAT-8)."""
    if spec.get("transport") != "stdio":
        return None
    command = os.path.basename(str(spec.get("command") or ""))
    return command if command in RUNNERS else None


def render(spec: Dict[str, Any], overrides: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """An entry as it goes into an agent's board_mcp_servers, overrides applied."""
    entry = copy.deepcopy(spec)
    for key in ("tools", "approval"):
        value = (overrides or {}).get(key)
        if value not in (None, "", []):
            entry[key] = value
    return entry


def board_servers(links: List[Dict[str, Any]]) -> Dict[str, Any]:
    """An agent's board_mcp_servers from its links (name → entry)."""
    return {link["name"]: render(link["spec"], link.get("overrides")) for link in links}


def apply(yaml_text: str, servers: Dict[str, Any]) -> str:
    """
    agent.yaml with board_mcp_servers set to ``servers`` (MCP-CAT-6).

    Re-emitted the way the Settings form does: leading comments kept, keys in
    their order. With nothing attached the key is removed.
    """
    data = agent_config.parse(yaml_text)
    if servers:
        data[BOARD_KEY] = servers
    else:
        data.pop(BOARD_KEY, None)
    body = yaml.safe_dump(data, sort_keys=False, default_flow_style=False, width=100)
    header = agent_config.leading_comments(yaml_text)
    return f"{header}\n\n{body}" if header else body


def current(yaml_text: str) -> Dict[str, Any]:
    """What an agent.yaml has under board_mcp_servers now."""
    value = agent_config.parse(yaml_text).get(BOARD_KEY)
    return value if isinstance(value, dict) else {}


def hand_written(yaml_text: str) -> List[str]:
    """Names under the agent's own mcp_servers (MCP-CAT-7, MCP-CAT-8)."""
    value = agent_config.parse(yaml_text).get("mcp_servers")
    return sorted(str(k) for k in value) if isinstance(value, dict) else []
