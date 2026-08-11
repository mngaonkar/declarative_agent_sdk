"""
Chat backend protocol — the only LLM dependency of the lean core loop.

The harness (history, trim, skills catalog, tool approval, AgentEvent mapping)
stays in native code. Frameworks may supply *other* capabilities (tools, graphs)
but should not own the outer execution loop.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Protocol, runtime_checkable


@runtime_checkable
class ChatBackend(Protocol):
    """One-shot chat completion with optional tool schemas (OpenAI shape)."""

    def chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """
        Return an assistant message dict, e.g.::

            {"role": "assistant", "content": "...", "tool_calls": [...]}
        """
        ...
