"""
Reading, validating and rewriting an installed agent's ``agent.yaml``.

Two ways to save, and the difference matters:

  - **properties** — merge a few keys into the file. Rewriting YAML through
    ``safe_dump`` loses the comments install.sh wrote, so the merge keeps the
    leading comment block and re-emits the rest.
  - **raw** — the text is written exactly as given, comments and all.

Either way the file is validated before it leaves here. A bad agent.yaml does
not fail loudly at save time; it fails when supervisord next starts the agent
and the process dies in a restart loop.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import yaml

from declarative_agent_sdk.core.agent_logging import get_logger
from declarative_agent_sdk.tools.tool_registry import ToolRegistry

logger = get_logger(__name__)

FRAMEWORKS = ("lean", "adk", "deepagent")

# What the Agents screen offers as a form. Adding a row here is all it takes to
# make another property editable; the UI renders whatever this says.
EDITABLE_PROPERTIES: List[Dict[str, Any]] = [
    {"key": "description", "label": "Description", "type": "text",
     "help": "One line, shown on the agent card."},
    {"key": "agent_framework", "label": "Framework", "type": "choice",
     "choices": list(FRAMEWORKS),
     "help": "Changing this swaps the whole runtime loop."},
    {"key": "provider", "label": "Provider", "type": "choice",
     "choices": ["openai", "google", "anthropic", "vllm", "litellm"],
     "help": "A new provider usually needs a key in the agent's own .env."},
    {"key": "model", "label": "Model", "type": "text"},
    {"key": "endpoint.url", "label": "Endpoint URL", "type": "text",
     "help": "OpenAI-compatible base URL (vLLM, Ollama). Empty removes it."},
    {"key": "temperature", "label": "Temperature", "type": "number", "step": "0.05"},
    {"key": "max_tokens", "label": "Max output tokens", "type": "int"},
    {"key": "request_timeout", "label": "Model call timeout (s)", "type": "number",
     "step": "10",
     "help": "One model call, not one turn. A local model asked for something "
             "long needs several minutes; the default is 300."},
    {"key": "tools", "label": "Tools", "type": "list",
     "help": "Comma-separated. The list is an allowlist of non-core tools."},
    {"key": "tools_approval_required", "label": "Require tool approval", "type": "bool"},
    {"key": "instruction_file", "label": "Instruction file", "type": "text"},
    {"key": "skills_directory", "label": "Skills directory", "type": "text"},
    {"key": "workspace_directory", "label": "Workspace directory", "type": "text"},
    {"key": "max_tool_iterations", "label": "Max tool iterations", "type": "int"},
    {"key": "history_limit", "label": "History limit", "type": "int"},
]

_EDITABLE_KEYS = {prop["key"] for prop in EDITABLE_PROPERTIES}


class ConfigError(ValueError):
    """The edited config is not usable. Surfaced to the UI as a 400."""


# ---------------------------------------------------------------------------
# read side
# ---------------------------------------------------------------------------

def parse(yaml_text: str) -> Dict[str, Any]:
    try:
        data = yaml.safe_load(yaml_text or "") or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError("agent.yaml must be a mapping of keys to values")
    return data


def properties(data: Dict[str, Any]) -> Dict[str, Any]:
    """Flatten the editable subset for the form (``endpoint.url`` included)."""
    out: Dict[str, Any] = {}
    for prop in EDITABLE_PROPERTIES:
        key = prop["key"]
        if "." in key:
            parent, child = key.split(".", 1)
            section = data.get(parent)
            value = section.get(child) if isinstance(section, dict) else None
        else:
            value = data.get(key)
        if prop["type"] == "list" and isinstance(value, (list, tuple)):
            value = ", ".join(str(item) for item in value)
        out[key] = "" if value is None else value
    return out


def leading_comments(yaml_text: str) -> str:
    """The comment block at the top of the file, so a merge can keep it."""
    kept = []
    for line in (yaml_text or "").splitlines():
        if line.startswith("#") or not line.strip():
            kept.append(line)
            continue
        break
    return "\n".join(kept).rstrip("\n")


# ---------------------------------------------------------------------------
# write side
# ---------------------------------------------------------------------------

def _coerce(prop: Dict[str, Any], value: Any) -> Any:
    kind = prop["type"]
    if kind == "bool":
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("1", "true", "yes", "on")
    text = str(value).strip() if value is not None else ""
    if text == "":
        return None                      # empty means "remove this key"
    if kind == "int":
        try:
            return int(float(text))
        except ValueError as exc:
            raise ConfigError(f"{prop['label']} must be a whole number") from exc
    if kind == "number":
        try:
            return float(text)
        except ValueError as exc:
            raise ConfigError(f"{prop['label']} must be a number") from exc
    if kind == "list":
        if isinstance(value, (list, tuple)):
            items = [str(item).strip() for item in value]
        else:
            items = [part.strip() for part in text.replace("\n", ",").split(",")]
        return [item for item in items if item]
    if kind == "choice" and text not in prop.get("choices", []):
        raise ConfigError(f"{prop['label']} must be one of {', '.join(prop['choices'])}")
    return text


def merge(yaml_text: str, updates: Dict[str, Any]) -> str:
    """
    Apply form values to an existing agent.yaml and re-emit it.

    An empty value removes the key, which is how you drop an ``endpoint`` when
    moving an agent off a local server back to a hosted provider.
    """
    data = parse(yaml_text)
    by_key = {prop["key"]: prop for prop in EDITABLE_PROPERTIES}

    for key, raw in (updates or {}).items():
        if key not in _EDITABLE_KEYS:
            continue                     # ignore anything the form made up
        value = _coerce(by_key[key], raw)
        if "." in key:
            parent, child = key.split(".", 1)
            section = data.get(parent)
            section = dict(section) if isinstance(section, dict) else {}
            if value is None:
                section.pop(child, None)
            else:
                section[child] = value
            if section:
                data[parent] = section
            else:
                data.pop(parent, None)
        elif value is None:
            data.pop(key, None)
        else:
            data[key] = value

    body = yaml.safe_dump(data, sort_keys=False, default_flow_style=False, width=100)
    header = leading_comments(yaml_text)
    return f"{header}\n\n{body}" if header else body


def validate(data: Dict[str, Any], expected_name: str) -> List[str]:
    """
    Hard errors raise; everything else comes back as warnings for the UI.

    Renaming is refused on purpose: the name keys the install directory, the
    supervisord program and this control plane's own record of the agent, none
    of which this edit can move.
    """
    name = data.get("name")
    if not name:
        raise ConfigError("'name' is required in agent.yaml")
    if expected_name and str(name) != expected_name:
        raise ConfigError(
            f"'name' cannot be changed here (it is '{expected_name}' on disk and in "
            f"supervisord). Create a new agent instead of renaming this one."
        )

    framework = str(data.get("agent_framework") or data.get("backend") or "lean").lower()
    if framework not in FRAMEWORKS and framework not in (
        "simple", "esp", "native", "google_adk", "deepagents", "langchain", "langgraph"
    ):
        raise ConfigError(
            f"unknown agent_framework '{framework}' — use one of {', '.join(FRAMEWORKS)}"
        )

    warnings: List[str] = []

    tools = data.get("tools")
    if tools is not None:
        if not isinstance(tools, (list, tuple)):
            raise ConfigError("'tools' must be a list")
        ToolRegistry.register_built_in_tools()
        known = set(ToolRegistry.list_available())
        for tool in tools:
            if str(tool) not in known:
                warnings.append(
                    f"tool '{tool}' is not a registered built-in — it will be skipped "
                    f"unless a skill on that machine provides it"
                )

    if data.get("provider") and str(data["provider"]).lower() == "vllm":
        endpoint = data.get("endpoint")
        if not (isinstance(endpoint, dict) and endpoint.get("url")):
            warnings.append("provider 'vllm' usually needs endpoint.url set")

    if data.get("tools_approval_required") is False:
        tool_list = [str(t) for t in (tools or [])]
        if any(t in ("exec_command", "exec_async") for t in tool_list):
            warnings.append(
                "approval is off and exec_command is enabled — this agent will run "
                "shell commands without asking"
            )

    return warnings


def prepare(
    current_yaml: str,
    expected_name: str,
    raw: Optional[str] = None,
    updates: Optional[Dict[str, Any]] = None,
) -> Tuple[str, List[str]]:
    """Produce the text to write, plus warnings. Raises ConfigError if unusable."""
    if raw is not None:
        text = raw
    elif updates:
        text = merge(current_yaml, updates)
    else:
        raise ConfigError("nothing to save: pass properties or raw YAML")
    return text, validate(parse(text), expected_name)
