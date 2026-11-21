from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore
from a2a.utils.constants import DEFAULT_RPC_URL, TransportProtocol, PROTOCOL_VERSION_CURRENT
from a2a.types import AgentInterface
from starlette.applications import Starlette
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Route
import socket

from declarative_agent_sdk.core.agent_logging import get_logger
logger = get_logger(__name__)
from declarative_agent_sdk.core import shares
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

        # Workspace files are served from the address peers already dial, so a
        # /files/<path> link works wherever the agent card does.
        try:
            shares.configure(
                workspace=str(getattr(agent, "workspace_directory", "") or "workspace"),
                base_url=self._agent.agent_card.supported_interfaces[0].url,
            )
        except Exception as exc:      # noqa: BLE001 — sharing is optional; serving is not
            logger.warning(f"file sharing disabled: {exc}")

        request_handler = DefaultRequestHandler(
            agent_executor=self._agent_executor,
            task_store=InMemoryTaskStore(),
            agent_card=self._agent.agent_card,
        )

        routes = (
            create_agent_card_routes(self._agent.agent_card)
            + create_jsonrpc_routes(request_handler, DEFAULT_RPC_URL)
            + [Route("/context", self._context_route, methods=["GET"]),
               Route("/context", self._reset_context_route, methods=["DELETE"]),
               Route("/files/{path:path}", self._file_route, methods=["GET"])]
        )

        self.app = Starlette(routes=routes)
        self.app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_methods=["*"],
            allow_headers=["*"],
        )

    async def _file_route(self, request):
        """
        A file in this agent's workspace, by path, streamed from disk.

        See core/shares.py for what is served and what is refused. Errors say
        why without echoing the requested path.
        """
        files = shares.current()
        if files is None:
            return JSONResponse({"error": "file serving is not enabled"}, status_code=404)
        try:
            path, kind = files.resolve(request.path_params["path"])
        except shares.ShareError as exc:
            return JSONResponse({"error": str(exc)}, status_code=exc.status)
        return FileResponse(
            path, media_type=kind, filename=path.name, content_disposition_type="inline",
            # Files change as agents edit them; always read the current one.
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )

    async def _reset_context_route(self, request):
        """
        Forget one conversation (?session=ID) or every conversation.

        No more exposed than the JSON-RPC endpoint beside it, which takes
        messages from anyone who can reach the port.
        """
        reset = getattr(self._agent, "reset_context", None)
        if reset is None:
            return JSONResponse(
                {"supported": False, "agent": self._agent.name,
                 "note": "this runtime keeps no history the board can clear"},
                status_code=501,
            )
        result = reset(request.query_params.get("session") or None)
        return JSONResponse({"supported": True, "agent": self._agent.name, **result})

    async def _context_route(self, request):
        """
        How big a prompt this agent is prefixing to each model request.

        The conversation history lives in this process and nowhere else: a
        model server only knows about the request it was last handed, and the
        A2A protocol has no shape for "how much context are you carrying?".
        agents-control polls this to show it on the Agents tab.

        Sizes only — no message content is returned, so this exposes nothing
        the agent card does not already.
        """
        report = getattr(self._agent, "context_report", None)
        if report is None:
            return JSONResponse({
                "supported": False,
                "agent": self._agent.name,
                "note": (
                    "this runtime does not report its context size — "
                    "the lean runtime does"
                ),
            })
        try:
            return JSONResponse({"supported": True, **report()})
        except Exception as exc:      # noqa: BLE001 — diagnostics must not 500 silently
            logger.warning(f"/context failed: {exc}")
            return JSONResponse(
                {"supported": False, "agent": self._agent.name, "error": str(exc)},
                status_code=500,
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
