"""
Canonical agent stream events.

Both ADKAIAgent (ADK) and LangChainAIAgent (deepagent) yield ``AgentEvent``.
Transports (Discord, A2A) already duck-type a small ADK-like surface:

    event.is_final_response()
    event.content.parts[*].text / .function_call
    event.long_running_tool_ids
    event.actions.requested_tool_confirmations

``AgentEvent`` implements that surface so Discord needs no changes while
backends stop inventing private event shapes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# ADK-compatible content tree (what Discord / A2A already parse)
# ---------------------------------------------------------------------------

class FunctionCall:
    """Mirrors ADK function_call parts used for tool approval prompts."""

    def __init__(self, call_id: str, name: str, args: Optional[Dict[str, Any]] = None) -> None:
        self.id = call_id
        self.name = name
        self.args = {
            "originalFunctionCall": {
                "name": name,
                "args": args or {},
            }
        }


class Part:
    def __init__(
        self,
        text: Optional[str] = None,
        function_call: Optional[FunctionCall] = None,
    ) -> None:
        self.text = text
        self.function_call = function_call


class Content:
    def __init__(self, parts: Optional[List[Part]] = None) -> None:
        self.parts = parts or []

    @classmethod
    def text(cls, text: str) -> "Content":
        return cls([Part(text=text)])

    @classmethod
    def tool_call(cls, call_id: str, name: str, args: Optional[Dict[str, Any]] = None) -> "Content":
        return cls([Part(function_call=FunctionCall(call_id, name, args))])


class Actions:
    def __init__(self, requested_tool_confirmations: Optional[List[str]] = None) -> None:
        self.requested_tool_confirmations: List[str] = list(requested_tool_confirmations or [])


# ---------------------------------------------------------------------------
# AgentEvent
# ---------------------------------------------------------------------------

@dataclass
class AgentEvent:
    """
    One step in an agent run.

    ``kind`` is for SDK-side clarity; transports rely on the ADK-like fields.
    """

    kind: str = "text"  # text | status | tool_approval | error
    text: str = ""
    is_final: bool = False
    tool_call_id: Optional[str] = None
    tool_name: Optional[str] = None
    tool_args: Dict[str, Any] = field(default_factory=dict)

    # Duck-type fields (Discord / A2A)
    content: Optional[Content] = None
    long_running_tool_ids: List[str] = field(default_factory=list)
    actions: Actions = field(default_factory=Actions)

    def __post_init__(self) -> None:
        if self.kind == "tool_approval":
            call_id = self.tool_call_id or "unknown"
            name = self.tool_name or "unknown"
            if self.content is None:
                self.content = Content.tool_call(call_id, name, self.tool_args)
            if call_id not in self.long_running_tool_ids:
                self.long_running_tool_ids = [call_id]
            if call_id not in self.actions.requested_tool_confirmations:
                self.actions = Actions([call_id])
            self.is_final = False
        elif self.content is None and self.text:
            self.content = Content.text(self.text)

    def is_final_response(self) -> bool:
        """True for the agent's completed answer (not a tool-approval pause)."""
        return bool(self.is_final) and not self.long_running_tool_ids

    # -- constructors -------------------------------------------------------

    @classmethod
    def final_text(cls, text: str) -> "AgentEvent":
        return cls(kind="text", text=text, is_final=True)

    @classmethod
    def status(cls, text: str) -> "AgentEvent":
        return cls(kind="status", text=text, is_final=False)

    @classmethod
    def tool_approval(
        cls,
        call_id: str,
        name: str,
        args: Optional[Dict[str, Any]] = None,
    ) -> "AgentEvent":
        return cls(
            kind="tool_approval",
            tool_call_id=call_id,
            tool_name=name,
            tool_args=dict(args or {}),
            is_final=False,
        )

    # Back-compat name used by earlier LangChainEvent API / tests
    @classmethod
    def tool_confirmation(
        cls,
        call_id: str,
        name: str,
        args: Optional[Dict[str, Any]] = None,
    ) -> "AgentEvent":
        return cls.tool_approval(call_id, name, args)

    @classmethod
    def error(cls, text: str) -> "AgentEvent":
        return cls(kind="error", text=text, is_final=True)


# ---------------------------------------------------------------------------
# ADK → AgentEvent
# ---------------------------------------------------------------------------

def from_adk_event(event: Any) -> Optional[AgentEvent]:
    """
    Map a Google ADK Event onto AgentEvent.

    Returns None for empty / non-actionable events (caller should skip).
    """
    if event is None:
        return None

    # Already canonical
    if isinstance(event, AgentEvent):
        return event

    long_running = list(getattr(event, "long_running_tool_ids", None) or [])
    actions = getattr(event, "actions", None)
    requested = list(getattr(actions, "requested_tool_confirmations", None) or [])

    content = getattr(event, "content", None)
    parts = list(getattr(content, "parts", None) or [])

    # Tool approval pause
    if long_running or requested:
        call_id = "unknown"
        name = "unknown"
        args: Dict[str, Any] = {}
        if parts:
            fc = getattr(parts[0], "function_call", None)
            if fc is not None:
                call_id = getattr(fc, "id", None) or call_id
                fc_args = getattr(fc, "args", None) or {}
                if isinstance(fc_args, dict):
                    original = fc_args.get("originalFunctionCall") or {}
                    name = original.get("name") or getattr(fc, "name", None) or name
                    args = original.get("args") or {}
                else:
                    name = getattr(fc, "name", None) or name
        return AgentEvent.tool_approval(str(call_id), str(name), args if isinstance(args, dict) else {})

    # Text (final or intermediate)
    texts = [p.text for p in parts if getattr(p, "text", None)]
    text = "\n".join(t for t in texts if t).strip()
    is_final_fn = getattr(event, "is_final_response", None)
    is_final = bool(is_final_fn()) if callable(is_final_fn) else False

    if text:
        return AgentEvent.final_text(text) if is_final else AgentEvent.status(text)

    # Non-text intermediate (e.g. bare function calls without confirmation flag):
    # keep a thin status so A2A still sees activity without inventing text.
    if parts and not is_final:
        return AgentEvent.status("Working…")

    return None
