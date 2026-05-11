from a2a.server.agent_execution import RequestContext
from a2a.server.events import EventQueue
from langgraph.graph.state import CompiledStateGraph
from a2a.server.tasks import TaskUpdater
from declarative_agent_sdk.agent_logging import get_logger
from declarative_agent_sdk.base_executor import BaseExecutor

logger = get_logger(__name__)

from a2a.types import (
    Part,
    TextPart,
)

class AIWorkflowExecutor(BaseExecutor):
    def __init__(self, graph: CompiledStateGraph):
        self._graph = graph
        self._state = None

    async def _execute_implementation(
        self,
        query: str,
        context: RequestContext,
        updater: TaskUpdater
    ) -> None:
        """Execute the LangGraph workflow."""
        self._state = {
            "user_query": query,
            "agent_output": {}
        }
        
        result = await self._graph.ainvoke(self._state)
        logger.info(f"Agent execution result: {result}")
        
        if result:
            await updater.add_artifact([Part(root=TextPart(text=str(result)))], name="final_response")
            await updater.complete()
