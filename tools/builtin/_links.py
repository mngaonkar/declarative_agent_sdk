"""
Fetching an agent's file link to bytes: plumbing for write_link (not a tool).

The leading underscore keeps ToolRegistry from offering these helpers to the
model (see register_built_in_tools).

Only agent file links (http(s)://<agent>/files/<path>, see core/shares.py) are
accepted. write_link writes to disk, and a tool that downloaded any URL a model
typed would be a general downloader that drops arbitrary content into the
workspace. web_request is the tool for the open web.
"""

from __future__ import annotations

import os
from declarative_agent_sdk.core.board_env import board_env
import re
from typing import Dict, Optional, Tuple

import httpx

# http(s)://host[:port][/prefix]/files/<path> — what WorkspaceFiles.url_for
# produces. No query string: these links carry none.
SHARE_LINK = re.compile(r"^https?://[^/\s?#]+(?:/[^\s?#]*)?/files/[^\s?#]+$")

# Bigger than read_shared's 4 MB, since nothing here enters the model's
# context; still bounded, so a link cannot fill the disk.
MAX_BYTES = 25 * 1024 * 1024


class LinkError(Exception):
    """A link that could not be fetched; the message is for the model."""


def is_share_link(url: str) -> bool:
    url = (url or "").strip()
    if not SHARE_LINK.match(url):
        return False
    path = url.split("/files/", 1)[1]
    return not any(part in ("", "..") or part.startswith(".") for part in path.split("/"))


def download(url: str, max_bytes: int = MAX_BYTES) -> Tuple[bytes, str]:
    """
    The bytes behind a shared link, and their content type.

    Tries the owner directly, then the board's relay — the same route
    read_shared takes, for owners on a network this agent cannot reach.
    Refuses (rather than truncates) a file over ``max_bytes``: a partial copy
    on disk would pass for the whole one.
    """
    url = (url or "").strip()
    if not is_share_link(url):
        raise LinkError(
            "that is not an agent file link (expected http(s)://<agent address>/files/<path>). "
            "Use web_request for other URLs.")
    try:
        return _get(url, max_bytes)
    except httpx.TransportError as direct:
        board = (board_env("URL") or "").rstrip("/")
        if not board:
            raise LinkError(f"could not reach the owner ({type(direct).__name__}), "
                            "and this agent has no board to relay through") from direct
        token = board_env("TOKEN") or ""
        try:
            return _get(f"{board}/api/files/relay", max_bytes, params={"url": url},
                        headers={"Authorization": f"Bearer {token}"} if token else {})
        except httpx.TransportError as relay:
            raise LinkError(
                f"could not reach the owner ({type(direct).__name__}) or relay "
                f"through the board ({type(relay).__name__}: {relay})") from relay


def _get(url: str, max_bytes: int, params: Optional[Dict[str, str]] = None,
         headers: Optional[Dict[str, str]] = None) -> Tuple[bytes, str]:
    with httpx.stream("GET", url, params=params, headers=headers or {},
                      timeout=60, follow_redirects=False) as response:
        if response.status_code != 200:
            try:
                reason = response.read().decode("utf-8", "replace")[:300]
            except Exception:  # noqa: BLE001
                reason = ""
            raise LinkError(f"the owner answered {response.status_code}: "
                            f"{reason or 'no detail'} (check the path with the "
                            "agent that gave you the link)")
        kind = response.headers.get("content-type", "application/octet-stream")
        body = bytearray()
        for chunk in response.iter_bytes():
            body.extend(chunk)
            if len(body) > max_bytes:
                raise LinkError(f"the shared file is larger than {max_bytes // (1024 * 1024)} MB; "
                                "not saved")
    return bytes(body), kind


def receipt(path: str, data: bytes, kind: str, preview_lines: int = 3,
            hint: str = "The file is on disk: read_file it only if you need its content.") -> str:
    """What write_link tells the model: enough to trust the save, not the content."""
    lines = []
    if kind.lower().startswith(("text/", "application/json", "application/xml",
                                "application/yaml", "application/x-yaml", "application/toml")):
        text = data[:2000].decode("utf-8", "replace")
        lines = [line[:160] for line in text.splitlines()[:preview_lines]]
    head = f"Saved {len(data):,} bytes ({kind}) to {path}."
    if lines:
        head += " It begins:\n" + "\n".join(f"  | {line}" for line in lines)
    return head + ("\n" + hint if hint else "")
