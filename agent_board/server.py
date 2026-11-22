"""
agent-board service — JSON API, SSE stream and the static board.

Security posture, stated up front because it decides the defaults: this service
creates agents whose default toolset includes ``exec_command`` on every
registered machine, over SSH keys it holds. It is a remote-code-execution
control plane. So it binds 127.0.0.1 unless told otherwise, and any non-loopback
bind demands a token (``AGENT_BOARD_TOKEN``) or refuses to start.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from starlette.applications import Starlette
from starlette.requests import Request
from urllib.parse import unquote, urlencode, urlparse

import httpx
from starlette.background import BackgroundTask
from starlette.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse

from declarative_agent_sdk.core.agent_logging import get_logger
from declarative_agent_sdk.agent_board import config as agent_config
from declarative_agent_sdk.agent_board import catalog as model_catalog
from declarative_agent_sdk.agent_board import metrics as model_metrics
from declarative_agent_sdk.agent_board import logbook
from declarative_agent_sdk.agent_board import mcp_catalog
from declarative_agent_sdk.agent_board.machines import (
    MachineDriver, MachineError, key_name_for,
)
from declarative_agent_sdk.agent_board.router import ChannelRouter, EventBus
from declarative_agent_sdk.agent_board.store import Store, running_from_checkout
from declarative_agent_sdk.core.board_env import board_env

logger = get_logger(__name__)

# When this process loaded its code. Shown by /healthz so a stale server can be
# recognised by eye as well as by revision.
_started = datetime.now(timezone.utc).isoformat(timespec="seconds")

STATIC_DIR = Path(__file__).parent / "static"
# The board itself is unauthenticated (it is a shell that asks for the token);
# every /api path is not. Exact paths and prefixes are kept apart deliberately:
# a prefix list containing "/" matches every request and opens the whole API.
# An agent's file link: /files/<path inside its workspace> (core/shares.py).
SHARE_PATH_RE = re.compile(r"^/files/(.+)$")


# Bind addresses that are valid on any machine, so never checked against one.
ANY_HOST = {"", "0.0.0.0", "::", "127.0.0.1", "::1", "localhost"}


def _is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


def _nearest_address(addresses: List[str], wanted: str) -> str:
    """
    The machine address most like ``wanted``: same family and first octet
    (a 100.x tailnet address for a 100.x one), else "".

    Used to repair an agent bound to an address its machine does not have,
    keeping it on the same kind of network it was meant to be on.
    """
    try:
        want = ipaddress.ip_address(wanted)
    except ValueError:
        return ""
    for address in addresses:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            continue
        if ip.version == want.version == 4 and str(ip).split(".")[0] == str(want).split(".")[0]:
            return address
    return ""


def _servable(path: str) -> bool:
    """No empty, parent or hidden segments — the owner refuses them anyway."""
    return not any(part in ("", "..") or part.startswith(".") for part in path.split("/"))


# How long a file ticket lets a <video> or <audio> fetch one link. Long enough
# to watch a long video and seek in it; a seek is a new request on the ticket.
TICKET_TTL_SECONDS = 3600


def make_ticket(key: bytes, link: str, now: Optional[float] = None) -> str:
    """
    A ticket for one file link: ``<expiry>.<hmac>``.

    A media element fetches its own URL and cannot send the Authorization
    header, so it carries this instead of the board token. It names one link
    and expires, so a ticket that leaks (history, logs) opens that one file
    for a while, not the board.
    """
    expires = int((now if now is not None else time.time()) + TICKET_TTL_SECONDS)
    mac = hmac.new(key, f"{expires}\n{link}".encode(), hashlib.sha256).hexdigest()
    return f"{expires}.{mac}"


def check_ticket(key: bytes, ticket: str, link: str, now: Optional[float] = None) -> bool:
    """True when ``ticket`` was made for exactly ``link`` and has not expired."""
    expires, _, mac = (ticket or "").partition(".")
    if not expires.isdigit() or not mac:
        return False
    if int(expires) < (now if now is not None else time.time()):
        return False
    want = hmac.new(key, f"{expires}\n{link}".encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(mac, want)


def _netloc(url: str) -> str:
    parsed = urlparse(url or "")
    host = (parsed.hostname or "").lower()
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return f"{host}:{port}" if host else ""


# The board is edited in place and served from disk, so a browser must ask
# before reusing its copy: without this it applies heuristic caching to a file
# with only Last-Modified, and keeps running yesterday's app.js. no-cache still
# allows the copy — the ETag makes the check a cheap 304.
NO_STALE = {"Cache-Control": "no-cache"}


class FreshStaticFiles(StaticFiles):
    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers.update(NO_STALE)
        return response


PUBLIC_EXACT = frozenset({"/", "/index.html", "/healthz"})
PUBLIC_PREFIXES = ("/static/",)

# An address to bind to or publish: IPv4, IPv6, or a hostname. Checked because
# it travels into a shell command and then into a supervisord environment line,
# and because a typo here produces an agent that starts but cannot be reached.
HOST_RE = re.compile(r"^[A-Za-z0-9_.:\[\]-]{1,255}$")

# A machine name is half of an agent's address ("<agent>.<machine>"), so it has
# to survive being parsed back out of a mention: letters, digits, dash and
# underscore, and no dot.
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


class ControlPlane:
    """Wires the store, the machine driver and the router into one app."""

    def __init__(
        self,
        db_path: Optional[str] = None,
        token: Optional[str] = None,
        public_url: str = "",
    ) -> None:
        self.store = Store(db_path)
        # Beside the database: ~/.agent-board/logs/control.log by default.
        self.logbook = logbook.install(str(Path(self.store.path).parent / "logs"))
        self.driver = MachineDriver()
        self.bus = EventBus()
        self.router = ChannelRouter(self.store, self.bus)
        # Holds the previous /metrics reading per endpoint so throughput can be
        # reported over the window between polls, not just since boot.
        self.sampler = model_metrics.Sampler()
        self.token = token if token is not None else board_env("TOKEN")
        # Signs file tickets. Per process: a restart ends every ticket, and
        # the page simply asks for a new one.
        self.ticket_key = secrets.token_bytes(32)
        self.public_url = public_url.rstrip("/")
        self.app = create_app(self)

    # ------------------------------------------------------------------ run

    def run(self, host: str = "127.0.0.1", port: int = 8900) -> None:
        if host not in ("127.0.0.1", "localhost", "::1") and not self.token:
            raise RuntimeError(
                f"refusing to bind {host} without a token: this service provisions "
                "agents with shell access on every registered machine.\n"
                "  export AGENT_BOARD_TOKEN=$(openssl rand -hex 24)\n"
                "  ...or bind 127.0.0.1 and reach it over an SSH tunnel."
            )
        if not self.public_url:
            # What agents are told to call back on. A loopback bind is only
            # reachable by agents on this same machine.
            self.public_url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '::') else host}:{port}"

        try:
            import uvicorn
        except ImportError as exc:
            raise RuntimeError("uvicorn is required — pip install uvicorn") from exc

        logger.info(f"agent-board on http://{host}:{port} (db {self.store.path})")
        if not self.token:
            logger.warning("no AGENT_BOARD_TOKEN set — API is open to anything "
                           "that can reach the loopback port")
        uvicorn.run(self.app, host=host, port=port)

    # -------------------------------------------------------------- helpers

    def agent_env(self, agent_name: str, machine_name: str = "") -> Dict[str, str]:
        """
        Handshake an agent needs so ``send_to_agent`` can find us.

        AGENT_NAME is what the agent is called locally; AGENT_QUALIFIED_NAME is
        how the board addresses it, and is what it reports itself as when it
        talks to a peer.
        """
        return {
            "AGENT_BOARD_URL": self.public_url,
            "AGENT_BOARD_TOKEN": self.token,
            # The names from when agent-board was agents-control, for agents
            # still running an SDK that only knows those (an older --sdk-ref).
            "AGENTS_CONTROL_URL": self.public_url,
            "AGENTS_CONTROL_TOKEN": self.token,
            "AGENT_NAME": agent_name,
            "AGENT_QUALIFIED_NAME": f"{agent_name}.{machine_name}" if machine_name
                                    else agent_name,
        }

    def refresh_machine(self, machine: Dict[str, Any]) -> Dict[str, Any]:
        """Re-read a machine's installed agents into the store."""
        found = self.driver.list_agents(machine)
        for meta in found:
            self.store.upsert_agent(int(machine["id"]), meta)
        removed = self.store.prune_agents(
            int(machine["id"]), [str(m.get("name") or "") for m in found]
        )
        return {"found": len(found), "removed": removed}


# ---------------------------------------------------------------------------
# app factory
# ---------------------------------------------------------------------------

