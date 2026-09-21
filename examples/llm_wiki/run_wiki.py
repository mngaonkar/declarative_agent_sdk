#!/usr/bin/env python3
"""CLI for the wiki maintainer — the same three operations as llm-wiki.

    cd examples/llm_wiki
    export OPENAI_API_KEY=...
    python run_wiki.py ingest raw/journal-2026-08-14.md
    python run_wiki.py ask "what actually improves their sleep?" --save
    python run_wiki.py lint
    python run_wiki.py chat

Each command names the skill to load, so the agent picks up that workflow
rather than carrying all three in every prompt. Tool approvals (when
`tools_approval_required: true` in agent.yaml) are answered at the prompt.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import sys
from datetime import date
from pathlib import Path

# The repo directory *is* the package, so its parent is what goes on sys.path.
# Only a fallback: normally the SDK is installed (pip install -e .).
_PKG = Path(__file__).resolve().parents[2]
_ROOT = _PKG.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

for _env in (Path(__file__).parent / ".env", _PKG / ".env"):
    if _env.is_file():
        try:
            from dotenv import load_dotenv

            load_dotenv(_env)
        except ImportError:
            pass
        break

import wiki_log  # noqa: E402  — same directory
import wiki_tools  # noqa: E402

from declarative_agent_sdk import AgentFactory, ToolRegistry  # noqa: E402

DIM, CYAN, GREEN, YELLOW, RESET = (
    "\033[2m", "\033[36m", "\033[32m", "\033[33m", "\033[0m",
)

CONFIG = "agent.yaml"

# The skills are written in terms of wiki/ and raw/, which is what agent.yaml
# defaults to. Naming the resolved locations up front keeps them correct when
# it does not.
WHERE = """The wiki is at {wiki}/ and its sources are at {raw}/ — where the
skills say wiki/ and raw/, they mean those."""

INGEST = """{where}

Load the wiki-ingest skill and ingest this source: {path}

Follow every step in the skill. Today is {today}; if the source carries its own
date, use that for content and today's for the log entry."""

QUERY = """{where}

Load the wiki-query skill and answer this question against the wiki:

{question}

{save}"""

QUERY_SAVE = "File the answer back into the wiki as the skill describes."
QUERY_NOSAVE = "Do not write anything to the wiki — answer here only."

LINT = """{where}

Load the wiki-lint skill and health-check the wiki. Report findings; do not fix
anything. Today is {today}."""


def rel(path: Path) -> str:
    """Shortest readable form of a path, for prompts and messages."""
    relative = os.path.relpath(path, Path.cwd())
    return str(path) if relative.startswith("..") else relative


def where() -> str:
    return WHERE.format(
        wiki=rel(wiki_tools.wiki_root()), raw=rel(wiki_tools.raw_root())
    )


def build_agent():
    """Resolve the layout, register the two extra tools, build the agent."""
    here = Path(__file__).resolve().parent
    os.chdir(here)
    wiki, raw = wiki_tools.load_layout(CONFIG)
    wiki_tools.configure(wiki, raw)
    # LeanAIAgent resolves YAML tool names through ToolRegistry, and
    # register_built_in_tools() only adds — so registering first is enough.
    ToolRegistry.register("edit_page", wiki_tools.edit_page)
    ToolRegistry.register("search_wiki", wiki_tools.search_wiki)
    ToolRegistry.register("log_note", wiki_log.log_note)

    import yaml

    with open(CONFIG, encoding="utf-8") as handle:
        config = yaml.safe_load(handle) or {}
    # The wiki *is* the workspace. Pin the resolved absolute path so a
    # wiki_directory alias, or a relative value read from somewhere else,
    # cannot leave lean writing to a different tree than the tools read.
    config["workspace_directory"] = str(wiki)
    return AgentFactory.from_dict(config)


async def drive(agent, prompt: str, session_id: str = "cli") -> None:
    """Run one turn, answering any tool-approval pause at the prompt."""
    stream = agent.run_query(prompt, session_id=session_id)
    while True:
        pending = None
        async for event in stream:
            if event.kind == "tool_approval":
                pending = event
                break
            if event.kind == "status":
                print(f"{DIM}{event.text}{RESET}")
            elif event.kind == "error":
                print(f"{YELLOW}error: {event.text}{RESET}", file=sys.stderr)
            else:
                print(f"\n{event.text}")
        if pending is None:
            return
        args = ", ".join(f"{k}={str(v)[:60]!r}" for k, v in pending.tool_args.items())
        print(f"{CYAN}approve {pending.tool_name}({args})?{RESET} [y/N] ", end="")
        approved = input().strip().lower() in ("y", "yes")
        stream = agent.tool_confirmation(
            pending.tool_call_id, session_id, approved
        )


def transact(agent, command: str, prompt: str, before=None) -> None:
    """Run one command as one logged transaction.

    The snapshot has to be taken before anything moves — including the copy
    into the raw directory, which is why cmd_ingest passes its own. Committing
    from `finally` means an interrupted run still records the half of the work
    that landed, which is the case a log exists for.
    """
    if before is None:
        before = wiki_log.snapshot()
    wiki_log.start()
    try:
        asyncio.run(drive(agent, prompt))
    finally:
        summary = wiki_log.commit(command, before)
        if summary:
            print(f"{DIM}{summary}{RESET}")


