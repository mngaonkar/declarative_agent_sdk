"""
agents-control service — JSON API, SSE stream and the static board.

Security posture, stated up front because it decides the defaults: this service
creates agents whose default toolset includes ``exec_command`` on every
registered machine, over SSH keys it holds. It is a remote-code-execution
control plane. So it binds 127.0.0.1 unless told otherwise, and any non-loopback
bind demands a token (``AGENTS_CONTROL_TOKEN``) or refuses to start.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse

from declarative_agent_sdk.core.agent_logging import get_logger
from declarative_agent_sdk.agents_control import config as agent_config
from declarative_agent_sdk.agents_control.machines import MachineDriver, MachineError
from declarative_agent_sdk.agents_control.router import ChannelRouter, EventBus
from declarative_agent_sdk.agents_control.store import Store, DEFAULT_DB_PATH

logger = get_logger(__name__)

# When this process loaded its code. Shown by /healthz so a stale server can be
# recognised by eye as well as by revision.
_started = datetime.now(timezone.utc).isoformat(timespec="seconds")

STATIC_DIR = Path(__file__).parent / "static"
# The board itself is unauthenticated (it is a shell that asks for the token);
# every /api path is not. Exact paths and prefixes are kept apart deliberately:
# a prefix list containing "/" matches every request and opens the whole API.
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
        db_path: str = DEFAULT_DB_PATH,
        token: Optional[str] = None,
        public_url: str = "",
    ) -> None:
        self.store = Store(db_path)
        self.driver = MachineDriver()
        self.bus = EventBus()
        self.router = ChannelRouter(self.store, self.bus)
        self.token = token if token is not None else os.environ.get("AGENTS_CONTROL_TOKEN", "")
        self.public_url = public_url.rstrip("/")
        self.app = create_app(self)

    # ------------------------------------------------------------------ run

    def run(self, host: str = "127.0.0.1", port: int = 8900) -> None:
        if host not in ("127.0.0.1", "localhost", "::1") and not self.token:
            raise RuntimeError(
                f"refusing to bind {host} without a token: this service provisions "
                "agents with shell access on every registered machine.\n"
                "  export AGENTS_CONTROL_TOKEN=$(openssl rand -hex 24)\n"
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

        logger.info(f"agents-control on http://{host}:{port} (db {self.store.path})")
        if not self.token:
            logger.warning("no AGENTS_CONTROL_TOKEN set — API is open to anything "
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
        return bool(supplied) and secrets.compare_digest(supplied, cp.token)

    def guard(handler):
        async def wrapped(request: Request):
            if not authorized(request):
                return JSONResponse({"error": "unauthorized"}, status_code=401)
            try:
                return await handler(request)
            except MachineError as exc:
                logger.warning(f"machine command failed: {exc.detail()}")
                return JSONResponse(
                    {"error": str(exc), "detail": exc.stderr}, status_code=502
                )
            except LookupError as exc:
                return JSONResponse({"error": str(exc)}, status_code=404)
            except ValueError as exc:
                return JSONResponse({"error": str(exc)}, status_code=400)
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
        return FileResponse(STATIC_DIR / "index.html")

    async def healthz(request: Request) -> Response:
        """
        Public, and deliberately so: the board reads it before it has a token,
        to find out whether this process is as new as the files it was served.
        """
        from declarative_agent_sdk.__version__ import __version__
        from declarative_agent_sdk.agents_control import API_REVISION

        return JSONResponse({
            "ok": True,
            "version": __version__,
            "api_revision": API_REVISION,
            "subscribers": bus.subscriber_count,
            "started": _started,
        })

    @guard
    async def get_state(request: Request) -> Response:
        from declarative_agent_sdk.agents_control.store import DEFAULT_SETTINGS

        return JSONResponse({
            "machines": store.machines(),
            "agents": store.agents(),
            "channels": store.channels(),
            "settings": store.settings(),
            "defaults": dict(DEFAULT_SETTINGS),
            "auth": bool(cp.token),
        })

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

        if store.agent_by_name(int(machine["id"]), name) is not None:
            raise ValueError(
                f"'{name}.{machine['name']}' already exists. Agent names are unique "
                "per machine — pick another name, or delete that agent first."
            )
        settings = store.settings()
        tools = payload.get("tools") or settings["default_tools"]
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
            sdk_source=str(payload.get("sdk_source") or settings["default_sdk_source"]),
            sdk_ref=str(payload.get("sdk_ref") or ""),
        )
        result.setdefault("name", name)
        result["tools"] = str(tools)
        result["sdk_source"] = result.get("sdk_source") or ""
        result["description"] = str(payload.get("description") or "")
        agent = await in_thread(store.upsert_agent, int(machine["id"]), result)
        bus.publish({"type": "agents"})
        return JSONResponse({"agent": agent, "result": result})

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
        status = await in_thread(
            driver.supervisor_action, machine, str(agent["prefix"] or ""),
            agent["name"], "status",
        )
        return JSONResponse({
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

        written = await in_thread(
            driver.write_agent_files, machine, prefix, text, instructions
        )

        restart = {}
        if payload.get("restart", True):
            restart = await in_thread(
                driver.supervisor_action, machine, prefix, agent["name"], "restart"
            )
            if not restart.get("ok"):
                warnings.append(
                    f"saved, but the restart failed: {restart.get('output', '')[:200]} "
                    f"— the agent is still running its old config"
                )

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
    async def resolve_approval(request: Request) -> Response:
        payload = await body(request)
        approval_id = str(request.path_params["approval_id"])
        if not router.resolve_approval(approval_id, bool(payload.get("approved"))):
            raise LookupError("no such pending approval (it may have timed out)")
        return JSONResponse({"resolved": approval_id})

    # -- settings ---------------------------------------------------------

    @guard
    async def get_settings(request: Request) -> Response:
        """
        Current values, and the defaults this build ships.

        A store keeps whatever it was created with: seeding only fills keys
        that are missing, so a default changed in a later version never reaches
        an existing store. The board shows the difference rather than
        overwriting a value someone may have chosen deliberately.
        """
        from declarative_agent_sdk.agents_control.store import DEFAULT_SETTINGS

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
        Route("/api/agents/{agent_id:int}", delete_agent, methods=["DELETE"]),
        Route("/api/agents/{agent_id:int}/config", get_agent_config),
        Route("/api/agents/{agent_id:int}/config", put_agent_config, methods=["PUT"]),
        Route("/api/agents/{agent_id:int}/action", agent_action, methods=["POST"]),
        Route("/api/channels", create_channel, methods=["POST"]),
        Route("/api/channels/{channel_id:int}", delete_channel, methods=["DELETE"]),
        Route("/api/channels/{channel_id:int}/members", join_channel, methods=["POST"]),
        Route("/api/channels/{channel_id:int}/members/{agent_id:int}",
              leave_channel, methods=["DELETE"]),
        Route("/api/channels/{channel_id:int}/messages", get_messages),
        Route("/api/channels/{channel_id:int}/messages", post_message, methods=["POST"]),
        Route("/api/channels/{channel_id:int}/halt", set_halt, methods=["POST"]),
        Route("/api/approvals/{approval_id}", resolve_approval, methods=["POST"]),
        Route("/api/settings", get_settings),
        Route("/api/settings", put_settings, methods=["PUT"]),
        Route("/api/board/say", board_say, methods=["POST"]),
        Route("/api/peer/roster", peer_roster),
        Route("/api/peer/preflight", peer_preflight),
        Route("/api/peer/log", peer_log, methods=["POST"]),
        Route("/api/events", events),
        Mount("/static", app=StaticFiles(directory=str(STATIC_DIR)), name="static"),
    ]
    return Starlette(routes=routes)
