"""
The connections to an agent's MCP servers (MCP-TOOL-*, MCP-LIFE-*).

The MCP client library is async and the lean loop is not, so the hub runs its
own event loop on a background thread. Each server is held open by one task on
that loop for its whole life: the library's transports are anyio task groups,
which must be entered and left by the same task. Calls come in from any
thread (``call``) or any event loop (``acall``) and run on the hub's loop.
"""

from __future__ import annotations

import asyncio
import atexit
import re
import tempfile
import threading
import time
import weakref
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from declarative_agent_sdk.core.agent_logging import get_logger
from declarative_agent_sdk.tools.mcp.config import McpServerConfig
from declarative_agent_sdk.tools.mcp.results import render_result

logger = get_logger(__name__)

TOOL_NAME_MAX = 64          # what OpenAI-style function names allow
SEPARATOR = "__"


class McpError(RuntimeError):
    """The MCP layer could not do what was asked (not a tool's own error)."""


def tool_name(server: str, tool: str) -> str:
    """``<server>__<tool>`` with only [A-Za-z0-9_-], at most 64 characters (MCP-TOOL-1)."""
    clean = re.sub(r"[^A-Za-z0-9_-]", "_", tool)
    return f"{server}{SEPARATOR}{clean}"[:TOOL_NAME_MAX]


@dataclass
class McpTool:
    """One server tool as the runtimes see it."""

    name: str                       # <server>__<tool>
    server: str
    remote_name: str                # the server's own name for it
    description: str
    input_schema: Dict[str, Any]
    read_only: bool = False
    destructive: bool = False
    approval: str = "false"

    @property
    def needs_approval(self) -> bool:
        """MCP-APPR-1, before the agent-wide tools_approval_required."""
        if self.approval == "true":
            return True
        if self.approval == "destructive":
            return self.destructive
        return False


def _import_client():
    try:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        from mcp.client.sse import sse_client
        from mcp.client.streamable_http import streamable_http_client
        from mcp.shared._httpx_utils import create_mcp_http_client
        import mcp.types as types
    except ImportError as exc:
        raise McpError(
            "this agent declares mcp_servers, but the MCP client is not installed — "
            "install the SDK with the mcp extra: declarative-agent-sdk[mcp]") from exc
    return {
        "ClientSession": ClientSession, "StdioServerParameters": StdioServerParameters,
        "stdio_client": stdio_client, "sse_client": sse_client,
        "streamable_http_client": streamable_http_client,
        "create_mcp_http_client": create_mcp_http_client, "types": types,
    }


@dataclass
class _Server:
    config: McpServerConfig
    session: Any = None
    tools: List[McpTool] = field(default_factory=list)
    error: str = ""
    connected_at: Optional[float] = None
    attempts: int = 0
    task: Optional[asyncio.Task] = None
    settled: Optional[asyncio.Event] = None      # an attempt finished, good or bad
    restart: Optional[asyncio.Event] = None
    stop: Optional[asyncio.Event] = None
    errlog: Any = None


