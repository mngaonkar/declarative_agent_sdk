"""
write_link — save a file another agent shared straight to disk.

Without it, keeping a shared file meant read_shared, then retyping the whole
text into write_file: twice the tokens, and on a quantized model a tool call
long enough to break its own JSON escaping (observed: a 14 KB article lost at
character 13,722). Here the bytes go from the link to the workspace and the
model only ever sees a short receipt.

The lean runtime has its own write_link (sandboxed like its write_file); this
one is for the adk / deepagent runtimes, and writes inside the workspace the
A2A server serves shares from.
"""

from __future__ import annotations

from pathlib import Path

from declarative_agent_sdk.core import shares
from declarative_agent_sdk.tools.builtin import _links


def _workspace() -> Path:
    registry = shares.current()
    return registry.workspace if registry is not None else Path.cwd().resolve()


def write_link(url: str, path: str, overwrite: bool = False) -> str:
    """
    Save another agent's file link (…/files/<path>) to a file in your workspace.

    Use this instead of read_shared + write_file whenever you want to keep,
    edit or pass on a file another agent shared: the file is copied directly,
    so you never retype its contents.

    Args:
        url: the link exactly as given (http://<agent>/files/<path>).
        path: where to save it, relative to your workspace (e.g. "review.md").
        overwrite: replace an existing file (default false).

    Returns:
        Where it was saved, its size and type, and its first lines.
    """
    workspace = _workspace()
    target = Path(path).expanduser()
    target = (target if target.is_absolute() else workspace / target).resolve()
    if target != workspace and workspace not in target.parents:
        return f"Error: {path} is outside your workspace ({workspace})"
    if target.is_dir():
        return f"Error: {path} is a directory; give a file name"
    if target.exists() and not overwrite:
        return f"Error: {path} already exists; pass overwrite=true to replace it"
    try:
        data, kind = _links.download(url)
    except _links.LinkError as exc:
        return f"Error: {exc}"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return _links.receipt(str(target), data, kind)
