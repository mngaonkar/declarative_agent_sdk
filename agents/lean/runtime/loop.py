"""Lean deliberative agent loop (plan → act → reflect → retry).

Unlike a bare ReAct “exit when no tool calls” loop, this runtime:

1. Deliberates (pros/cons, clarifying questions, plan).
2. Executes the plan step by step via tools.
3. On step failure, reasons and retries up to N times, then asks the user.
4. When the model returns no tool call, re-reasons over progress and only
   exits on explicit done/ask (or budget exhaustion).

Tool approval (Discord/A2A) still pauses mid-batch when required.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional, Tuple

from declarative_agent_sdk.core.agent_logging import get_logger
from declarative_agent_sdk.agents.lean.runtime.chat_backend import ChatBackend
from declarative_agent_sdk.agents.lean.runtime.llm import LLMError
from declarative_agent_sdk.agents.lean.runtime.skills import SkillRegistry
from declarative_agent_sdk.agents.lean.runtime.tools import AUTO_APPROVE_TOOLS, LeanToolRegistry

logger = get_logger(__name__)

# Explicit control tags the model should emit (parsed out of user-facing text).
_DECISION_RE = re.compile(
    r"\[\[\s*decision\s*:\s*(done|ask|continue)\s*\]\]",
    re.IGNORECASE,
)
_PHASE_RE = re.compile(
    r"\[\[\s*phase\s*:\s*(plan|act|reflect|done|ask)\s*\]\]",
    re.IGNORECASE,
)

_BASE_PROMPT = """You are a careful, iterative AI agent with tools and skills.

# Operating protocol (mandatory)

Work in cycles. Do **not** stop just because you have text and no tool call.

## 1. Deliberate (every new user request, and whenever stuck)
- Restate the goal in one sentence.
- Note uncertainties, risks, and missing information.
- If the request is ambiguous or unsafe without more info, ask clarifying \
questions and end with [[decision:ask]].
- Otherwise produce a short ordered **plan** (numbered steps). Prefer \
loading a matching skill before acting.

## 2. Execute one step at a time
- Call tools for the **next** plan step only (batch small).
- After each tool result, reflect: did the step succeed? What changed?

## 3. On failure
- Reason about the error; change approach (different args, tool, or skill).
- Retry the step; the runtime allows up to %(max_step_retries)s consecutive \
tool failures before you must stop and ask the user for help with \
[[decision:ask]].

## 4. When you have no tool call
You must still reason over everything so far, then choose **exactly one**:
- [[decision:continue]] — more work remains (plan next step; usually call tools next)
- [[decision:done]] — goal achieved; give the user a clear final answer
- [[decision:ask]] — blocked or unsure; ask a specific clarifying / help question

Optional phase tag (helpful): [[phase:plan|act|reflect|done|ask]]

**Only exit the task with [[decision:done]] or [[decision:ask]].**  
If you omit a decision tag after producing only reasoning, the runtime will \
nudge you to continue iterating.

# Skills
You are extended through SKILLS. Each is listed with name + description only \
until loaded.

RULE: if a request matches a skill description, call the Skill tool with that \
name before other tools for that task (unless no skill applies).

Available skills:
%(skills)s

