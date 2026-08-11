"""
Shared agent configuration parsed from YAML / dict.

Used by AgentFactory so ADK and deepagent paths share one parse step.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from declarative_agent_sdk.constants import (
    DEFAULT_MODEL,
    SKILLS_DIRECTORY,
    WORKSPACE_DIRECTORY,
)


@dataclass
class CommonAgentConfig:
    """Fields common to every agent_framework."""

    name: str
    description: str = ""
    instruction_file: str = ""
    model: Any = DEFAULT_MODEL
    provider: Optional[str] = None
    endpoint_url: Optional[str] = None
    max_tokens: Optional[int] = None
    temperature: Optional[float] = None
    tools: List[Any] = field(default_factory=list)
    tools_approval_required: bool = True
    skills: Optional[List[str]] = None
    skills_directory: str = SKILLS_DIRECTORY
    workspace_directory: str = WORKSPACE_DIRECTORY
    output_key: Optional[str] = None
    publish_url: Optional[str] = None
    context_window: Optional[int] = None
    enable_truncation: bool = False
    truncate_strategy: str = "end"
    safety_margin: int = 100
    # deepagent-only extras (ignored by ADK)
    middleware: List[Any] = field(default_factory=list)
    framework: str = "adk"


def parse_common_config(
    config: Dict[str, Any],
    *,
    framework: str,
    default_model: Any = DEFAULT_MODEL,
    default_provider: Optional[str] = None,
) -> CommonAgentConfig:
    """Extract shared fields from a raw agent config dict."""
    endpoint = config.get("endpoint") or {}
    if not isinstance(endpoint, dict):
        endpoint = {}

    max_tokens = endpoint.get("max_tokens")
    if max_tokens is None:
        max_tokens = config.get("max_tokens")

    temperature = endpoint.get("temperature")
    if temperature is None:
        temperature = config.get("temperature")

    return CommonAgentConfig(
        name=config["name"],
        description=config.get("description", "") or "",
        instruction_file=config.get("instruction_file") or "",
        model=config.get("model", default_model),
        provider=config.get("provider", default_provider),
        endpoint_url=endpoint.get("url"),
        max_tokens=max_tokens,
        temperature=temperature,
        tools=list(config.get("tools") or []),
        tools_approval_required=config.get("tools_approval_required", True),
        skills=config.get("skills"),
        skills_directory=config.get("skills_directory", SKILLS_DIRECTORY),
        workspace_directory=config.get("workspace_directory", WORKSPACE_DIRECTORY),
        output_key=config.get("output_key"),
        publish_url=config.get("publish_url"),
        context_window=config.get("context_window"),
        enable_truncation=config.get("enable_truncation", False),
        truncate_strategy=config.get("truncate_strategy", "end"),
        safety_margin=config.get("safety_margin", 100),
        middleware=list(config.get("middleware") or []),
        framework=framework,
    )
