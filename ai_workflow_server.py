from a2a.server.apps import A2AStarletteApplication
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore
from starlette.middleware.cors import CORSMiddleware
from starlette.staticfiles import StaticFiles
from declarative_agent_sdk.ai_graph_executor import AIWorkflowExecutor
from langgraph.graph.state import CompiledStateGraph
from declarative_agent_sdk.ai_workflow import AIWorkflow


from declarative_agent_sdk.agent_logging import get_logger
logger = get_logger(__name__)


class AIWorkflowServer():
    def __init__(self, workflow: AIWorkflow, graph: CompiledStateGraph, host: str = "0.0.0.0", port: int = 8000):
        self._workflow = workflow
        self._graph = graph
        self._workflow_executor = AIWorkflowExecutor(graph)
        self._host = host
        self._port = port

        if self._workflow.agent_card is None:
            raise ValueError("agent_card cannot be None")

        request_handler = DefaultRequestHandler(
            agent_executor=self._workflow_executor,
            task_store=InMemoryTaskStore(),
        )
        self.server = A2AStarletteApplication(
            agent_card=self._workflow.agent_card, http_handler=request_handler
        )

        
        self.app = self.server.build()
        self.app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_methods=["*"],
            allow_headers=["*"])
        
        # self.app.mount("/static", StaticFiles(directory="images"), name="static")

    def run(self):
        try:
            import uvicorn
        except ImportError:
            logger.error("uvicorn is not installed. Please install it with 'pip install uvicorn' to run the server.")
            return
        
        try:
            uvicorn.run(self.app, host=self._host, port=self._port)
        except Exception as e:
            logger.error(f"Error running server: {e}")
