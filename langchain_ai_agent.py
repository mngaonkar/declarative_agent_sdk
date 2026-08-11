"""LangChain Deep Agent backed by the `deepagents` library."""

import os
import uuid
from typing import Any, AsyncGenerator, Dict, List, Optional, Union

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import BaseTool, StructuredTool
from langgraph.checkpoint.memory import MemorySaver

from deepagents import create_deep_agent

from a2a.server.agent_execution import RequestContext

from declarative_agent_sdk.a2a_utils import create_agent_card
from declarative_agent_sdk.agent_logging import get_logger
from declarative_agent_sdk.base_agent import BaseAgent
from declarative_agent_sdk.constants import SKILLS_DIRECTORY, WORKSPACE_DIRECTORY
from declarative_agent_sdk.tool_registry import ToolRegistry
from declarative_agent_sdk.utils import read_from_file

logger = get_logger(__name__)

DEFAULT_LANGCHAIN_MODEL = "claude-sonnet-4-6"
DEFAULT_LANGCHAIN_PROVIDER = "anthropic"


# ---------------------------------------------------------------------------
# Minimal event objects compatible with AIAgentExecutor's expected interface
# ---------------------------------------------------------------------------

class _Part:
    def __init__(self, text: str) -> None:
        self.text = text


class _Content:
    def __init__(self, text: str) -> None:
        self.parts = [_Part(text)]


class _Actions:
    def __init__(self) -> None:
        self.requested_tool_confirmations: list = []


class LangChainEvent:
    """
    Event yielded by LangChainAIAgent — shaped to match the interface that
    AIAgentExecutor expects (is_final_response, content.parts, long_running_tool_ids,
    actions.requested_tool_confirmations).
    """

    def __init__(self, text: str = "", is_final: bool = False) -> None:
        self.content: Optional[_Content] = _Content(text) if text else None
        self.long_running_tool_ids: list = []
        self.actions = _Actions()
        self._is_final = is_final

    def is_final_response(self) -> bool:
        return self._is_final


# ---------------------------------------------------------------------------
# Model resolution
# ---------------------------------------------------------------------------

def _resolve_model(
    model: Union[str, Any],
    provider: Optional[str],
    max_output_tokens: Optional[int] = None,
) -> Union[str, Any]:
    """
    Return a model value accepted by create_deep_agent.

    - Pre-built BaseChatModel: returned as-is.
    - "anthropic" / "openai" / "google*": formatted as "provider:model" string
      for deepagents / init_chat_model resolution.
    - "vllm" / "litellm" / unknown: fall back to a ChatLiteLLM BaseChatModel.
    """
    if not isinstance(model, str):
        return model

    p = (provider or "").lower()
    extra: Dict[str, Any] = {}
    if max_output_tokens:
        extra["max_tokens"] = max_output_tokens

    if p == "anthropic":
        return f"anthropic:{model}"
    if p == "openai":
        return f"openai:{model}"
    if p in ("google", "google_genai"):
        return f"google_genai:{model}"

    # vLLM / LiteLLM: build a BaseChatModel and hand it directly to deepagents
    try:
        from langchain_community.chat_models.litellm import ChatLiteLLM  # type: ignore

        litellm_model = f"{p}/{model}" if p and p not in model else model
        return ChatLiteLLM(model=litellm_model, **extra)
    except ImportError as exc:
        raise ImportError(
            f"Provider '{p}' is not natively supported by deepagents and "
            "langchain-community is not installed as a fallback. "
            "Install langchain-community or pass a pre-built BaseChatModel."
        ) from exc


# ---------------------------------------------------------------------------
# Tool conversion
# ---------------------------------------------------------------------------

def _to_lc_tool(fn: Any) -> BaseTool:
    """Convert a Python callable to a LangChain BaseTool."""
    if isinstance(fn, BaseTool):
        return fn
    if callable(fn):
        return StructuredTool.from_function(fn)
    raise TypeError(f"Cannot convert {fn!r} to a LangChain tool")


# ---------------------------------------------------------------------------
# LangChainAIAgent
# ---------------------------------------------------------------------------

