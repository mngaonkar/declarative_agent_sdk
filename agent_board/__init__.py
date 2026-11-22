"""
agent-board — a control plane for SDK agents.

A single supervised service that provisions agents on registered machines
(over SSH, using ``packaging/deploy/install.sh``), keeps the canonical record
of what exists, and hosts an IRC-style board where agents collaborate in
channels.

    from declarative_agent_sdk.agent_board import ControlPlane
    ControlPlane(db_path="~/.agent-board/control.db").run(port=8900)

Or from the CLI::

    declarative-agent board --port 8900

Layout:
    store.py      SQLite record of machines, agents, channels, transcript
    machines.py   install.sh / uninstall.sh driver (ssh push, or local)
    router.py     @mention dispatch over A2A, approvals, kill switch, SSE bus
    server.py     Starlette app: JSON API + SSE + static UI
    static/       the board itself (HTML/CSS/JS, no build step)
"""

# Bumped whenever the HTTP surface changes in a way the board depends on —
# a new route, a renamed field, different semantics.
#
# The board checks this because the two halves can drift apart on their own:
# static/*.js is read from disk on every request, so a browser gets the current
# board, while the Python routes were loaded when the process started. Edit the
# tree without restarting and the new board talks to the old server, which
# answers a route it does not have with a bare 404.
#
# Keep in step with EXPECTS_API in static/app.js.
API_REVISION = 31

from declarative_agent_sdk.agent_board.store import Store
from declarative_agent_sdk.agent_board.machines import MachineDriver, MachineError
from declarative_agent_sdk.agent_board.router import ChannelRouter, EventBus
from declarative_agent_sdk.agent_board.server import ControlPlane, create_app

__all__ = [
    "API_REVISION",
    "Store",
    "MachineDriver",
    "MachineError",
    "ChannelRouter",
    "EventBus",
    "ControlPlane",
    "create_app",
]
