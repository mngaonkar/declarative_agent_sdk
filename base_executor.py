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
            logger.info("No current task found in context, enqueuing new task for TaskUpdater.")
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
        context: RequestContext,
        updater: TaskUpdater
    ) -> None:
        """
        Execute the actual agent/workflow logic.

        This method must be implemented by subclasses to define their specific
        execution behavior (e.g., running an agent vs. invoking a graph).

        Args:
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

        Initializes task updater, delegates to implementation,
        and handles errors uniformly.
        """
        updater = None
        try:
            updater = await self._initialize_task_updater(context, event_queue)
            await self._execute_implementation(context, updater)
        except Exception as e:
            logger.error(f"Error during execution: {e}")
            if updater:
                try:
                    await updater.update_status(
                        TaskState.TASK_STATE_FAILED,
                        message=updater.new_agent_message(parts=[Part(text=f"An error occurred: {str(e)}")])
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
