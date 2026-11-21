"""BaseAgent adapter around the lean ESP-style runtime."""

from __future__ import annotations

import asyncio

import os
import uuid
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, List, Optional, Union

from a2a.server.agent_execution import RequestContext

from declarative_agent_sdk.transports.a2a.utils import create_agent_card
from declarative_agent_sdk.core.agent_event import AgentEvent
from declarative_agent_sdk.core.agent_logging import get_logger
from declarative_agent_sdk.core.base_agent import BaseAgent
from declarative_agent_sdk.core.constants import SKILLS_DIRECTORY, WORKSPACE_DIRECTORY
from declarative_agent_sdk.agents.lean.runtime.llm import LeanLLMClient
from declarative_agent_sdk.agents.lean.runtime.loop import LeanLoop, LoopEvent
from declarative_agent_sdk.agents.lean.runtime.skills import SkillRegistry
from declarative_agent_sdk.agents.lean.runtime.tools import LeanToolRegistry
from declarative_agent_sdk.tools.tool_registry import ToolRegistry
from declarative_agent_sdk.core.utils import read_from_file

logger = get_logger(__name__)

DEFAULT_LEAN_MODEL = "gpt-4o-mini"
DEFAULT_OPENAI_BASE = "https://api.openai.com/v1"

_PROVIDER_BASE_URLS = {
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com/v1",  # only if OpenAI-compat proxy
}


def _resolve_base_url(provider: Optional[str], endpoint_url: Optional[str]) -> str:
    if endpoint_url:
        return endpoint_url.rstrip("/")
    p = (provider or "openai").lower()
    return _PROVIDER_BASE_URLS.get(p, DEFAULT_OPENAI_BASE)


def _resolve_api_key(provider: Optional[str], api_key: Optional[str] = None) -> str:
    if api_key:
        return api_key
    p = (provider or "openai").lower()
    if p in ("openai", "vllm", "litellm", ""):
        return os.environ.get("OPENAI_API_KEY", "")
    if p in ("google", "google_genai"):
        return os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY") or ""
    if p == "anthropic":
        return os.environ.get("ANTHROPIC_API_KEY", "")
    return os.environ.get("OPENAI_API_KEY", "")


def _loop_event_to_agent_event(ev: LoopEvent) -> AgentEvent:
    if ev.kind == "tool_approval":
        return AgentEvent.tool_approval(ev.tool_call_id, ev.tool_name, ev.tool_args)
    if ev.kind == "final":
        return AgentEvent.final_text(ev.text)
    if ev.kind == "error":
        return AgentEvent.error(ev.text)
    return AgentEvent.status(ev.text)


