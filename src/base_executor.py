from abc import ABC, abstractmethod
from typing import Any, Optional
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.utils.errors import ServerError
from a2a.utils import new_agent_text_message
from a2a.server.tasks import TaskUpdater
from declarative_agent_sdk.agent_logging import get_logger

from a2a.types import (
    DataPart,
    TaskState,
    TextPart,
    UnsupportedOperationError,
)

logger = get_logger(__name__)


class BaseExecutor(AgentExecutor, ABC):
    """Base executor with common logic for query extraction and task management."""
    
    def _extract_query_from_context(self, context: RequestContext) -> str:
        """
        Extract query from request context by parsing message parts.
        
        Handles:
        - userAction DataParts (with action-specific logic)
        - request DataParts (direct query)
        - TextParts (fallback)
        - Empty messages (uses get_user_input)
        
        Returns:
            The extracted query string
        """
        ui_event_part = None
        query = ""
        action = None
        query_part = None

        logger.info(f"Received execution request with context: {context.message}")
        
        if context.message and context.message.parts:
            logger.info("Executing AI agent with message parts: %s", context.message.parts)
            for i, part in enumerate(context.message.parts):
                if isinstance(part.root, DataPart):
                    if "userAction" in part.root.data:
                        ui_event_part = part.root.data["userAction"]
                        logger.info(f"Found userAction in DataPart: {ui_event_part}")
                    elif "request" in part.root.data:
                        query_part = part.root.data["request"]
                        logger.info(f"Found request in DataPart with query: {query_part}")
                elif isinstance(part.root, TextPart):
                    logger.info(f"Processing TextPart: {part.root.text}")
        
        if ui_event_part:
            action = ui_event_part.get("action")
            ctx = ui_event_part.get("context", {})

            if action == "find_route":
                origin = ctx.get("origin")
                destination = ctx.get("destination")
                logger.info(f"Finding route from {origin} to {destination}")
                query = f"Find a route from {origin} to {destination}"
        elif query_part:
            query = query_part
            logger.info(f"Using query from request DataPart: {query}")
        else:
            logger.warning("No userAction found in message parts. Executing agent with empty input.")
            query = context.get_user_input()
        
        logger.info(f"User input query: {query}")
        return query

    async def _initialize_task_updater(
        self, 
        context: RequestContext, 
        event_queue: EventQueue
    ) -> TaskUpdater:
        """
        Initialize and prepare TaskUpdater for execution.
        
        Args:
            context: Request context with task and context IDs
            event_queue: Event queue for status updates
            
        Returns:
            Initialized TaskUpdater instance
            
        Raises:
            ServerError: If task_id or context_id is missing
        """
        logger.info(f"task_id: {context.task_id}, context_id: {context.context_id}")
        
        if not context.task_id or not context.context_id:
            raise ServerError(error=UnsupportedOperationError(message="task_id or context_id is None"))
        
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        if not context.current_task:
            await updater.submit()
        await updater.start_work()
        
        return updater

    @abstractmethod
    async def _execute_implementation(
        self,
        query: str,
        context: RequestContext,
        updater: TaskUpdater
    ) -> None:
        """
        Execute the actual agent/workflow logic.
        
        This method must be implemented by subclasses to define their specific
        execution behavior (e.g., running an agent vs. invoking a graph).
        
        Args:
            query: The extracted user query
            context: Request context
            updater: TaskUpdater for sending status updates
        """
        pass

    async def execute(
        self,
        context: RequestContext,
        event_queue: EventQueue,
    ) -> None:
        """
        Main execution method with common error handling.
        
        Extracts query, initializes task updater, delegates to implementation,
        and handles errors uniformly.
        """
        updater = None
        try:
            query = self._extract_query_from_context(context)
            updater = await self._initialize_task_updater(context, event_queue)
            await self._execute_implementation(query, context, updater)
        except Exception as e:
            logger.error(f"Error during execution: {e}")
            # Try to send error status if updater was initialized
            if updater:
                try:
                    await updater.update_status(
                        TaskState.failed,
                        message=new_agent_text_message(f"An error occurred: {str(e)}"),
                        final=True,
                    )
                except:
                    # If sending status fails, just log
                    logger.error(f"Failed to send error status: {e}")

    async def cancel(
        self, 
        context: RequestContext, 
        event_queue: EventQueue
    ) -> None:
        """Cancel execution - not currently supported."""
        raise ServerError(error=UnsupportedOperationError())
