from abc import ABC, abstractmethod
from typing import Any, Optional
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.utils.errors import UnsupportedOperationError
from a2a.types import Part, TaskState
from a2a.helpers import new_task, new_task_from_user_message
from google.protobuf.json_format import MessageToDict
from declarative_agent_sdk.agent_logging import get_logger

logger = get_logger(__name__)


class BaseExecutor(AgentExecutor, ABC):
    """Base executor with common logic for query extraction and task management."""

    def _extract_query_from_context(self, context: RequestContext) -> str:
        """
        Extract query from request context by parsing message parts.

        Handles:
        - userAction data parts (with action-specific logic)
        - request data parts (direct query)
        - text parts (fallback)
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
                if part.WhichOneof('content') == 'data':
                    part_data = MessageToDict(part.data)
                    if "userAction" in part_data:
                        ui_event_part = part_data["userAction"]
                        logger.info(f"Found userAction in data part: {ui_event_part}")
                    elif "request" in part_data:
                        query_part = part_data["request"]
                        logger.info(f"Found request in data part with query: {query_part}")
                elif part.WhichOneof('content') == 'text':
                    logger.info(f"Processing text part: {part.text}")

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
            logger.info(f"Using query from request data part: {query}")
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
            UnsupportedOperationError: If task_id or context_id is missing
        """
        logger.info(f"task_id: {context.task_id}, context_id: {context.context_id}")

        if not context.task_id or not context.context_id:
            raise UnsupportedOperationError(message="task_id or context_id is None")

        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        if not context.current_task:
            # The consumer requires a Task object in the queue before it will
            # accept any TaskStatusUpdateEvent. For new tasks the task doesn't
            # exist in the store yet, so enqueue a Task directly first.
            if context.message:
                await event_queue.enqueue_event(
                    new_task_from_user_message(context.message)
                )
            else:
                await event_queue.enqueue_event(
                    new_task(context.task_id, context.context_id, TaskState.TASK_STATE_SUBMITTED)
                )
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
            if updater:
                try:
                    await updater.update_status(
                        TaskState.TASK_STATE_FAILED,
                        message=updater.new_agent_message(parts=[Part(text=f"An error occurred: {str(e)}")]),
                        final=True,
                    )
                except:
                    logger.error(f"Failed to send error status: {e}")

    async def cancel(
        self,
        context: RequestContext,
        event_queue: EventQueue
    ) -> None:
        """Cancel execution - not currently supported."""
        raise UnsupportedOperationError()
