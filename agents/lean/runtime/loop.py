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
# Common reasoning wrappers (Qwen/DeepSeek-style and generic).
_THINK_RE = re.compile(
    r"<think>(.*?)</think>",
    re.IGNORECASE | re.DOTALL,
)
_THINK_ALT_RE = re.compile(
    r"<thinking>(.*?)</thinking>",
    re.IGNORECASE | re.DOTALL,
)

_PHASE_LABELS = {
    "plan": "Planning",
    "act": "Acting",
    "reflect": "Reflecting",
    "done": "Finishing",
    "ask": "Asking",
}

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
        # Never start mid tool-response block.
        while cut < len(history) and history[cut].get("role") == "tool":
            cut += 1
        # Walk cut earlier until the kept suffix has a valid tool-call chain
        # (no orphan tools, no assistant.tool_calls without responses).
        while cut > 0 and not _tool_chain_valid(history[cut:]):
            cut -= 1
        trimmed = history[cut:]
        # Last resort: fill any remaining holes rather than send an invalid list.
        _close_open_tool_calls(trimmed, reason="Dropped by history trim.")
        return trimmed

    def _needs_approval(self, tool_name: str) -> bool:
        if not self.tools_approval_required:
            return False
        return tool_name not in self.auto_approve

    def run(self, query: str, session_id: str) -> Iterator[LoopEvent]:
        abandoned = self._pending.pop(session_id, None)

        history = self._history.setdefault(session_id, [])
        # A new user message abandons any mid-batch approval. OpenAI-compatible
        # APIs require every assistant tool_call_id to have a tool response
        # before the next non-tool message — close the batch first.
        if abandoned is not None:
            _fill_missing_tool_responses(
                history,
                abandoned.tool_calls,
                reason=(
                    "User sent a new message before tool approval completed; "
                    "this tool was not executed."
                ),
            )
        else:
            _close_open_tool_calls(
                history,
                reason="Incomplete tool batch left from a previous turn.",
            )

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

        stop_batch = False
        if i < len(tool_calls):
            failed = yield from self._execute_one(
                messages, history, tool_calls[i], approved, turn
            )
            if failed and turn.consecutive_failures >= self.max_step_retries:
                stop_batch = True
            i += 1

        while i < len(tool_calls) and not stop_batch:
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
                stop_batch = True
                break
            i += 1

        if stop_batch:
            # Must answer every tool_call_id before the next chat/user message.
            _fill_missing_tool_responses(
                messages,
                tool_calls,
                history=history,
                reason=(
                    f"Skipped after {turn.consecutive_failures} consecutive "
                    "tool failures; ask the user or change approach."
                ),
            )
            self._inject_system(
                messages,
                history,
                _FORCE_ASK_NUDGE % {"n": turn.consecutive_failures},
            )

        yield from self._drive(session_id, messages, history, rounds_left, turn)

    def _inject_system(
        self,
        messages: List[Dict[str, Any]],
        history: List[Dict[str, Any]],
        text: str,
    ) -> None:
        # Never insert a user/system turn while assistant tool_calls are open.
        _close_open_tool_calls(
            messages,
            history=history,
            reason="Closed before runtime nudge.",
        )
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

            # Safety net: OpenAI-compatible APIs reject incomplete tool batches.
            _close_open_tool_calls(
                messages,
                history=history,
                reason="Closed incomplete tool batch before model call.",
            )

            try:
                message = self.client.chat(messages, tools=self.tools.schemas())
            except LLMError as exc:
                yield LoopEvent(kind="error", text=f"LLM error: {exc}")
                return

            tool_calls = message.get("tool_calls") or []
            content = _content_to_text(message.get("content"))
            think_text, without_think = _extract_think_blocks(content)
            visible, decision, phase = _parse_control_tags(without_think)

            assistant_msg: Dict[str, Any] = {
                "role": "assistant",
                "content": content,
            }
            if tool_calls:
                assistant_msg["tool_calls"] = tool_calls
            messages.append(assistant_msg)
            history.append(assistant_msg)

            # Surface model deliberation (think blocks + plan/reason text).
            # Skip when the only content is the final answer (avoids Discord
            # posting the same text as both "thinking" and the reply).
            is_terminal = not tool_calls and (
                decision in ("done", "ask") or phase in ("done", "ask")
            )
            if think_text:
                yield LoopEvent(
                    kind="status",
                    text=_compose_thinking(think_text, "" if is_terminal else visible, phase),
                )
            elif visible and not is_terminal:
                yield LoopEvent(
                    kind="status",
                    text=_compose_thinking("", visible, phase),
                )

            # ── No tools this round: deliberative continue or exit ────
            if not tool_calls:
                # Explicit terminal decisions
                if decision in ("done", "ask") or phase in ("done", "ask"):
                    history[:] = self._trim(history)
                    final = visible or (
                        "I need your help to proceed."
                        if decision == "ask" or phase == "ask"
                        else "(done)"
                    )
                    # If the only content was a think block, fall back to a
                    # short default rather than dumping raw tags.
                    if not visible and think_text:
                        final = (
                            "I need your help to proceed."
                            if decision == "ask" or phase == "ask"
                            else "Done."
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
            yield LoopEvent(
                kind="status", text=f"Calling tools: {', '.join(names)}"
            )

            i = 0
            stop_batch = False
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
                    stop_batch = True
                    break
                i += 1

            if stop_batch:
                # Answer remaining tool_call_ids so the next model call is valid.
                _fill_missing_tool_responses(
                    messages,
                    tool_calls,
                    history=history,
                    reason=(
                        f"Skipped after {turn.consecutive_failures} consecutive "
                        "tool failures; ask the user or change approach."
                    ),
                )
                self._inject_system(
                    messages,
                    history,
                    _FORCE_ASK_NUDGE % {"n": turn.consecutive_failures},
                )
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

        # Ensure the assistant tool_calls entry uses this same id.
        if not call.get("id"):
            call["id"] = call_id

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


def _tool_call_id(call: Dict[str, Any]) -> str:
    cid = call.get("id")
    if cid:
        return str(cid)
    cid = str(uuid.uuid4())
    call["id"] = cid
    return cid


def _answered_tool_ids(messages: List[Dict[str, Any]]) -> set:
    return {
        m.get("tool_call_id")
        for m in messages
        if m.get("role") == "tool" and m.get("tool_call_id")
    }


def _fill_missing_tool_responses(
    messages: List[Dict[str, Any]],
    tool_calls: List[Dict[str, Any]],
    *,
    history: Optional[List[Dict[str, Any]]] = None,
    reason: str,
) -> None:
    """Append synthetic tool messages for any tool_call_id not yet answered."""
    answered = _answered_tool_ids(messages)
    if history is not None:
        answered |= _answered_tool_ids(history)
    for call in tool_calls:
        call_id = _tool_call_id(call)
        if call_id in answered:
            continue
        name = (call.get("function") or {}).get("name") or "unknown"
        msg = {
            "role": "tool",
            "tool_call_id": call_id,
            "content": f"Error: tool `{name}` was not executed. {reason}",
        }
        messages.append(msg)
        if history is not None and history is not messages:
            history.append(msg)
        answered.add(call_id)


def _close_open_tool_calls(
    messages: List[Dict[str, Any]],
    *,
    history: Optional[List[Dict[str, Any]]] = None,
    reason: str,
) -> None:
    """
    Ensure every assistant message with tool_calls is followed by a tool
    response for each tool_call_id before any later non-tool message.
    """
    i = 0
    while i < len(messages):
        msg = messages[i]
        if msg.get("role") != "assistant":
            i += 1
            continue
        tool_calls = msg.get("tool_calls") or []
        if not tool_calls:
            i += 1
            continue

        needed: List[Tuple[str, str]] = []
        for call in tool_calls:
            if not isinstance(call, dict):
                continue
            cid = _tool_call_id(call)
            name = (call.get("function") or {}).get("name") or "unknown"
            needed.append((cid, name))

        j = i + 1
        answered: set = set()
        while j < len(messages) and messages[j].get("role") == "tool":
            tid = messages[j].get("tool_call_id")
            if tid:
                answered.add(tid)
            j += 1

        inserts = [
            {
                "role": "tool",
                "tool_call_id": cid,
                "content": f"Error: tool `{name}` was not executed. {reason}",
            }
            for cid, name in needed
            if cid not in answered
        ]
        if inserts:
            messages[j:j] = inserts
            if history is not None and history is not messages:
                # Mirror inserts into history if this assistant msg is shared.
                _mirror_tool_inserts(history, msg, inserts)
            j += len(inserts)
        i = j


def _mirror_tool_inserts(
    history: List[Dict[str, Any]],
    assistant_msg: Dict[str, Any],
    inserts: List[Dict[str, Any]],
) -> None:
    """Insert the same synthetic tool msgs after assistant_msg in history."""
    try:
        idx = history.index(assistant_msg)
    except ValueError:
        # History may hold a copy; fall back to full sanitize on history alone.
        _close_open_tool_calls(history, reason=inserts[0]["content"] if inserts else "")
        return
    j = idx + 1
    answered = set()
    while j < len(history) and history[j].get("role") == "tool":
        tid = history[j].get("tool_call_id")
        if tid:
            answered.add(tid)
        j += 1
    extra = [m for m in inserts if m.get("tool_call_id") not in answered]
    if extra:
        history[j:j] = extra


def _tool_chain_valid(messages: List[Dict[str, Any]]) -> bool:
    """True if messages form a valid OpenAI-style tool-call chain."""
    if messages and messages[0].get("role") == "tool":
        return False
    open_ids: set = set()
    for msg in messages:
        role = msg.get("role")
        if role == "assistant":
            if open_ids:
                return False
            tool_calls = msg.get("tool_calls") or []
            open_ids = set()
            for call in tool_calls:
                if isinstance(call, dict) and call.get("id"):
                    open_ids.add(call["id"])
        elif role == "tool":
            tid = msg.get("tool_call_id")
            if not tid or tid not in open_ids:
                return False
            open_ids.discard(tid)
        else:
            if open_ids:
                return False
    return not open_ids


def _extract_think_blocks(content: str) -> Tuple[str, str]:
    """Return (think_text, content_with_think_blocks_removed)."""
    if not content:
        return "", ""
    thinks: List[str] = []
    for pattern in (_THINK_RE, _THINK_ALT_RE):
        thinks.extend(m.strip() for m in pattern.findall(content) if m and m.strip())
        content = pattern.sub("", content)
    think_text = "\n\n".join(thinks).strip()
    rest = re.sub(r"\n{3,}", "\n\n", content).strip()
    return think_text, rest


def _compose_thinking(
    think_text: str,
    visible: str,
    phase: Optional[str],
) -> str:
    """Build a user-visible thinking/status string (no control tags)."""
    parts: List[str] = []
    if think_text:
        parts.append(think_text)
    if visible:
        # Skip if visible is already fully covered by think_text
        if not think_text or visible not in think_text:
            parts.append(visible)
    body = "\n\n".join(p for p in parts if p).strip()
    if not body:
        return ""
    label = _PHASE_LABELS.get(phase or "", "")
    if label:
        return f"[{label}] {body}"
    return body


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
