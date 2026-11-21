"""
An agent's workspace files, served by path at ``/files/<path>``.

Agents on different machines cannot read each other's workspace, and pasting a
file into a message copies it into every reader's context. Instead an agent
hands out a plain link to the file where it already is::

    http://<agent address>/files/<path inside the workspace>

The agent's own A2A server streams the file from disk when the link is
followed. There is no share step, no expiry and no signature: any file in the
workspace can be read by anyone who can reach the agent's port — the same
reach that can already send it tasks over JSON-RPC. That is a deliberate
trade for simplicity; keep anything private out of the workspace.

What is still enforced, on every read:

- only the workspace: a path must resolve, symlinks included, to a file inside
  it, so neither ``../`` nor a symlink leads out;
- no hidden names: a path with any component starting with ``.`` (``.env``,
  ``.git/…``) is refused;
- files only, read-only: a directory is not listed.
"""

from __future__ import annotations

import mimetypes
from pathlib import Path, PurePosixPath
from typing import Optional, Tuple
from urllib.parse import quote

from declarative_agent_sdk.core.agent_logging import get_logger

logger = get_logger(__name__)


class ShareError(ValueError):
    """A path that cannot be served; ``status`` is the HTTP answer."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


class WorkspaceFiles:
    """Maps link paths to workspace files and back."""

    def __init__(self, workspace: str, base_url: str) -> None:
        self.workspace = Path(workspace).resolve()
        self.base_url = base_url.rstrip("/") + "/"

    def relative(self, path: str) -> str:
        """
        A path a link may name, as a workspace-relative POSIX path.

        Accepts a path relative to the workspace or to the agent's directory
        (``workspace/report.md`` — what a lean agent passes to write_file), or
        an absolute path inside the workspace.
        """
        candidate = Path(path).expanduser()
        if candidate.is_absolute():
            real = candidate.resolve()
        else:
            # The agent's process runs in its own directory, so a path the
            # model wrote with (workspace/report.md) resolves from there;
            # failing that, read it as relative to the workspace itself.
            real = candidate.resolve()
            if not self._inside(real):
                real = (self.workspace / candidate).resolve()
        if not self._inside(real):
            raise ShareError(f"{path} is outside this agent's workspace", status=403)
        rel = real.relative_to(self.workspace).as_posix()
        if any(part.startswith(".") for part in PurePosixPath(rel).parts):
            raise ShareError("hidden files are not served", status=403)
        return rel

    def _inside(self, real: Path) -> bool:
        return real == self.workspace or self.workspace in real.parents

    def url_for(self, path: str) -> str:
        """The link for a workspace file (it need not exist yet)."""
        return f"{self.base_url}files/{quote(self.relative(path))}"

    def resolve(self, link_path: str) -> Tuple[Path, str]:
        """
        The file a ``/files/<link_path>`` request may read, and its type.

        ``link_path`` arrives percent-decoded. Checked against the workspace
        on every request, so a file swapped for a symlink is caught.
        """
        rel = (link_path or "").strip("/")
        if not rel:
            raise ShareError("no file named", status=404)
        parts = PurePosixPath(rel).parts
        if any(part in ("..", "") or part.startswith(".") for part in parts):
            raise ShareError("that path is not served", status=403)
        real = (self.workspace / PurePosixPath(*parts)).resolve()
        if not self._inside(real):
            raise ShareError("that path is not served", status=403)
        if not real.is_file():
            raise ShareError("no such file in this agent's workspace", status=404)
        kind = mimetypes.guess_type(real.name)[0] or "application/octet-stream"
        return real, kind


_current: Optional[WorkspaceFiles] = None


def configure(workspace: str, base_url: str) -> WorkspaceFiles:
    """Called by the A2A server at startup; links and tools use what it set up."""
    global _current
    _current = WorkspaceFiles(workspace, base_url)
    logger.info(f"[files] serving {_current.workspace} at {_current.base_url}files/")
    return _current


def current() -> Optional[WorkspaceFiles]:
    return _current


def link_for(path: str) -> Optional[str]:
    """The link for a path, or None when not serving or not in the workspace."""
    files = current()
    if files is None:
        return None
    try:
        return files.url_for(path)
    except ShareError:
        return None