def cmd_ingest(agent, args) -> None:
    before = wiki_log.snapshot()
    src = Path(args.source).expanduser()
    if not src.is_absolute():
        src = (Path.cwd() / src).resolve()
    if not src.is_file():
        sys.exit(f"no such source: {args.source}")
    if wiki_tools.wiki_root() in src.parents:
        sys.exit(f"{rel(src)} is inside the wiki — only source material can be ingested")
    raw = wiki_tools.raw_root()
    if raw not in src.parents:
        raw.mkdir(parents=True, exist_ok=True)
        dest = raw / src.name
        if dest.exists() and not args.force:
            sys.exit(f"{rel(dest)} already exists; pass --force to replace it")
        shutil.copy2(src, dest)
        print(f"{DIM}copied {src.name} into {rel(raw)}/{RESET}")
        src = dest
    transact(
        agent,
        f"ingest {rel(src)}",
        INGEST.format(where=where(), path=rel(src), today=date.today().isoformat()),
        before=before,
    )


def cmd_ask(agent, args) -> None:
    saved = " --save" if args.save else ""
    transact(
        agent,
        f"ask {args.question!r}{saved}",
        QUERY.format(
            where=where(),
            question=args.question,
            save=QUERY_SAVE if args.save else QUERY_NOSAVE,
        ),
    )


def cmd_lint(agent, args) -> None:
    # Lint reports and does not fix, so this normally commits nothing — but it
    # is still wrapped, because "normally" is not "never".
    transact(
        agent,
        "lint",
        LINT.format(where=where(), today=date.today().isoformat()),
    )


def cmd_chat(agent, args) -> None:
    print(f"{DIM}chatting with the wiki — ctrl-d to exit{RESET}")
    first = True
    while True:
        try:
            line = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if line in ("exit", "quit"):
            return
        if line:
            # Every turn shares one session, so the locations only need saying
            # once — after that they are in the history. Each turn is its own
            # transaction: chat can write, so chat gets logged.
            transact(
                agent,
                f"chat {line!r}",
                f"{where()}\n\n{line}" if first else line,
            )
            first = False


def cmd_log(agent, args) -> None:
    entries = wiki_log.transactions()
    if not entries:
        print(f"{DIM}no transactions yet — {rel(wiki_log.log_path())} is empty{RESET}")
        return
    for tx in entries[-args.limit:]:
        ops = tx["ops"]
        print(f"\n{CYAN}tx {tx['tx']:04d}{RESET} {DIM}{tx['at']}{RESET}  {tx['command']}")
        if tx["note"]:
            print(f"  {tx['note']}")
        for op in ops:
            print(f"  {DIM}{op['op']:<13}{RESET} {op['path']}")


def cmd_replay(agent, args) -> None:
    steps, warnings = wiki_log.replay_plan()
    if not steps:
        print(f"{DIM}nothing to replay — no ingests in the log{RESET}")
        return

    print(f"{CYAN}replay plan — {len(steps)} ingest(s), in recorded order{RESET}")
    for step in steps:
        print(f"  tx {step['tx']:04d}  {step['command']}")
        if step["note"]:
            print(f"           {DIM}{step['note']}{RESET}")
    for warning in warnings:
        print(f"{YELLOW}  warning: {warning}{RESET}")

    if not args.execute:
        print(
            f"\n{DIM}dry run. Pass --execute to re-run these against the current "
            f"wiki.\nThe model is not a deterministic function of its input, so a "
            f"replay lands\non a similar wiki, not a byte-identical one — start "
            f"from an empty wiki\nif you want to compare fairly.{RESET}"
        )
        return

    for step in steps:
        source = str(step["command"]).removeprefix("ingest ").strip()
        print(f"\n{GREEN}replaying tx {step['tx']:04d}: {source}{RESET}")
        transact(
            agent,
            f"replay {source}",
            INGEST.format(where=where(), path=source, today=date.today().isoformat()),
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="run_wiki.py",
        description="Compile sources into a personal wiki, on the lean runtime.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("ingest", help="read a source and integrate it")
    s.add_argument("source")
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_ingest)

    s = sub.add_parser("ask", help="ask a question against the wiki")
    s.add_argument("question")
    s.add_argument("--save", action="store_true", help="file the answer as a page")
    s.set_defaults(fn=cmd_ask)

    s = sub.add_parser("lint", help="health-check the wiki")
    s.set_defaults(fn=cmd_lint)

    s = sub.add_parser("chat", help="open-ended session against the wiki")
    s.set_defaults(fn=cmd_chat)

    s = sub.add_parser("log", help="show the transaction log")
    s.add_argument("--limit", type=int, default=20, help="most recent N (default 20)")
    s.set_defaults(fn=cmd_log)

    s = sub.add_parser("replay", help="re-run the logged ingests in order")
    s.add_argument("--execute", action="store_true", help="actually run them")
    s.set_defaults(fn=cmd_replay)

    args = parser.parse_args()
    agent = build_agent()
    try:
        args.fn(agent, args)
    except KeyboardInterrupt:
        print("\ninterrupted — the wiki may be half-updated; `git diff` to see",
              file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
