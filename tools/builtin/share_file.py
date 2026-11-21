"""
share_file — give another agent a link to a file in this agent's workspace.

Agents on other machines cannot open this workspace, and pasting a file into a
message copies it into every reader's context. A link instead: this agent's own
A2A server serves the file from disk while the link is valid, and the reader
fetches it with ``read_shared`` only if it needs it. See core/shares.py for what
is and is not exposed.

``share_file`` and ``read_shared`` are core tools, registered for every agent;
list ``unshare_file`` / ``list_shared_files`` under ``tools:`` if wanted.
"""

from __future__ import annotations

from declarative_agent_sdk.core import shares


def _registry():
    registry = shares.current()
    if registry is None:
        raise RuntimeError(
            "file sharing is only available while this agent is served over A2A "
            "(run_server.py / agents-control); nothing is listening for links here"
        )
    return registry


def share_file(path: str, ttl_hours: float = 24) -> str:
    """
    Share one file from your workspace with other agents, by link.

    Put the returned link in your reply (or in send_to_agent); the other agent
    reads it with read_shared. Nothing is copied: the link serves the file as
    it is on disk, so later edits are seen through the same link.

    Args:
        path: file path, relative to your workspace (e.g. "report.md") or
            absolute inside it. Files outside the workspace cannot be shared.
        ttl_hours: how long the link stays valid (default 24, at most 720).

    Returns:
        The link, with the file's name, size and expiry.
    """
    try:
        share = _registry().share(path, ttl_hours)
    except (shares.ShareError, RuntimeError) as exc:
        return f"Error: {exc}"
    hours = max(0, (share["exp"] - share["created"]) / 3600)
    return (
        f"Shared {share['path']} ({share['size']} bytes, {share['type']}) for "
        f"{hours:g}h:\n{share['url']}\n"
        "Give this link to the agent that needs the file; it can read it with "
        "read_shared. Revoke it early with unshare_file."
    )


def unshare_file(id_or_path: str) -> str:
    """
    Revoke a shared link before it expires.

    Args:
        id_or_path: the share id (the part after /files/ in the link), or a
            workspace-relative path to revoke every link to that file.
    """
    try:
        count = _registry().unshare(id_or_path)
    except RuntimeError as exc:
        return f"Error: {exc}"
    return f"Revoked {count} link(s)." if count else f"Nothing shared matches {id_or_path!r}."


def list_shared_files() -> str:
    """List the files you are currently sharing, with their links and expiry."""
    try:
        live = _registry().list()
    except RuntimeError as exc:
        return f"Error: {exc}"
    if not live:
        return "You are not sharing any files."
    return "\n".join(f"{item['path']} ({item['size']} bytes) → {item['url']}" for item in live)
