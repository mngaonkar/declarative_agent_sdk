"""
The agent-board environment variables, under their current and legacy names.

agent-board was called agents-control, and agents installed then have
``AGENTS_CONTROL_URL`` / ``AGENTS_CONTROL_TOKEN`` in their ``.env``. Every
reader goes through :func:`board_env` so those agents keep working: the new
``AGENT_BOARD_*`` name wins, the legacy one is the fallback.
"""

from __future__ import annotations

import os

PREFIX = "AGENT_BOARD_"
LEGACY_PREFIX = "AGENTS_CONTROL_"


def board_env(name: str, default: str = "") -> str:
    """``AGENT_BOARD_<name>``, else ``AGENTS_CONTROL_<name>``, else ``default``."""
    return (os.environ.get(PREFIX + name)
            or os.environ.get(LEGACY_PREFIX + name)
            or default)
