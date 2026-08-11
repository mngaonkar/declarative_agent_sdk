"""
Agent Factory — create BaseAgent instances from YAML / dict configuration.

Three peer runtimes — each owns its loop end-to-end (no mixing):

  - ``lean`` (default)  → LeanAIAgent
        Native ReAct + progressive skills (ESP-style)
  - ``adk``             → AIAgent
        Google ADK Runner + sessions + tool confirmation
  - ``deepagent``       → LangChainAIAgent
        deepagents / LangGraph create_deep_agent

Shared only: YAML config shape, BaseAgent/AgentEvent surface for Discord/A2A.
Do not nest one framework inside another.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

import yaml

from declarative_agent_sdk.core.agent_config import CommonAgentConfig, parse_common_config
from declarative_agent_sdk.core.agent_logging import get_logger
from declarative_agent_sdk.core.base_agent import BaseAgent
from declarative_agent_sdk.core.constants import DEFAULT_MODEL
from declarative_agent_sdk.tools.tool_registry import ToolRegistry

logger = get_logger(__name__)

_LEAN_ALIASES = frozenset({"lean", "simple", "esp", "esp32", "native"})
_ADK_ALIASES = frozenset({"adk", "google_adk", "google-adk"})
_DEEPAGENT_ALIASES = frozenset({
    "deepagent",
    "deepagents",
    "langchain",
    "langgraph",
})


def resolve_agent_framework(config: Dict[str, Any]) -> str:
    """
    Read ``agent_framework`` (preferred) or legacy ``backend``.

    Returns ``"lean"`` (default), ``"adk"``, or ``"deepagent"``.
    """
    raw = config.get("agent_framework")
    if raw is None:
        raw = config.get("backend", "lean")
    value = str(raw).strip().lower()

    if value in _LEAN_ALIASES:
        return "lean"
    if value in _ADK_ALIASES:
        return "adk"
    if value in _DEEPAGENT_ALIASES:
        return "deepagent"
    raise ValueError(
        f"Unknown agent_framework '{raw}'. "
        f"Supported values: lean (default), adk, deepagent — "
        f"each manages its own loop end-to-end."
    )


class AgentFactory:
    """Create a BaseAgent; framework choice is exclusive end-to-end."""

    @staticmethod
    def from_yaml_file(yaml_file_path: str) -> BaseAgent:
        """Load YAML; resolve relative paths against the file's directory."""
        yaml_path = Path(yaml_file_path).resolve()
        if not yaml_path.exists():
            raise FileNotFoundError(f"YAML file not found: {yaml_file_path}")
        with open(yaml_path, "r") as f:
            config = yaml.safe_load(f) or {}
        base_dir = yaml_path.parent
        for key in ("instruction_file", "workspace_directory", "skills_directory"):
            value = config.get(key)
            if isinstance(value, str) and value and not Path(value).is_absolute():
                config[key] = str((base_dir / value).resolve())
        return AgentFactory.from_dict(config)

    @staticmethod
    def from_yaml_string(yaml_string: str) -> BaseAgent:
        return AgentFactory.from_dict(yaml.safe_load(yaml_string) or {})

    @staticmethod
    def from_dict(config: Dict[str, Any]) -> BaseAgent:
        name = config.get("name")
        if not name:
            raise ValueError("Agent 'name' is required in configuration")

        framework = resolve_agent_framework(config)
        logger.info(f"Agent '{name}' agent_framework={framework} (end-to-end loop)")

        if framework == "lean":
            common = parse_common_config(
                config,
                framework=framework,
                default_model="gpt-4o-mini",
                default_provider="openai",
            )
            common.tools = AgentFactory._resolve_tool_names(common.tools, name)
            return AgentFactory._create_lean_agent(common)

        if framework == "adk":
            common = parse_common_config(
                config,
                framework=framework,
                default_model=DEFAULT_MODEL,
                default_provider=None,
            )
            common.tools = AgentFactory._resolve_tool_names(common.tools, name)
            return AgentFactory._create_adk_agent(common)

        # deepagent
        common = parse_common_config(
            config,
            framework=framework,
            default_model="claude-sonnet-4-6",
            default_provider="anthropic",
        )
        common.tools = AgentFactory._resolve_tool_names(common.tools, name)
        common.middleware = AgentFactory._resolve_middleware(common.middleware)
        return AgentFactory._create_langchain_agent(common)

    @staticmethod
    def _create_lean_agent(common: CommonAgentConfig) -> BaseAgent:
        from declarative_agent_sdk.agents.lean.agent import LeanAIAgent

        logger.info(
            f"Creating lean agent '{common.name}' "
            f"(provider={common.provider}, "
            f"tools_approval_required={common.tools_approval_required})"
        )
        return LeanAIAgent(
            name=common.name,
            description=common.description,
            instruction_file=common.instruction_file,
            tools=common.tools,
            tools_approval_required=common.tools_approval_required,
            skills_directory=common.skills_directory,
            workspace_directory=common.workspace_directory,
            skills=common.skills,
            model=common.model,
            provider=common.provider or "openai",
            max_output_tokens=common.max_tokens,
            endpoint_url=common.endpoint_url,
            temperature=common.temperature,
            publish_url=common.publish_url,
            output_key=common.output_key,
            context_window=common.context_window,
            enable_truncation=common.enable_truncation,
            truncate_strategy=common.truncate_strategy,
            safety_margin=common.safety_margin,
        )

    @staticmethod
    def _create_adk_agent(common: CommonAgentConfig) -> BaseAgent:
        from declarative_agent_sdk.agents.adk.agent import AIAgent
        from declarative_agent_sdk.models.model_factory import ModelFactory

        if not common.instruction_file:
            logger.warning(
                f"Agent '{common.name}' has no instruction_file; "
                "using skill instructions only"
            )

        model_kwargs: Dict[str, Any] = {}
        if common.max_tokens is not None:
            model_kwargs["max_tokens"] = common.max_tokens
        if common.temperature is not None:
            model_kwargs["temperature"] = common.temperature

        model = ModelFactory.create_model(
            model_name=common.model,
            provider=common.provider,
            endpoint_url=common.endpoint_url,
            **model_kwargs,
        )
        logger.info(f"Creating ADK agent '{common.name}' (ADK Runner end-to-end)")
        return AIAgent(
            name=common.name,
            description=common.description,
            instruction_file=common.instruction_file,
            tools=common.tools,
            tools_approval_required=common.tools_approval_required,
            output_key=common.output_key,
            model=model,
            skills_directory=common.skills_directory,
            skills=common.skills,
            context_window=common.context_window,
            max_output_tokens=common.max_tokens,
            enable_truncation=common.enable_truncation,
            truncate_strategy=common.truncate_strategy,
            safety_margin=common.safety_margin,
            workspace_directory=common.workspace_directory,
            publish_url=common.publish_url,
        )

    @staticmethod
    def _create_langchain_agent(common: CommonAgentConfig) -> BaseAgent:
        from declarative_agent_sdk.agents.deepagent.agent import LangChainAIAgent

        logger.info(
            f"Creating deepagent '{common.name}' "
            f"(provider={common.provider}, deepagents loop end-to-end, "
            f"tools_approval_required={common.tools_approval_required})"
        )
        return LangChainAIAgent(
            name=common.name,
            description=common.description,
            instruction_file=common.instruction_file,
            tools=common.tools,
            tools_approval_required=common.tools_approval_required,
            output_key=common.output_key,
            model=common.model,
            provider=common.provider or "anthropic",
            max_output_tokens=common.max_tokens,
            context_window=common.context_window,
            enable_truncation=common.enable_truncation,
            truncate_strategy=common.truncate_strategy,
            safety_margin=common.safety_margin,
            skills_directory=common.skills_directory,
            skills=common.skills,
            workspace_directory=common.workspace_directory,
            publish_url=common.publish_url,
            middleware=common.middleware or None,
        )

    @staticmethod
    def _resolve_tool_names(tool_names: List[Any], agent_name: str) -> List[Any]:
        if not tool_names:
            return []
        resolved = []
        for item in tool_names:
            if isinstance(item, str):
                try:
                    resolved.append(ToolRegistry.get(item))
                except ValueError:
                    logger.info(
                        f"Tool '{item}' not in global registry for agent "
                        f"'{agent_name}', will resolve from skills"
                    )
                    resolved.append(item)
            else:
                resolved.append(item)
        return resolved

    @staticmethod
    def _resolve_middleware(middleware_config: List[Any]) -> List[Any]:
        if not middleware_config:
            return []

        resolved = []
        for entry in middleware_config:
            if not isinstance(entry, str):
                resolved.append(entry)
                continue
            try:
                import deepagents as _da

                cls = getattr(_da, entry, None)
                if cls is None:
                    logger.warning(
                        f"Middleware '{entry}' not found in deepagents; skipping."
                    )
                    continue
                try:
                    resolved.append(cls())
                except TypeError:
                    logger.warning(
                        f"Middleware '{entry}' requires constructor arguments; "
                        "skipping YAML instantiation."
                    )
            except ImportError:
                logger.warning("deepagents not installed; skipping middleware resolution")

        return resolved
