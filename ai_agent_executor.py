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

logger = get_logger(__name__)


def _data_part(data: dict) -> Part:
    return Part(data=ParseDict(data, struct_pb2.Value()))


class AIAgentExecutor(BaseExecutor):
    def __init__(self, agent: AIAgent):
        self._agent = agent

    async def _execute_implementation(
        self,
        query: str,
        context: RequestContext,
        updater: TaskUpdater
    ) -> None:
        """Execute the AI agent and send A2UI formatted response."""
        final_response = ""
        async for event in self._agent.run(query):
            if event.is_final_response() and event.content and event.content.parts:
                final_response = event.content.parts[0].text
        logger.info(f"Agent final response: {final_response}")

        if final_response:
            try:
                result_text = remove_think_content(final_response)

                await updater.update_status(
                    TaskState.TASK_STATE_COMPLETED,
                    message=updater.new_agent_message(parts=[
                        _data_part({"beginRendering": {"surfaceId": "main", "root": "response"}}),
                        _data_part({"surfaceUpdate": {
                            "surfaceId": "main",
                            "components": [{"id": "response", "component": {"Text": {"text": {"path": "/result"}}}}]
                        }}),
                        _data_part({"dataModelUpdate": {
                            "surfaceId": "main",
                            "path": "/",
                            "contents": [{"key": "result", "valueString": result_text}]
                        }}),
                    ])
                )

            except Exception as e:
                logger.warning(f"Failure sending A2UI response: {e}")
                await updater.update_status(
                    TaskState.TASK_STATE_FAILED,
                    message=updater.new_agent_message(parts=[Part(text=str(e))])
                )
