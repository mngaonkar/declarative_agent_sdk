"""BaseAgent adapter around the lean ESP-style runtime."""

from __future__ import annotations

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
        max_tool_iterations: int = 24,
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

        self._skills = SkillRegistry(root=skills_directory)
        self._tools = LeanToolRegistry(
            self._skills, workspace=workspace_directory
        )

        # Bridge user / builtin callables
        ToolRegistry.register_built_in_tools()
        resolved: List[Any] = []
        if tools:
            for item in tools:
                if isinstance(item, str):
                    try:
                        resolved.append(ToolRegistry.get(item))
                    except ValueError:
                        logger.warning(f"Tool '{item}' not found; skipping")
                elif callable(item):
                    resolved.append(item)
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
        )
        self._loop = LeanLoop(
            client=self._client,
            tools=self._tools,
            skills=self._skills,
            instruction=instruction,
            max_tool_iterations=max_tool_iterations,
            tools_approval_required=tools_approval_required,
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
        for ev in self._loop.run(query, sid):
            yield _loop_event_to_agent_event(ev)

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
        for ev in self._loop.resume(session_id, approved=yes):
            yield _loop_event_to_agent_event(ev)

    async def invoke(
        self, context: RequestContext
    ) -> AsyncGenerator[AgentEvent, None]:
        assert context is not None and context.message is not None
        assert context.context_id is not None
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
