"""
Files an agent has chosen to share, served from where they already are.

Agents on different machines cannot read each other's workspace, and copying a
file into every reader's context or directory is what this avoids. Instead the
owner registers one file and hands out a link; its own A2A server streams the
file from disk when the link is followed (``GET /files/{id}``).

Deliberately narrow, because that server listens without authentication:

- nothing is browsable: only a file explicitly shared can be fetched, by an
  unguessable id;
- a link carries an expiry and an HMAC over (id, expiry), so it cannot be
  forged or extended, and it stops working on its own;
- the file must resolve, symlinks included, to somewhere inside the workspace,
  checked again on every read, so a link cannot be turned into a path out;
- read-only, and revocable (``unshare_file``).

State lives beside the workspace (``.shares.json`` and a 0600 ``.share-key``)
so links survive a restart of the agent.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import mimetypes
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from declarative_agent_sdk.core.agent_logging import get_logger

logger = get_logger(__name__)

DEFAULT_TTL_HOURS = 24.0
MAX_TTL_HOURS = 24.0 * 30


class ShareError(ValueError):
    """A share could not be created or a link does not resolve."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


class ShareRegistry:
    def __init__(self, workspace: str, base_url: str, state_dir: Optional[str] = None) -> None:
        self.workspace = Path(workspace).resolve()
        self.base_url = base_url.rstrip("/") + "/"
        state = Path(state_dir).resolve() if state_dir else self.workspace.parent
        self._index_path = state / ".shares.json"
        self._key_path = state / ".share-key"
        self._lock = threading.Lock()
        self._key = self._load_key()
        self._shares: Dict[str, Dict[str, Any]] = self._load_index()

    # ------------------------------------------------------------- state

    def _load_key(self) -> bytes:
        env = os.environ.get("AGENT_SHARE_KEY", "").strip()
        if env:
            return env.encode()
        try:
            return self._key_path.read_bytes().strip()
        except FileNotFoundError:
            pass
        key = secrets.token_hex(32).encode()
        self._key_path.parent.mkdir(parents=True, exist_ok=True)
        # Created 0600 rather than chmod'ed after, so it is never readable by
        # others even for a moment.
        fd = os.open(str(self._key_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(key)
        return key

    def _load_index(self) -> Dict[str, Dict[str, Any]]:
        try:
            data = json.loads(self._index_path.read_text())
        except (FileNotFoundError, ValueError):
            return {}
        return {k: v for k, v in data.items() if isinstance(v, dict)}

    def _save(self) -> None:
        now = time.time()
        live = {k: v for k, v in self._shares.items() if float(v.get("exp", 0)) > now}
        self._shares = live
        tmp = self._index_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(live, indent=1))
        os.replace(tmp, self._index_path)

    # ----------------------------------------------------------- helpers

    def _sign(self, share_id: str, exp: int) -> str:
        return hmac.new(self._key, f"{share_id}.{exp}".encode(), hashlib.sha256).hexdigest()[:32]

    def _inside(self, path: Path) -> Path:
        """Resolve ``path`` (relative to the workspace) and refuse anything outside it."""
        candidate = path if path.is_absolute() else self.workspace / path
        real = candidate.resolve()
        if real != self.workspace and self.workspace not in real.parents:
            raise ShareError(
                f"{path} is outside this agent's workspace ({self.workspace}); "
                "only files in the workspace can be shared", status=403)
        return real

    def url_for(self, share_id: str) -> str:
        record = self._shares[share_id]
        exp = int(record["exp"])
        return f"{self.base_url}files/{share_id}?exp={exp}&sig={self._sign(share_id, exp)}"

    # ------------------------------------------------------------- tools

    def share(self, path: str, ttl_hours: float = DEFAULT_TTL_HOURS) -> Dict[str, Any]:
        real = self._inside(Path(path))
        if not real.is_file():
            raise ShareError(f"{path} is not a file in the workspace", status=404)
        ttl = max(0.01, min(float(ttl_hours or DEFAULT_TTL_HOURS), MAX_TTL_HOURS))
        relative = str(real.relative_to(self.workspace))
        with self._lock:
            share_id = secrets.token_urlsafe(12)
            self._shares[share_id] = {
                "path": relative,
                "created": int(time.time()),
                "exp": int(time.time() + ttl * 3600),
                "type": mimetypes.guess_type(real.name)[0] or "application/octet-stream",
                "size": real.stat().st_size,
            }
            self._save()
        record = self._shares[share_id]
        logger.info(f"[shares] shared {relative} as {share_id} for {ttl:g}h")
        return {"id": share_id, "url": self.url_for(share_id), **record}

    def unshare(self, id_or_path: str) -> int:
        """Revoke by id, or every share of one path. Returns how many went."""
        with self._lock:
            doomed = [k for k, v in self._shares.items()
                      if k == id_or_path or v.get("path") == id_or_path.strip("/")]
            for key in doomed:
                self._shares.pop(key, None)
            if doomed:
                self._save()
        return len(doomed)

    def list(self) -> List[Dict[str, Any]]:
        now = time.time()
        return [
            {"id": k, "url": self.url_for(k), **v}
            for k, v in sorted(self._shares.items(), key=lambda kv: kv[1].get("created", 0))
            if float(v.get("exp", 0)) > now
        ]

    # ------------------------------------------------------------- route

    def resolve(self, share_id: str, exp: str, sig: str) -> Tuple[Path, Dict[str, Any]]:
        """Check a link and return the file it may read. Raises ShareError."""
        try:
            expiry = int(exp)
        except (TypeError, ValueError):
            raise ShareError("malformed link", status=403) from None
        if not hmac.compare_digest(self._sign(share_id, expiry), str(sig or "")):
            raise ShareError("invalid link", status=403)
        if expiry <= time.time():
            raise ShareError("this link has expired", status=410)
        record = self._shares.get(share_id)
        if record is None or int(record.get("exp", 0)) != expiry:
            raise ShareError("this file is no longer shared", status=410)
        # Again at read time: the file may have been swapped for a symlink.
        real = self._inside(Path(record["path"]))
        if not real.is_file():
            raise ShareError("the shared file no longer exists", status=410)
        return real, record


_current: Optional[ShareRegistry] = None


def configure(workspace: str, base_url: str, state_dir: Optional[str] = None) -> ShareRegistry:
    """Called by the A2A server at startup; the tools use what it set up."""
    global _current
    _current = ShareRegistry(workspace, base_url, state_dir)
    return _current


def current() -> Optional[ShareRegistry]:
    return _current
