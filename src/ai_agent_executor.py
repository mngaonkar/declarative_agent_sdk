from a2a.server.agent_execution import RequestContext
from a2a.server.events import EventQueue
from declarative_agent_sdk import AIAgent
from a2a.utils import new_agent_text_message
from a2a.server.tasks import TaskUpdater
from declarative_agent_sdk.agent_logging import get_logger
from declarative_agent_sdk.utils import remove_think_content
from declarative_agent_sdk.base_executor import BaseExecutor

logger = get_logger(__name__)

from a2a.types import (
    DataPart,
    Message,
    Part,
    TaskState,
)

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
        result = await self._agent.run(query)
        logger.info(f"Agent execution result: {result}")
        
        if result:
            # Send A2UI response with all messages together
            try:                    
                # Get the actual result text
                result_text = str(result.get("final_response", result)) if isinstance(result, dict) else str(result)
                result_text = remove_think_content(result_text)
                
                # Send all A2UI messages together
                response_message = Message(
                    role="user",  # type: ignore
                    message_id=f"{context.task_id}-response",
                    parts=[
                        # 1. Begin rendering
                        Part(root=DataPart(data={
                            "beginRendering": {
                                "surfaceId": "main",
                                "root": "response"
                            }
                        })),
                        # 2. Surface update (structure)
                        Part(root=DataPart(data={
                            "surfaceUpdate": {
                                "surfaceId": "main",
                                "components": [
                                    {
                                        "id": "response",
                                        "component": {
                                            "Text": {
                                                "text": {
                                                    "path": "/result"
                                                }
                                            }
                                        }
                                    }
                                ]
                            }
                        })),
                        # 3. Data model update (content)
                        Part(root=DataPart(data={
                            "dataModelUpdate": {
                                "surfaceId": "main",
                                "path": "/",
                                "contents": [
                                    {
                                        "key": "result",
                                        "valueString": result_text
                                    }
                                ]
                            }
                        }))
                    ]
                )
                
                await updater.update_status(
                    TaskState.completed,
                    message=response_message,
                    final=True
                )

            except Exception as e:
                logger.warning(f"Failure sending A2UI response: {e}")
                # Fallback to plain text
                await updater.update_status(
                    TaskState.completed,
                    message=new_agent_text_message(str(result)),
                    final=True
                )
