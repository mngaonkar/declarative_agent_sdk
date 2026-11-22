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
import re
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


# ------------------------------------------------------------------ registry
# The official MCP Registry (registry.modelcontextprotocol.io) lists servers
# published from GitHub, npm, PyPI and Docker. An entry is turned into ready
# catalog specs (MCP-CAT-13); nothing in it is trusted to run as given — the
# user reviews the filled-in form before saving.

REGISTRY_URL = "https://registry.modelcontextprotocol.io/v0/servers"


def _slug(registry_name: str) -> str:
    """A catalog name from a registry name: io.github.owner/some-server → some-server."""
    tail = str(registry_name or "").rsplit("/", 1)[-1].lower()
    slug = re.sub(r"[^a-z0-9_-]+", "-", tail).strip("-_")
    return slug[:40] or "server"


def _var(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", str(name or "")).upper().strip("_") or "VALUE"


def _arguments(items: Any) -> List[str]:
    """Registry arguments as argv: positional values, named --flag value pairs.
    A required one with no value gets a <hint> placeholder to fill in."""
    out: List[str] = []
    for arg in items or []:
        if not isinstance(arg, dict):
            continue
        value = arg.get("value", arg.get("default"))
        missing = value in (None, "")
        placeholder = f"<{arg.get('valueHint') or arg.get('name') or 'value'}>"
        if arg.get("type") == "named" and arg.get("name"):
            out.append(str(arg["name"]))
            if not missing:
                out.append(str(value))
            elif arg.get("isRequired") or arg.get("valueHint"):
                out.append(placeholder)
        elif not missing:
            out.append(str(value))
        elif arg.get("isRequired"):
            out.append(placeholder)
    return out


def _variables(items: Any) -> List[Dict[str, Any]]:
    found = []
    for item in items or []:
        if isinstance(item, dict) and item.get("name"):
            found.append({"name": _var(item["name"]),
                          "description": str(item.get("description") or ""),
                          "required": bool(item.get("isRequired")),
                          "secret": bool(item.get("isSecret")),
                          "default": item.get("default")})
    return found


def _from_package(package: Dict[str, Any]) -> Dict[str, Any]:
    kind = str(package.get("registryType") or "")
    identifier = str(package.get("identifier") or "")
    version = str(package.get("version") or "")
    transport = (package.get("transport") or {}).get("type") or "stdio"
    variables = _variables(package.get("environmentVariables"))
    labels = {"npm": "npm package, run with npx", "pypi": "PyPI package, run with uvx",
              "oci": "Docker image, run with docker"}
    option: Dict[str, Any] = {"kind": kind, "label": labels.get(kind, f"{kind} package"),
                              "vars": variables}
    if transport != "stdio":
        option["unsupported"] = (f"this package serves {transport} on the machine it runs "
                                 "on; start it yourself and add its URL as an http server")
        return option
    if not identifier or kind not in labels:
        option["unsupported"] = f"{kind or 'this'} packages are not supported"
        return option
    # Only what the server needs: required and secret variables, as ${NAME}.
    env = {v["name"]: "${%s}" % v["name"] for v in variables if v["required"] or v["secret"]}
    runtime = _arguments(package.get("runtimeArguments"))
    arguments = _arguments(package.get("packageArguments"))
    if kind == "npm":
        command = "npx"
        args = runtime or ["-y"]
        if "-y" not in args:
            args = ["-y", *args]
        args = [*args, f"{identifier}@{version}" if version else identifier, *arguments]
    elif kind == "pypi":
        command = "uvx"
        args = [*runtime, f"{identifier}=={version}" if version else identifier, *arguments]
    else:
        command = "docker"
        passed = [part for name in env for part in ("-e", name)]
        args = ["run", "-i", "--rm", *passed, *runtime, identifier, *arguments]
    spec: Dict[str, Any] = {"transport": "stdio", "command": command, "args": args,
                            "startup_timeout": 180}
    if env:
        spec["env"] = env
    option["spec"] = spec
    return option


def _from_remote(remote: Dict[str, Any], slug: str) -> Dict[str, Any]:
    kind = str(remote.get("type") or "")
    transport = {"streamable-http": "http", "sse": "sse"}.get(kind)
    url = str(remote.get("url") or "")
    option: Dict[str, Any] = {"kind": "remote", "label": f"hosted server ({kind or 'http'})",
                              "vars": []}
    if not transport or not re.match(r"^https?://", url):
        option["unsupported"] = f"a {kind or 'remote'} server without an http(s) URL"
        return option
    headers = {}
    for header in remote.get("headers") or []:
        if not isinstance(header, dict) or not header.get("name"):
            continue
        name = str(header["name"])
        if header.get("value"):
            headers[name] = str(header["value"])
            continue
        if not (header.get("isRequired") or header.get("isSecret")):
            continue
        var = f"{_var(slug)}_TOKEN" if name.lower() == "authorization" else _var(name)
        headers[name] = "Bearer ${%s}" % var if name.lower() == "authorization" else "${%s}" % var
        option["vars"].append({"name": var, "description": str(header.get("description") or ""),
                               "required": bool(header.get("isRequired")), "secret": True,
                               "default": None})
    spec: Dict[str, Any] = {"transport": transport, "url": url}
    if headers:
        spec["headers"] = headers
    option["spec"] = spec
    return option


def registry_entry(item: Dict[str, Any]) -> Dict[str, Any]:
    """One registry result as the MCP tab shows it, with ready specs (MCP-CAT-13)."""
    server = item.get("server") if isinstance(item.get("server"), dict) else item
    meta = ((item.get("_meta") or {}).get("io.modelcontextprotocol.registry/official") or {})
    slug = _slug(server.get("name"))
    options = [_from_package(p) for p in server.get("packages") or [] if isinstance(p, dict)]
    options += [_from_remote(r, slug) for r in server.get("remotes") or [] if isinstance(r, dict)]
    for option in options:
        if "spec" in option:
            try:
                check(slug, option["spec"])
            except ValueError as exc:
                option.pop("spec")
                option["unsupported"] = str(exc)
    repository = (server.get("repository") or {}).get("url") or ""
    website = server.get("websiteUrl") or ""
    return {
        "name": str(server.get("name") or ""),
        "slug": slug,
        "title": str(server.get("title") or ""),
        "description": str(server.get("description") or ""),
        "version": str(server.get("version") or ""),
        "repository": repository if re.match(r"^https?://", str(repository)) else "",
        "website": website if re.match(r"^https?://", str(website)) else "",
        "status": str(meta.get("status") or ""),
        "options": options,
    }
