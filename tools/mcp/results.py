"""
An MCP tool result as the text a model reads (MCP-RES-1..5).

Images, audio and binary resources are saved in the agent's workspace and
replaced by a line with their file link, so the board previews them and
nothing large or binary goes into the conversation.
"""

from __future__ import annotations

import base64
import json
import mimetypes
import re
import time
from pathlib import Path
from typing import Any, List

from declarative_agent_sdk.core import shares

MAX_TEXT_CHARS = 20_000


def _extension(mime: str) -> str:
    guessed = mimetypes.guess_extension((mime or "").split(";")[0].strip()) or ".bin"
    return ".jpg" if guessed == ".jpe" else guessed


def _save(workspace: str, server: str, tool: str, index: int, data: bytes, mime: str) -> str:
    """Write one binary item under workspace/mcp/<server>/; a line describing it."""
    folder = Path(workspace) / "mcp" / server
    folder.mkdir(parents=True, exist_ok=True)
    safe_tool = re.sub(r"[^A-Za-z0-9_-]", "_", tool)[:40] or "tool"
    path = folder / f"{safe_tool}-{time.strftime('%Y%m%d-%H%M%S')}-{index}{_extension(mime)}"
    path.write_bytes(data)
    where = shares.link_for(str(path))
    shown = f"{where}" if where else str(path)
    return f"[{mime or 'file'}, {len(data):,} bytes, saved as {path.name}: {shown}]"


def _get(item: Any, *names: str) -> Any:
    for name in names:
        value = getattr(item, name, None)
        if value is not None:
            return value
    return None


def render_result(result: Any, workspace: str, server: str, tool: str,
                  max_chars: int = MAX_TEXT_CHARS) -> str:
    """The text for one CallToolResult."""
    parts: List[str] = []
    for index, item in enumerate(_get(result, "content") or []):
        kind = _get(item, "type")
        if kind == "text":
            parts.append(str(_get(item, "text") or ""))
        elif kind in ("image", "audio"):
            raw = base64.b64decode(_get(item, "data") or "")
            parts.append(_save(workspace, server, tool, index, raw,
                               str(_get(item, "mime_type", "mimeType") or "")))
        elif kind == "resource":
            resource = _get(item, "resource")
            text = _get(resource, "text")
            uri = str(_get(resource, "uri") or "")
            if text is not None:
                parts.append(f"[resource {uri}]\n{text}" if uri else str(text))
            else:
                raw = base64.b64decode(_get(resource, "blob") or "")
                parts.append(_save(workspace, server, tool, index, raw,
                                   str(_get(resource, "mime_type", "mimeType") or "")))
        elif kind == "resource_link":
            parts.append(f"[link {_get(item, 'name') or ''}: {_get(item, 'uri') or ''}]")
        else:
            parts.append(f"[{kind or 'unknown'} content not shown]")

    text = "\n".join(p for p in parts if p != "")
    structured = _get(result, "structured_content", "structuredContent")
    if not text and structured is not None:
        text = json.dumps(structured, ensure_ascii=False, separators=(",", ":"))
    if not text:
        text = "(the tool returned nothing)"
    if len(text) > max_chars:
        text = (text[:max_chars]
                + f"\n...[cut: {len(text) - max_chars:,} more characters not shown]")
    if _get(result, "is_error", "isError"):
        text = f"Error: {text}"
    return text
