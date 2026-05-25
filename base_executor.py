from abc import ABC, abstractmethod

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.utils.errors import UnsupportedOperationError
from a2a.types import Part, TaskState, Task, TaskStatus

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
            UnsupportedOperationError: If task_id, context_id, or message is missing
        """
        logger.info(f"task_id: {context.task_id}, context_id: {context.context_id}")

        if not context.task_id or not context.context_id or not context.message:
            raise UnsupportedOperationError(message="task_id, context_id, or message is None")

        user_message = context.message
        task_id = context.task_id
        context_id = context.context_id

        task = context.current_task
        if not task:
            await event_queue.enqueue_event(
                Task(
                    id=task_id,
                    context_id=context_id,
                    status=TaskStatus(state=TaskState.TASK_STATE_SUBMITTED),
                    history=[user_message],
                )
            )

        updater = TaskUpdater(event_queue, 
                              context.task_id, 
                              context.context_id)
        
        working_message = updater.new_agent_message(
            parts=[Part(text='Processing your question...')]
        )

        await updater.start_work(message=working_message)

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
            assert updater is not None, "TaskUpdater initialization failed"

            logger.info(f"Updater initialized successfully for task_id: {context.task_id}, context_id: {context.context_id}")
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