class McpHub:
    """Starts, holds and calls one agent's MCP servers."""

    def __init__(self, servers: List[McpServerConfig], workspace: str) -> None:
        self.workspace = workspace
        self._servers: Dict[str, _Server] = {s.name: _Server(config=s) for s in servers}
        self._tools: Dict[str, McpTool] = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._closed = False
        self._lib: Dict[str, Any] = {}

    # ------------------------------------------------------------------ life

    def start(self) -> "McpHub":
        """Connect to every server, in parallel (MCP-LIFE-1). Never raises for a server."""
        if not self._servers:
            return self
        self._lib = _import_client()
        ready = threading.Event()

        def run_loop() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            ready.set()
            loop.run_forever()
            # Whatever is still running (a transport closing) is finished or
            # cancelled before the loop goes, so nothing is left half-closed.
            leftover = [t for t in asyncio.all_tasks(loop) if not t.done()]
            for task in leftover:
                task.cancel()
            if leftover:
                loop.run_until_complete(asyncio.gather(*leftover, return_exceptions=True))
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()

        self._thread = threading.Thread(target=run_loop, name="mcp-hub", daemon=True)
        self._thread.start()
        ready.wait()
        longest = max(s.config.startup_timeout for s in self._servers.values())
        try:
            self._submit(self._start_all()).result(timeout=longest + 5)
        except Exception as exc:  # noqa: BLE001 — a bad server must not stop the agent
            logger.warning(f"[mcp] starting servers: {type(exc).__name__}: {exc}")
        for server in self._servers.values():
            if server.session is None:
                logger.warning(f"[mcp] {server.config.name} not started "
                               f"({server.config.describe()}): {server.error}")
            else:
                logger.info(f"[mcp] {server.config.name}: {len(server.tools)} tool(s) "
                            f"({server.config.describe()})")
        _open_hubs.add(self)
        return self

    def close(self) -> None:
        """Close every session and stop every stdio process (MCP-LIFE-4)."""
        if self._closed or self._loop is None:
            return
        self._closed = True
        try:
            self._submit(self._stop_all(), closing=True).result(timeout=15)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[mcp] closing: {type(exc).__name__}: {exc}")
        self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=5)

    # ----------------------------------------------------------------- tools

    @property
    def tools(self) -> List[McpTool]:
        return list(self._tools.values())

    def call(self, name: str, arguments: Optional[Dict[str, Any]] = None) -> str:
        """Call a tool from synchronous code (the lean loop)."""
        tool = self._tool(name)
        timeout = self._servers[tool.server].config.timeout
        return self._submit(self._call(tool, arguments or {})).result(timeout=timeout * 2 + 15)

    async def acall(self, name: str, arguments: Optional[Dict[str, Any]] = None) -> str:
        """Call a tool from another event loop (ADK, deepagent)."""
        tool = self._tool(name)
        return await asyncio.wrap_future(self._submit(self._call(tool, arguments or {})))

    def status(self) -> List[Dict[str, Any]]:
        """Each server's state, for the A2A /mcp route (MCP-LIFE-5)."""
        return [{
            "name": s.config.name,
            "transport": s.config.transport,
            "where": s.config.describe(),
            "connected": s.session is not None,
            "tools": [t.remote_name for t in s.tools],
            "error": s.error,
            "connected_at": s.connected_at,
            "approval": s.config.approval,
        } for s in self._servers.values()]

    # --------------------------------------------------------------- private

    def _tool(self, name: str) -> McpTool:
        tool = self._tools.get(name)
        if tool is None:
            raise McpError(f"no MCP tool named {name!r}")
        return tool

    def _submit(self, coro, closing: bool = False):
        if self._loop is None or (self._closed and not closing):
            coro.close()
            raise McpError("the MCP hub is not running")
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    async def _start_all(self) -> None:
        for server in self._servers.values():
            server.settled, server.restart, server.stop = (
                asyncio.Event(), asyncio.Event(), asyncio.Event())
            server.task = asyncio.create_task(self._hold(server))
        await asyncio.gather(*(self._settle(s) for s in self._servers.values()))
        self._index_tools()

    async def _settle(self, server: _Server) -> None:
        try:
            await asyncio.wait_for(server.settled.wait(), server.config.startup_timeout)
        except asyncio.TimeoutError:
            server.error = (f"did not start within {server.config.startup_timeout:g}s"
                            + self._stderr_tail(server))
            server.restart.clear()
            if server.task is not None:
                server.task.cancel()

    async def _stop_all(self) -> None:
        for server in self._servers.values():
            if server.stop is not None:
                server.stop.set()
        tasks = [s.task for s in self._servers.values() if s.task is not None]
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=8)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.wait(pending, timeout=4)

    def _transport(self, config: McpServerConfig, stack: AsyncExitStack, server: _Server):
        lib = self._lib
        if config.transport == "stdio":
            import os
            params = lib["StdioServerParameters"](
                command=config.command, args=config.args,
                env={**os.environ, **config.env} if config.env else None, cwd=config.cwd)
            if server.errlog is None:
                server.errlog = tempfile.TemporaryFile(mode="w+", encoding="utf-8",
                                                       errors="replace")
            return lib["stdio_client"](params, errlog=server.errlog)
        if config.transport == "sse":
            return lib["sse_client"](config.url, headers=config.headers or None,
                                     timeout=config.startup_timeout)
        return None        # http: built in _hold, it needs its client entered first

    async def _hold(self, server: _Server) -> None:
        """Keep one server connected until stop (MCP-LIFE-1, MCP-LIFE-2)."""
        config, lib = server.config, self._lib
        while not server.stop.is_set():
            server.restart.clear()
            server.attempts += 1
            try:
                async with AsyncExitStack() as stack:
                    if config.transport == "http":
                        client = await stack.enter_async_context(
                            lib["create_mcp_http_client"](headers=config.headers or None))
                        streams = await stack.enter_async_context(
                            lib["streamable_http_client"](config.url, http_client=client))
                    else:
                        streams = await stack.enter_async_context(
                            self._transport(config, stack, server))
                    read, write = streams[0], streams[1]
                    session = await stack.enter_async_context(
                        lib["ClientSession"](read, write))
                    await session.initialize()
                    server.tools = await self._list_tools(server, session)
                    server.session, server.error = session, ""
                    server.connected_at = time.time()
                    server.settled.set()
                    waiters = [asyncio.create_task(server.stop.wait()),
                               asyncio.create_task(server.restart.wait())]
                    try:
                        await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
                    finally:
                        for waiter in waiters:
                            waiter.cancel()
            except asyncio.CancelledError:
                server.session = None
                raise
            except BaseException as exc:  # noqa: BLE001 — anyio raises groups
                server.error = _describe(exc) + self._stderr_tail(server)
            finally:
                server.session = None
            # Torn down to reconnect: the next attempt reports, not this one.
            if server.stop.is_set() or not server.restart.is_set():
                server.settled.set()
            if server.stop.is_set():
                break
            # Down or asked to reconnect: wait for a caller to want it again.
            if not server.restart.is_set():
                waiters = [asyncio.create_task(server.stop.wait()),
                           asyncio.create_task(server.restart.wait())]
                try:
                    await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for waiter in waiters:
                        waiter.cancel()

    async def _list_tools(self, server: _Server, session: Any) -> List[McpTool]:
        config, types = server.config, self._lib["types"]
        found, cursor = [], None
        while True:
            params = types.PaginatedRequestParams(cursor=cursor) if cursor else None
            listed = await session.list_tools(params=params)
            found.extend(listed.tools)
            cursor = getattr(listed, "next_cursor", None)
            if not cursor:
                break
        names = {t.name for t in found}
        if config.tools is not None:
            missing = [n for n in config.tools if n not in names]
            if missing:
                logger.warning(f"[mcp] {config.name} has no tool(s) {missing}; "
                               f"it has: {sorted(names)}")
            found = [t for t in found if t.name in set(config.tools)]
        tools = []
        for t in found:
            hints = getattr(t, "annotations", None)
            tools.append(McpTool(
                name=tool_name(config.name, t.name),
                server=config.name,
                remote_name=t.name,
                description=f"[{config.name}] {t.description or t.title or t.name}".strip(),
                input_schema=dict(getattr(t, "input_schema", None) or {"type": "object"}),
                read_only=bool(getattr(hints, "read_only_hint", False)),
                destructive=bool(getattr(hints, "destructive_hint", False)),
                approval=config.approval,
            ))
        return tools

    def _index_tools(self) -> None:
        self._tools = {t.name: t for s in self._servers.values() for t in s.tools}

    async def _reconnect(self, server: _Server) -> bool:
        server.settled.clear()
        server.restart.set()
        try:
            await asyncio.wait_for(server.settled.wait(), server.config.startup_timeout)
        except asyncio.TimeoutError:
            return False
        self._index_tools()
        return server.session is not None

    async def _call(self, tool: McpTool, arguments: Dict[str, Any]) -> str:
        server = self._servers[tool.server]
        timeout = server.config.timeout
        for attempt in (1, 2):
            if server.session is None and not await self._reconnect(server):
                return (f"Error: MCP server {tool.server!r} is not connected: "
                        f"{server.error or 'unknown error'}")
            try:
                result = await asyncio.wait_for(
                    server.session.call_tool(tool.remote_name, arguments), timeout)
            except asyncio.TimeoutError:
                return f"Error: {tool.name} took longer than {timeout:g}s and was stopped"
            except Exception as exc:  # noqa: BLE001
                if _is_protocol_error(exc, self._lib):
                    return f"Error: {tool.name}: {_describe(exc)}"
                # The connection itself failed: reconnect once and retry.
                server.error = _describe(exc)
                logger.warning(f"[mcp] {tool.server} failed during {tool.remote_name}: "
                               f"{server.error}; reconnecting")
                if attempt == 1 and await self._reconnect(server):
                    continue
                return f"Error: MCP server {tool.server!r} failed: {server.error}"
            types = self._lib["types"]
            if not isinstance(result, types.CallToolResult):
                return (f"Error: {tool.name} asked for input during the call, which this "
                        "agent cannot give")
            return render_result(result, self.workspace, tool.server, tool.remote_name)
        return f"Error: MCP server {tool.server!r} failed"

    @staticmethod
    def _stderr_tail(server: _Server, lines: int = 5) -> str:
        if server.errlog is None:
            return ""
        try:
            server.errlog.flush()
            server.errlog.seek(0)
            tail = [l.rstrip() for l in server.errlog.read().splitlines() if l.strip()][-lines:]
        except Exception:  # noqa: BLE001
            return ""
        return (" | stderr: " + " / ".join(tail)) if tail else ""


def _describe(exc: BaseException) -> str:
    """The innermost real error of an exception (anyio wraps them in groups)."""
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    return f"{type(exc).__name__}: {exc}"


def _is_protocol_error(exc: BaseException, lib: Dict[str, Any]) -> bool:
    """An error the server answered with (bad arguments, unknown tool): not a broken link."""
    try:
        from mcp import MCPError
        from mcp.types import CONNECTION_CLOSED
    except ImportError:
        return False
    # A closed connection arrives as an MCPError too, but it is the link
    # failing, not the server answering.
    return isinstance(exc, MCPError) and getattr(exc, "code", None) != CONNECTION_CLOSED


# Hubs still open at exit are closed, so no stdio server outlives its agent.
_open_hubs: "weakref.WeakSet[McpHub]" = weakref.WeakSet()


@atexit.register
def _close_open_hubs() -> None:
    for hub in list(_open_hubs):
        hub.close()
