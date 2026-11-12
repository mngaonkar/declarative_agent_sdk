from abc import ABC, abstractmethod

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.utils.errors import A2AError, UnsupportedOperationError
from a2a.types import Part, TaskState, Task, TaskStatus

from declarative_agent_sdk.core.agent_logging import get_logger

logger = get_logger(__name__)

# What a caller is told when the agent or workflow raises. The exception itself
# is logged, not sent: str(exc) routinely carries filesystem paths, prompt
# fragments and provider error bodies, and any client that can reach the RPC
# endpoint would receive them.
EXECUTION_FAILED_MESSAGE = "The agent could not complete this request."


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

    async def fail(self, updater: TaskUpdater, text: str = EXECUTION_FAILED_MESSAGE) -> None:
        """Move the task to FAILED, tolerating a failure to send that update."""
        try:
            await updater.update_status(
                TaskState.TASK_STATE_FAILED,
                message=updater.new_agent_message(parts=[Part(text=text)]),
            )
        except Exception:
            # Not a bare `except`: that also swallowed CancelledError and
            # KeyboardInterrupt, so a cancelled request could not be cancelled
            # and Ctrl-C was absorbed by the server.
            logger.exception("Failed to send the FAILED status")

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
        # Outside the try below, deliberately. An A2AError raised here (missing
        # task_id, context_id or message) is a protocol error that the request
        # handler maps to a JSON-RPC or gRPC error for the caller. Catching it
        # alongside agent failures left the caller with an empty stream and no
        # terminal event at all, because there is no TaskUpdater yet to report
        # a failure through.
        updater = await self._initialize_task_updater(context, event_queue)

        logger.info(
            f"Updater initialized successfully for task_id: {context.task_id}, "
            f"context_id: {context.context_id}"
        )

        try:
            await self._execute_implementation(context, updater)
        except A2AError:
            # Same reasoning: protocol errors belong to the request handler.
            raise
        except Exception:
            # exception() so the traceback reaches the log, while the client
            # gets EXECUTION_FAILED_MESSAGE instead of str(exc).
            logger.exception("Error during execution")
            await self.fail(updater)

    async def cancel(
        self,
        context: RequestContext,
        event_queue: EventQueue
    ) -> None:
        """Cancel execution - not currently supported."""
        raise UnsupportedOperationError()
