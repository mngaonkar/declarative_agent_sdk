"""Lean ReAct agent loop (from esp32s3-ai-agent), CPython host edition.

Supports pausing for tool approval so Discord / A2A can gate dangerous tools.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional

from declarative_agent_sdk.agent_logging import get_logger
from declarative_agent_sdk.runtime.llm import LeanLLMClient, LLMError
from declarative_agent_sdk.runtime.skills import SkillRegistry
from declarative_agent_sdk.runtime.tools import AUTO_APPROVE_TOOLS, LeanToolRegistry

logger = get_logger(__name__)

_BASE_PROMPT = """You are a helpful AI agent with tools and skills.

Be concise unless the user asks for detail.

You are extended through SKILLS. Each skill below is listed with only its name \
and description; the instructions themselves are not loaded yet.

RULE: if a request matches a skill's description, your FIRST action must be to \
call the Skill tool with that name. Do not call other tools for that task until \
you have read the skill. Only skip this when no skill covers the request.

Skills hold procedure and calibration you cannot invent. A plausible-looking \
direct tool call is the most common way to get things wrong.

CREATING SKILLS: you can grow the skill list by writing \
skills/<name>/SKILL.md (load write-skill if present for format). Prefer \
editing a close existing skill over duplicating it.

ERRORS: do not give up after the first tool failure. Read the error, change \
something concrete, and retry within the tool-round budget.

Available skills:
%s

