"""The wiki's transaction log — `wiki/logs.md`.

A database keeps a log so that state can be rebuilt by replaying it. This does
the same job for the wiki: every command is one transaction, and the log
records what it did to both trees. Replay the ingests in the recorded order
against an empty wiki and you land on the same state — or close to it, since a
model is not a deterministic function of its input. See `replay_plan`.

**The log is written by code, not by the model.** A log the model maintains by
hand is a log that drifts on the first long ingest, and a drifted log is worse
than none — it reads as authoritative and is not. So instead of instrumenting
each write path, this takes a content snapshot of the wiki and its sources
before a command and again after, and records the difference. That catches
every write regardless of which tool made it (`write_file`, `edit_page`,
`exec_command`), and it catches edits made outside the agent entirely: change a
page in Obsidian between two runs and the next transaction records it, which is
exactly what you want a log to do.

The model's only contribution is the one-line `note:` — the *why*, which a
diff cannot see — supplied through the `log_note` tool. If it never calls it,
the block simply has no note; the log's integrity never depends on the model.

Operations, matching the two trees:

    raw.add       a source document appeared in the raw directory
    raw.update    a source document changed — sources are meant to be
                  immutable, so this is an integrity event, not routine
    raw.delete    a source document was removed
    page.create   a new wiki page
    page.update   an existing wiki page changed
    page.delete   a wiki page was removed
    index.update  the catalog changed (index.md, called out separately
                  because "when did the catalog last move" is a real question)

Paths are logged under the logical prefixes `wiki/` and `raw/` whatever the
directories are actually called on disk, so a log stays readable after the
locations in agent.yaml change.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import wiki_tools

LOG_NAME = "logs.md"
HASH_CHARS = 16

# The log is not part of the state it describes: a database does not log its
# own log. Excluded from every snapshot, so appending never shows up as a page
# update in the next transaction.
EXCLUDED = {LOG_NAME}

LOG_HEADER = """---
title: Logs
type: logs
---

# Transaction log

Append-only, machine-written — one block per command, oldest first. Do not
edit this file by hand; `wiki_log.py` writes it and `run_wiki.py replay` reads
it back.

    ## tx NNNN | <UTC timestamp> | <command>
    note: what the run was for, in one line (from the agent, when it says)
    <op>  <path>  sha=<content hash after>  size=<before>-><after>

