from a2a.server.agent_execution import RequestContext
from a2a.server.events import EventQueue
from declarative_agent_sdk import AIAgent
from a2a.server.tasks import TaskUpdater
from a2a.types import Part, TaskState
from google.protobuf import struct_pb2
from google.protobuf.json_format import ParseDict
from declarative_agent_sdk.agent_logging import get_logger
from declarative_agent_sdk.utils import remove_think_content
from declarative_agent_sdk.base_executor import BaseExecutor
from declarative_agent_sdk.a2a_utils import _data_part, ResponseType
from declarative_agent_sdk.response_formatter import ResponseFormatter

logger = get_logger(__name__)

class AIAgentExecutor(BaseExecutor):
    """Executor for running an AIAgent and sending updates to the A2A client."""
    def __init__(self, agent: AIAgent, formatter: ResponseFormatter | None = None):
        self._agent = agent
        self._formatter = formatter

    async def _execute_implementation(
        self,
        context: RequestContext,
        updater: TaskUpdater
    ) -> None:
        """Execute the AI agent and send A2UI formatted response."""
        final_response = ""
        logger.info(f"Executing agent with context: {context.message}")
       
        async for event in self._agent.run(context):
            logger.info(f"Received event from agent: {event}")
            logger.info(f"final response so far: {event.is_final_response()}")

            if event.is_final_response() and not event.long_running_tool_ids and event.content and event.content.parts:
                final_response = event.content.parts[0].text
                if not final_response:
                    logger.warning("Final response is empty, skipping A2UI update.")
                    continue

                logger.info(f"Agent final response: {final_response}")

                try:
                    result_text = remove_think_content(final_response)

                    await updater.update_status(
                        TaskState.TASK_STATE_COMPLETED,
                        message=self._formatter.format_response(result_text, ResponseType.TASK_COMPLETED, {"response": result_text}) if self._formatter else None
                    )

                except Exception as e:
                    logger.warning(f"Failure sending A2UI response: {e}")
                    await updater.update_status(
                        TaskState.TASK_STATE_FAILED,
                        message=updater.new_agent_message(parts=[Part(text=str(e))])
                    )
            elif event.actions.requested_tool_confirmations or event.long_running_tool_ids:
                try:
                    function_id = event.content.parts[0].function_call.id if event.content and event.content.parts and event.content.parts[0].function_call else "unknown"
                    logger.info(f"Agent is requesting tool confirmation for function_id: {function_id}")
                    await updater.update_status(
                        TaskState.TASK_STATE_INPUT_REQUIRED,
                        message=updater.new_agent_message(
                            parts=[_data_part({
                                "function_response": {
                                    "id": function_id,
                                    "name": "adk_request_confirmation"
                                }
                            })]
                        )
                    )
                except Exception as e:
                    logger.warning(f"Failure sending event response: {e}")
                    await updater.update_status(
                        TaskState.TASK_STATE_FAILED,
                        message=updater.new_agent_message(parts=[Part(text=str(e))])
                    )