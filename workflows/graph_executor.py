from a2a.server.agent_execution import RequestContext
from a2a.server.events import EventQueue
from langgraph.graph.state import CompiledStateGraph
from a2a.server.tasks import TaskUpdater
from declarative_agent_sdk.core.agent_logging import get_logger
from declarative_agent_sdk.transports.a2a.base_executor import BaseExecutor

logger = get_logger(__name__)

from a2a.types import Part

class AIWorkflowExecutor(BaseExecutor):
    def __init__(self, graph: CompiledStateGraph):
        self._graph = graph
        self._state = None

    async def _execute_implementation(
        self,
        context: RequestContext,
        updater: TaskUpdater
    ) -> None:
        """Execute the LangGraph workflow."""
        if context is None:
            raise ValueError("RequestContext cannot be None")
        
        query = context.get_user_input()
        self._state = {
            "user_query": query,
            "context": context,
            "agents_output": {}
        }
        
        logger.info(f"Executing AI workflow with context: {context}")
        result = await self._graph.ainvoke(self._state)
        logger.info(f"Agent execution result: {result}")
        
        if result:
            await updater.add_artifact([Part(text=str(result))], name="final_response")
            await updater.complete()
