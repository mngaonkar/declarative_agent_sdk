"""
Shared client for the agent-board board.

Private (leading underscore), so the tool registry's auto-discovery does not
turn these helpers into tools of their own. ``send_to_agent`` and
``list_agents`` are the tools; this is the plumbing under both.

An agent reaches its board over plain HTTP using the handshake agent-board
seeded into its ``.env`` at install time.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

import httpx

from declarative_agent_sdk.core.agent_logging import get_logger
from declarative_agent_sdk.core.board_env import board_env

logger = get_logger(__name__)

DEFAULT_TIMEOUT = 30


class BoardError(RuntimeError):
    """The board could not be reached, or refused the request."""


class Board:
    """Where the board is, and who this agent is on it."""

    def __init__(self) -> None:
        self.url = (board_env("URL") or "").rstrip("/")
        self.token = board_env("TOKEN") or ""
        # The qualified name is this agent's address; fall back to the bare one
        # for an agent installed before machines were named.
        self.name = (
            os.environ.get("AGENT_QUALIFIED_NAME")
            or os.environ.get("AGENT_NAME")
            or ""
        )
        self.timeout = int(board_env("TIMEOUT") or DEFAULT_TIMEOUT)

    @property
    def headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}


def board() -> Board:
    return Board()


def _raise_for(exc: Exception, what: str) -> None:
    if isinstance(exc, httpx.HTTPStatusError):
        detail = ""
        try:
            detail = str(exc.response.json().get("error") or "")
        except Exception:  # noqa: BLE001 - a non-JSON error body
            detail = exc.response.text[:200]
        raise BoardError(f"{what}: {detail or exc.response.status_code}") from exc
    raise BoardError(f"{what}: {exc}") from exc


def get(link: Board, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    try:
        response = httpx.get(
            f"{link.url}{path}", params=params or {}, headers=link.headers,
            timeout=link.timeout,
        )
        response.raise_for_status()
        return response.json()
    except Exception as exc:  # noqa: BLE001 - reported to the model
        _raise_for(exc, f"could not read {path}")
        return {}


def post_to_board(link: Board, text: str, channel: str = "") -> Dict[str, Any]:
    """
    Say something on the board as this agent, and let it relay the mentions.

    Returns the board's answer: which channel it landed in, who it was
    delivered to, and that channel's members.
    """
    try:
        response = httpx.post(
            f"{link.url}/api/board/say",
            json={"from": link.name, "text": text, "channel": channel},
            headers=link.headers,
            timeout=link.timeout,
        )
        response.raise_for_status()
        return response.json()
    except Exception as exc:  # noqa: BLE001 - reported to the model
        _raise_for(exc, "could not post to the board")
        return {}


def roster(link: Board, channel: str = "", scope: str = "channel") -> List[Dict[str, Any]]:
    """Agents this one can see: its channels' members, or every agent."""
    data = get(link, "/api/peer/roster",
               {"agent": link.name, "channel": channel, "scope": scope})
    return list(data.get("roster") or [])
