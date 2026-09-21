"""Two tools the lean runtime does not ship, both needed to maintain a wiki.

``write_file`` can only replace a whole page, which loses detail every time a
long thread page gains one line, and there is no search at all — so finding the
pages that mention an entity means reading the whole tree. These fill both gaps.

``run_wiki.py`` registers them with ``ToolRegistry`` before the agent is built,
so ``agent.yaml`` can list them under ``tools:`` like any built-in.

Both directories come from ``agent.yaml`` (see ``load_layout``) and default to
``wiki/`` and ``raw/`` beside it. Writes are confined to the wiki here as well
as in lean's own ``_is_mutable`` check: these are separate handlers, so they
need their own guard. The raw directory is readable but never writable.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional, Tuple

MAX_SEARCH_HITS = 60

DEFAULT_WIKI_DIRNAME = "wiki"
DEFAULT_RAW_DIRNAME = "raw"

_WIKI = (Path.cwd() / DEFAULT_WIKI_DIRNAME).resolve()
_RAW = (Path.cwd() / DEFAULT_RAW_DIRNAME).resolve()


def load_layout(
    config_path: str = "agent.yaml", base: Optional[str] = None
) -> Tuple[Path, Path]:
    """Read the wiki and raw locations out of agent.yaml.

    ``workspace_directory`` is the wiki — it has to be, since that is the only
    tree lean will let the agent write to. ``wiki_directory`` is accepted as an
    alias for it. ``raw_directory`` is the source material.

    Both default to ``wiki``/``raw``, and a relative value resolves against the
    directory holding agent.yaml, so the defaults mean "beside this file".
    Absolute values are taken as given, so the wiki and its sources can live
    anywhere — outside the example, or shared between several agents.
    """
    config_file = Path(config_path).expanduser()
    root = Path(base).expanduser() if base else config_file.resolve().parent
    config = {}
    if config_file.is_file():
        import yaml

        with open(config_file, encoding="utf-8") as handle:
            config = yaml.safe_load(handle) or {}

    def _dir(*keys: str, default: str) -> Path:
        for key in keys:
            value = config.get(key)
            if value:
                candidate = Path(str(value)).expanduser()
                return (candidate if candidate.is_absolute() else root / candidate).resolve()
        return (root / default).resolve()

    return (
        _dir("wiki_directory", "workspace_directory", default=DEFAULT_WIKI_DIRNAME),
        _dir("raw_directory", default=DEFAULT_RAW_DIRNAME),
    )


def configure(wiki: str | Path, raw: str | Path) -> None:
    """Point the tools at a wiki and its sources (called by run_wiki.py)."""
    global _WIKI, _RAW
    _WIKI = Path(wiki).expanduser().resolve()
    _RAW = Path(raw).expanduser().resolve()


def wiki_root() -> Path:
    """The writable wiki directory, absolute."""
    return _WIKI


def raw_root() -> Path:
    """The read-only source directory, absolute."""
    return _RAW


def _within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _display(path: Path) -> str:
    """Shortest readable form: relative to cwd when that does not escape."""
    try:
        relative = os.path.relpath(path, Path.cwd())
    except ValueError:  # different drive on Windows
        return str(path)
    return str(path) if relative.startswith("..") else relative


def _locate(path: str) -> Path:
    """Resolve a path without judging it — permission is checked separately.

    A path may be absolute, relative to the working directory (which is how
    the skills write them: ``wiki/sleep-quality.md``), or relative to either root.
    The plain reading wins whenever it names somewhere inside the wiki or the
    sources, so ``raw/journal.md`` stays a source path and is refused as a
    write, rather than being re-read as ``wiki/raw/journal.md``.
    """
    given = Path(path.strip()).expanduser()
    if given.is_absolute():
        return given.resolve()
    plain = (Path.cwd() / given).resolve()
    if plain.exists() or _within(plain, _WIKI) or _within(plain, _RAW):
        return plain
    for root in (_WIKI, _RAW):
        candidate = (root / given).resolve()
        if candidate.exists():
            return candidate
    return plain


def _resolve(path: str, *, writable: bool) -> Path:
    if not path or not path.strip():
        raise ValueError("path is required")
    located = _locate(path)
    if not any(_within(located, root) for root in (_WIKI, _RAW)):
        raise ValueError(
            f"path is outside the wiki: {path} "
            f"(readable: {_display(_WIKI)}/ and {_display(_RAW)}/)"
        )
    if writable:
        if not _within(located, _WIKI):
            raise ValueError(
                f"refusing to write to {path}: only {_display(_WIKI)}/ is writable "
                f"({_display(_RAW)}/ holds immutable source material)"
            )
        if located.suffix != ".md":
            raise ValueError(f"wiki pages must be .md files, got {path}")
    return located


def edit_page(path: str, old_text: str, new_text: str) -> str:
    """Replace one unique occurrence of old_text with new_text in a wiki page.

    Prefer this over write_file for any page that already exists — rewriting a
    long page to change one claim silently destroys the rest of it. old_text
    must appear exactly once, so include enough surrounding context to be
    unique.

    Args:
        path: Page to edit, e.g. wiki/sleep-quality.md
        old_text: Exact text to replace, including enough context to be unique.
        new_text: Replacement text.
    """
    try:
        target = _resolve(path, writable=True)
    except ValueError as exc:
        return f"Error: {exc}"
    if not target.is_file():
        return f"Error: no such page: {path} (use write_file to create it)"

    body = target.read_text(encoding="utf-8")
    found = body.count(old_text)
    if found == 0:
        return (
            f"Error: old_text not found in {path}. Read the page again — it may "
            "have changed since you last read it, or the whitespace may differ."
        )
    if found > 1:
        return (
            f"Error: old_text appears {found} times in {path}; include more "
            "surrounding context so it matches exactly once."
        )
    target.write_text(body.replace(old_text, new_text), encoding="utf-8")
    return f"Edited {path} (replaced {len(old_text)} chars with {len(new_text)})."


def search_wiki(pattern: str, path: str = ".", ignore_case: bool = True) -> str:
    """Regex-search the wiki and sources, returning path:line: text matches.

    The fastest way to answer "which pages already mention this?" before
    writing anything. Search the wiki to find pages to update, the raw sources
    to find source material, or "." for both.

    Args:
        pattern: Python regular expression.
        path: Directory to search, or "." for the whole wiki and its sources.
        ignore_case: Case-insensitive matching (default true).
    """
    if path.strip() in ("", ".", "all"):
        roots = [_WIKI, _RAW]
    else:
        try:
            roots = [_resolve(path, writable=False)]
        except ValueError as exc:
            return f"Error: {exc}"
    try:
        rx = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
    except re.error as exc:
        return f"Error: bad regex {pattern!r}: {exc}"

    targets: list[Path] = []
    for root in roots:
        targets.extend([root] if root.is_file() else sorted(root.rglob("*")))

    hits: list[str] = []
    files = 0
    for candidate in targets:
        if not candidate.is_file() or any(p.startswith(".") for p in candidate.parts):
            continue
        try:
            text = candidate.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        rel = _display(candidate)
        matched = False
        for number, line in enumerate(text.splitlines(), 1):
            if rx.search(line):
                matched = True
                hits.append(f"{rel}:{number}: {line.strip()[:200]}")
                if len(hits) >= MAX_SEARCH_HITS:
                    break
        files += matched
        if len(hits) >= MAX_SEARCH_HITS:
            hits.append(f"[stopped at {MAX_SEARCH_HITS} matches — narrow the pattern]")
            break

    if not hits:
        return f"No matches for {pattern!r} under {path}."
    return f"{len(hits)} matches in {files} files:\n" + "\n".join(hits)
