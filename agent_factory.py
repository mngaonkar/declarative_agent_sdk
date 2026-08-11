"""
Agent Factory — create BaseAgent instances from YAML / dict configuration.

The ``backend`` key in the config selects the implementation:
  - ``adk`` (default) → AIAgent  (Google ADK + Gemini/vLLM/OpenAI)
  - ``langchain``      → LangChainAIAgent  (deepagents + LangGraph)
"""

import yaml
from typing import Any, Dict, List, Optional
from pathlib import Path

from declarative_agent_sdk.base_agent import BaseAgent
from declarative_agent_sdk.constants import DEFAULT_MODEL, SKILLS_DIRECTORY, WORKSPACE_DIRECTORY
from declarative_agent_sdk.tool_registry import ToolRegistry
from declarative_agent_sdk.agent_logging import get_logger

logger = get_logger(__name__)


class AgentFactory:
    """
    Factory class that creates BaseAgent instances from YAML configuration.

    The ``backend`` field in the config selects the agent implementation.
    Omitting it defaults to ``adk``.

    ── ADK agent (backend: adk) ──────────────────────────────────────────

        name: search_agent
        description: Searches the web and summarises results
        instruction_file: agents/search/instructions.md
        model: gemini-2.0-flash-exp
        # optional — defaults to adk
        backend: adk

    With a local vLLM server:

        name: local_agent
        instruction_file: agents/local/instructions.md
        model: Qwen/Qwen3-4B-Thinking-FP8
        provider: vllm
        endpoint:
          url: http://localhost:8000/v1
          max_tokens: 3000
          temperature: 0.7

    With token truncation:

        name: long_ctx_agent
        instruction_file: agents/long/instructions.md
        model: Qwen/Qwen3-4B-Thinking-FP8
        provider: vllm
        endpoint:
          url: http://localhost:8000/v1
        enable_truncation: true
        context_window: 20384
        truncate_strategy: end
        safety_margin: 100

    ── LangChain / deepagents (backend: langchain) ───────────────────────

        name: deep_agent
        backend: langchain
        description: Deep research agent backed by deepagents
        instruction_file: agents/deep/instructions.md
        model: claude-3-5-sonnet-20241022
        provider: anthropic          # anthropic | openai | google | google_genai | vllm | litellm
        skills:
          - skills/search
        tools:
          - my_custom_tool
        max_tokens: 4096             # maps to max_output_tokens

    ── Common optional fields (both backends) ────────────────────────────

        output_key: agent_response
        tools_approval_required: false
        skills_directory: skills
        workspace_directory: workspace
        publish_url: http://my-host:8000/
        enable_truncation: false
        context_window: 20384
        truncate_strategy: end
        safety_margin: 100
    """

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @staticmethod
    def from_yaml_file(yaml_file_path: str) -> BaseAgent:
        """Create a BaseAgent from a YAML configuration file."""
        yaml_path = Path(yaml_file_path)
        if not yaml_path.exists():
            raise FileNotFoundError(f"YAML file not found: {yaml_file_path}")
        with open(yaml_path, "r") as f:
            config = yaml.safe_load(f)
        return AgentFactory.from_dict(config)

    @staticmethod
    def from_yaml_string(yaml_string: str) -> BaseAgent:
        """Create a BaseAgent from a YAML string."""
        config = yaml.safe_load(yaml_string)
        return AgentFactory.from_dict(config)

    @staticmethod
    def from_dict(config: Dict[str, Any]) -> BaseAgent:
        """
        Create a BaseAgent from a configuration dictionary.

        Required keys: ``name``
        Routing key:   ``backend`` — ``"adk"`` (default) or ``"langchain"``
        """
        name = config.get("name")
        if not name:
            raise ValueError("Agent 'name' is required in configuration")

        backend = config.get("backend", "adk").lower()

        if backend == "adk":
            return AgentFactory._create_adk_agent(config)
        elif backend in ("langchain", "deepagents"):
            return AgentFactory._create_langchain_agent(config)
        else:
            raise ValueError(
                f"Unknown backend '{backend}'. Supported values: 'adk', 'langchain'."
            )

    # ------------------------------------------------------------------
    # Private: ADK backend
    # ------------------------------------------------------------------

    @staticmethod
    def _create_adk_agent(config: Dict[str, Any]) -> BaseAgent:
        """Build and return an AIAgent from a config dict."""
        from declarative_agent_sdk.ai_agent import AIAgent
        from declarative_agent_sdk.model_factory import ModelFactory

        name = config["name"]
        instruction_file = config.get("instruction_file")
        if not instruction_file:
            logger.warning(f"Agent '{name}' has no instruction_file; using skill instructions only")

        description = config.get("description", "")
        output_key = config.get("output_key")
        model_name = config.get("model", DEFAULT_MODEL)
        skills = config.get("skills")
        skills_directory = config.get("skills_directory", SKILLS_DIRECTORY)
        workspace_directory = config.get("workspace_directory", WORKSPACE_DIRECTORY)
        provider = config.get("provider")
        endpoint_config = config.get("endpoint") or {}
        tools_approval_required = config.get("tools_approval_required", True)
        publish_url = config.get("publish_url")

        endpoint_url = endpoint_config.get("url") if isinstance(endpoint_config, dict) else None
        max_tokens = (
            endpoint_config.get("max_tokens") if isinstance(endpoint_config, dict) else None
        ) or config.get("max_tokens")
        temperature = (
            endpoint_config.get("temperature") if isinstance(endpoint_config, dict) else None
        ) or config.get("temperature")

        context_window = config.get("context_window")
        enable_truncation = config.get("enable_truncation", False)
        truncate_strategy = config.get("truncate_strategy", "end")
        safety_margin = config.get("safety_margin", 100)

        model_kwargs: Dict[str, Any] = {}
        if max_tokens is not None:
            model_kwargs["max_tokens"] = max_tokens
        if temperature is not None:
            model_kwargs["temperature"] = temperature

        model = ModelFactory.create_model(
            model_name=model_name,
            provider=provider,
            endpoint_url=endpoint_url,
            **model_kwargs,
        )
        logger.debug(f"ADK model for agent '{name}': {model}")

        tools = AgentFactory._resolve_tool_names(config.get("tools", []), name)

        logger.info(f"Creating ADK agent '{name}'")
        return AIAgent(
            name=name,
            description=description,
            instruction_file=instruction_file or "",
            tools=tools,
            tools_approval_required=tools_approval_required,
            output_key=output_key,
            model=model,
            skills_directory=skills_directory,
            skills=skills,
            context_window=context_window,
            max_output_tokens=max_tokens,
            enable_truncation=enable_truncation,
            truncate_strategy=truncate_strategy,
            safety_margin=safety_margin,
            workspace_directory=workspace_directory,
            publish_url=publish_url,
        )

    # ------------------------------------------------------------------
    # Private: LangChain / deepagents backend
    # ------------------------------------------------------------------

    @staticmethod
    def _create_langchain_agent(config: Dict[str, Any]) -> BaseAgent:
        """Build and return a LangChainAIAgent from a config dict."""
        from declarative_agent_sdk.langchain_ai_agent import LangChainAIAgent

        name = config["name"]
        instruction_file = config.get("instruction_file", "")
        description = config.get("description", "")
        output_key = config.get("output_key")
        model_name = config.get("model", "claude-sonnet-4-6")
        provider = config.get("provider", "anthropic")
        skills = config.get("skills")
        skills_directory = config.get("skills_directory", SKILLS_DIRECTORY)
        workspace_directory = config.get("workspace_directory", WORKSPACE_DIRECTORY)
        publish_url = config.get("publish_url")

        # max_tokens can live at root or under endpoint block
        endpoint_config = config.get("endpoint") or {}
        max_tokens = (
            endpoint_config.get("max_tokens") if isinstance(endpoint_config, dict) else None
        ) or config.get("max_tokens")

        context_window = config.get("context_window")
        enable_truncation = config.get("enable_truncation", False)
        truncate_strategy = config.get("truncate_strategy", "end")
        safety_margin = config.get("safety_margin", 100)

        tools = AgentFactory._resolve_tool_names(config.get("tools", []), name)

        # middleware / subagents are advanced and typically passed programmatically;
        # the YAML can list middleware class names as strings for built-in support
        middleware = AgentFactory._resolve_middleware(config.get("middleware", []))

        logger.info(f"Creating LangChain deep agent '{name}' (provider={provider})")
        return LangChainAIAgent(
            name=name,
            description=description,
            instruction_file=instruction_file,
            tools=tools,
            output_key=output_key,
            model=model_name,
            provider=provider,
            max_output_tokens=max_tokens,
            context_window=context_window,
            enable_truncation=enable_truncation,
            truncate_strategy=truncate_strategy,
            safety_margin=safety_margin,
            skills_directory=skills_directory,
            skills=skills,
            workspace_directory=workspace_directory,
            publish_url=publish_url,
            middleware=middleware if middleware else None,
        )

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_tool_names(tool_names: List[Any], agent_name: str) -> List[Any]:
        """
        Resolve string tool names via ToolRegistry; pass non-string items as-is.
        Returns an empty list when tool_names is empty (agent will use all built-ins).
        """
        if not tool_names:
            return []
        resolved = []
        for item in tool_names:
            if isinstance(item, str):
                try:
                    resolved.append(ToolRegistry.get(item))
                except ValueError:
                    # May be discovered from skills — let the agent resolve it
                    logger.info(f"Tool '{item}' not in global registry for agent '{agent_name}', will resolve from skills")
                    resolved.append(item)
            else:
                resolved.append(item)
        return resolved

    @staticmethod
    def _resolve_middleware(middleware_config: List[Any]) -> List[Any]:
        """
        Resolve middleware entries from YAML.

        Each entry can be:
        - A string naming a deepagents middleware class
          (e.g. "FilesystemMiddleware", "MemoryMiddleware")
        - An already-instantiated AgentMiddleware object

        Note: middleware classes that require constructor arguments
        (e.g. MemoryMiddleware needs ``backend`` and ``sources``) cannot
        be fully configured from a plain string.  Pass those programmatically
        via LangChainAIAgent(middleware=[...]) instead.
        """
        if not middleware_config:
            return []

        resolved = []
        for entry in middleware_config:
            if not isinstance(entry, str):
                # Already an instantiated middleware object
                resolved.append(entry)
                continue
            try:
                import deepagents as _da
                cls = getattr(_da, entry, None)
                if cls is None:
                    logger.warning(
                        f"Middleware '{entry}' not found in deepagents; skipping. "
                        "Pass pre-instantiated middleware objects for complex configs."
                    )
                    continue
                # Only instantiate if it can be called with no args
                try:
                    resolved.append(cls())
                except TypeError:
                    logger.warning(
                        f"Middleware '{entry}' requires constructor arguments; "
                        "skipping YAML instantiation. Pass a pre-built instance instead."
                    )
            except ImportError:
                logger.warning("deepagents not installed; skipping middleware resolution")

        return resolved
