"""
SQLite record for agent-board.

This is the control plane's own durable state, deliberately *not* AgentRegistry:
that one is an in-memory, per-process map keyed on a bare agent name, so it
cannot survive a restart and would collide across machines. Here an agent is
identified by (machine, name).

It also holds the canonical channel transcript. That is not a convenience: every
agent runtime keeps its conversation in memory (lean ``_history``, deepagent
``MemorySaver``, adk ``InMemorySessionService``) and supervisord restarts agents
on failure, so the agents themselves are amnesiac. If the board is to remember a
conversation, the board has to store it.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from declarative_agent_sdk.core.agent_logging import get_logger

logger = get_logger(__name__)

HOME_DIR = "~/.agent-board"
# agent-board was called agents-control; a board set up then keeps its data
# here, and is used where it is until it is moved.
LEGACY_HOME_DIR = "~/.agents-control"
DEFAULT_DB_PATH = HOME_DIR + "/control.db"


def board_home() -> Path:
    """
    ~/.agent-board, or the legacy ~/.agents-control when only that holds a board.

    Decided by where the database is, not by which folder exists: the board
    itself creates folders (wheels, staged .env files), and a folder alone
    once switched a board with four machines onto a new, empty database.
    """
    home = Path(HOME_DIR).expanduser()
    legacy = Path(LEGACY_HOME_DIR).expanduser()
    if not (home / "control.db").is_file() and (legacy / "control.db").is_file():
        return legacy
    return home


def default_db_path() -> str:
    """Where the board's database is when no path is given."""
    return str(board_home() / "control.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS machines (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    host        TEXT NOT NULL,
    ssh_user    TEXT NOT NULL DEFAULT '',
    ssh_key     TEXT NOT NULL DEFAULT '',
    ssh_port    INTEGER NOT NULL DEFAULT 22,
    is_local    INTEGER NOT NULL DEFAULT 0,
    agents_root TEXT NOT NULL DEFAULT '',
    os_kind     TEXT NOT NULL DEFAULT '',
    interfaces  TEXT NOT NULL DEFAULT '[]',
    status      TEXT NOT NULL DEFAULT 'unknown',
    last_error  TEXT NOT NULL DEFAULT '',
    created     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agents (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    machine_id  INTEGER NOT NULL REFERENCES machines(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    prefix      TEXT NOT NULL DEFAULT '',
    url         TEXT NOT NULL DEFAULT '',
    agent_card  TEXT NOT NULL DEFAULT '',
    port        INTEGER,
    framework   TEXT NOT NULL DEFAULT 'lean',
    provider    TEXT NOT NULL DEFAULT '',
    model       TEXT NOT NULL DEFAULT '',
    -- where the model actually runs, so /metrics and /slots can be read
    endpoint    TEXT NOT NULL DEFAULT '',
    tools       TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    ready       INTEGER NOT NULL DEFAULT 0,
    problems    TEXT NOT NULL DEFAULT '',
    created     TEXT NOT NULL,
    UNIQUE (machine_id, name)
);

CREATE TABLE IF NOT EXISTS channels (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    name    TEXT NOT NULL UNIQUE,
    topic   TEXT NOT NULL DEFAULT '',
    halted  INTEGER NOT NULL DEFAULT 0,
    -- '' ask every time | 'safe' everything but shell | 'all'
    auto_approve TEXT NOT NULL DEFAULT '',
    created TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS channel_members (
    channel_id INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    agent_id   INTEGER NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    joined     TEXT NOT NULL,
    PRIMARY KEY (channel_id, agent_id)
);

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id  INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    sender_kind TEXT NOT NULL,            -- human | agent | system
    sender      TEXT NOT NULL,
    target      TEXT NOT NULL DEFAULT '', -- set for a directed peer message
    text        TEXT NOT NULL,
    created     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_channel ON messages(channel_id, id);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

def running_from_checkout() -> bool:
    """
    True when this code runs from a source checkout (pyproject.toml beside
    the package), False when it was installed as a package — e.g. by the
    one-line installer, which leaves no working tree to build a wheel from.
    """
    return (Path(__file__).resolve().parents[1] / "pyproject.toml").is_file()


DEFAULT_SETTINGS: Dict[str, Any] = {
    "default_framework": "lean",
    "default_provider": "openai",
    "default_model": "gpt-4o-mini",
    "default_approval": True,
    # "local" builds a wheel from the working tree this control plane runs and
    # sends it; "git" installs from the repository. Local is the default in a
    # checkout, because an agent created from unpushed code would otherwise
    # run whatever happens to be on the branch, silently. An installed control
    # plane has no working tree, so there it is git.
    "default_sdk_source": "local" if running_from_checkout() else "git",
    # How long the board follows a turn. Dispatch submits and polls, so this is
    # a budget on the agent's work, not a limit on silence — and waiting for a
    # human approval does not spend it.
    "agent_timeout_seconds": 1800,
    # Poll cadence: starts here and backs off to the maximum.
    "poll_interval_seconds": 2,
    "poll_max_interval_seconds": 10,
    # One submit or tasks/get call. Short on purpose: these answer immediately,
    # and a long task is waited for by polling rather than by holding a request.
    "poll_request_timeout_seconds": 60,
    "approval_timeout_seconds": 300,
    "transcript_context_messages": 8,
    # 0 = unlimited. Every agent-to-agent hop is a relay through the board, so
    # this is the one place a cascade can be bounded if you ever want it to be.
    "max_relay_hops": 0,
}


# An agent's name is unique only on its own machine, so every row that leaves
# this module carries the fully qualified "<agent>.<machine>" as well. Derived
# in SQL rather than stored: one source of truth, and renaming a machine cannot
# leave a stale copy behind.
QUALIFIED = "a.name || '.' || m.name AS qualified"


def _decode_interfaces(row: Dict[str, Any]) -> Dict[str, Any]:
    """interfaces is stored as JSON text; callers want the list."""
    raw = row.get("interfaces")
    if isinstance(raw, str):
        try:
            row["interfaces"] = json.loads(raw or "[]")
        except json.JSONDecodeError:
            row["interfaces"] = []
    return row


_TOOL_IN_APPROVAL = re.compile(r"wants to run ([A-Za-z_][A-Za-z0-9_]*)\(")


def _summarise_approvals(rows: List[Dict[str, Any]]) -> str:
    """
    One line for a run of approval bookkeeping.

    The full argument text of a past tool call is not worth replaying: the
    agent either still has it in its own history, or it has been restarted and
    is about to look at the files anyway. What it needs to know is that the
    calls happened and whether they were allowed.
    """
    counts: Dict[str, int] = {}
    for row in rows:
        found = _TOOL_IN_APPROVAL.search(str(row["text"] or ""))
        if found:
            name = found.group(1)
            counts[name] = counts.get(name, 0) + 1
    verdicts = " ".join(str(row["text"] or "") for row in rows)
    if "auto-denied" in verdicts:
        outcome = "some denied"
    elif "timed out" in verdicts:
        outcome = "some timed out — denied"
    else:
        outcome = "approved"
    if not counts:
        return f"[{len(rows)} tool-approval lines, {outcome}]"
    tools = ", ".join(
        name if n == 1 else f"{name} x{n}" for name, n in sorted(counts.items())
    )
    return f"[tool calls: {tools} — {outcome}]"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    """Thin synchronous SQLite wrapper. One connection, guarded by a lock."""

    def __init__(self, db_path: Optional[str] = None) -> None:
        self.path = Path(db_path or default_db_path()).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: Starlette runs sync handlers in a threadpool.
        self._db = sqlite3.connect(str(self.path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys = ON")
        self._db.execute("PRAGMA journal_mode = WAL")
        self._lock = threading.RLock()
        with self._lock:
            self._db.executescript(SCHEMA)
            self._db.commit()
        self._migrate()
        self._seed_settings()
        logger.info(f"agent-board store at {self.path}")

    def _migrate(self) -> None:
        """Add columns introduced after a store was first created."""
        agent_columns = {row["name"] for row in self._rows("PRAGMA table_info(agents)")}
        for column, ddl in (("prefix", "TEXT NOT NULL DEFAULT ''"),
                            ("endpoint", "TEXT NOT NULL DEFAULT ''"),
                            # Bumped by "clear context": see context_id_for().
                            ("context_epoch", "INTEGER NOT NULL DEFAULT 0")):
            if column not in agent_columns:
                self._write(f"ALTER TABLE agents ADD COLUMN {column} {ddl}")
                logger.info(f"store: added agents.{column}")

        channel_columns = {row["name"] for row in self._rows("PRAGMA table_info(channels)")}
        for column, ddl in (("auto_approve", "TEXT NOT NULL DEFAULT ''"),
                            ("context_epoch", "INTEGER NOT NULL DEFAULT 0")):
            if column not in channel_columns:
                self._write(f"ALTER TABLE channels ADD COLUMN {column} {ddl}")
                logger.info(f"store: added channels.{column}")

        machine_columns = {row["name"] for row in self._rows("PRAGMA table_info(machines)")}
        for column, ddl in (("interfaces", "TEXT NOT NULL DEFAULT '[]'"),):
            if column not in machine_columns:
                self._write(f"ALTER TABLE machines ADD COLUMN {column} {ddl}")
                logger.info(f"store: added machines.{column}")

    # ------------------------------------------------------------------ util

    def _rows(self, sql: str, args: tuple = ()) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._db.execute(sql, args).fetchall()]

    def _row(self, sql: str, args: tuple = ()) -> Optional[Dict[str, Any]]:
        rows = self._rows(sql, args)
        return rows[0] if rows else None

    def _write(self, sql: str, args: tuple = ()) -> int:
        with self._lock:
            cur = self._db.execute(sql, args)
            self._db.commit()
            return cur.lastrowid or 0

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # -------------------------------------------------------------- settings

    def _seed_settings(self) -> None:
        for key, value in DEFAULT_SETTINGS.items():
            if self._row("SELECT key FROM settings WHERE key = ?", (key,)) is None:
                self._write(
                    "INSERT INTO settings (key, value) VALUES (?, ?)",
                    (key, json.dumps(value)),
                )

    def settings(self) -> Dict[str, Any]:
        out = dict(DEFAULT_SETTINGS)
        for row in self._rows("SELECT key, value FROM settings"):
            try:
                out[row["key"]] = json.loads(row["value"])
            except json.JSONDecodeError:
                out[row["key"]] = row["value"]
        return out

    def setting(self, key: str, default: Any = None) -> Any:
        return self.settings().get(key, default)

    def update_settings(self, values: Dict[str, Any]) -> Dict[str, Any]:
        for key, value in values.items():
            if key not in DEFAULT_SETTINGS:
                continue  # ignore unknown keys rather than storing junk
            self._write(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, json.dumps(value)),
            )
        return self.settings()

    # -------------------------------------------------------------- machines

    def add_machine(
        self,
        name: str,
        host: str,
        ssh_user: str = "",
        ssh_key: str = "",
        ssh_port: int = 22,
        is_local: bool = False,
        agents_root: str = "",
    ) -> Dict[str, Any]:
        machine_id = self._write(
            "INSERT INTO machines (name, host, ssh_user, ssh_key, ssh_port, "
            "is_local, agents_root, created) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (name, host, ssh_user, ssh_key, ssh_port, int(is_local), agents_root, _now()),
        )
        return self.machine(machine_id) or {}

    def machine(self, machine_id: int) -> Optional[Dict[str, Any]]:
        row = self._row("SELECT * FROM machines WHERE id = ?", (machine_id,))
        return _decode_interfaces(row) if row else None

    def machine_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        return self._row("SELECT * FROM machines WHERE name = ?", (name,))

    def machines(self) -> List[Dict[str, Any]]:
        return [_decode_interfaces(row)
                for row in self._rows("SELECT * FROM machines ORDER BY name")]

    def set_machine_status(
        self,
        machine_id: int,
        status: str,
        error: str = "",
        os_kind: str = "",
        interfaces: Optional[List[Dict[str, str]]] = None,
    ) -> None:
        self._write(
            "UPDATE machines SET status = ?, last_error = ?, "
            "os_kind = CASE WHEN ? = '' THEN os_kind ELSE ? END, "
            "interfaces = CASE WHEN ? IS NULL THEN interfaces ELSE ? END "
            "WHERE id = ?",
            (status, error, os_kind, os_kind,
             None if interfaces is None else json.dumps(interfaces),
             None if interfaces is None else json.dumps(interfaces),
             machine_id),
        )

    def delete_machine(self, machine_id: int) -> None:
        self._write("DELETE FROM machines WHERE id = ?", (machine_id,))

    # ---------------------------------------------------------------- agents

    def upsert_agent(self, machine_id: int, meta: Dict[str, Any]) -> Dict[str, Any]:
        """Record an agent from an install.sh result / --list entry."""
        name = str(meta.get("name") or "")
        tools = meta.get("tools")
        if isinstance(tools, (list, tuple)):
            tools = ",".join(str(t) for t in tools)
        problems = meta.get("problems")
        if isinstance(problems, (list, tuple)):
            problems = "; ".join(str(p) for p in problems)
        self._write(
            """
            INSERT INTO agents (machine_id, name, prefix, url, agent_card, port, framework,
                                provider, model, endpoint, tools, description,
                                ready, problems, created)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(machine_id, name) DO UPDATE SET
                prefix = CASE WHEN excluded.prefix = ''
                              THEN agents.prefix ELSE excluded.prefix END,
                url = excluded.url, agent_card = excluded.agent_card,
                port = excluded.port, framework = excluded.framework,
                provider = excluded.provider, model = excluded.model,
                endpoint = CASE WHEN excluded.endpoint = ''
                                THEN agents.endpoint ELSE excluded.endpoint END,
                tools = CASE WHEN excluded.tools = '' THEN agents.tools ELSE excluded.tools END,
                description = CASE WHEN excluded.description = ''
                                   THEN agents.description ELSE excluded.description END,
                ready = excluded.ready, problems = excluded.problems
            """,
            (
                machine_id, name, str(meta.get("prefix") or ""),
                str(meta.get("url") or ""), str(meta.get("agent_card") or ""),
                meta.get("port"), str(meta.get("framework") or "lean"),
                str(meta.get("provider") or ""), str(meta.get("model") or ""),
                str(meta.get("endpoint") or ""),
                str(tools or ""), str(meta.get("description") or ""),
                int(bool(meta.get("ready"))), str(problems or ""), _now(),
            ),
        )
        return self.agent_by_name(machine_id, name) or {}

    def set_agent_url(self, agent_id: int, url: str) -> None:
        """Record where an agent now listens, after its address was changed."""
        url = str(url or "")
        self._write(
            "UPDATE agents SET url = ?, agent_card = ? WHERE id = ?",
            (url, f"{url}.well-known/agent-card.json" if url else "", agent_id),
        )

    def set_agent_endpoint(self, agent_id: int, endpoint: str) -> None:
        """
        Record where an agent's model runs.

        Called when an agent is created and again whenever its ``agent.yaml``
        is read, which is how agents installed before this column existed
        acquire an endpoint without being reinstalled.
        """
        self._write("UPDATE agents SET endpoint = ? WHERE id = ?",
                    (str(endpoint or ""), agent_id))

    def agent(self, agent_id: int) -> Optional[Dict[str, Any]]:
        return self._row(
            f"SELECT a.*, m.name AS machine_name, m.host AS machine_host, {QUALIFIED} "
            "FROM agents a JOIN machines m ON m.id = a.machine_id WHERE a.id = ?",
            (agent_id,),
        )

    def agent_by_name(self, machine_id: int, name: str) -> Optional[Dict[str, Any]]:
        return self._row(
            f"SELECT a.*, m.name AS machine_name, m.host AS machine_host, {QUALIFIED} "
            "FROM agents a JOIN machines m ON m.id = a.machine_id "
            "WHERE a.machine_id = ? AND a.name = ?",
            (machine_id, name),
        )

    def agents(self) -> List[Dict[str, Any]]:
        return self._rows(
            f"SELECT a.*, m.name AS machine_name, m.host AS machine_host, {QUALIFIED} "
            "FROM agents a JOIN machines m ON m.id = a.machine_id "
            "ORDER BY m.name, a.name"
        )

    def agents_named(self, name: str) -> List[Dict[str, Any]]:
        """Every agent carrying this bare name — names are unique per machine only."""
        return self._rows(
            f"SELECT a.*, m.name AS machine_name, m.host AS machine_host, {QUALIFIED} "
            "FROM agents a JOIN machines m ON m.id = a.machine_id WHERE a.name = ?",
            (name,),
        )

    def agent_by_qualified(self, qualified: str) -> Optional[Dict[str, Any]]:
        """Find an agent by '<agent>.<machine>' (case-insensitive)."""
        name, _, machine = (qualified or "").partition(".")
        if not name or not machine:
            return None
        return self._row(
            f"SELECT a.*, m.name AS machine_name, m.host AS machine_host, {QUALIFIED} "
            "FROM agents a JOIN machines m ON m.id = a.machine_id "
            "WHERE lower(a.name) = lower(?) AND lower(m.name) = lower(?)",
            (name, machine),
        )

    def delete_agent(self, agent_id: int) -> None:
        self._write("DELETE FROM agents WHERE id = ?", (agent_id,))

    def prune_agents(self, machine_id: int, keep_names: List[str]) -> int:
        """Drop records for agents that no longer exist on the machine."""
        existing = [a["name"] for a in self._rows(
            "SELECT name FROM agents WHERE machine_id = ?", (machine_id,)
        )]
        gone = [n for n in existing if n not in keep_names]
        for name in gone:
            self._write(
                "DELETE FROM agents WHERE machine_id = ? AND name = ?", (machine_id, name)
            )
        return len(gone)

    # -------------------------------------------------------------- channels

    def add_channel(self, name: str, topic: str = "") -> Dict[str, Any]:
        channel_id = self._write(
            "INSERT INTO channels (name, topic, created) VALUES (?, ?, ?)",
            (name, topic, _now()),
        )
        return self.channel(channel_id) or {}

    def channel(self, channel_id: int) -> Optional[Dict[str, Any]]:
        return self._row("SELECT * FROM channels WHERE id = ?", (channel_id,))

    def channel_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        return self._row("SELECT * FROM channels WHERE name = ?", (name,))

    def channels(self) -> List[Dict[str, Any]]:
        rows = self._rows("SELECT * FROM channels ORDER BY name")
        for row in rows:
            row["members"] = [m["name"] for m in self.members(int(row["id"]))]
        return rows

    def clear_messages(self, channel_id: int) -> int:
        """Drop a channel's transcript; returns how many messages went."""
        count = int((self._row(
            "SELECT COUNT(*) AS n FROM messages WHERE channel_id = ?", (channel_id,)
        ) or {}).get("n") or 0)
        self._write("DELETE FROM messages WHERE channel_id = ?", (channel_id,))
        return count

    def bump_channel_context(self, channel_id: int) -> None:
        self._write("UPDATE channels SET context_epoch = context_epoch + 1 WHERE id = ?",
                    (channel_id,))

    def bump_agent_context(self, agent_id: int) -> None:
        self._write("UPDATE agents SET context_epoch = context_epoch + 1 WHERE id = ?",
                    (agent_id,))

    def delete_channel(self, channel_id: int) -> None:
        self._write("DELETE FROM channels WHERE id = ?", (channel_id,))

    def set_halted(self, channel_id: int, halted: bool) -> None:
        self._write("UPDATE channels SET halted = ? WHERE id = ?", (int(halted), channel_id))

    def set_auto_approve(self, channel_id: int, level: str) -> None:
        """'' ask every time, 'safe' everything but shell, 'all' everything."""
        if level not in ("", "safe", "all"):
            raise ValueError(f"unknown auto-approve level: {level!r}")
        self._write("UPDATE channels SET auto_approve = ? WHERE id = ?",
                    (level, channel_id))

    def auto_approve(self, channel_id: int) -> str:
        row = self.channel(channel_id)
        return str(row["auto_approve"]) if row else ""

    def is_halted(self, channel_id: int) -> bool:
        row = self.channel(channel_id)
        return bool(row and row["halted"])

    def join(self, channel_id: int, agent_id: int) -> None:
        self._write(
            "INSERT OR IGNORE INTO channel_members (channel_id, agent_id, joined) "
            "VALUES (?, ?, ?)",
            (channel_id, agent_id, _now()),
        )

    def leave(self, channel_id: int, agent_id: int) -> None:
        self._write(
            "DELETE FROM channel_members WHERE channel_id = ? AND agent_id = ?",
            (channel_id, agent_id),
        )

    def members(self, channel_id: int) -> List[Dict[str, Any]]:
        return self._rows(
            f"SELECT a.*, m.name AS machine_name, {QUALIFIED} FROM channel_members cm "
            "JOIN agents a ON a.id = cm.agent_id "
            "JOIN machines m ON m.id = a.machine_id "
            "WHERE cm.channel_id = ? ORDER BY a.name",
            (channel_id,),
        )

    def channels_for_agent_name(self, agent_name: str) -> List[Dict[str, Any]]:
        """
        Channels an agent belongs to, by bare or qualified name.

        A bare name matches on every machine, which is what the peer tool wants
        when it only knows what it was called locally.
        """
        name, _, machine = (agent_name or "").partition(".")
        if machine:
            return self._rows(
                "SELECT DISTINCT c.* FROM channels c "
                "JOIN channel_members cm ON cm.channel_id = c.id "
                "JOIN agents a ON a.id = cm.agent_id "
                "JOIN machines m ON m.id = a.machine_id "
                "WHERE lower(a.name) = lower(?) AND lower(m.name) = lower(?) "
                "ORDER BY c.name",
                (name, machine),
            )
        # DISTINCT: a bare name can match an agent on several machines, and the
        # join would then return the same channel once per match — which reads
        # downstream as "this agent is in several channels".
        return self._rows(
            "SELECT DISTINCT c.* FROM channels c "
            "JOIN channel_members cm ON cm.channel_id = c.id "
            "JOIN agents a ON a.id = cm.agent_id "
            "WHERE lower(a.name) = lower(?) ORDER BY c.name",
            (name,),
        )

    # -------------------------------------------------------------- messages

    def add_message(
        self,
        channel_id: int,
        sender_kind: str,
        sender: str,
        text: str,
        target: str = "",
    ) -> Dict[str, Any]:
        message_id = self._write(
            "INSERT INTO messages (channel_id, sender_kind, sender, target, text, created) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (channel_id, sender_kind, sender, target, text, _now()),
        )
        return self._row("SELECT * FROM messages WHERE id = ?", (message_id,)) or {}

    def messages(self, channel_id: int, limit: int = 200, after_id: int = 0) -> List[Dict[str, Any]]:
        rows = self._rows(
            "SELECT * FROM messages WHERE channel_id = ? AND id > ? "
            "ORDER BY id DESC LIMIT ?",
            (channel_id, after_id, limit),
        )
        return list(reversed(rows))

    # Tool-approval bookkeeping. Two of these are written per tool call, so a
    # turn with four calls buries the conversation it belongs to.
    _APPROVAL_MARKS = (
        " wants to run ",
        "auto-approved for @",
        "auto-denied",
        "Approval for ",
    )

    @classmethod
    def _is_approval_line(cls, text: str) -> bool:
        return any(mark in text for mark in cls._APPROVAL_MARKS)

    def recent_transcript(
        self, channel_id: int, limit: int = 8, exclude_id: Optional[int] = None
    ) -> str:
        """
        Recent channel lines, for prepending to a dispatch.

        Needed because an agent restarted by supervisord has lost its own
        history; without this the thread silently resets mid-conversation.

        ``limit`` counts *conversation* messages — what people and agents
        actually said. It used to count rows, which meant tool-approval
        bookkeeping competed with the conversation for the same slots: one
        observed dispatch spent six of its eight on "wants to run" and
        "auto-approved" pairs, and replayed a single reply. Those lines are now
        summarised instead, so raising the limit buys more conversation.

        ``exclude_id`` leaves out one message — the one being dispatched, which
        the caller appends itself and which would otherwise appear twice.
        """
        if limit <= 0:
            return ""

        # Over-fetch, because the bookkeeping between two conversation lines is
        # unbounded and no longer spends the budget.
        rows = self.messages(channel_id, limit=max(limit * 6, 40))
        kept: List[Dict[str, Any]] = []
        conversation = 0
        for row in reversed(rows):                      # newest first
            if exclude_id is not None and int(row["id"]) == int(exclude_id):
                continue
            if row["sender_kind"] != "system":
                if conversation >= limit:
                    break
                conversation += 1
            kept.append(row)
        kept.reverse()

        lines: List[str] = []
        pending: List[Dict[str, Any]] = []              # a run of approval lines

        def flush() -> None:
            if pending:
                lines.append(f"system: {_summarise_approvals(pending)}")
                pending.clear()

        for row in kept:
            text = str(row["text"] or "")
            if row["sender_kind"] == "system" and self._is_approval_line(text):
                pending.append(row)
                continue
            flush()
            who = row["sender"] if row["sender_kind"] != "system" else "system"
            lines.append(f"{who}: {text}")
        flush()
        return "\n".join(lines)