def create_app(cp: "ControlPlane") -> Starlette:
    store, driver, bus, router = cp.store, cp.driver, cp.bus, cp.router
    sampler = cp.sampler

    # ------------------------------------------------------------------ auth

    def authorized(request: Request) -> bool:
        if not cp.token:
            return True
        path = request.url.path
        if path in PUBLIC_EXACT or path.startswith(PUBLIC_PREFIXES):
            return True
        header = request.headers.get("authorization", "")
        if header.startswith("Bearer "):
            return secrets.compare_digest(header[7:], cp.token)
        supplied = request.query_params.get("token", "")
        if supplied:
            return secrets.compare_digest(supplied, cp.token)
        # A file ticket opens the relay for the one link it was made for.
        if path == "/api/files/relay" and request.method in ("GET", "HEAD"):
            return check_ticket(cp.ticket_key, request.query_params.get("ticket", ""),
                                request.query_params.get("url", ""))
        return False

    def guard(handler):
        """
        Auth, error mapping, and the control plane's request log.

        Changes (POST / PUT / DELETE) are logged with their outcome and how
        long they took; reads only when they fail, since the board polls
        several of them every few seconds. Anything unexpected is logged with
        its traceback and answered as a 500 the board can show, instead of
        surfacing only in the terminal the service happens to run in.
        """
        async def wrapped(request: Request):
            if not authorized(request):
                logger.warning(f"{request.method} {request.url.path}: unauthorized")
                return JSONResponse({"error": "unauthorized"}, status_code=401)
            started = time.monotonic()
            what = f"{request.method} {request.url.path}"
            mutating = request.method in ("POST", "PUT", "DELETE")

            def took() -> str:
                return f"{(time.monotonic() - started) * 1000:.0f}ms"

            try:
                response = await handler(request)
            except MachineError as exc:
                logger.warning(f"{what} -> 502 in {took()}: {exc.detail()}")
                return JSONResponse(
                    {"error": str(exc), "detail": exc.stderr}, status_code=502
                )
            except LookupError as exc:
                logger.warning(f"{what} -> 404: {exc}")
                return JSONResponse({"error": str(exc)}, status_code=404)
            except ValueError as exc:
                logger.warning(f"{what} -> 400: {exc}")
                return JSONResponse({"error": str(exc)}, status_code=400)
            except Exception as exc:  # noqa: BLE001 - logged, then reported
                logger.exception(f"{what} -> 500 in {took()}")
                return JSONResponse(
                    {"error": f"internal error: {type(exc).__name__}: {exc}",
                     "detail": "see the Logs tab (control plane) for the traceback"},
                    status_code=500,
                )
            status = getattr(response, "status_code", 200)
            if status >= 400:
                logger.warning(f"{what} -> {status} in {took()}")
            elif mutating:
                logger.info(f"{what} -> {status} in {took()}")
            return response
        wrapped.__name__ = getattr(handler, "__name__", "handler")
        return wrapped

    async def body(request: Request) -> Dict[str, Any]:
        try:
            payload = await request.json()
        except Exception:
            return {}
        return payload if isinstance(payload, dict) else {}

    def need(payload: Dict[str, Any], key: str) -> Any:
        value = payload.get(key)
        if value in (None, ""):
            raise ValueError(f"'{key}' is required")
        return value

    def in_thread(fn, *args, **kwargs):
        """SSH and SQLite calls are blocking; keep the event loop free."""
        return asyncio.to_thread(fn, *args, **kwargs)

    def sdk_source_for(payload: Dict[str, Any], settings: Dict[str, Any]) -> str:
        """
        The SDK source an install uses: the request's, else the default.

        "local" needs a source checkout; an installed control plane (the
        one-line installer) has none, so it falls back to git rather than
        failing every create and reinstall.
        """
        source = str(payload.get("sdk_source") or settings["default_sdk_source"])
        if source == "local" and not running_from_checkout():
            logger.warning("SDK source 'local' needs a source checkout; "
                           "this control plane is installed as a package, using git")
            return "git"
        return source

    async def machine_addresses(machine: Dict[str, Any], fresh: bool = False) -> List[str]:
        """
        The IP addresses a machine has: as recorded, or read again now.

        A fresh read also updates the record, so the create form's picker
        shows the same list the check used.
        """
        if not fresh:
            recorded = [str(i.get("address") or "") for i in machine.get("interfaces") or []]
            if recorded:
                return recorded
        probe = await in_thread(driver.probe, machine)
        if not probe.get("ok"):
            return []
        await in_thread(
            store.set_machine_status, int(machine["id"]),
            "ready" if probe.get("uv") else "no-uv",
            probe.get("error", ""), probe.get("os", ""), probe.get("interfaces"),
            probe.get("runners"))
        return [str(i.get("address") or "") for i in probe.get("interfaces") or []]

    async def bind_problem(machine: Dict[str, Any], host: str) -> Optional[str]:
        """
        Why ``host`` cannot be bound on ``machine``, or None when it can.

        A server can only listen on an address its own machine has. Bound to
        another machine's, it starts, fails to bind and exits — supervisord
        retries it into BACKOFF and the agent is simply never reachable.
        Hostnames and wildcards are left to install.sh, which tries the bind
        itself; the recorded list is re-read before refusing, since it may
        predate an interface that has come up since.
        """
        host = (host or "").strip()
        if host in ANY_HOST or not _is_ip(host):
            return None
        if host in await machine_addresses(machine):
            return None
        addresses = await machine_addresses(machine, fresh=True)
        if not addresses or host in addresses:
            return None           # unreachable or found: let install.sh decide
        listed = ", ".join(a for a in addresses if _is_ip(a) and ":" not in a) or "none found"
        return (f"{host} is not an address of '{machine['name']}' (its addresses: "
                f"{listed}). An agent can only listen on its own machine's "
                "addresses — pick one of those, or 0.0.0.0 for all of them.")

    def _qualify(name: str) -> str:
        """
        Store the full address when a bare name can only mean one agent.

        An agent whose .env predates AGENT_QUALIFIED_NAME reports itself by its
        bare name; recording that as-is would leave half the transcript
        unqualified for good.
        """
        bare = (name or "").strip()
        if not bare or "." in bare or bare == "system":
            return bare
        matches = store.agents_named(bare)
        return str(matches[0]["qualified"]) if len(matches) == 1 else bare

    # --------------------------------------------------------------- routes

    async def index(request: Request) -> Response:
        return FileResponse(STATIC_DIR / "index.html", headers=NO_STALE)

    async def healthz(request: Request) -> Response:
        """
        Public, and deliberately so: the board reads it before it has a token,
        to find out whether this process is as new as the files it was served.
        """
        from declarative_agent_sdk.__version__ import __version__
        from declarative_agent_sdk.agent_board import API_REVISION

        return JSONResponse({
            "ok": True,
            "version": __version__,
            "api_revision": API_REVISION,
            "subscribers": bus.subscriber_count,
            "started": _started,
        })

    @guard
    async def get_state(request: Request) -> Response:
        from declarative_agent_sdk.agent_board.store import DEFAULT_SETTINGS

        return JSONResponse({
            "machines": store.machines(),
            "agents": store.agents(),
            "channels": store.channels(),
            "settings": store.settings(),
            "defaults": dict(DEFAULT_SETTINGS),
            # Who is mid-turn, so a page opened during one shows it working
            # instead of waiting for the next typing event.
            "working": router.working(),
            # Per-provider endpoints for the create form to fill in.
            "providers": model_catalog.defaults(),
            "auth": bool(cp.token),
        })

    @guard
    async def list_models(request: Request) -> Response:
        """
        The models an endpoint serves, for the create form's model picker.

        POST rather than GET so the API key travels in the body, never in a
        URL that ends up in access logs.
        """
        payload = await body(request)
        return JSONResponse(await model_catalog.list_models(
            str(payload.get("provider") or ""),
            str(payload.get("endpoint") or ""),
            str(payload.get("api_key") or ""),
        ))

    # -- machines ---------------------------------------------------------

    @guard
    async def add_machine(request: Request) -> Response:
        payload = await body(request)
        is_local = bool(payload.get("is_local"))
        name = str(need(payload, "name")).strip()
        if not NAME_RE.match(name):
            raise ValueError(
                f"machine name '{name}' is not usable as part of an agent address — "
                "use letters, digits, '-' and '_' only (no dots or spaces)"
            )
        existing = store.machine_by_name(name)
        if existing is None:
            # UNIQUE is case-sensitive in SQLite, but two machines differing only
            # in case would make '@agent.Box' and '@agent.box' two addresses for
            # different hosts.
            existing = next(
                (m for m in store.machines() if m["name"].lower() == name.lower()), None
            )
        if existing is not None:
            raise ValueError(
                f"a machine called '{existing['name']}' is already registered "
                f"({'this machine' if existing['is_local'] else existing['host']}). "
                "Pick another name, or remove that one first."
            )
        host = str(payload.get("host") or ("localhost" if is_local else ""))
        if not host:
            raise ValueError("'host' is required")
        machine = await in_thread(
            store.add_machine,
            name,
            host,
            str(payload.get("ssh_user") or ""),
            str(payload.get("ssh_key") or ""),
            int(payload.get("ssh_port") or 22),
            is_local,
            str(payload.get("agents_root") or ""),
        )
        probe = await in_thread(driver.probe, machine)
        await in_thread(
            store.set_machine_status, int(machine["id"]),
            "ready" if probe["ok"] and probe["uv"] else ("unreachable" if not probe["ok"] else "no-uv"),
            probe.get("error", ""), probe.get("os", ""), probe.get("interfaces"),
            probe.get("runners"),
        )
        if probe["ok"]:
            try:
                await in_thread(cp.refresh_machine, machine)
            except MachineError as exc:
                logger.info(f"no agents listed on new machine: {exc}")
        machine = store.machine(int(machine["id"])) or machine
        bus.publish({"type": "machines"})
        return JSONResponse({"machine": machine, "probe": probe})

    @guard
    async def test_machine(request: Request) -> Response:
        machine = store.machine(int(request.path_params["machine_id"]))
        if machine is None:
            raise LookupError("no such machine")
        probe = await in_thread(driver.probe, machine)
        await in_thread(
            store.set_machine_status, int(machine["id"]),
            "ready" if probe["ok"] and probe["uv"] else ("unreachable" if not probe["ok"] else "no-uv"),
            probe.get("error", ""), probe.get("os", ""), probe.get("interfaces"),
            probe.get("runners"),
        )
        bus.publish({"type": "machines"})
        return JSONResponse({"probe": probe, "machine": store.machine(int(machine["id"]))})

    @guard
    async def machine_interfaces(request: Request) -> Response:
        """
        Re-read a machine's addresses.

        The create form calls this when it has none stored — a machine
        registered before addresses were collected has an empty list, and the
        picker would otherwise offer nothing but 0.0.0.0.
        """
        machine = store.machine(int(request.path_params["machine_id"]))
        if machine is None:
            raise LookupError("no such machine")
        probe = await in_thread(driver.probe, machine)
        if probe["ok"]:
            await in_thread(
                store.set_machine_status, int(machine["id"]),
                "ready" if probe["uv"] else "no-uv",
                probe.get("error", ""), probe.get("os", ""), probe.get("interfaces"),
            probe.get("runners"),
            )
            bus.publish({"type": "machines"})
        return JSONResponse({
            "interfaces": probe.get("interfaces") or [],
            "ok": probe["ok"],
            "error": probe.get("error", ""),
        })

    @guard
    async def refresh_machine(request: Request) -> Response:
        machine = store.machine(int(request.path_params["machine_id"]))
        if machine is None:
            raise LookupError("no such machine")
        result = await in_thread(cp.refresh_machine, machine)
        bus.publish({"type": "agents"})
        return JSONResponse(result)

    @guard
    async def delete_machine(request: Request) -> Response:
        machine_id = int(request.path_params["machine_id"])
        if store.machine(machine_id) is None:
            raise LookupError("no such machine")
        # Records only: agents keep running on the box. Delete them first to
        # tear them down, which is why this is not implicit.
        await in_thread(store.delete_machine, machine_id)
        bus.publish({"type": "machines"})
        bus.publish({"type": "agents"})
        return JSONResponse({"deleted": machine_id})

    # -- agents -----------------------------------------------------------

    @guard
    async def create_agent(request: Request) -> Response:
        payload = await body(request)
        machine = store.machine(int(need(payload, "machine_id")))
        if machine is None:
            raise LookupError("no such machine")

        name = str(need(payload, "name")).strip()
        if not re.match(r"^[a-z0-9][a-z0-9_-]*$", name):
            raise ValueError(
                f"agent name '{name}' is not valid — install.sh allows lowercase "
                "letters, digits, '-' and '_' (it becomes a directory and a "
                "service name)"
            )
        for field in ("bind_host", "advertise_host"):
            value = str(payload.get(field) or "").strip()
            if value and not HOST_RE.match(value):
                raise ValueError(
                    f"'{value}' is not a usable address for {field} — give an IP "
                    "address or a hostname"
                )

        problem = await bind_problem(machine, str(payload.get("bind_host") or ""))
        if problem:
            raise ValueError(problem)

        if store.agent_by_name(int(machine["id"]), name) is not None:
            raise ValueError(
                f"'{name}.{machine['name']}' already exists. Agent names are unique "
                "per machine — pick another name, or delete that agent first."
            )
        settings = store.settings()
        # Extras only: the core tools (web_request, exec_command, send_to_agent,
        # share_file, read_shared, ...) are registered by every runtime.
        tools = payload.get("tools") or ""
        if isinstance(tools, (list, tuple)):
            tools = ",".join(str(t) for t in tools)

        result = await in_thread(
            driver.create_agent,
            machine,
            name=name,
            framework=str(payload.get("framework") or settings["default_framework"]),
            provider=str(payload.get("provider") or settings["default_provider"]),
            model=str(payload.get("model") or settings["default_model"]),
            endpoint=str(payload.get("endpoint") or ""),
            description=str(payload.get("description") or ""),
            instructions=str(payload.get("instructions") or ""),
            tools=str(tools),
            approval=bool(payload.get("approval", settings["default_approval"])),
            api_key=str(payload.get("api_key") or ""),
            bind_host=str(payload.get("bind_host") or ""),
            advertise_host=str(payload.get("advertise_host") or ""),
            control_env=cp.agent_env(name, str(machine["name"])),
            sdk_source=sdk_source_for(payload, settings),
            sdk_ref=str(payload.get("sdk_ref") or ""),
        )
        result.setdefault("name", name)
        result["tools"] = str(tools)
        result["sdk_source"] = result.get("sdk_source") or ""
        result["description"] = str(payload.get("description") or "")
        # Kept so the board can read this model server's /metrics and /slots
        # later; install.sh --list never reports it back.
        result["endpoint"] = str(payload.get("endpoint") or "")
        agent = await in_thread(store.upsert_agent, int(machine["id"]), result)
        bus.publish({"type": "agents"})
        return JSONResponse({"agent": agent, "result": result})

    @guard
    async def reinstall_agent(request: Request) -> Response:
        """
        Re-run install.sh for an existing agent, keeping what it has.

        For picking up new SDK code, and for agents installed before a fix in
        install.sh or the board. What is kept, and how:

        - agent.yaml, instructions.md, workspace, port: install.sh keeps them
          (no --force);
        - .env: no --env-file, which would replace it wholesale and drop a key
          set since; only the board handshake is refreshed, one variable at a
          time, before the install so its restart reads them;
        - bind and published address: read from the machine and passed back,
          because install.sh would otherwise re-guess them from the default
          route — exactly what the Settings dialog exists to override.
        """
        agent, machine = _agent_and_machine(int(request.path_params["agent_id"]))
        payload = await body(request)
        prefix = str(agent["prefix"] or "")
        settings = store.settings()

        files = await in_thread(driver.read_agent_files, machine, prefix)
        parsed = agent_config.parse(files["yaml"])
        endpoint = parsed.get("endpoint") if isinstance(parsed.get("endpoint"), dict) else {}
        tools = parsed.get("tools")
        if isinstance(tools, (list, tuple)):
            tools = ",".join(str(t) for t in tools)
        try:
            network = await in_thread(driver.agent_network, machine, prefix, agent["name"])
        except MachineError:
            network = {}

        # Reinstall passes the current address back, so a wrong one would be
        # kept forever. One the machine does not have can never work; repair
        # it to the machine's nearest address (or all interfaces) instead.
        bind_host = str(network.get("bind_host") or "") if network.get("ok") else ""
        advertise_host = str(network.get("advertise_host") or "") if network.get("ok") else ""
        address_fixed = ""
        problem = await bind_problem(machine, bind_host)
        if problem:
            addresses = await machine_addresses(machine)
            replacement = _nearest_address(addresses, bind_host)
            address_fixed = (f"{bind_host} is not an address of '{machine['name']}'; "
                             f"rebound to {replacement or 'all interfaces (0.0.0.0)'}")
            logger.warning(f"reinstall of {agent['qualified']}: {address_fixed}")
            if advertise_host == bind_host:
                # It published the same wrong address; publish the new one,
                # or let install.sh pick when binding everything.
                advertise_host = replacement
            bind_host = replacement or "0.0.0.0"

        refreshed = []
        if payload.get("refresh_board_env", True):
            for key, value in cp.agent_env(agent["name"], str(machine["name"])).items():
                if not value:
                    continue          # an unset token is left as it is
                await in_thread(driver.set_env_var, machine, prefix, key, value)
                refreshed.append(key)

        result = await in_thread(
            driver.create_agent,
            machine,
            name=agent["name"],
            framework=agent_config.runtime_of(
                str(parsed.get("agent_framework") or parsed.get("backend") or "lean")),
            provider=str(parsed.get("provider") or agent.get("provider") or ""),
            model=str(parsed.get("model") or ""),
            endpoint=str((endpoint or {}).get("url") or ""),
            description=str(parsed.get("description") or ""),
            tools=str(tools or ""),
            approval=bool(parsed.get("tools_approval_required", True)),
            bind_host=bind_host,
            advertise_host=advertise_host,
            sdk_source=sdk_source_for(payload, settings),
            sdk_ref=str(payload.get("sdk_ref") or ""),
            seed_env=False,
            prefix=prefix,
        )
        result.setdefault("name", agent["name"])
        result["endpoint"] = str((endpoint or {}).get("url") or agent.get("endpoint") or "")
        result["description"] = str(parsed.get("description") or agent.get("description") or "")
        record = await in_thread(store.upsert_agent, int(machine["id"]), result)
        bus.publish({"type": "agents"})
        return JSONResponse({"agent": record, "result": result,
                             "refreshed_env": refreshed,
                             "kept_address": bool(network.get("ok")) and not address_fixed,
                             "address_fixed": address_fixed})

    @guard
    async def delete_agent(request: Request) -> Response:
        agent_id = int(request.path_params["agent_id"])
        agent = store.agent(agent_id)
        if agent is None:
            raise LookupError("no such agent")
        machine = store.machine(int(agent["machine_id"]))
        purge = request.query_params.get("purge") in ("1", "true", "yes")
        result = {}
        if machine is not None:
            result = await in_thread(driver.delete_agent, machine, agent["name"], purge)
        await in_thread(store.delete_agent, agent_id)
        bus.publish({"type": "agents"})
        bus.publish({"type": "channels"})
        return JSONResponse({"deleted": agent_id, "purged": purge, "result": result})

    def _agent_and_machine(agent_id: int):
        agent = store.agent(agent_id)
        if agent is None:
            raise LookupError("no such agent")
        machine = store.machine(int(agent["machine_id"]))
        if machine is None:
            raise LookupError("this agent's machine is no longer registered")
        return agent, machine

    @guard
    async def get_agent_config(request: Request) -> Response:
        """agent.yaml + instructions.md + supervisord state, as installed."""
        agent, machine = _agent_and_machine(int(request.path_params["agent_id"]))
        files = await in_thread(
            driver.read_agent_files, machine, str(agent["prefix"] or "")
        )
        parsed = agent_config.parse(files["yaml"])
        # agent.yaml is the authority on where the model runs, so a read is
        # also the chance to correct the stored endpoint — including for agents
        # installed before the board tracked one.
        endpoint = str((parsed.get("endpoint") or {}).get("url") or "") \
            if isinstance(parsed.get("endpoint"), dict) else ""
        if endpoint != str(agent["endpoint"] or ""):
            await in_thread(store.set_agent_endpoint, int(agent["id"]), endpoint)
            agent = dict(agent, endpoint=endpoint)
        status = await in_thread(
            driver.supervisor_action, machine, str(agent["prefix"] or ""),
            agent["name"], "status",
        )
        provider = str(parsed.get("provider") or agent.get("provider") or "")
        try:
            env_names = await in_thread(
                driver.env_names, machine, str(agent["prefix"] or ""))
        except MachineError:
            env_names = []
        try:
            network = await in_thread(
                driver.agent_network, machine, str(agent["prefix"] or ""), agent["name"])
        except MachineError as exc:
            network = {"ok": False, "error": exc.detail()}
        return JSONResponse({
            # Where it binds and what its card publishes, read from the files
            # the running process was started from.
            "network": network,
            # Names only, never values: enough to say whether a key is set.
            "env_names": env_names,
            "key_name": key_name_for(provider),
            "key_names": {p: key_name_for(p)
                          for p in ("openai", "google", "anthropic", "vllm")},
            "agent": agent,
            "prefix": agent["prefix"],
            "yaml": files["yaml"],
            "instructions": files["instructions"],
            "properties": agent_config.properties(parsed),
            # The whole schema, each row carrying the runtimes it reaches, so
            # the form can re-decide when the framework dropdown changes
            # without another round trip.
            "schema": agent_config.EDITABLE_PROPERTIES,
            # Normalised, so a legacy `backend: langchain` still selects the
            # deepagent rows rather than matching none of them.
            "framework": agent_config.runtime_of(
                str(parsed.get("agent_framework") or parsed.get("backend") or "lean")),
            "status": status,
        })

    def relay_target(link: str):
        """
        (owner, URL to fetch) for an agent file link, or ValueError / LookupError.

        Not an open proxy: the link must be a /files/ link whose address is a
        registered agent's, and the request goes to that agent's recorded
        address, never to whatever host the link names.
        """
        parsed = urlparse(link)
        match = SHARE_PATH_RE.match(parsed.path or "")
        if parsed.scheme not in ("http", "https") or not match or not _servable(
                unquote(match.group(1))):
            raise ValueError("not an agent file link (expected http://<agent>/files/<path>)")
        wanted = _netloc(link)
        owner = next((a for a in store.agents() if _netloc(str(a.get("url") or "")) == wanted), None)
        if owner is None:
            raise LookupError(f"no agent on this board serves {wanted}")
        # Still percent-encoded as it was in the link, so it goes on unchanged.
        return owner, f"{str(owner['url']).rstrip('/')}/files/{match.group(1)}"

    @guard
    async def file_ticket(request: Request) -> Response:
        """
        A ticket that lets a media element stream one file through the relay.

        Asked for with the board token (in the header, like every other call);
        the answer is a relay URL that carries the ticket instead of the token.
        """
        link = str(need(await body(request), "url"))
        relay_target(link)                       # refuse what the relay would
        ticket = make_ticket(cp.ticket_key, link)
        query = urlencode({"url": link, "ticket": ticket})
        return JSONResponse({"ticket": ticket, "expires": int(ticket.split(".")[0]),
                             "src": f"/api/files/relay?{query}"})

    @guard
    async def relay_file(request: Request) -> Response:
        """
        Pass a shared file through from the agent that owns it.

        For readers that cannot reach the owner themselves — this board's
        page, and agents on another network. Nothing is stored: the response
        streams as it arrives.

        Only for links on registered agents (``relay_target``); the owner
        still checks the path stays inside its workspace. Authorised by the
        board token, or by a ticket for exactly this link (``file_ticket``).
        """
        owner, target = relay_target(str(request.query_params.get("url") or ""))
        # A media element streams by asking for byte ranges; the owner answers
        # them (206), so pass the request and the answer through.
        ask = {k: v for k, v in request.headers.items() if k.lower() in ("range", "if-range")}
        client = httpx.AsyncClient(timeout=httpx.Timeout(30, read=120))
        try:
            upstream = await client.send(client.build_request("GET", target, headers=ask),
                                         stream=True)
        except Exception as exc:  # noqa: BLE001 — reported to the reader
            await client.aclose()
            return JSONResponse(
                {"error": f"could not reach {owner['qualified']}: {type(exc).__name__}"},
                status_code=502)

        async def close():
            await upstream.aclose()
            await client.aclose()

        # aiter_bytes undoes any Content-Encoding, so a compressed length
        # would no longer match what is sent; let the server count it then.
        keep = {"content-type", "content-disposition", "content-range", "accept-ranges"}
        if not upstream.headers.get("content-encoding"):
            keep.add("content-length")
        passed = {k: v for k, v in upstream.headers.items() if k.lower() in keep}
        passed["Cache-Control"] = "no-store"
        passed["X-Content-Type-Options"] = "nosniff"
        passed["X-Shared-By"] = str(owner["qualified"])
        return StreamingResponse(upstream.aiter_bytes(), status_code=upstream.status_code,
                                 headers=passed, background=BackgroundTask(close))

    @guard
    async def agent_models(request: Request) -> Response:
        """
        Models this agent's endpoint serves, for the settings form's picker.

        Asked from the agent's machine with the key in its own .env, so the
        board never handles the key. ``provider`` and ``endpoint`` in the query
        reflect unsaved edits in the form; otherwise the saved ones are used.
        """
        agent, machine = _agent_and_machine(int(request.path_params["agent_id"]))
        provider = (request.query_params.get("provider")
                    or str(agent.get("provider") or "")).strip().lower()
        if provider not in model_catalog.LISTABLE:
            return JSONResponse({"ok": False, "models": [], "error":
                                 f"model listing is not available for {provider or 'this provider'}"})
        endpoint = (request.query_params.get("endpoint")
                    or str(agent.get("endpoint") or "")
                    or model_catalog.PROVIDER_ENDPOINTS.get(provider, "")).strip().rstrip("/")
        if not endpoint:
            return JSONResponse({"ok": False, "models": [],
                                 "error": "enter the endpoint URL to list its models"})
        key_name = key_name_for(provider)
        try:
            reply = await in_thread(driver.list_models_on_machine, machine,
                                    str(agent["prefix"] or ""), endpoint, key_name)
        except MachineError as exc:
            return JSONResponse({"ok": False, "models": [], "endpoint": endpoint,
                                 "error": exc.detail()[:300]})
        if reply.get("ok"):
            return JSONResponse({"ok": True, "endpoint": endpoint, "error": "",
                                 "models": model_catalog.chat_models(provider, reply["ids"])})
        if reply.get("status") in (401, 403):
            error = (f"the endpoint rejected the agent's {key_name}" if reply.get("has_key")
                     else f"{key_name} is not set for this agent — add it above")
        elif reply.get("status"):
            error = f"{endpoint}/models answered {reply['status']}"
        else:
            error = (f"could not reach {endpoint} from {machine['name']}: "
                     f"{reply.get('error', 'no reply')}")
        return JSONResponse({"ok": False, "models": [], "endpoint": endpoint, "error": error})

    @guard
    async def agent_metrics(request: Request) -> Response:
        """
        What each agent's model server is doing, grouped by endpoint.

        Grouped rather than listed per agent because that is the shape of the
        underlying fact: two agents pointed at one single-slot server are not
        two independent model servers, and their turns serialise. The grouping
        makes that visible instead of showing the same numbers twice.

        Endpoints are learned at create time and corrected whenever an
        agent.yaml is read; an agent with none reports as unknown rather than
        being silently dropped.
        """
        agents = await in_thread(store.agents)
        # Two different questions, so two different sources: the agent knows
        # how much history it prefixes to a request, the server knows what it
        # did with the one it received.
        contexts, mcps = await asyncio.gather(
            model_metrics.contexts_for(agents), model_metrics.mcps_for(agents))
        # The router reads this for prompts and the 'safe' level (MCP-BOARD-2/3).
        router.remember_mcp(mcps)
        groups: Dict[str, Any] = {}
        for agent in agents:
            endpoint = str(agent.get("endpoint") or "")
            entry = groups.setdefault(endpoint, {"endpoint": endpoint, "agents": []})
            entry["agents"].append({
                "id": agent["id"], "qualified": agent["qualified"],
                "model": agent["model"], "provider": agent["provider"],
                "ready": bool(agent["ready"]),
                "context": contexts.get(int(agent["id"]), {}),
            })

        reports = await model_metrics.probe_all(
            [e for e in groups if e], sampler=sampler,
        )
        by_url = {str(r.get("endpoint")): r for r in reports}
        servers = []
        for endpoint, entry in sorted(groups.items()):
            report = dict(by_url.get(endpoint) or {})
            if not endpoint:
                report = {
                    "endpoint": "", "reachable": False,
                    "error": "no endpoint recorded — open ⚙ Settings for one of "
                             "these agents and the board will learn it",
                }
            report["agents"] = entry["agents"]
            # Two agents on one endpoint share its slots; say so once here
            # rather than leaving it to be inferred from the numbers.
            report["shared"] = len(entry["agents"]) > 1
            servers.append(report)
        return JSONResponse({
            "servers": servers,
            # Flat, keyed by agent id, so the agents table can show a context
            # column without walking the grouping.
            "contexts": {str(k): v for k, v in contexts.items()},
            # Each agent's MCP servers, keyed by agent id (MCP-BOARD-1).
            "mcp": {str(k): v for k, v in mcps.items()},
        })

    @guard
    async def put_agent_config(request: Request) -> Response:
        """
        Save agent.yaml (and instructions.md), then restart so it takes effect.

        A running agent read its config once at startup; without the restart the
        edit is invisible until something else bounces it.
        """
        agent, machine = _agent_and_machine(int(request.path_params["agent_id"]))
        payload = await body(request)
        prefix = str(agent["prefix"] or "")

        current = await in_thread(driver.read_agent_files, machine, prefix)
        # A save may change only the key or the address; agent.yaml is then
        # left exactly as it is rather than refused as "nothing to save".
        touches_yaml = payload.get("yaml") is not None or bool(payload.get("properties"))
        if not touches_yaml and (payload.get("api_key") or payload.get("network")):
            text, warnings = current["yaml"], []
        else:
            try:
                text, warnings = agent_config.prepare(
                    current["yaml"],
                    agent["name"],
                    raw=payload.get("yaml"),
                    updates=payload.get("properties"),
                )
            except agent_config.ConfigError as exc:
                raise ValueError(str(exc)) from exc

        instructions = payload.get("instructions")
        if instructions is not None and instructions == current["instructions"]:
            instructions = None                      # nothing changed, skip the write

        # A new key is checked before anything is written, so a rejected one
        # does not leave the config saved and the key not.
        api_key = str(payload.get("api_key") or "").strip()
        if api_key and (any(ch.isspace() for ch in api_key)
                        or any(c in api_key for c in "'\"\\$`")):
            raise ValueError("the API key must be a single token without spaces, "
                             "quotes, '$', '`' or backslashes")

        network = payload.get("network") or {}
        for field in ("bind_host", "advertise_host"):
            value = str(network.get(field) or "").strip()
            if value and not HOST_RE.match(value):
                raise ValueError(f"'{value}' is not a usable address for {field}")
        if network:
            problem = await bind_problem(machine, str(network.get("bind_host") or ""))
            if problem:
                raise ValueError(problem)

        written = await in_thread(
            driver.write_agent_files, machine, prefix,
            text if touches_yaml else None, instructions,
        )
        if api_key:
            # Named after the provider in the config just written, so switching
            # provider and pasting its key in one save lands in the right place.
            key_name = key_name_for(str(agent_config.parse(text).get("provider") or ""))
            await in_thread(driver.set_env_var, machine, prefix, key_name, api_key)
            written = dict(written, written=list(written.get("written", [])) + [f".env ({key_name})"])

        # Bind address / published URL. Validated before anything else is
        # touched below, applied after the config, and it restarts the agent
        # itself: a new HOST is only read when supervisord re-creates the
        # process, so it cannot wait for the restart checkbox.
        network_result: Dict[str, Any] = {}
        if network:
            network_result = await in_thread(
                driver.agent_network, machine, prefix, agent["name"],
                str(network.get("bind_host") or "").strip(),
                str(network.get("advertise_host") or "").strip(),
                True,
            )
            if not network_result.get("ok"):
                warnings.append(f"address not changed: {network_result.get('error', '')}")
            else:
                await in_thread(store.set_agent_url, int(agent["id"]), network_result["url"])
                agent = dict(agent, url=network_result["url"],
                             agent_card=f"{network_result['url']}.well-known/agent-card.json")
                written = dict(written, written=list(written.get("written", []))
                               + ["supervisor program", "run_server.py", ".agent-meta"])

        restart = {}
        if network_result.get("ok"):
            restart = await in_thread(
                driver.apply_service_change, machine, prefix, agent["name"])
            if not restart.get("ok"):
                warnings.append(
                    f"address saved, but supervisord could not apply it: "
                    f"{restart.get('output', '')[:600]}")
        elif payload.get("restart", True):
            restart = await in_thread(
                driver.supervisor_action, machine, prefix, agent["name"], "restart"
            )
            if not restart.get("ok"):
                output = str(restart.get("output", ""))
                # "Still running its old config" is only true while supervisord
                # is; when it is down, the agent is not running at all.
                tail = "" if "supervisord is not running" in output \
                    else " — the agent is still running its old config"
                warnings.append(f"saved, but the restart failed: {output[:600]}{tail}")

        # Keep the record in step with the file the agent will now read.
        parsed = agent_config.parse(text)
        tools = parsed.get("tools")
        await in_thread(store.upsert_agent, int(machine["id"]), {
            "name": agent["name"], "prefix": prefix,
            "url": agent["url"], "agent_card": agent["agent_card"],
            "port": agent["port"], "framework": parsed.get("agent_framework") or "lean",
            "provider": parsed.get("provider") or "", "model": parsed.get("model") or "",
            "tools": tools if isinstance(tools, list) else (tools or ""),
            "description": parsed.get("description") or "",
            "ready": agent["ready"], "problems": agent["problems"],
        })
        bus.publish({"type": "agents"})
        return JSONResponse({
            "yaml": text, "written": written, "restart": restart, "warnings": warnings,
            "network": network_result,
        })

    @guard
    async def agent_action(request: Request) -> Response:
        """start / stop / restart / status through the machine's supervisord."""
        agent, machine = _agent_and_machine(int(request.path_params["agent_id"]))
        payload = await body(request)
        action = str(payload.get("action") or "status")
        result = await in_thread(
            driver.supervisor_action, machine, str(agent["prefix"] or ""),
            agent["name"], action,
        )
        bus.publish({"type": "agents"})
        return JSONResponse(result)

    # -- channels ---------------------------------------------------------

    @guard
    async def create_channel(request: Request) -> Response:
        payload = await body(request)
        name = str(need(payload, "name")).lstrip("#").strip()
        if store.channel_by_name(name):
            raise ValueError(f"channel '{name}' already exists")
        channel = await in_thread(store.add_channel, name, str(payload.get("topic") or ""))
        bus.publish({"type": "channels"})
        return JSONResponse({"channel": channel})

    @guard
    async def delete_channel(request: Request) -> Response:
        channel_id = int(request.path_params["channel_id"])
        if store.channel(channel_id) is None:
            raise LookupError("no such channel")
        await in_thread(store.delete_channel, channel_id)
        bus.publish({"type": "channels"})
        return JSONResponse({"deleted": channel_id})

    @guard
    async def clear_channel(request: Request) -> Response:
        """Clear conversation: transcript plus each member's history of it."""
        return JSONResponse(await router.clear_channel(int(request.path_params["channel_id"])))

    @guard
    async def clear_agent_context(request: Request) -> Response:
        """Clear context: one agent forgets every channel's conversation."""
        return JSONResponse(await router.clear_agent(int(request.path_params["agent_id"])))

    @guard
    async def join_channel(request: Request) -> Response:
        channel_id = int(request.path_params["channel_id"])
        payload = await body(request)
        agent_id = int(need(payload, "agent_id"))
        if store.channel(channel_id) is None or store.agent(agent_id) is None:
            raise LookupError("no such channel or agent")
        await in_thread(store.join, channel_id, agent_id)
        bus.publish({"type": "channels"})
        return JSONResponse({"members": store.members(channel_id)})

    @guard
    async def leave_channel(request: Request) -> Response:
        channel_id = int(request.path_params["channel_id"])
        agent_id = int(request.path_params["agent_id"])
        await in_thread(store.leave, channel_id, agent_id)
        bus.publish({"type": "channels"})
        return JSONResponse({"members": store.members(channel_id)})

    @guard
    async def get_messages(request: Request) -> Response:
        channel_id = int(request.path_params["channel_id"])
        if store.channel(channel_id) is None:
            raise LookupError("no such channel")
        after = int(request.query_params.get("after") or 0)
        limit = min(int(request.query_params.get("limit") or 200), 500)
        return JSONResponse({
            "messages": store.messages(channel_id, limit=limit, after_id=after),
            "members": store.members(channel_id),
            "halted": store.is_halted(channel_id),
            "auto_approve": store.auto_approve(channel_id),
        })

    @guard
    async def post_message(request: Request) -> Response:
        channel_id = int(request.path_params["channel_id"])
        if store.channel(channel_id) is None:
            raise LookupError("no such channel")
        payload = await body(request)
        text = str(need(payload, "text"))
        result = await router.post_human(channel_id, text, str(payload.get("sender") or "you"))
        return JSONResponse(result)

    @guard
    async def set_halt(request: Request) -> Response:
        channel_id = int(request.path_params["channel_id"])
        if store.channel(channel_id) is None:
            raise LookupError("no such channel")
        payload = await body(request)
        return JSONResponse(router.halt(channel_id, bool(payload.get("halted", True))))

    @guard
    async def set_auto_approve(request: Request) -> Response:
        """How this channel answers tool approvals."""
        channel_id = int(request.path_params["channel_id"])
        if store.channel(channel_id) is None:
            raise LookupError("no such channel")
        payload = await body(request)
        level = str(payload.get("level") or "")
        try:
            await in_thread(store.set_auto_approve, channel_id, level)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
        router.announce_auto_approve(channel_id, level)
        bus.publish({"type": "channels"})
        return JSONResponse({"auto_approve": level})

    @guard
    async def resolve_approval(request: Request) -> Response:
        payload = await body(request)
        approval_id = str(request.path_params["approval_id"])
        if not router.resolve_approval(approval_id, bool(payload.get("approved"))):
            raise LookupError("no such pending approval (it may have timed out)")
        return JSONResponse({"resolved": approval_id})

    # -- settings ---------------------------------------------------------

    # ------------------------------------------------------------ MCP catalog
    # docs/design/mcp.md, MCP-CAT-*. Definitions live here; an agent's attached
    # entries are written into its agent.yaml (board_mcp_servers) and it is
    # restarted. Secret values pass through to agents' .env files only.

    def _mcp_view(server: Dict[str, Any]) -> Dict[str, Any]:
        links = store.mcp_links(server_id=int(server["id"]))
        return {
            **server,
            "vars": mcp_catalog.referenced_vars(server["spec"]),
            "runner": mcp_catalog.runner_for(server["spec"]),
            "agents": [{"agent_id": l["agent_id"], "overrides": l["overrides"]} for l in links],
        }

    def _mcp_entry(payload: Dict[str, Any]) -> "tuple[str, Dict[str, Any]]":
        name = str(payload.get("name") or "").strip()
        spec = mcp_catalog.check(name, mcp_catalog.clean_spec(payload.get("spec") or {}))
        return name, spec

    def _overrides(raw: Any) -> Dict[str, Any]:
        raw = raw if isinstance(raw, dict) else {}
        overrides: Dict[str, Any] = {}
        tools = raw.get("tools")
        if isinstance(tools, str):
            tools = [t.strip() for t in tools.split(",") if t.strip()]
        if tools:
            if not isinstance(tools, list) or not all(isinstance(t, str) for t in tools):
                raise ValueError("tools must be a list of tool names")
            overrides["tools"] = tools
        approval = str(raw.get("approval") or "").strip().lower()
        if approval:
            if approval not in ("false", "true", "destructive"):
                raise ValueError("approval must be false, true or destructive")
            overrides["approval"] = approval
        return overrides

    async def sync_agent_mcp(agent_id: int) -> Dict[str, Any]:
        """
        Write an agent's attached entries into its agent.yaml and restart it
        (MCP-CAT-6). An agent already up to date is left alone (MCP-CAT-10).
        """
        agent, machine = _agent_and_machine(agent_id)
        result: Dict[str, Any] = {"agent_id": agent_id,
                                  "agent": agent.get("qualified") or agent["name"],
                                  "ok": False, "changed": False}
        try:
            prefix = str(agent["prefix"] or "")
            files = await in_thread(driver.read_agent_files, machine, prefix)
            wanted = mcp_catalog.board_servers(await in_thread(store.mcp_links, agent_id))
            if mcp_catalog.current(files["yaml"]) == wanted:
                result["ok"] = True
                return result
            text = mcp_catalog.apply(files["yaml"], wanted)
            await in_thread(driver.write_agent_files, machine, prefix, text, None)
            result["changed"] = True
            outcome = await in_thread(
                driver.supervisor_action, machine, prefix, agent["name"], "restart")
            result["ok"] = bool(outcome.get("ok"))
            if not outcome.get("ok"):
                result["error"] = ("agent.yaml saved, but the restart failed: "
                                   f"{str(outcome.get('output', ''))[:300]}")
        except MachineError as exc:
            result["error"] = exc.detail()
        except (agent_config.ConfigError, LookupError, ValueError) as exc:
            result["error"] = str(exc)
        logger.info(f"mcp: synced {result['agent']}: "
                    f"{'changed' if result['changed'] else 'unchanged'}"
                    f"{', ' + result['error'] if result.get('error') else ''}")
        return result

    async def _sync_attached(server_id: int) -> List[Dict[str, Any]]:
        agent_ids = sorted({int(l["agent_id"]) for l in store.mcp_links(server_id=server_id)})
        return list(await asyncio.gather(*(sync_agent_mcp(a) for a in agent_ids)))

    async def _attach_warnings(agent: Dict[str, Any], machine: Dict[str, Any],
                               server: Dict[str, Any]) -> List[str]:
        """What may stop this server working on this agent (MCP-CAT-8)."""
        warnings: List[str] = []
        runner = mcp_catalog.runner_for(server["spec"])
        found = machine.get("runners")
        if runner and isinstance(found, list) and found and runner not in found:
            warnings.append(f"{runner} was not found on {machine['name']} when it was last "
                            f"tested — install it there, or this server will not start")
        prefix = str(agent["prefix"] or "")
        try:
            names = set(await in_thread(driver.env_names, machine, prefix))
            missing = [v for v in mcp_catalog.referenced_vars(server["spec"]) if v not in names]
            if missing:
                warnings.append(f"{', '.join(missing)} not set in this agent's .env — use "
                                "Set… on the MCP tab")
            files = await in_thread(driver.read_agent_files, machine, prefix)
            if server["name"] in mcp_catalog.hand_written(files["yaml"]):
                warnings.append(f"agent.yaml already has a hand-written '{server['name']}' "
                                "server, which is used instead of this one")
        except MachineError as exc:
            warnings.append(f"could not check the agent's files: {exc}")
        return warnings

    @guard
    async def search_mcp_registry(request: Request) -> Response:
        """
        Search the official MCP Registry and offer each result as ready
        catalog specs (MCP-CAT-13). Only the search words and the page cursor
        are sent; nothing is added to the catalog from here.
        """
        params = {"limit": "30", "version": "latest"}
        search = str(request.query_params.get("search") or "").strip()[:100]
        cursor = str(request.query_params.get("cursor") or "").strip()[:300]
        if search:
            params["search"] = search
        if cursor:
            params["cursor"] = cursor
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(20)) as client:
                reply = await client.get(mcp_catalog.REGISTRY_URL, params=params)
            reply.raise_for_status()
            data = reply.json()
        except Exception as exc:  # noqa: BLE001 — reported to the page
            return JSONResponse({"error": f"could not read the MCP Registry: "
                                          f"{type(exc).__name__}: {exc}"}, status_code=502)
        servers = [mcp_catalog.registry_entry(item) for item in data.get("servers") or []
                   if isinstance(item, dict)]
        servers = [s for s in servers if s["status"] not in ("deleted",)]
        return JSONResponse({"servers": servers,
                             "next": (data.get("metadata") or {}).get("nextCursor") or ""})

    @guard
    async def list_mcp(request: Request) -> Response:
        servers = await in_thread(store.mcp_servers)
        return JSONResponse({"servers": [_mcp_view(s) for s in servers],
                             "presets": mcp_catalog.PRESETS})

    @guard
    async def create_mcp(request: Request) -> Response:
        name, spec = _mcp_entry(await body(request))
        server = await in_thread(store.add_mcp_server, name, spec)
        bus.publish({"type": "mcp"})
        return JSONResponse({"server": _mcp_view(server)})

    @guard
    async def update_mcp(request: Request) -> Response:
        """Change an entry; with ``sync``, rewrite and restart attached agents (MCP-CAT-9)."""
        server_id = int(request.path_params["server_id"])
        if store.mcp_server(server_id) is None:
            raise LookupError("no such MCP server")
        payload = await body(request)
        name, spec = _mcp_entry(payload)
        server = await in_thread(store.update_mcp_server, server_id, name, spec)
        synced = await _sync_attached(server_id) if payload.get("sync") else []
        bus.publish({"type": "mcp"})
        return JSONResponse({"server": _mcp_view(server), "synced": synced})

    @guard
    async def delete_mcp(request: Request) -> Response:
        """Detach from every agent (rewriting each), then forget the entry (MCP-CAT-9)."""
        server_id = int(request.path_params["server_id"])
        if store.mcp_server(server_id) is None:
            raise LookupError("no such MCP server")
        agent_ids = sorted({int(l["agent_id"]) for l in store.mcp_links(server_id=server_id)})
        for agent_id in agent_ids:
            await in_thread(store.detach_mcp, agent_id, server_id)
        synced = list(await asyncio.gather(*(sync_agent_mcp(a) for a in agent_ids)))
        await in_thread(store.delete_mcp_server, server_id)
        bus.publish({"type": "mcp"})
        return JSONResponse({"deleted": server_id, "synced": synced})

    @guard
    async def set_mcp_secret(request: Request) -> Response:
        """
        Put one ${NAME}'s value into every attached agent's .env and restart
        them (MCP-CAT-3). The value is never stored here and never returned.
        """
        server_id = int(request.path_params["server_id"])
        server = store.mcp_server(server_id)
        if server is None:
            raise LookupError("no such MCP server")
        payload = await body(request)
        name = str(payload.get("name") or "")
        value = str(payload.get("value") or "")
        if name not in mcp_catalog.referenced_vars(server["spec"]):
            raise ValueError(f"'{name}' is not a ${{NAME}} this server uses")
        if not value:
            raise ValueError("a value is needed")
        results = []
        for link in store.mcp_links(server_id=server_id):
            agent, machine = _agent_and_machine(int(link["agent_id"]))
            entry: Dict[str, Any] = {"agent_id": agent["id"],
                                     "agent": agent.get("qualified") or agent["name"]}
            try:
                prefix = str(agent["prefix"] or "")
                await in_thread(driver.set_env_var, machine, prefix, name, value)
                outcome = await in_thread(
                    driver.supervisor_action, machine, prefix, agent["name"], "restart")
                entry["ok"] = bool(outcome.get("ok"))
                if not outcome.get("ok"):
                    entry["error"] = "set, but the restart failed"
            except MachineError as exc:
                entry.update(ok=False, error=str(exc))
            results.append(entry)
        logger.info(f"mcp: set {name} for {server['name']} on {len(results)} agent(s)")
        return JSONResponse({"name": name, "agents": results})

    @guard
    async def get_agent_mcp(request: Request) -> Response:
        """An agent's attached entries, with the catalog to choose from (MCP-CAT-12)."""
        agent_id = int(request.path_params["agent_id"])
        _agent_and_machine(agent_id)
        links = await in_thread(store.mcp_links, agent_id)
        servers = await in_thread(store.mcp_servers)
        return JSONResponse({
            "attached": [{"server_id": l["server_id"], "name": l["name"],
                          "overrides": l["overrides"]} for l in links],
            "catalog": [_mcp_view(s) for s in servers],
        })

    @guard
    async def attach_agent_mcp(request: Request) -> Response:
        """Attach an entry (or change its overrides), then sync the agent (MCP-CAT-5)."""
        agent_id = int(request.path_params["agent_id"])
        agent, machine = _agent_and_machine(agent_id)
        payload = await body(request)
        server = store.mcp_server(int(payload.get("server_id") or 0))
        if server is None:
            raise LookupError("no such MCP server")
        overrides = _overrides(payload.get("overrides"))
        await in_thread(store.attach_mcp, agent_id, int(server["id"]), overrides)
        warnings = await _attach_warnings(agent, machine, server)
        synced = await sync_agent_mcp(agent_id)
        bus.publish({"type": "mcp"})
        return JSONResponse({"synced": synced, "warnings": warnings})

    @guard
    async def detach_agent_mcp(request: Request) -> Response:
        agent_id = int(request.path_params["agent_id"])
        _agent_and_machine(agent_id)
        await in_thread(store.detach_mcp, agent_id, int(request.path_params["server_id"]))
        synced = await sync_agent_mcp(agent_id)
        bus.publish({"type": "mcp"})
        return JSONResponse({"synced": synced})

    @guard
    async def get_settings(request: Request) -> Response:
        """
        Current values, and the defaults this build ships.

        A store keeps whatever it was created with: seeding only fills keys
        that are missing, so a default changed in a later version never reaches
        an existing store. The board shows the difference rather than
        overwriting a value someone may have chosen deliberately.
        """
        from declarative_agent_sdk.agent_board.store import DEFAULT_SETTINGS

        return JSONResponse({
            "settings": store.settings(),
            "defaults": dict(DEFAULT_SETTINGS),
        })

    @guard
    async def put_settings(request: Request) -> Response:
        payload = await body(request)
        updated = await in_thread(store.update_settings, payload)
        bus.publish({"type": "settings"})
        return JSONResponse({"settings": updated})

    # -- peer endpoints (called by the send_to_agent tool) -----------------

    @guard
    async def board_say(request: Request) -> Response:
        """
        An agent speaking on the board. The board relays what it mentioned.

        This is the whole of agent-to-agent communication: no agent calls
        another, so every hop is visible here and stops when the channel is
        halted.
        """
        payload = await body(request)
        sender = _qualify(str(need(payload, "from")))
        text = str(need(payload, "text"))

        wanted = str(payload.get("channel") or "").lstrip("#").strip()
        if wanted:
            channel = store.channel_by_name(wanted)
            if channel is None:
                raise LookupError(f"no channel called '{wanted}'")
        else:
            # No channel named: fine when the agent is in exactly one, because
            # then there is nothing to choose between.
            candidates = store.channels_for_agent_name(sender)
            if len(candidates) == 1:
                channel = candidates[0]
            elif not candidates:
                raise LookupError(
                    f"'{sender}' is not a member of any channel, so it has "
                    "nowhere to post"
                )
            else:
                raise ValueError(
                    f"'{sender}' is in several channels "
                    f"({', '.join(c['name'] for c in candidates)}) — name one"
                )

        channel_id = int(channel["id"])
        router.record_peer_message(channel_id, sender, "", text)
        relayed = await router.relay(channel_id, sender, text, depth=1)
        return JSONResponse({
            "channel": channel["name"],
            "relayed": relayed,
            "halted": bool(channel["halted"]),
            "members": [
                str(m.get("qualified") or m["name"]) for m in store.members(channel_id)
            ],
        })

    @guard
    async def peer_roster(request: Request) -> Response:
        """
        Who an agent may talk to, and where they listen.

        scope=channel (default) lists the members of the caller's channels —
        the only agents it can address. scope=all lists every agent on the
        board, which the caller can report but not reach.
        """
        agent_name = request.query_params.get("agent") or ""
        channel_name = request.query_params.get("channel") or ""
        if (request.query_params.get("scope") or "channel") == "all":
            return JSONResponse({"roster": [
                {
                    "name": agent["name"],
                    "qualified": agent["qualified"],
                    "machine": agent["machine_name"],
                    "url": agent["url"],
                    "model": agent["model"],
                    "description": agent["description"],
                    "ready": bool(agent["ready"]),
                    "channel": "",
                }
                for agent in store.agents()
            ]})
        roster = []
        channels = store.channels_for_agent_name(agent_name) if agent_name else store.channels()
        caller = (agent_name or "").lower()
        for channel in channels:
            if channel_name and channel["name"] != channel_name:
                continue
            for member in store.members(int(channel["id"])):
                qualified = str(member.get("qualified") or member["name"])
                # Leave the caller out, matched on either form of its name.
                if caller in (qualified.lower(), str(member["name"]).lower()):
                    continue
                roster.append({
                    "channel": channel["name"],
                    "channel_id": channel["id"],
                    "halted": bool(channel["halted"]),
                    "name": member["name"],
                    "qualified": qualified,
                    "machine": member["machine_name"],
                    "url": member["url"],
                    "model": member["model"],
                    "description": member["description"],
                    "ready": bool(member["ready"]),
                })
        return JSONResponse({"roster": roster})

    @guard
    async def peer_preflight(request: Request) -> Response:
        """Cooperative kill switch: may this send go ahead?"""
        channel_name = request.query_params.get("channel") or ""
        channel = store.channel_by_name(channel_name) if channel_name else None
        halted = bool(channel and channel["halted"])
        return JSONResponse({
            "allowed": not halted,
            "halted": halted,
            "reason": "channel is halted" if halted else "",
        })

    @guard
    async def peer_log(request: Request) -> Response:
        """Transcript line for an agent-to-agent exchange."""
        payload = await body(request)
        channel = store.channel_by_name(str(payload.get("channel") or ""))
        if channel is None:
            raise LookupError("no such channel")
        message = router.record_peer_message(
            int(channel["id"]),
            _qualify(str(need(payload, "from"))),
            _qualify(str(payload.get("to") or "")),
            str(need(payload, "text")),
            kind=str(payload.get("kind") or "agent"),
        )
        return JSONResponse({"message": message})

    # -- logs -------------------------------------------------------------

    @guard
    async def control_logs(request: Request) -> Response:
        """This process's recent log records; ?after=<seq> for only new ones."""
        q = request.query_params
        try:
            after = int(q.get("after") or 0)
            limit = int(q.get("limit") or 500)
        except ValueError:
            raise ValueError("'after' and 'limit' must be integers")
        return JSONResponse(cp.logbook.read(
            after=after, level=q.get("level") or "", query=q.get("q") or "", limit=limit))

    @guard
    async def agent_logs(request: Request) -> Response:
        """Tail of an agent's stdout / stderr, or its machine's supervisord log."""
        agent, machine = _agent_and_machine(int(request.path_params["agent_id"]))
        q = request.query_params
        try:
            lines = int(q.get("lines") or 200)
        except ValueError:
            raise ValueError("'lines' must be an integer")
        result = await in_thread(
            driver.agent_logs, machine, str(agent["prefix"] or ""), agent["name"],
            stream=q.get("stream") or "stderr", lines=lines,
        )
        return JSONResponse(dict(result, agent=agent["name"],
                                 machine=machine.get("name")))

    def attachment(text: str, filename: str, extra: Optional[Dict[str, str]] = None) -> Response:
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", filename)
        headers = {"Content-Disposition": f'attachment; filename="{safe}"'}
        headers.update(extra or {})
        return Response(text, media_type="text/plain; charset=utf-8", headers=headers)

    @guard
    async def control_log_download(request: Request) -> Response:
        """
        control.log as a file. Falls back to the in-memory records when there
        is no file (it could not be created), so a download never comes back
        empty-handed while the buffer holds something.
        """
        stamp = time.strftime("%Y%m%d-%H%M%S")
        path = cp.logbook.file_path
        if path and Path(path).is_file():
            for handler in logging.getLogger().handlers:
                if getattr(handler, "baseFilename", None) == str(Path(path).resolve()):
                    handler.flush()
            text = await in_thread(Path(path).read_text, encoding="utf-8", errors="replace")
        else:
            records = cp.logbook.read(limit=logbook.CAPACITY)["records"]
            text = "".join(
                f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(r['ts']))} - "
                f"{r['logger']} - {r['level']} - {r['message']}\n" for r in records)
        return attachment(text, f"agent-board-{stamp}.log")

    @guard
    async def agent_log_download(request: Request) -> Response:
        """One of an agent's logs, whole (up to the driver's cap), as a file."""
        agent, machine = _agent_and_machine(int(request.path_params["agent_id"]))
        stream = request.query_params.get("stream") or "stderr"
        result = await in_thread(
            driver.agent_log_file, machine, str(agent["prefix"] or ""), agent["name"],
            stream=stream,
        )
        stamp = time.strftime("%Y%m%d-%H%M%S")
        extra = {"X-Log-Truncated": "1"} if result["truncated"] else {}
        return attachment(
            result["text"],
            f"{agent['name']}.{machine.get('name')}-{stream}-{stamp}.log", extra)

    # -- SSE --------------------------------------------------------------

    async def events(request: Request) -> Response:
        if not authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        queue = bus.subscribe()

        async def stream():
            try:
                yield {"event": "hello", "data": json.dumps({"ok": True})}
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=20)
                    except asyncio.TimeoutError:
                        yield {"event": "ping", "data": "{}"}
                        continue
                    yield {"event": "update", "data": json.dumps(event, default=str)}
            finally:
                bus.unsubscribe(queue)

        return EventSourceResponse(stream())

    routes = [
        Route("/", index),
        Route("/healthz", healthz),
        Route("/api/state", get_state),
        Route("/api/machines", add_machine, methods=["POST"]),
        Route("/api/machines/{machine_id:int}", delete_machine, methods=["DELETE"]),
        Route("/api/machines/{machine_id:int}/test", test_machine, methods=["POST"]),
        Route("/api/machines/{machine_id:int}/refresh", refresh_machine, methods=["POST"]),
        Route("/api/machines/{machine_id:int}/interfaces",
              machine_interfaces, methods=["POST"]),
        Route("/api/agents", create_agent, methods=["POST"]),
        Route("/api/models", list_models, methods=["POST"]),
        Route("/api/files/relay", relay_file),
        Route("/api/files/ticket", file_ticket, methods=["POST"]),
        Route("/api/agents/{agent_id:int}", delete_agent, methods=["DELETE"]),
        Route("/api/agents/metrics", agent_metrics),
        Route("/api/logs", control_logs),
        Route("/api/logs/download", control_log_download),
        Route("/api/agents/{agent_id:int}/logs", agent_logs),
        Route("/api/agents/{agent_id:int}/logs/download", agent_log_download),
        Route("/api/agents/{agent_id:int}/config", get_agent_config),
        Route("/api/agents/{agent_id:int}/models", agent_models),
        Route("/api/agents/{agent_id:int}/config", put_agent_config, methods=["PUT"]),
        Route("/api/agents/{agent_id:int}/action", agent_action, methods=["POST"]),
        Route("/api/channels", create_channel, methods=["POST"]),
        Route("/api/channels/{channel_id:int}", delete_channel, methods=["DELETE"]),
        Route("/api/channels/{channel_id:int}/clear", clear_channel, methods=["POST"]),
        Route("/api/agents/{agent_id:int}/clear-context", clear_agent_context, methods=["POST"]),
        Route("/api/agents/{agent_id:int}/reinstall", reinstall_agent, methods=["POST"]),
        Route("/api/channels/{channel_id:int}/members", join_channel, methods=["POST"]),
        Route("/api/channels/{channel_id:int}/members/{agent_id:int}",
              leave_channel, methods=["DELETE"]),
        Route("/api/channels/{channel_id:int}/messages", get_messages),
        Route("/api/channels/{channel_id:int}/messages", post_message, methods=["POST"]),
        Route("/api/channels/{channel_id:int}/halt", set_halt, methods=["POST"]),
        Route("/api/channels/{channel_id:int}/auto-approve",
              set_auto_approve, methods=["POST"]),
        Route("/api/approvals/{approval_id}", resolve_approval, methods=["POST"]),
        Route("/api/mcp", list_mcp),
        Route("/api/mcp/registry", search_mcp_registry),
        Route("/api/mcp", create_mcp, methods=["POST"]),
        Route("/api/mcp/{server_id:int}", update_mcp, methods=["PUT"]),
        Route("/api/mcp/{server_id:int}", delete_mcp, methods=["DELETE"]),
        Route("/api/mcp/{server_id:int}/secret", set_mcp_secret, methods=["POST"]),
        Route("/api/agents/{agent_id:int}/mcp", get_agent_mcp),
        Route("/api/agents/{agent_id:int}/mcp", attach_agent_mcp, methods=["POST"]),
        Route("/api/agents/{agent_id:int}/mcp/{server_id:int}", detach_agent_mcp,
              methods=["DELETE"]),
        Route("/api/settings", get_settings),
        Route("/api/settings", put_settings, methods=["PUT"]),
        Route("/api/board/say", board_say, methods=["POST"]),
        Route("/api/peer/roster", peer_roster),
        Route("/api/peer/preflight", peer_preflight),
        Route("/api/peer/log", peer_log, methods=["POST"]),
        Route("/api/events", events),
        Mount("/static", app=FreshStaticFiles(directory=str(STATIC_DIR)), name="static"),
    ]
    return Starlette(routes=routes)
