"""
list_agents — who else is on the agent-board board.

Discovery has two layers, and this is the second one. The roster of the current
channel is already pushed into every dispatch, so an agent normally knows who it
can address without asking. This tool is for the cases that push cannot cover:

  - looking again part-way through a long task, after someone joined or left;
  - finding an agent that exists on the board but is *not* in this channel, so
    the agent can say who it needs instead of guessing;
  - reading what the other members are for, by description, before choosing.

Only members of a shared channel can actually be addressed; anything else is
listed so the agent can ask a human to add it.

This is not a core tool. List it under ``tools:`` alongside ``send_to_agent``.
"""

from __future__ import annotations

from typing import Any, Dict, List

from declarative_agent_sdk.core.agent_logging import get_logger
from declarative_agent_sdk.tools.builtin._board import BoardError, board, roster

logger = get_logger(__name__)


def list_agents(channel: str = "", include_all: bool = False) -> str:
    """
    List the agents on the board, so you know who you can address with @name.

    Args:
        channel: restrict to one channel; omit for every channel you are in.
        include_all: also list agents that are on the board but not in your
            channels. You cannot address those — report them to the human
            instead of trying.

    Returns:
        A readable roster: '@name.machine' per line with what it is for.
    """
    link = board()
    if not link.url:
        return (
            "Error: this agent has no AGENT_BOARD_URL, so it is not on a board. "
            "It was not created by agent-board, or its .env was not seeded."
        )

    try:
        reachable = roster(link, channel=channel, scope="channel")
        outside = roster(link, channel="", scope="all") if include_all else []
    except BoardError as exc:
        return f"Error: {exc}"

    lines: List[str] = []
    if reachable:
        lines.append("Agents you can address with @name (same channel):")
        lines.extend(_format(entry, with_channel=True) for entry in reachable)
    else:
        lines.append(
            "No other agents share a channel with you, so there is nobody to "
            "address. Say so plainly rather than inventing a name."
        )

    if include_all:
        addressable = {str(entry.get("qualified") or "") for entry in reachable}
        others = [
            entry for entry in outside
            if str(entry.get("qualified") or "") not in addressable
            and str(entry.get("qualified") or "") != link.name
        ]
        if others:
            lines.append("")
            lines.append(
                "Also on the board, but not in your channel — you cannot address "
                "these; ask the human to add one if you need it:"
            )
            lines.extend(_format(entry, with_channel=False) for entry in others)

    return "\n".join(lines)


def _format(entry: Dict[str, Any], with_channel: bool) -> str:
    name = entry.get("qualified") or entry.get("name") or "?"
    bits = []
    if entry.get("description"):
        bits.append(str(entry["description"]))
    if entry.get("model"):
        bits.append(str(entry["model"]))
    if with_channel and entry.get("channel"):
        bits.append(f"in #{entry['channel']}")
    if not entry.get("ready", True):
        bits.append("NOT RUNNING")
    return f"  @{name}" + (f" — {'; '.join(bits)}" if bits else "")