Greppable: `grep '^page.update' logs.md` is every page edit ever made,
`grep '^## tx' logs.md` is the command history.
"""

Entry = Tuple[str, int]  # (sha, size)
Snapshot = Dict[str, Entry]

_pending_note: Optional[str] = None


# --------------------------------------------------------------- snapshotting


def _digest(path: Path) -> Entry:
    data = path.read_bytes()
    return hashlib.sha256(data).hexdigest()[:HASH_CHARS], len(data)


def _walk(root: Path, prefix: str) -> Snapshot:
    if not root.is_dir():
        return {}
    out: Snapshot = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if any(part.startswith(".") for part in relative.parts):
            continue
        if prefix == "wiki/" and relative.as_posix() in EXCLUDED:
            continue
        try:
            out[prefix + relative.as_posix()] = _digest(path)
        except OSError:
            continue
    return out


def snapshot() -> Snapshot:
    """Content hashes of every file in the wiki and its sources."""
    return {
        **_walk(wiki_tools.wiki_root(), "wiki/"),
        **_walk(wiki_tools.raw_root(), "raw/"),
    }


# ------------------------------------------------------------------ the note


def start() -> None:
    """Clear the note left over from the previous transaction."""
    global _pending_note
    _pending_note = None


def log_note(text: str) -> str:
    """Record one line in the wiki's transaction log saying what this run did.

    The log records *what* changed on its own. This is for the *why* — the
    thing a diff cannot show. One sentence, plain text, once per run: what you
    filed and what it changed about the wiki's picture. Call it before you
    finish.

    Args:
        text: One line. What this run did and why it mattered.
    """
    global _pending_note
    line = " ".join(str(text).split())
    if not line:
        return "Error: the note is empty."
    _pending_note = line[:400]
    return "Noted — it will be written into logs.md when this run commits."


# -------------------------------------------------------------- the log file


def log_path() -> Path:
    return wiki_tools.wiki_root() / LOG_NAME


def _next_tx() -> int:
    path = log_path()
    if not path.is_file():
        return 1
    found = re.findall(r"^## tx (\d+)", path.read_text(encoding="utf-8"), re.M)
    return (max(int(n) for n in found) + 1) if found else 1


def _classify(key: str, before: Optional[Entry], after: Optional[Entry]) -> str:
    if key.startswith("raw/"):
        verb = "add" if before is None else ("delete" if after is None else "update")
        return f"raw.{verb}"
    verb = "create" if before is None else ("delete" if after is None else "update")
    if key == "wiki/index.md":
        # The catalog is a page like any other, but "when did the index last
        # move" is a question worth one grep, so it gets its own op name.
        return "index.update" if verb == "create" else f"index.{verb}"
    return f"page.{verb}"


def diff(before: Snapshot, after: Snapshot) -> List[str]:
    """One log line per changed file, sorted by path."""
    lines: List[str] = []
    for key in sorted(set(before) | set(after)):
        was, now = before.get(key), after.get(key)
        if was == now:
            continue
        op = _classify(key, was, now)
        sha = now[0] if now else "-"
        if was and now:
            size = f"{was[1]}->{now[1]}"
        elif now:
            size = str(now[1])
        else:
            size = f"{was[1]}->0"
        lines.append(f"{op:<13} {key:<44} sha={sha} size={size}")
    return lines


def commit(command: str, before: Snapshot) -> str:
    """Append one transaction block. Returns a one-line summary, or ''."""
    ops = diff(before, snapshot())
    note = _pending_note
    if not ops and not note:
        return ""

    path = log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.is_file():
        path.write_text(LOG_HEADER, encoding="utf-8")

    tx = _next_tx()
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    block = [f"\n## tx {tx:04d} | {stamp} | {command}"]
    if note:
        block.append(f"note: {note}")
    block.extend(ops or ["(no file changed)"])
    with open(path, "a", encoding="utf-8") as handle:
        handle.write("\n".join(block) + "\n")

    start()
    counts: Dict[str, int] = {}
    for line in ops:
        counts[line.split()[0]] = counts.get(line.split()[0], 0) + 1
    detail = ", ".join(f"{n} {op}" for op, n in sorted(counts.items())) or "no changes"
    return f"logged tx {tx:04d}: {detail}"


# ---------------------------------------------------------------- reading back


def transactions() -> List[Dict[str, object]]:
    """Parse the log back into transactions, oldest first."""
    path = log_path()
    if not path.is_file():
        return []
    out: List[Dict[str, object]] = []
    current: Optional[Dict[str, object]] = None
    for line in path.read_text(encoding="utf-8").splitlines():
        header = re.match(r"^## tx (\d+) \| (\S+) \| (.*)$", line)
        if header:
            current = {
                "tx": int(header.group(1)),
                "at": header.group(2),
                "command": header.group(3).strip(),
                "note": "",
                "ops": [],
            }
            out.append(current)
            continue
        if current is None:
            continue
        if line.startswith("note: "):
            current["note"] = line[len("note: "):]
        elif re.match(r"^(raw|page|index)\.\w+\s", line):
            fields = line.split()
            current["ops"].append({
                "op": fields[0],
                "path": fields[1],
                "sha": fields[2].removeprefix("sha="),
                "size": fields[3].removeprefix("size=") if len(fields) > 3 else "",
            })
    return out


def replay_plan() -> Tuple[List[Dict[str, object]], List[str]]:
    """The ingests to re-run, in order, and any reason replay would diverge.

    Only ingests rebuild state: a query that filed an answer is reproducible
    from the wiki it read, and a query that saved nothing changed nothing. The
    warnings are the honest part — every way the replay would not land back on
    the same wiki.
    """
    steps: List[Dict[str, object]] = []
    warnings: List[str] = []
    sources = {
        key: entry for key, entry in snapshot().items() if key.startswith("raw/")
    }
    seen: set[str] = set()

    for tx in transactions():
        command = str(tx["command"])
        added = [op for op in tx["ops"] if op["op"] == "raw.add"]  # type: ignore[index]
        if not command.startswith("ingest "):
            if tx["ops"]:
                warnings.append(
                    f"tx {tx['tx']:04d} changed pages outside an ingest "
                    f"({command}) — replay cannot reproduce it"
                )
            continue
        for op in added:
            path = str(op["path"])
            seen.add(path)
            present = sources.get(path)
            if present is None:
                warnings.append(
                    f"tx {tx['tx']:04d} ingested {path}, which is no longer in "
                    "the raw directory — that source cannot be replayed"
                )
            elif present[0] != op["sha"]:
                warnings.append(
                    f"tx {tx['tx']:04d} ingested {path} at sha={op['sha']}, but "
                    f"it is now sha={present[0]} — replay would read a different "
                    "document"
                )
        steps.append({
            "tx": tx["tx"],
            "command": command,
            "sources": [op["path"] for op in added],
            "note": tx["note"],
        })

    for path in sorted(set(sources) - seen):
        warnings.append(f"{path} is in the raw directory but was never ingested")
    return steps, warnings