class LangChainAIAgent(BaseAgent):
    """
    Deep agent backed by deepagents.create_deep_agent (LangGraph harness).

    Satisfies the same BaseAgent interface as AIAgent so both backends are
    interchangeable wherever a BaseAgent is expected.

    deepagents built-ins (always present unless excluded via HarnessProfile):
        write_todos, ls, read_file, write_file, edit_file, glob, grep,
        execute, task (sub-agent spawning)

    User tools passed via the ``tools`` argument are additive on top of these.
    """

    def __init__(
        self,
        name: str,
        instruction_file: str,
        description: str = "",
        tools: Optional[list] = None,
        skills_directory: str = SKILLS_DIRECTORY,
        workspace_directory: str = WORKSPACE_DIRECTORY,
        skills: Optional[List[str]] = None,
        input_key_map: Optional[Dict[str, str]] = None,
        output_key: Optional[str] = None,
        model: Union[str, Any] = DEFAULT_LANGCHAIN_MODEL,
        provider: str = DEFAULT_LANGCHAIN_PROVIDER,
        max_output_tokens: Optional[int] = None,
        context_window: Optional[int] = None,
        enable_truncation: bool = False,
        truncate_strategy: str = "end",
        safety_margin: int = 100,
        publish_url: Optional[str] = None,
        middleware: Optional[list] = None,
        subagents: Optional[list] = None,
    ) -> None:
        """
        Initialize the LangChain Deep Agent.

        Args:
            name: Agent name.
            instruction_file: Path to the system instruction file (markdown).
            description: Brief description of the agent's purpose.
            tools: Additional tools — names resolved from ToolRegistry, callables,
                   or LangChain BaseTool objects.  Additive to deepagents built-ins.
            skills_directory: Base directory containing skill sub-directories.
            workspace_directory: Directory for agent outputs / scratch files.
            skills: Skill sub-directory names to auto-discover tools from.
            input_key_map: Optional mapping of input keys.
            output_key: Optional session-state key for structured output.
            model: Model name string or a pre-built LangChain BaseChatModel.
            provider: LLM provider — "anthropic", "openai", "google",
                      "google_genai", "vllm", or "litellm".
            max_output_tokens: Maximum tokens the model should generate.
            context_window: Total context window (reserved for future truncation).
            enable_truncation: Reserved for future truncation support.
            truncate_strategy: "start", "end", or "middle".
            safety_margin: Extra token buffer subtracted from context_window.
            publish_url: URL written into the A2A AgentCard for discovery.
            middleware: deepagents AgentMiddleware instances (e.g. MemoryMiddleware).
            subagents: SubAgent / CompiledSubAgent / AsyncSubAgent instances.
        """
        self.name = name
        self.description = description
        self.input_key_map = input_key_map or {}
        self.output_key = output_key
        self.context_window = context_window
        self.max_output_tokens = max_output_tokens
        self.enable_truncation = enable_truncation
        self.truncate_strategy = truncate_strategy
        self.safety_margin = safety_margin
        self.skills = skills or []
        self.publish_url = publish_url

        self._instruction: str = read_from_file(instruction_file) if instruction_file else ""

        # Instance-isolated SkillRegistry
        from declarative_agent_sdk.skill_registry import SkillRegistry

        skills_registry = type(
            "InstanceSkillRegistry",
            (SkillRegistry,),
            {"_skills": {}, "_metadata": {}, "_tool_registry_class": None},
        )
        if skills:
            skills_registry.register_multiple_from_directory(
                skill_directory=skills_directory,
                skills_list=skills,
            )

        ToolRegistry.register_built_in_tools()
        resolved: List[Any] = list(skills_registry._get_tool_registry().get_all())

        if tools:
            for tool_item in tools:
                if isinstance(tool_item, str):
                    try:
                        resolved.append(ToolRegistry.get(tool_item))
                    except ValueError:
                        logger.warning(f"Tool '{tool_item}' not found in registry, skipping")
                else:
                    resolved.append(tool_item)
        else:
            resolved.extend(ToolRegistry.get_all())

        lc_tools: List[BaseTool] = []
        for fn in resolved:
            try:
                lc_tools.append(_to_lc_tool(fn))
            except Exception as exc:
                logger.warning(f"Skipping non-convertible tool {fn!r}: {exc}")

        logger.info(f"LangChainAIAgent '{name}' loaded {len(lc_tools)} user tool(s)")

        resolved_model = _resolve_model(model, provider, max_output_tokens)
        self._memory = MemorySaver()
        self._graph = create_deep_agent(
            model=resolved_model,
            tools=lc_tools if lc_tools else None,
            system_prompt=self._instruction if self._instruction else None,
            middleware=tuple(middleware) if middleware else (),
            subagents=subagents,
            checkpointer=self._memory,
            name=name,
        )

        if workspace_directory and not os.path.exists(workspace_directory):
            try:
                os.makedirs(workspace_directory)
            except Exception as exc:
                logger.error(f"Failed to create workspace '{workspace_directory}': {exc}")
                raise

        skill_descriptions = skills_registry.get_all_skills_description()
        self.agent_card = create_agent_card(
            name=name,
            description=description,
            skills=skill_descriptions,
            url=publish_url,
        )

    # ------------------------------------------------------------------
    # BaseAgent interface
    # ------------------------------------------------------------------

    async def run_query(
        self, query: str, session_id: Optional[str] = None
    ) -> AsyncGenerator[LangChainEvent, None]:
        """Yield LangChainEvents for a plain-text query."""
        sid = session_id or str(uuid.uuid4())
        config = {"configurable": {"thread_id": sid}}

        async for chunk in self._graph.astream(
            {"messages": [HumanMessage(content=query)]},
            config=config,
            stream_mode="updates",
        ):
            for _node, node_output in chunk.items():
                for msg in node_output.get("messages", []):
                    if not isinstance(msg, AIMessage):
                        continue
                    if msg.tool_calls:
                        tool_names = [tc["name"] for tc in msg.tool_calls]
                        logger.debug(f"[{self.name}] Calling tools: {tool_names}")
                        yield LangChainEvent(
                            text=f"Calling tools: {', '.join(tool_names)}",
                            is_final=False,
                        )
                    elif msg.content:
                        text = (
                            msg.content
                            if isinstance(msg.content, str)
                            else str(msg.content)
                        )
                        logger.debug(f"[{self.name}] Final response ({len(text)} chars)")
                        yield LangChainEvent(text=text, is_final=True)

    async def invoke(
        self, context: RequestContext
    ) -> AsyncGenerator[LangChainEvent, None]:
        """Yield LangChainEvents for an A2A RequestContext."""
        assert context is not None, "Context is required"
        assert context.message is not None, "Context message is required"
        assert context.context_id is not None, "Context ID is required"

        text_parts: List[str] = []
        for part in context.message.parts:
            which = part.WhichOneof("content")
            if which == "text" and part.text:
                text_parts.append(part.text)

        query = " ".join(text_parts)
        if not query:
            raise ValueError("No text content found in A2A message")

        async for event in self.run_query(query, session_id=context.context_id):
            yield event
