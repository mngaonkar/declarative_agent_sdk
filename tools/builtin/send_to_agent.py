"""
send_to_agent — address another agent through the agent-board board.

The agent does **not** call its peer. It posts to the channel, and the board
relays the message to whoever was named, exactly as if a human had typed it.
The reply arrives on the board as a new message, so this call does not wait for
one and the agent's turn can end straight after.

Writing ``@name.machine`` in a normal reply does the same thing — the board
reads every reply for mentions. This tool exists for agents that would rather
make the intent an explicit call than trust a mention to survive a rewrite.

Routing through the board is what makes the transcript complete and the kill
switch real: a halted channel simply does not relay.

The agent learns where the board is from its own ``.env``, which agent-board
seeds at install time (``AGENT_BOARD_URL``, ``AGENT_BOARD_TOKEN``,
``AGENT_NAME``, ``AGENT_QUALIFIED_NAME``).

This is not a core tool. List it under ``tools:`` for agents that should be able
to bring a peer into a conversation.
"""

from __future__ import annotations

from typing import Any, Dict

from declarative_agent_sdk.core.agent_logging import get_logger
from declarative_agent_sdk.tools.builtin._board import (
    BoardError,
    board,
    post_to_board,
)

logger = get_logger(__name__)


def send_to_agent(agent: str, message: str, channel: str = "") -> str:
    """
    Ask another agent on the board to do something, via the board.

    Args:
        agent: the peer's name, with or without a leading '@'. Use the fully
            qualified '<agent>.<machine>' when two machines run an agent with
            the same name; the bare name works when it is unambiguous. Call
            list_agents if you are not sure who is here.
        message: what to say to them.
        channel: channel to speak in; optional when you are in exactly one.

    Returns:
        Confirmation that the board has it, or why it could not be delivered.
        The peer's answer is not returned here — it arrives as a new board
        message, which you will be given as a fresh turn.
    """
    target = (agent or "").lstrip("@").strip()
    if not target:
        return (
            "Error: no agent named. Pass the peer's name, e.g. "
            "agent='researcher.build-box'. Call list_agents to see who is here."
        )
    if not (message or "").strip():
        return "Error: message is empty."

    link = board()
    if not link.url:
        return (
            "Error: this agent has no AGENT_BOARD_URL, so it has no board to "
            "post to. It was not created by agent-board, or its .env was not "
            "seeded."
        )

    try:
        result = post_to_board(link, f"@{target} {message}", channel=channel)
    except BoardError as exc:
        return f"Error: {exc}"

    delivered = result.get("relayed") or []
    where = result.get("channel") or channel or "the channel"
    if result.get("halted"):
        return (
            f"Refused: #{where} is halted, so the board delivered nothing. "
            "A human stopped this channel — report that and stop; do not retry."
        )
    if not delivered:
        known = result.get("members") or []
        return (
            f"Posted to #{where}, but nothing was delivered: '@{target}' did not "
            f"match a member. Members here: "
            + (", ".join("@" + name for name in known) if known else "(none)")
            + ". Call list_agents for the current roster."
        )
    return (
        f"Delivered to {', '.join('@' + name for name in delivered)} via #{where}. "
        "Their reply will arrive on the board as a new message — end your turn "
        "now rather than waiting, and do not repeat the mention in your answer."
    )
