from a2a.server.apps import A2AStarletteApplication
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore
from starlette.middleware.cors import CORSMiddleware
from starlette.staticfiles import StaticFiles
import socket
import os


from declarative_agent_sdk.agent_logging import get_logger
logger = get_logger(__name__)
from declarative_agent_sdk import AIAgent
from declarative_agent_sdk import AIAgentExecutor

class AIAgentServer():
    def __init__(self, agent: AIAgent, host: str = "0.0.0.0", port: int = 8000):
        self._agent = agent
        self._agent_executor = AIAgentExecutor(agent)
        self._host = host
        self._port = port

        if self._agent.agent_card is None:
            raise ValueError("agent_card cannot be None")
        
        # Determine the actual URL for the agent card
        # Priority: published_url in YAML > host parameter > detected IP address
        if self._agent.agent_card.url is None:
            if host == "0.0.0.0":
                # Try to detect the actual IP address
                try:
                    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    s.connect(("8.8.8.8", 80))
                    card_host = s.getsockname()[0]
                    s.close()
                except Exception:
                    card_host = socket.gethostname()
            else:
                card_host = host

            self._agent.agent_card.url = f"http://{card_host}:{port}/"
            logger.info(f"Agent card URL set to: {self._agent.agent_card.url}")    

        request_handler = DefaultRequestHandler(
            agent_executor=self._agent_executor,
            task_store=InMemoryTaskStore(),
        )
        self.server = A2AStarletteApplication(
            agent_card=self._agent.agent_card, http_handler=request_handler
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
