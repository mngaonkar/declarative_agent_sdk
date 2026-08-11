"""
Agent Factory — create BaseAgent instances from YAML / dict configuration.

The ``agent_framework`` key (alias: ``backend``) selects the implementation:
  - ``adk`` (default)             → AIAgent  (Google ADK)
  - ``deepagent`` / ``langchain`` → LangChainAIAgent  (deepagents + LangGraph)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

import yaml

from declarative_agent_sdk.agent_config import CommonAgentConfig, parse_common_config
from declarative_agent_sdk.agent_logging import get_logger
from declarative_agent_sdk.base_agent import BaseAgent
from declarative_agent_sdk.constants import DEFAULT_MODEL
from declarative_agent_sdk.tool_registry import ToolRegistry

logger = get_logger(__name__)

_ADK_ALIASES = frozenset({"adk", "google_adk", "google-adk"})
_DEEPAGENT_ALIASES = frozenset({
    "deepagent",
    "deepagents",
    "langchain",
    "langgraph",
})
_SUPPORTED_FRAMEWORKS = sorted(_ADK_ALIASES | _DEEPAGENT_ALIASES)


def resolve_agent_framework(config: Dict[str, Any]) -> str:
    """
    Read ``agent_framework`` (preferred) or legacy ``backend`` from *config*.

    Returns ``"adk"`` or ``"deepagent"``. Defaults to ``"adk"``.
    """
    raw = config.get("agent_framework")
    if raw is None:
        raw = config.get("backend", "adk")
    value = str(raw).strip().lower()

    if value in _ADK_ALIASES:
        return "adk"
    if value in _DEEPAGENT_ALIASES:
        return "deepagent"
    raise ValueError(
        f"Unknown agent_framework '{raw}'. "
        f"Supported values: {', '.join(_SUPPORTED_FRAMEWORKS)}."
    )


class AgentFactory:
    """Create BaseAgent instances from YAML / dict configuration."""

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

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
        logger.info(f"Agent '{name}' agent_framework={framework}")

        if framework == "adk":
            common = parse_common_config(
                config,
                framework=framework,
                default_model=DEFAULT_MODEL,
                default_provider=None,
            )
            common.tools = AgentFactory._resolve_tool_names(common.tools, name)
            return AgentFactory._create_adk_agent(common)

        common = parse_common_config(
            config,
            framework=framework,
            default_model="claude-sonnet-4-6",
            default_provider="anthropic",
        )
        common.tools = AgentFactory._resolve_tool_names(common.tools, name)
        common.middleware = AgentFactory._resolve_middleware(common.middleware)
        return AgentFactory._create_langchain_agent(common)

    # ------------------------------------------------------------------
    # Backend constructors (shared CommonAgentConfig)
    # ------------------------------------------------------------------

    @staticmethod
    def _create_adk_agent(common: CommonAgentConfig) -> BaseAgent:
        from declarative_agent_sdk.ai_agent import AIAgent
        from declarative_agent_sdk.model_factory import ModelFactory

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
        logger.info(f"Creating ADK agent '{common.name}'")
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
        from declarative_agent_sdk.langchain_ai_agent import LangChainAIAgent

        logger.info(
            f"Creating LangChain deep agent '{common.name}' "
            f"(provider={common.provider}, "
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

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

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