class LeanAIAgent(BaseAgent):
    """
    Lean runtime end-to-end: ReAct loop + progressive skills.

    Peer of ADKAIAgent (ADK) and LangChainAIAgent (deepagent). Same BaseAgent
    surface for Discord/A2A; does not embed other frameworks.
    """

    def __init__(
        self,
        name: str,
        instruction_file: str = "",
        description: str = "",
        tools: Optional[list] = None,
        tools_approval_required: bool = True,
        skills_directory: str = SKILLS_DIRECTORY,
        workspace_directory: str = WORKSPACE_DIRECTORY,
        skills: Optional[List[str]] = None,  # unused — discovers all under skills_directory
        model: Union[str, Any] = DEFAULT_LEAN_MODEL,
        provider: str = "openai",
        max_output_tokens: Optional[int] = 4096,
        endpoint_url: Optional[str] = None,
        api_key: Optional[str] = None,
        temperature: Optional[float] = 0.7,
        request_timeout: Optional[float] = None,
        retry_malformed_tool_calls: Optional[int] = None,
        max_tool_iterations: int = 32,
        max_step_retries: int = 3,
        max_no_tool_continues: int = 4,
        history_limit: Optional[int] = None,
        tool_payload_keep: Optional[int] = None,
        tool_payload_max_chars: Optional[int] = None,
        publish_url: Optional[str] = None,
        # accepted for factory parity; unused
        output_key: Optional[str] = None,
        context_window: Optional[int] = None,
        enable_truncation: bool = False,
        truncate_strategy: str = "end",
        safety_margin: int = 100,
        input_key_map: Optional[Dict[str, str]] = None,
    ) -> None:
        self.name = name
        self.description = description
        self.publish_url = publish_url
        self.tools_approval_required = tools_approval_required
        self.output_key = output_key
        self.input_key_map = input_key_map or {}

        instruction = ""
        if instruction_file and Path(instruction_file).is_file():
            instruction = read_from_file(instruction_file)
        elif instruction_file:
            logger.warning(f"instruction_file not found: {instruction_file}")

        Path(workspace_directory).mkdir(parents=True, exist_ok=True)
        Path(skills_directory).mkdir(parents=True, exist_ok=True)
        # Read by the A2A server: the files it serves at /files/<path>.
        self.workspace_directory = workspace_directory

        self._skills = SkillRegistry(root=skills_directory)
        self._tools = LeanToolRegistry(
            self._skills, workspace=workspace_directory
        )

        # Bridge user / builtin callables (unknown names warn inside resolve)
        ToolRegistry.register_built_in_tools()
        resolved: List[Any] = [
            item
            for item in ToolRegistry.resolve(tools, agent_name=name)
            if callable(item)
        ]
        for fn in resolved:
            try:
                self._tools.add_callable(fn)
            except Exception as exc:
                logger.warning(f"Could not register tool {fn!r}: {exc}")

        base_url = _resolve_base_url(provider, endpoint_url)
        key = _resolve_api_key(provider, api_key)
        # vLLM often needs no key
        if (provider or "").lower() == "vllm" and not key:
            key = "not-needed"

        self._client = LeanLLMClient(
            model=str(model),
            api_key=key,
            base_url=base_url,
            temperature=temperature,
            max_tokens=max_output_tokens,
            **({} if request_timeout is None else {"timeout": float(request_timeout)}),
            **({} if retry_malformed_tool_calls is None
               else {"retry_malformed_tool_calls": int(retry_malformed_tool_calls)}),
        )
        self._loop = LeanLoop(
            client=self._client,
            tools=self._tools,
            skills=self._skills,
            instruction=instruction,
            max_tool_iterations=max_tool_iterations,
            max_step_retries=max_step_retries,
            max_no_tool_continues=max_no_tool_continues,
            tools_approval_required=tools_approval_required,
            **({} if history_limit is None else {"history_limit": history_limit}),
            **({} if tool_payload_keep is None
               else {"tool_payload_keep": tool_payload_keep}),
            **({} if tool_payload_max_chars is None
               else {"tool_payload_max_chars": tool_payload_max_chars}),
        )

        skill_descs = {
            n: s.description for n, s in self._skills.skills.items()
        }
        self.agent_card = create_agent_card(
            name=name,
            description=description,
            skills=skill_descs,
            url=publish_url,
        )
        logger.info(
            f"LeanAIAgent '{name}' ready (model={model}, provider={provider}, "
            f"skills={len(self._skills.skills)}, tools={len(self._tools.names())}, "
            f"approval={tools_approval_required})"
        )

    async def run_query(
        self, query: str, session_id: Optional[str] = None
    ) -> AsyncGenerator[AgentEvent, None]:
        sid = session_id or str(uuid.uuid4())
        async for ev in _drain(self._loop.run(query, sid)):
            yield _loop_event_to_agent_event(ev)

    def context_report(self) -> Dict[str, Any]:
        """
        How big the prompt this agent prefixes to every request has grown.

        Read over HTTP by agents-control, which cannot see it any other way:
        the history lives in this process, and a model server only knows about
        whichever request it was last handed.
        """
        report = self._loop.context_report()
        report["agent"] = self.name
        report["model"] = getattr(self._client, "model", "")
        return report

    def reset_context(self, session_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Forget a conversation, or all of them.

        Called by agents-control's "clear conversation" / "clear context". The
        board also starts a new session id, so this is about freeing what the
        old one held rather than about the next turn starting clean.
        """
        if session_id:
            known = session_id in self._loop._history or session_id in self._loop._pending
            self._loop.reset(session_id)
            return {"cleared": 1 if known else 0, "session": session_id}
        return {"cleared": self._loop.reset_all(), "session": ""}

    async def tool_confirmation(
        self,
        context_id: str,
        session_id: str,
        yes: bool,
    ) -> AsyncGenerator[AgentEvent, None]:
        logger.info(
            f"[{self.name}] lean tool_confirmation yes={yes} "
            f"context_id={context_id} session={session_id}"
        )
        async for ev in _drain(self._loop.resume(session_id, approved=yes)):
            yield _loop_event_to_agent_event(ev)

    async def invoke(
        self, context: RequestContext
    ) -> AsyncGenerator[AgentEvent, None]:
        """A2A entry: plain text queries or tool-approval resume messages."""
        assert context is not None and context.message is not None
        assert context.context_id is not None

        # A2A clients (see AIAgentClient.send_tool_confirmation) resume HITL
        # with a data part: {function_response: {id, name, response:{confirmed}}}
        confirmation = _a2a_tool_confirmation(context.message)
        if confirmation is not None:
            call_id, approved = confirmation
            logger.info(
                f"[{self.name}] A2A tool confirmation call_id={call_id} "
                f"approved={approved} session={context.context_id}"
            )
            async for event in self.tool_confirmation(
                call_id, context.context_id, approved
            ):
                yield event
            return

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


_DRAIN_SENTINEL = object()


async def _drain(generator: Any) -> AsyncGenerator[Any, None]:
    """
    Pull a synchronous generator without blocking the event loop.

    The lean loop is deliberately synchronous and its LLM client uses blocking
    urllib, so iterating it directly from an async method stalls the whole
    process: while a model call is in flight, the A2A server cannot answer
    anything — not a status poll, not `tasks/get`, not even the next
    `message/send`. One `next()` per thread hop keeps the loop free, and the
    calls stay strictly sequential so the generator is only ever touched by one
    thread at a time.
    """
    while True:
        event = await asyncio.to_thread(next, generator, _DRAIN_SENTINEL)
        if event is _DRAIN_SENTINEL:
            return
        yield event


def _a2a_tool_confirmation(message: Any) -> Optional[tuple]:
    """
    Return (function_id, approved) when the A2A message is a tool-approval
    resume, else None.
    """
    parts = getattr(message, "parts", None) or []
    for part in parts:
        which = None
        try:
            which = part.WhichOneof("content")
        except Exception:
            which = None
        data = None
        if which == "data":
            raw = getattr(part, "data", None)
            if raw is None:
                continue
            try:
                from google.protobuf.json_format import MessageToDict

                data = MessageToDict(raw)
            except Exception:
                data = raw if isinstance(raw, dict) else None
        elif isinstance(getattr(part, "data", None), dict):
            data = part.data
        if not isinstance(data, dict):
            continue
        fn_resp = data.get("function_response") or data.get("functionResponse")
        if not isinstance(fn_resp, dict):
            continue
        response = fn_resp.get("response") or {}
        if not isinstance(response, dict):
            continue
        if "confirmed" not in response and "approved" not in response:
            # Not a confirmation payload (might be another data shape).
            continue
        approved = bool(response.get("confirmed", response.get("approved", False)))
        call_id = str(fn_resp.get("id") or "unknown")
        return call_id, approved
    return None
