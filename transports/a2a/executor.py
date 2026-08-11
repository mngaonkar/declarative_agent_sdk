from a2a.server.agent_execution import RequestContext
from a2a.server.events import EventQueue
from declarative_agent_sdk.core.base_agent import BaseAgent
from a2a.server.tasks import TaskUpdater
from a2a.types import Part, TaskState
from google.protobuf import struct_pb2
from google.protobuf.json_format import ParseDict
from declarative_agent_sdk.core.agent_logging import get_logger
from declarative_agent_sdk.core.utils import remove_think_content
from declarative_agent_sdk.transports.a2a.base_executor import BaseExecutor
from declarative_agent_sdk.transports.a2a.utils import _data_part, ResponseType
from declarative_agent_sdk.transports.a2a.formatters.response_formatter import ResponseFormatter
from declarative_agent_sdk.transports.a2a.converter import A2AConverter

logger = get_logger(__name__)

class AIAgentExecutor(BaseExecutor):
    """Executor for running any BaseAgent and sending updates to the A2A client."""
    def __init__(self, agent: BaseAgent, formatter: ResponseFormatter | None = None):
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
       
        async for event in self._agent.invoke(context):
            logger.info(f"Received event from agent: {event}")
            logger.info(f"final response so far: {event.is_final_response()}")

            if event.is_final_response() and not event.long_running_tool_ids and event.content and event.content.parts:
                """Final response received, send TASK_COMPLETED update to A2A client.
                """
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
                    logger.warning(f"Failure sending response: {e}")
                    await updater.update_status(
                        TaskState.TASK_STATE_FAILED,
                        message=updater.new_agent_message(parts=[Part(text=str(e))])
                    )
            elif event.actions.requested_tool_confirmations or event.long_running_tool_ids:
                """Agent is requesting tool confirmation, send TASK_INPUT_REQUIRED update to A2A client with function call details for confirmation."""
                try:
                    function_id = event.content.parts[0].function_call.id if event.content and event.content.parts and event.content.parts[0].function_call else "unknown"
                    function_name = event.content.parts[0].function_call.args["originalFunctionCall"]["name"] if event.content and event.content.parts and event.content.parts[0].function_call else "unknown"
                    function_args = event.content.parts[0].function_call.args["originalFunctionCall"]["args"] if event.content and event.content.parts and event.content.parts[0].function_call else {}
                    logger.info(f"Agent is requesting tool confirmation for function_id: {function_id}")
                    await updater.update_status(
                        TaskState.TASK_STATE_INPUT_REQUIRED,
                        message=updater.new_agent_message(
                            parts=[_data_part({
                                "function_response": {
                                    "id": function_id,
                                    "name": function_name,
                                    "args": function_args
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
            else:
                """For any other events (like intermediate responses), we can send a TASK_WORKING update to the A2A client to indicate that the agent is still processing."""
                logger.warning("Received event with no actionable content.")
                parts = event.content.parts if event.content and event.content.parts else []
                a2a_parts = A2AConverter().convert_parts_to_a2a(parts)

                if not a2a_parts:
                    logger.warning("No valid parts to send for this event, skipping A2A update.")
                    continue
                logger.info(f"Sending TASK_WORKING update with parts: {a2a_parts}")

                await updater.start_work(message=updater.new_agent_message(parts=a2a_parts))