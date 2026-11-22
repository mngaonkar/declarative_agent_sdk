"""
The ``mcp_servers`` block of an agent's YAML (MCP-CFG-1..8, docs/design/mcp.md).

Pure parsing: nothing here imports the MCP client library, so an agent's
config can be checked on a machine that does not have the ``mcp`` extra.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

NAME_RE = re.compile(r"^[a-z0-9_-]+$")
VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
TRANSPORTS = ("stdio", "http", "sse")
APPROVALS = ("false", "true", "destructive")

DEFAULT_TIMEOUT = 60.0
DEFAULT_STARTUP_TIMEOUT = 30.0


class McpConfigError(ValueError):
    """The mcp_servers block is wrong; the message says where and how."""


@dataclass
class McpServerConfig:
    name: str
    transport: str
    command: str = ""
    args: List[str] = field(default_factory=list)
    env: Dict[str, str] = field(default_factory=dict)
    cwd: Optional[str] = None
    url: str = ""
    headers: Dict[str, str] = field(default_factory=dict)
    tools: Optional[List[str]] = None          # allowlist; None = all
    approval: str = "false"                    # false | true | destructive
    timeout: float = DEFAULT_TIMEOUT
    startup_timeout: float = DEFAULT_STARTUP_TIMEOUT

    def describe(self) -> str:
        """Where the server is, for logs. Never headers or env values (tokens)."""
        if self.transport == "stdio":
            return f"stdio: {' '.join([self.command, *self.args])}"
        return f"{self.transport}: {self.url}"


def _expand(value: Any, where: str, env: Mapping[str, str], workspace: str) -> Any:
    """Fill ${NAME} from the environment (MCP-CFG-3), in strings, lists and maps."""
    if isinstance(value, str):
        def swap(match: "re.Match[str]") -> str:
            name = match.group(1)
            if name == "WORKSPACE":
                return workspace
            if name not in env or env[name] == "":
                raise McpConfigError(
                    f"{where} uses ${{{name}}}, which is not set — add {name} to "
                    "the agent's .env")
            return env[name]
        return VAR_RE.sub(swap, value)
    if isinstance(value, list):
        return [_expand(item, f"{where}[{i}]", env, workspace) for i, item in enumerate(value)]
    if isinstance(value, dict):
        return {str(k): _expand(v, f"{where}.{k}", env, workspace) for k, v in value.items()}
    return value


def _strings(value: Any, where: str) -> List[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, (str, int, float)) for v in value):
        raise McpConfigError(f"{where} must be a list of strings")
    return [str(v) for v in value]


def _mapping(value: Any, where: str) -> Dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise McpConfigError(f"{where} must be a mapping of names to values")
    return {str(k): str(v) for k, v in value.items()}


def _seconds(value: Any, where: str, default: float) -> float:
    if value is None:
        return default
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        raise McpConfigError(f"{where} must be a number of seconds") from None
    if seconds <= 0:
        raise McpConfigError(f"{where} must be more than 0")
    return seconds


def parse_mcp_servers(raw: Any, env: Mapping[str, str], workspace: str,
                      base_dir: Optional[str] = None) -> List[McpServerConfig]:
    """
    The enabled servers of an ``mcp_servers`` block, checked and expanded.

    ``env`` fills ``${NAME}``; ``workspace`` fills ``${WORKSPACE}``; a relative
    ``cwd`` is taken against ``base_dir`` (the YAML's folder) when given.
    """
    if raw in (None, {}, []):
        return []
    if not isinstance(raw, dict):
        raise McpConfigError("mcp_servers must map server names to their settings")
    servers: List[McpServerConfig] = []
    for name, settings in raw.items():
        if isinstance(name, bool):
            # YAML reads a bare on / off / yes / no as true or false.
            raise McpConfigError(
                f"mcp_servers: a server is named {str(name).lower()}, which YAML read as "
                "a yes/no value (on, off, yes and no do that) — put the name in quotes")
        name = str(name)
        where = f"mcp_servers.{name}"
        if not NAME_RE.match(name):
            raise McpConfigError(
                f"{where}: a server name is lower-case letters, digits, '-' and '_'")
        if settings is None:
            settings = {}
        if not isinstance(settings, dict):
            raise McpConfigError(f"{where} must be a mapping of settings")
        if settings.get("enabled", True) is False:
            continue
        settings = _expand(settings, where, env, workspace)
        transport = str(settings.get("transport") or "").lower()
        if transport not in TRANSPORTS:
            raise McpConfigError(f"{where}.transport must be one of: {', '.join(TRANSPORTS)}")
        approval = str(settings.get("approval", False)).lower()
        if approval not in APPROVALS:
            raise McpConfigError(f"{where}.approval must be false, true or destructive")
        tools = settings.get("tools")
        server = McpServerConfig(
            name=name,
            transport=transport,
            command=str(settings.get("command") or ""),
            args=_strings(settings.get("args"), f"{where}.args"),
            env=_mapping(settings.get("env"), f"{where}.env"),
            cwd=str(settings["cwd"]) if settings.get("cwd") else None,
            url=str(settings.get("url") or ""),
            headers=_mapping(settings.get("headers"), f"{where}.headers"),
            tools=None if tools is None else _strings(tools, f"{where}.tools"),
            approval=approval,
            timeout=_seconds(settings.get("timeout"), f"{where}.timeout", DEFAULT_TIMEOUT),
            startup_timeout=_seconds(settings.get("startup_timeout"),
                                     f"{where}.startup_timeout", DEFAULT_STARTUP_TIMEOUT),
        )
        if transport == "stdio" and not server.command:
            raise McpConfigError(f"{where}: a stdio server needs a command")
        if transport != "stdio" and not re.match(r"^https?://", server.url):
            raise McpConfigError(f"{where}: an {transport} server needs an http(s) url")
        if server.cwd and base_dir and not Path(server.cwd).is_absolute():
            server.cwd = str((Path(base_dir) / server.cwd).resolve())
        servers.append(server)
    return servers
