"""
The control plane's own log, kept where the board can show it.

agent-board usually runs in a terminal nobody is looking at, so an install
that fails on a remote machine left its reason in scrollback the board could
not reach. Every record from this process now also goes to:

* a ring buffer in memory, which ``/api/logs`` pages through by sequence
  number so the Logs tab can poll for only what is new, and
* a rotating file beside the database (``~/.agent-board/logs/control.log``)
  that survives a restart.

Both hang off the root logger, so the SDK's own modules, uvicorn's errors and
anything else in the process are caught without each module opting in.
uvicorn's access log is left out: the board polls several endpoints every few
seconds, and one line per poll would bury everything else.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional

CAPACITY = 5000
FILE_BYTES = 5 * 1024 * 1024
FILE_BACKUPS = 3
FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"

# Loggers whose records are noise here (see the module docstring).
_SKIP = ("uvicorn.access",)

LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}


class _Quiet(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not record.name.startswith(_SKIP)


class LogBook(logging.Handler):
    """Ring buffer of recent records, each with a monotonically rising seq."""

    def __init__(self, capacity: int = CAPACITY) -> None:
        super().__init__(level=logging.DEBUG)
        self.addFilter(_Quiet())
        self._records: Deque[Dict[str, Any]] = deque(maxlen=capacity)
        self._seq = 0
        self._guard = threading.Lock()
        self.file_path: Optional[str] = None

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
            if record.exc_info:
                message += "\n" + logging.Formatter().formatException(record.exc_info)
            with self._guard:
                self._seq += 1
                self._records.append({
                    "seq": self._seq,
                    "ts": record.created,
                    "level": record.levelname,
                    "logger": record.name,
                    "message": message,
                })
        except Exception:  # noqa: BLE001 - a log handler must never raise
            self.handleError(record)

    def read(
        self,
        after: int = 0,
        level: str = "",
        query: str = "",
        limit: int = 500,
    ) -> Dict[str, Any]:
        """
        Records with seq > ``after``, oldest first, at most ``limit`` (the
        newest ones when more match). ``last`` is the highest seq seen, to pass
        back as ``after`` next time, whether or not anything matched.
        """
        floor = LEVELS.get(level.upper(), 0) if level else 0
        needle = query.lower()
        limit = max(1, min(int(limit), CAPACITY))
        with self._guard:
            snapshot = list(self._records)
            last = self._seq
        matched = [
            r for r in snapshot
            if r["seq"] > after
            and LEVELS.get(r["level"], 0) >= floor
            and (not needle or needle in r["message"].lower() or needle in r["logger"].lower())
        ]
        return {
            "records": matched[-limit:],
            "last": last,
            "truncated": len(matched) > limit,
            "file": self.file_path,
        }

    def clear(self) -> None:
        with self._guard:
            self._records.clear()


_book: Optional[LogBook] = None
_file: Optional[RotatingFileHandler] = None
_install_lock = threading.Lock()


def book() -> LogBook:
    """The process-wide LogBook, attached to the root logger on first use."""
    global _book
    with _install_lock:
        if _book is None:
            _book = LogBook()
            logging.getLogger().addHandler(_book)
        return _book


def install(log_dir: Optional[str] = None) -> LogBook:
    """
    Attach the buffer and, given ``log_dir``, the rotating file.

    Safe to call repeatedly (every ControlPlane does): the buffer is attached
    once, and the file handler is replaced only when the directory changes.
    """
    global _file
    logbook = book()
    root = logging.getLogger()
    # The SDK's setup_logging configures INFO by default; never let the root
    # sit above that, or the buffer would see nothing below WARNING.
    if root.level > logging.INFO or root.level == logging.NOTSET:
        root.setLevel(logging.INFO)
    if not log_dir:
        return logbook
    path = Path(log_dir).expanduser() / "control.log"
    with _install_lock:
        if _file is not None and _file.baseFilename == str(path.resolve()):
            return logbook
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            handler = RotatingFileHandler(
                str(path), maxBytes=FILE_BYTES, backupCount=FILE_BACKUPS, encoding="utf-8")
        except OSError as exc:
            logging.getLogger(__name__).warning(f"control log file disabled: {exc}")
            return logbook
        handler.setLevel(logging.INFO)
        handler.setFormatter(logging.Formatter(FORMAT, "%Y-%m-%d %H:%M:%S"))
        handler.addFilter(_Quiet())
        if _file is not None:
            root.removeHandler(_file)
            _file.close()
        root.addHandler(handler)
        _file = handler
        logbook.file_path = str(path)
    return logbook


def tail_lines(text: str, lines: int) -> List[str]:
    """Last ``lines`` lines of ``text`` (used to bound installer output in logs)."""
    parts = text.rstrip("\n").splitlines()
    return parts[-lines:] if lines > 0 else parts
