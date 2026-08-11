"""Deepagents / LangGraph agent — create_deep_agent owns the loop end-to-end.

Peer of LeanAIAgent and ADKAIAgent (ADK). Select with::

    agent_framework: deepagent

Do not nest LangGraph under lean; pick one outer loop via agent_framework.
"""

from __future__ import annotations

import os
import uuid
from typing import Any, AsyncGenerator, Dict, List, Optional, Union

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import BaseTool, StructuredTool
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from deepagents import create_deep_agent

from a2a.server.agent_execution import RequestContext

from declarative_agent_sdk.transports.a2a.utils import create_agent_card
from declarative_agent_sdk.core.agent_event import AgentEvent
from declarative_agent_sdk.core.agent_logging import get_logger
from declarative_agent_sdk.core.base_agent import BaseAgent
from declarative_agent_sdk.core.constants import SKILLS_DIRECTORY, WORKSPACE_DIRECTORY
from declarative_agent_sdk.tools.tool_registry import ToolRegistry
from declarative_agent_sdk.core.utils import read_from_file

logger = get_logger(__name__)

DEFAULT_LANGCHAIN_MODEL = "claude-sonnet-4-6"
DEFAULT_LANGCHAIN_PROVIDER = "anthropic"

_DEEPAGENT_BUILTIN_TOOL_NAMES = (
    "write_todos",
    "ls",
    "read_file",
    "write_file",
    "edit_file",
    "glob",
    "grep",
    "execute",
    "task",
)

_APPROVE_REJECT_CONFIG: Dict[str, Any] = {
    "allowed_decisions": ["approve", "reject"],
}

# Back-compat alias — tests and external code may still import LangChainEvent
LangChainEvent = AgentEvent


# ---------------------------------------------------------------------------
# Model / tool helpers
# ---------------------------------------------------------------------------

def _resolve_model(
    model: Union[str, Any],
    provider: Optional[str],
    max_output_tokens: Optional[int] = None,
) -> Union[str, Any]:
    """Return a model value accepted by create_deep_agent."""
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


