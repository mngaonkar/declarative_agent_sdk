from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore
from a2a.utils.constants import DEFAULT_RPC_URL, TransportProtocol, PROTOCOL_VERSION_CURRENT
from a2a.types import AgentInterface
from starlette.applications import Starlette
from starlette.middleware.cors import CORSMiddleware
import socket

from declarative_agent_sdk.core.agent_logging import get_logger
logger = get_logger(__name__)
from declarative_agent_sdk.core.base_agent import BaseAgent
from declarative_agent_sdk.transports.a2a.executor import AIAgentExecutor
from declarative_agent_sdk.transports.a2a.formatters.a2ui_response_formatter import A2UIResponseFormatter
from declarative_agent_sdk.transports.a2a.formatters.text_response_formatter import TextResponseFormatter


class AIAgentServer():
    def __init__(self, agent: BaseAgent, host: str = "0.0.0.0", port: int = 8000):
        self._agent = agent
        self._agent_executor = AIAgentExecutor(agent, formatter=TextResponseFormatter())
        self._host = host
        self._port = port

        if self._agent.agent_card is None:
            raise ValueError("agent_card cannot be None")

        if not self._agent.agent_card.supported_interfaces:
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
            self._agent.agent_card.supported_interfaces.append(AgentInterface(
                url=url,
                protocol_binding=TransportProtocol.JSONRPC.value,
                protocol_version=PROTOCOL_VERSION_CURRENT,
            ))
            logger.info(f"Agent card URL set to: {url}")

        request_handler = DefaultRequestHandler(
            agent_executor=self._agent_executor,
            task_store=InMemoryTaskStore(),
            agent_card=self._agent.agent_card,
        )

        routes = (
            create_agent_card_routes(self._agent.agent_card)
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
        # Both failure paths below raise rather than return. Logging and
        # returning meant a server that never bound a socket exited 0, which a
        # supervisor (systemd, launchd, Docker) reads as a clean shutdown and
        # `systemctl start` reports as success.
        try:
            import uvicorn
        except ImportError as exc:
            raise RuntimeError(
                "uvicorn is required to run the A2A server — pip install uvicorn"
            ) from exc

        # Deliberately not wrapped: "address already in use" and friends must
        # reach the caller as a non-zero exit, not a log line.
        uvicorn.run(self.app, host=self._host, port=self._port)
