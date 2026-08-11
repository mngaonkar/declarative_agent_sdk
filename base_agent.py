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
        - run_query  — streams events for a plain-text query
        - invoke     — streams events for an A2A RequestContext

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
    # Concrete helpers derived from run_query
    # ------------------------------------------------------------------

    async def run_query_and_collect(
        self, query: str, session_id: Optional[str] = None
    ) -> str:
        """Run the agent and return the final response text as a string."""
        async for event in self.run_query(query, session_id):
            if (
                event.is_final_response()
                and event.content
                and event.content.parts
            ):
                return event.content.parts[0].text or ""
        return ""

    def run_sync(self, input_text: str, session_id: Optional[str] = None) -> str:
        """
        Synchronous wrapper around run_query_and_collect.
        Cannot be called from within a running event loop.
        """

        async def _collect() -> str:
            return await self.run_query_and_collect(input_text, session_id)

        return asyncio.run(_collect())