Report what actually happened. If a tool fails after retries, say so."""


@dataclass
class PendingApproval:
    """State frozen while waiting for human tool approval."""

    session_id: str
    messages: List[Dict[str, Any]]
    tool_calls: List[Dict[str, Any]]
    index: int
    rounds_left: int


@dataclass
class LoopEvent:
    """Internal loop event before mapping to AgentEvent."""

    kind: str  # status | tool_approval | final | error
    text: str = ""
    tool_call_id: str = ""
    tool_name: str = ""
    tool_args: Dict[str, Any] = field(default_factory=dict)


class LeanLoop:
    """
    Multi-session ReAct loop.

    ``run(query, session_id)`` yields LoopEvents until a final answer or a
    tool-approval pause. Resume with ``resume(session_id, approved=...)``.
    """

    def __init__(
        self,
        client: LeanLLMClient,
        tools: LeanToolRegistry,
        skills: SkillRegistry,
        *,
        instruction: str = "",
        max_tool_iterations: int = 24,
        history_limit: int = 40,
        tools_approval_required: bool = True,
        auto_approve: Optional[set] = None,
    ) -> None:
        self.client = client
        self.tools = tools
        self.skills = skills
        self.instruction = instruction
        self.max_tool_iterations = max_tool_iterations
        self.history_limit = history_limit
        self.tools_approval_required = tools_approval_required
        self.auto_approve = set(auto_approve or AUTO_APPROVE_TOOLS)

        self._history: Dict[str, List[Dict[str, Any]]] = {}
        self._pending: Dict[str, PendingApproval] = {}

    def system_prompt(self) -> str:
        extra = self.instruction.strip()
        prompt = _BASE_PROMPT % self.skills.catalog()
        if extra:
            prompt = prompt + "\n\n# Additional instructions\n\n" + extra
        return prompt

    def reset(self, session_id: str) -> None:
        self._history.pop(session_id, None)
        self._pending.pop(session_id, None)

    def _trim(self, history: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if len(history) <= self.history_limit:
            return history
        cut = len(history) - self.history_limit
        while cut < len(history) and history[cut].get("role") == "tool":
            cut += 1
        return history[cut:]

    def _needs_approval(self, tool_name: str) -> bool:
        if not self.tools_approval_required:
            return False
        return tool_name not in self.auto_approve

    def run(self, query: str, session_id: str) -> Iterator[LoopEvent]:
        if session_id in self._pending:
            self._pending.pop(session_id, None)

        history = self._history.setdefault(session_id, [])
        history.append({"role": "user", "content": query})
        history[:] = self._trim(history)

        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": self.system_prompt()}
        ]
        messages.extend(history)

        yield from self._drive(session_id, messages, history, self.max_tool_iterations)

    def resume(self, session_id: str, approved: bool) -> Iterator[LoopEvent]:
        pending = self._pending.pop(session_id, None)
        if pending is None:
            yield LoopEvent(
                kind="error",
                text="No pending tool approval for this session.",
            )
            return

        history = self._history.setdefault(session_id, [])
        messages = pending.messages
        tool_calls = pending.tool_calls
        i = pending.index
        rounds_left = pending.rounds_left

        if i < len(tool_calls):
            yield from self._execute_one(messages, history, tool_calls[i], approved)
            i += 1

        # Remaining tools in the same assistant batch
        while i < len(tool_calls):
            call = tool_calls[i]
            name = (call.get("function") or {}).get("name") or ""
            if self._needs_approval(name):
                self._pending[session_id] = PendingApproval(
                    session_id=session_id,
                    messages=messages,
                    tool_calls=tool_calls,
                    index=i,
                    rounds_left=rounds_left,
                )
                yield from self._approval_event(call)
                return
            yield from self._execute_one(messages, history, call, approved=True)
            i += 1

        # Batch complete — continue model rounds
        yield from self._drive(session_id, messages, history, rounds_left)

    def _drive(
        self,
        session_id: str,
        messages: List[Dict[str, Any]],
        history: List[Dict[str, Any]],
        rounds_left: int,
    ) -> Iterator[LoopEvent]:
        while rounds_left > 0:
            rounds_left -= 1
            try:
                message = self.client.chat(messages, tools=self.tools.schemas())
            except LLMError as exc:
                yield LoopEvent(kind="error", text=f"LLM error: {exc}")
                return

            tool_calls = message.get("tool_calls") or []
            content = _content_to_text(message.get("content"))

            assistant_msg: Dict[str, Any] = {
                "role": "assistant",
                "content": content,
            }
            if tool_calls:
                assistant_msg["tool_calls"] = tool_calls
            messages.append(assistant_msg)
            history.append(assistant_msg)

            if not tool_calls:
                history[:] = self._trim(history)
                yield LoopEvent(kind="final", text=content or "(no reply)")
                return

            names = [
                (c.get("function") or {}).get("name", "?") for c in tool_calls
            ]
            yield LoopEvent(
                kind="status", text=f"Calling tools: {', '.join(names)}"
            )

            i = 0
            while i < len(tool_calls):
                call = tool_calls[i]
                name = (call.get("function") or {}).get("name") or ""
                if self._needs_approval(name):
                    self._pending[session_id] = PendingApproval(
                        session_id=session_id,
                        messages=messages,
                        tool_calls=tool_calls,
                        index=i,
                        rounds_left=rounds_left,
                    )
                    yield from self._approval_event(call)
                    return
                yield from self._execute_one(messages, history, call, approved=True)
                i += 1
            # all tools ran — next model round

        history[:] = self._trim(history)
        yield LoopEvent(
            kind="final",
            text=(
                f"Stopped after {self.max_tool_iterations} tool rounds without "
                "a final answer. Continue with a short follow-up if needed."
            ),
        )

    def _approval_event(self, call: Dict[str, Any]) -> Iterator[LoopEvent]:
        fn = call.get("function") or {}
        name = fn.get("name") or "unknown"
        raw_args = fn.get("arguments") or "{}"
        try:
            parsed = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
        except Exception:
            parsed = {}
        if not isinstance(parsed, dict):
            parsed = {"value": parsed}
        call_id = call.get("id") or str(uuid.uuid4())
        yield LoopEvent(
            kind="tool_approval",
            tool_call_id=call_id,
            tool_name=name,
            tool_args=parsed,
            text=f"Approve tool `{name}`?",
        )

    def _execute_one(
        self,
        messages: List[Dict[str, Any]],
        history: List[Dict[str, Any]],
        call: Dict[str, Any],
        approved: bool,
    ) -> Iterator[LoopEvent]:
        fn = call.get("function") or {}
        name = fn.get("name") or ""
        raw_args = fn.get("arguments") or "{}"
        try:
            parsed = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
        except Exception:
            parsed = {}
        if not isinstance(parsed, dict):
            parsed = {"value": parsed}
        call_id = call.get("id") or str(uuid.uuid4())

        if approved:
            yield LoopEvent(kind="status", text=f"Running tool: {name}")
            output = self.tools.invoke(name, parsed)
        else:
            output = (
                f"User rejected the tool call for `{name}`. "
                "The tool was not executed. Do not retry unless the user asks."
            )
            yield LoopEvent(kind="status", text=f"Denied tool: {name}")

        if len(output) > 12000:
            output = output[:12000] + "\n...[truncated]"
        result_msg = {
            "role": "tool",
            "tool_call_id": call_id,
            "content": output,
        }
        messages.append(result_msg)
        history.append(result_msg)


def _content_to_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") in (
                None,
                "text",
                "output_text",
            ):
                t = block.get("text")
                if isinstance(t, str):
                    parts.append(t)
        return "\n".join(parts).strip()
    return str(content)