def _message_text(content: Any) -> str:
    """Normalize AIMessage.content (str or content-block list) to plain text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for block in content:
            if block is None:
                continue
            if isinstance(block, str):
                parts.append(block)
                continue
            if isinstance(block, dict):
                block_type = block.get("type")
                if block_type in (None, "text", "output_text", "input_text"):
                    text = block.get("text")
                    if isinstance(text, str) and text:
                        parts.append(text)
                    elif isinstance(text, dict):
                        value = text.get("value") or text.get("text")
                        if isinstance(value, str) and value:
                            parts.append(value)
                continue
            text = getattr(block, "text", None)
            if isinstance(text, str) and text:
                parts.append(text)
                continue
            block_type = getattr(block, "type", None)
            if block_type in (None, "text", "output_text") and hasattr(block, "content"):
                nested = _message_text(getattr(block, "content", None))
                if nested:
                    parts.append(nested)
        return "\n".join(p for p in parts if p).strip()
    text = getattr(content, "text", None)
    if isinstance(text, str):
        return text
    return str(content)


def _to_lc_tool(fn: Any) -> BaseTool:
    if isinstance(fn, BaseTool):
        return fn
    if callable(fn):
        return StructuredTool.from_function(fn)
    raise TypeError(f"Cannot convert {fn!r} to a LangChain tool")


def _build_interrupt_on(
    lc_tools: List[BaseTool],
    tools_approval_required: bool,
    interrupt_on: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    if interrupt_on is not None:
        return interrupt_on
    if not tools_approval_required:
        return None
    resolved = {
        t.name: dict(_APPROVE_REJECT_CONFIG)
        for t in lc_tools
        if getattr(t, "name", None)
    }
    for builtin in _DEEPAGENT_BUILTIN_TOOL_NAMES:
        resolved.setdefault(builtin, dict(_APPROVE_REJECT_CONFIG))
    return resolved


# ---------------------------------------------------------------------------
# Stream → AgentEvent
# ---------------------------------------------------------------------------

def events_from_stream_chunk(
    chunk: Any,
    *,
    session_id: str,
    pending_hitl: Dict[str, int],
    agent_name: str = "",
) -> List[AgentEvent]:
    """Convert one LangGraph ``stream_mode='updates'`` chunk to AgentEvents."""
    if not isinstance(chunk, dict):
        return []

    if "__interrupt__" in chunk:
        return _events_from_interrupt(
            chunk["__interrupt__"],
            session_id=session_id,
            pending_hitl=pending_hitl,
            agent_name=agent_name,
        )

    events: List[AgentEvent] = []
    for node_name, node_output in chunk.items():
        if not isinstance(node_output, dict):
            continue
        messages = node_output.get("messages") or []
        if not isinstance(messages, (list, tuple)):
            try:
                messages = list(messages)
            except TypeError:
                continue

        for msg in messages:
            if not isinstance(msg, AIMessage):
                continue
            tool_calls = getattr(msg, "tool_calls", None) or []
            if tool_calls:
                names = [
                    tc.get("name", "?") if isinstance(tc, dict) else getattr(tc, "name", "?")
                    for tc in tool_calls
                ]
                logger.debug(f"[{agent_name}] Calling tools: {names}")
                events.append(AgentEvent.status(f"Calling tools: {', '.join(names)}"))
            else:
                text = _message_text(msg.content)
                if text:
                    logger.debug(f"[{agent_name}] Final response ({len(text)} chars)")
                    events.append(AgentEvent.final_text(text))
    return events


def _events_from_interrupt(
    interrupts: Any,
    *,
    session_id: str,
    pending_hitl: Dict[str, int],
    agent_name: str = "",
) -> List[AgentEvent]:
    if interrupts is None:
        return []
    if not isinstance(interrupts, (list, tuple)):
        interrupts = (interrupts,)

    events: List[AgentEvent] = []
    for intr in interrupts:
        value = getattr(intr, "value", intr)
        if not isinstance(value, dict):
            logger.warning(f"[{agent_name}] Unexpected interrupt value: {type(value)!r}")
            continue

        action_requests = value.get("action_requests") or []
        if not action_requests:
            call_id = str(getattr(intr, "id", None) or uuid.uuid4())
            pending_hitl[session_id] = 1
            events.append(AgentEvent.tool_approval(call_id, "action", value))
            continue

        pending_hitl[session_id] = len(action_requests)

        if len(action_requests) == 1:
            ar = action_requests[0]
            name = ar.get("name", "unknown")
            args = ar.get("args") or {}
            call_id = str(uuid.uuid4())
            logger.info(f"[{agent_name}] Tool approval required: {name}({args})")
            events.append(AgentEvent.tool_approval(call_id, name, args))
        else:
            names = [ar.get("name", "?") for ar in action_requests]
            combined_args = {
                ar.get("name", f"tool_{i}"): (ar.get("args") or {})
                for i, ar in enumerate(action_requests)
            }
            call_id = str(uuid.uuid4())
            display = f"{names[0]} (+{len(names) - 1} more)"
            logger.info(f"[{agent_name}] Tool approval required for batch: {names}")
            events.append(AgentEvent.tool_approval(call_id, display, combined_args))
    return events


# ---------------------------------------------------------------------------
# LangChainAIAgent
# ---------------------------------------------------------------------------

class LangChainAIAgent(BaseAgent):
    """
    Deep agent backed by deepagents.create_deep_agent (LangGraph harness).

    Yields ``AgentEvent`` — the same type as ADKAIAgent — so Discord / A2A need
    no backend-specific handling.
    """

    def __init__(
        self,
        name: str,
        instruction_file: str,
        description: str = "",
        tools: Optional[list] = None,
        tools_approval_required: bool = True,
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
        interrupt_on: Optional[Dict[str, Any]] = None,
    ) -> None:
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
        self.tools_approval_required = tools_approval_required
        self._pending_hitl: Dict[str, int] = {}

        self._instruction: str = (
            read_from_file(instruction_file) if instruction_file else ""
        )

        from declarative_agent_sdk.tools.skill_registry import SkillRegistry

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

        resolved_interrupt_on = _build_interrupt_on(
            lc_tools, tools_approval_required, interrupt_on
        )
        if resolved_interrupt_on:
            logger.info(
                f"LangChainAIAgent '{name}' tool approval enabled for "
                f"{len(resolved_interrupt_on)} tool(s)"
            )

        resolved_model = _resolve_model(model, provider, max_output_tokens)
        self._memory = MemorySaver()
        create_kwargs: Dict[str, Any] = {
            "model": resolved_model,
            "tools": lc_tools if lc_tools else None,
            "system_prompt": self._instruction if self._instruction else None,
            "middleware": tuple(middleware) if middleware else (),
            "subagents": subagents,
            "checkpointer": self._memory,
            "name": name,
        }
        if resolved_interrupt_on:
            create_kwargs["interrupt_on"] = resolved_interrupt_on
        self._graph = create_deep_agent(**create_kwargs)

        if workspace_directory and not os.path.exists(workspace_directory):
            try:
                os.makedirs(workspace_directory)
            except Exception as exc:
                logger.error(f"Failed to create workspace '{workspace_directory}': {exc}")
                raise

        self.agent_card = create_agent_card(
            name=name,
            description=description,
            skills=skills_registry.get_all_skills_description(),
            url=publish_url,
        )

    # ------------------------------------------------------------------
    # Streaming
    # ------------------------------------------------------------------

    async def _stream(
        self, input_data: Any, session_id: str
    ) -> AsyncGenerator[AgentEvent, None]:
        config = {"configurable": {"thread_id": session_id}}
        async for chunk in self._graph.astream(
            input_data, config=config, stream_mode="updates"
        ):
            for event in events_from_stream_chunk(
                chunk,
                session_id=session_id,
                pending_hitl=self._pending_hitl,
                agent_name=self.name,
            ):
                yield event

    async def run_query(
        self, query: str, session_id: Optional[str] = None
    ) -> AsyncGenerator[AgentEvent, None]:
        sid = session_id or str(uuid.uuid4())
        async for event in self._stream(
            {"messages": [HumanMessage(content=query)]}, sid
        ):
            yield event

    async def tool_confirmation(
        self,
        context_id: str,
        session_id: str,
        yes: bool,
    ) -> AsyncGenerator[AgentEvent, None]:
        """Resume after human approve/deny via LangGraph Command(resume=...)."""
        n = self._pending_hitl.pop(session_id, 1)
        decision_type = "approve" if yes else "reject"
        decisions = [{"type": decision_type} for _ in range(max(1, n))]
        logger.info(
            f"[{self.name}] Resuming after tool confirmation "
            f"(yes={yes}, decisions={len(decisions)}, context_id={context_id})"
        )
        async for event in self._stream(
            Command(resume={"decisions": decisions}), session_id
        ):
            yield event

    async def invoke(
        self, context: RequestContext
    ) -> AsyncGenerator[AgentEvent, None]:
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