# Style
- Be concise with the user; put deep reasoning in short bullets.
- Report what actually happened. Never invent tool success.
- Always include a [[decision:…]] tag when you are not calling tools.
"""

_CONTINUE_NUDGE = (
    "[runtime] You produced a reply without tools and without finishing. "
    "Reflect on progress vs the plan. Then either: (1) call tools for the next "
    "step, or (2) end with [[decision:done]] and the user-facing answer, or "
    "(3) end with [[decision:ask]] and a specific question if blocked. "
    "Prefer continuing work over stopping early."
)

_FORCE_ASK_NUDGE = (
    "[runtime] This step failed %(n)s times in a row. Stop retrying the same "
    "approach. Explain briefly what failed and ask the user for help or "
    "clarification. End with [[decision:ask]]."
)

_BUDGET_NUDGE = (
    "[runtime] Iteration budget is nearly exhausted. Summarize progress, "
    "deliver the best answer you can, and end with [[decision:done]] or "
    "[[decision:ask]] if you need the user."
)


@dataclass
class PendingApproval:
    """State frozen while waiting for human tool approval."""

    session_id: str
    messages: List[Dict[str, Any]]
    tool_calls: List[Dict[str, Any]]
    index: int
    rounds_left: int
    consecutive_failures: int = 0
    no_tool_continues: int = 0
    tools_used_this_turn: bool = False


@dataclass
class LoopEvent:
    """Internal loop event before mapping to AgentEvent."""

    kind: str  # status | tool_approval | final | error
    text: str = ""
    tool_call_id: str = ""
    tool_name: str = ""
    tool_args: Dict[str, Any] = field(default_factory=dict)


@dataclass
class _TurnState:
    consecutive_failures: int = 0
    no_tool_continues: int = 0
    tools_used_this_turn: bool = False


class LeanLoop:
    """
    Deliberative multi-session agent loop for agent_framework=lean.

    ``run(query, session_id)`` yields LoopEvents until a final answer, user
    ask, or tool-approval pause. Resume with ``resume(session_id, approved)``.
    """

    def __init__(
        self,
        client: ChatBackend,
        tools: LeanToolRegistry,
        skills: SkillRegistry,
        *,
        instruction: str = "",
        max_tool_iterations: int = 32,
        max_step_retries: int = 3,
        max_no_tool_continues: int = 4,
        history_limit: int = 48,
        tools_approval_required: bool = True,
        auto_approve: Optional[set] = None,
    ) -> None:
        self.client = client
        self.tools = tools
        self.skills = skills
        self.instruction = instruction
        self.max_tool_iterations = max_tool_iterations
        self.max_step_retries = max(1, max_step_retries)
        self.max_no_tool_continues = max(1, max_no_tool_continues)
        self.history_limit = history_limit
        self.tools_approval_required = tools_approval_required
        self.auto_approve = set(auto_approve or AUTO_APPROVE_TOOLS)

        self._history: Dict[str, List[Dict[str, Any]]] = {}
        self._pending: Dict[str, PendingApproval] = {}

    def system_prompt(self) -> str:
        prompt = _BASE_PROMPT % {
            "skills": self.skills.catalog(),
            "max_step_retries": self.max_step_retries,
        }
        extra = self.instruction.strip()
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

        turn = _TurnState()
        yield from self._drive(
            session_id, messages, history, self.max_tool_iterations, turn
        )

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
        turn = _TurnState(
            consecutive_failures=pending.consecutive_failures,
            no_tool_continues=pending.no_tool_continues,
            tools_used_this_turn=pending.tools_used_this_turn,
        )

        if i < len(tool_calls):
            failed = yield from self._execute_one(
                messages, history, tool_calls[i], approved, turn
            )
            if failed and turn.consecutive_failures >= self.max_step_retries:
                self._inject_system(
                    messages,
                    history,
                    _FORCE_ASK_NUDGE % {"n": turn.consecutive_failures},
                )
            i += 1

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
                    consecutive_failures=turn.consecutive_failures,
                    no_tool_continues=turn.no_tool_continues,
                    tools_used_this_turn=turn.tools_used_this_turn,
                )
                yield from self._approval_event(call)
                return
            failed = yield from self._execute_one(
                messages, history, call, approved=True, turn=turn
            )
            if failed and turn.consecutive_failures >= self.max_step_retries:
                self._inject_system(
                    messages,
                    history,
                    _FORCE_ASK_NUDGE % {"n": turn.consecutive_failures},
                )
                break
            i += 1

        yield from self._drive(session_id, messages, history, rounds_left, turn)

    def _inject_system(
        self,
        messages: List[Dict[str, Any]],
        history: List[Dict[str, Any]],
        text: str,
    ) -> None:
        # Use role=user so OpenAI-compatible APIs always accept the nudge
        # (some providers restrict multiple system messages).
        msg = {
            "role": "user",
            "content": text,
        }
        messages.append(msg)
        history.append(msg)

    def _drive(
        self,
        session_id: str,
        messages: List[Dict[str, Any]],
        history: List[Dict[str, Any]],
        rounds_left: int,
        turn: _TurnState,
    ) -> Iterator[LoopEvent]:
        while rounds_left > 0:
            rounds_left -= 1

            if rounds_left <= 1:
                self._inject_system(messages, history, _BUDGET_NUDGE)

            try:
                message = self.client.chat(messages, tools=self.tools.schemas())
            except LLMError as exc:
                yield LoopEvent(kind="error", text=f"LLM error: {exc}")
                return

            tool_calls = message.get("tool_calls") or []
            content = _content_to_text(message.get("content"))
            visible, decision, phase = _parse_control_tags(content)

            assistant_msg: Dict[str, Any] = {
                "role": "assistant",
                "content": content,
            }
            if tool_calls:
                assistant_msg["tool_calls"] = tool_calls
            messages.append(assistant_msg)
            history.append(assistant_msg)

            # ── No tools this round: deliberative continue or exit ────
            if not tool_calls:
                if visible:
                    yield LoopEvent(kind="status", text=visible)

                # Explicit terminal decisions
                if decision in ("done", "ask") or phase in ("done", "ask"):
                    history[:] = self._trim(history)
                    final = visible or content or (
                        "I need your help to proceed."
                        if decision == "ask" or phase == "ask"
                        else "(done)"
                    )
                    yield LoopEvent(kind="final", text=final)
                    return

                # Forced ask after too many step failures
                if turn.consecutive_failures >= self.max_step_retries:
                    history[:] = self._trim(history)
                    final = visible or (
                        "I hit repeated failures and need your help to continue. "
                        "Please clarify the goal or constraints."
                    )
                    if "[[decision:ask]]" not in (content or "").lower():
                        final = final.rstrip() + "\n\n(What should I try next?)"
                    yield LoopEvent(kind="final", text=final)
                    return

                # Keep iterating: reason more / execute next step
                turn.no_tool_continues += 1
                if turn.no_tool_continues >= self.max_no_tool_continues:
                    history[:] = self._trim(history)
                    yield LoopEvent(
                        kind="final",
                        text=visible
                        or content
                        or (
                            "Stopping after several reasoning-only turns without "
                            "a clear done/ask decision. Please restate the goal "
                            "or say how to proceed."
                        ),
                    )
                    return

                logger.info(
                    f"[{session_id}] no-tool continue "
                    f"#{turn.no_tool_continues} decision={decision!r} phase={phase!r}"
                )
                self._inject_system(messages, history, _CONTINUE_NUDGE)
                continue

            # ── Tools requested ───────────────────────────────────────
            turn.no_tool_continues = 0
            turn.tools_used_this_turn = True
            names = [
                (c.get("function") or {}).get("name", "?") for c in tool_calls
            ]
            status = f"Calling tools: {', '.join(names)}"
            if visible:
                status = f"{visible}\n{status}"
            yield LoopEvent(kind="status", text=status)

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
                        consecutive_failures=turn.consecutive_failures,
                        no_tool_continues=turn.no_tool_continues,
                        tools_used_this_turn=turn.tools_used_this_turn,
                    )
                    yield from self._approval_event(call)
                    return

                failed = yield from self._execute_one(
                    messages, history, call, approved=True, turn=turn
                )
                if failed and turn.consecutive_failures >= self.max_step_retries:
                    self._inject_system(
                        messages,
                        history,
                        _FORCE_ASK_NUDGE % {"n": turn.consecutive_failures},
                    )
                    # Let the model produce an ask on the next round
                    break
                i += 1
            # next model round (reflect / next step)

        history[:] = self._trim(history)
        yield LoopEvent(
            kind="final",
            text=(
                f"Stopped after {self.max_tool_iterations} iterations without "
                "[[decision:done]] / [[decision:ask]]. Please continue with a "
                "follow-up or clarify the goal."
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
        turn: _TurnState,
    ):
        """
        Execute one tool call. Yields status LoopEvents.
        Generator return value: True if the tool result looks like a failure.
        """
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

        failed = _tool_result_failed(output) if approved else False
        if failed:
            turn.consecutive_failures += 1
            yield LoopEvent(
                kind="status",
                text=(
                    f"Step failed ({turn.consecutive_failures}/"
                    f"{self.max_step_retries}): {name}"
                ),
            )
        elif approved:
            turn.consecutive_failures = 0

        if len(output) > 12000:
            output = output[:12000] + "\n...[truncated]"
        result_msg = {
            "role": "tool",
            "tool_call_id": call_id,
            "content": output,
        }
        messages.append(result_msg)
        history.append(result_msg)
        return failed


def _parse_control_tags(content: str) -> Tuple[str, Optional[str], Optional[str]]:
    """Return (visible_text, decision, phase)."""
    if not content:
        return "", None, None
    decision = None
    phase = None
    m = _DECISION_RE.search(content)
    if m:
        decision = m.group(1).lower()
    m = _PHASE_RE.search(content)
    if m:
        phase = m.group(1).lower()
    visible = _DECISION_RE.sub("", content)
    visible = _PHASE_RE.sub("", visible)
    visible = re.sub(r"\n{3,}", "\n\n", visible).strip()
    return visible, decision, phase


def _tool_result_failed(output: str) -> bool:
    if not output:
        return True
    lower = output.strip().lower()
    if lower.startswith("error"):
        return True
    if '"success": false' in lower or "'success': false" in lower:
        return True
    if "traceback (most recent call last)" in lower:
        return True
    if "command not found" in lower:
        return True
    if "permission denied" in lower and "error" in lower:
        return True
    return False


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
