"""Abstract base class shared by all SDK agent implementations."""

import asyncio
from abc import ABC, abstractmethod
from typing import Any, AsyncGenerator, Optional

from declarative_agent_sdk.agent_logging import get_logger

logger = get_logger(__name__)


class BaseAgent(ABC):
    """
    Common interface for all agents in the SDK.

    Concrete subclasses must implement:
        - run_query  — streams AgentEvent (or compatible) for a plain-text query
        - invoke     — streams events for an A2A RequestContext

    Tool approval (optional per backend):
        - tool_confirmation — resume after human approve/deny
          (Discord and A2A call this name; keep it stable)

    run_query_and_collect and run_sync are provided here so that every
    subclass gets them for free without code duplication.

    Expected attributes set by subclasses:
        name (str), description (str), agent_card (AgentCard)
    """

    # ------------------------------------------------------------------
    # Abstract interface
    # ------------------------------------------------------------------

    @abstractmethod
    async def run_query(
        self, query: str, session_id: Optional[str] = None
    ) -> AsyncGenerator[Any, None]:
        """Yield agent events for a plain-text query."""
        ...  # pragma: no cover

    @abstractmethod
    async def invoke(self, context: Any) -> AsyncGenerator[Any, None]:
        """Yield agent events for an A2A RequestContext."""
        ...  # pragma: no cover

    # ------------------------------------------------------------------
    # Tool approval (default: unsupported)
    # ------------------------------------------------------------------

    async def tool_confirmation(
        self,
        context_id: str,
        session_id: str,
        yes: bool,
    ) -> AsyncGenerator[Any, None]:
        """
        Resume after a human approve/deny for a pending tool call.

        DiscordAgentServer and A2A clients call this method by name.
        Backends that support approval (ADK, deepagent) override it.
        The default implementation yields nothing (no-op).
        """
        logger.warning(
            f"Agent '{getattr(self, 'name', type(self).__name__)}' does not "
            "support tool_confirmation; ignoring resume "
            f"(context_id={context_id}, yes={yes})"
        )
        if False:  # make this an async generator
            yield None  # pragma: no cover

    # Alias used in docs / newer call sites
    async def confirm_tool(
        self,
        context_id: str,
        session_id: str,
        yes: bool,
    ) -> AsyncGenerator[Any, None]:
        async for event in self.tool_confirmation(context_id, session_id, yes):
            yield event

    # ------------------------------------------------------------------
    # Concrete helpers derived from run_query
    # ------------------------------------------------------------------

    async def run_query_and_collect(
        self, query: str, session_id: Optional[str] = None
    ) -> str:
        """Run the agent and return the final response text as a string."""
        async for event in self.run_query(query, session_id):
            is_final = getattr(event, "is_final_response", None)
            if not callable(is_final) or not is_final():
                continue
            content = getattr(event, "content", None)
            parts = getattr(content, "parts", None) or []
            if parts and getattr(parts[0], "text", None):
                return parts[0].text or ""
        return ""

    def run_sync(self, input_text: str, session_id: Optional[str] = None) -> str:
        """
        Synchronous wrapper around run_query_and_collect.
        Cannot be called from within a running event loop.
        """

        async def _collect() -> str:
            return await self.run_query_and_collect(input_text, session_id)

        return asyncio.run(_collect())
