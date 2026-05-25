from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore
from a2a.utils.constants import DEFAULT_RPC_URL
from starlette.applications import Starlette
from starlette.middleware.cors import CORSMiddleware
from declarative_agent_sdk.ai_graph_executor import AIWorkflowExecutor
from langgraph.graph.state import CompiledStateGraph
from declarative_agent_sdk.ai_workflow import AIWorkflow
import socket
from a2a.utils.constants import DEFAULT_RPC_URL, TransportProtocol, PROTOCOL_VERSION_CURRENT
from a2a.types import AgentInterface

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

        if not self._workflow.agent_card.supported_interfaces:
            if host == "0.0.0.0":
                try:
                    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    s.connect(("8.8.8.8", 80))
                    card_host = s.getsockname()[0]
                    s.close()
                except Exception:
                    card_host = socket.gethostname()
            else:
                card_host = host

            url = f"http://{card_host}:{port}/"
            self._workflow.agent_card.supported_interfaces.append(AgentInterface(
                url=url,
                protocol_binding=TransportProtocol.JSONRPC.value,
                protocol_version=PROTOCOL_VERSION_CURRENT,
            ))
            logger.info(f"Agent card URL set to: {url}")
            
        request_handler = DefaultRequestHandler(
            agent_executor=self._workflow_executor,
            task_store=InMemoryTaskStore(),
            agent_card=self._workflow.agent_card,
        )

        routes = (
            create_agent_card_routes(self._workflow.agent_card)
            + create_jsonrpc_routes(request_handler, DEFAULT_RPC_URL)
        )

        self.app = Starlette(routes=routes)
        self.app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_methods=["*"],
            allow_headers=["*"],
        )

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
