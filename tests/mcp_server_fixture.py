"""
A tiny MCP server for the tests, run over stdio: ``python tests/mcp_server_fixture.py``.

Tools: echo (read-only), add, picture (an image), wipe (destructive), slow,
broken (raises), and die (exits the process, to test reconnecting).
"""

import base64
import os
import sys
import time

from mcp.server.mcpserver import Image, MCPServer
from mcp.types import ToolAnnotations

server = MCPServer(name="fixture")

# The smallest valid PNG: one transparent pixel.
PIXEL = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")


@server.tool(annotations=ToolAnnotations(read_only_hint=True))
def echo(text: str) -> str:
    """Repeat the text back."""
    return f"echo: {text}"


@server.tool()
def add(a: int, b: int) -> str:
    """Add two whole numbers."""
    return str(a + b)


@server.tool()
def picture() -> Image:
    """Return a one-pixel PNG."""
    return Image(data=PIXEL, format="png")


@server.tool(annotations=ToolAnnotations(destructive_hint=True))
def wipe(target: str) -> str:
    """Pretend to delete something."""
    return f"wiped {target}"


@server.tool()
def slow(seconds: float) -> str:
    """Sleep, then answer."""
    time.sleep(seconds)
    return "done"


@server.tool()
def broken() -> str:
    """Always fails."""
    raise ValueError("this tool always fails")


@server.tool()
def die() -> str:
    """Exit the server process without answering."""
    os._exit(3)


def serve_http(port: int, token: str) -> None:
    """Streamable HTTP on /mcp, refusing requests without ``Bearer <token>``."""
    import uvicorn

    app = server.streamable_http_app()

    async def guarded(scope, receive, send):
        if scope["type"] == "http":
            headers = dict(scope.get("headers") or [])
            if headers.get(b"authorization", b"").decode() != f"Bearer {token}":
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"text/plain")]})
                await send({"type": "http.response.body", "body": b"no token"})
                return
        await app(scope, receive, send)

    uvicorn.run(guarded, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    if os.environ.get("FIXTURE_HTTP_PORT"):
        serve_http(int(os.environ["FIXTURE_HTTP_PORT"]), os.environ.get("FIXTURE_TOKEN", ""))
        sys.exit(0)
    if os.environ.get("FIXTURE_STDERR"):
        print(os.environ["FIXTURE_STDERR"], file=sys.stderr, flush=True)
    if os.environ.get("FIXTURE_FAIL_START"):
        sys.exit("fixture refused to start")
    server.run("stdio")
