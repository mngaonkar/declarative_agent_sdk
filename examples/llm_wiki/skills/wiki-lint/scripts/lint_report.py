"""Deterministic structural checks over the wiki.

Run through the lean `run_script` tool. Everything here is a graph or filesystem
fact — orphans, dangling links, index drift, missing frontmatter — so it should
be computed, not inferred by reading every page and burning tokens on it.

The judgement calls that remain (contradictions, stale claims, gaps) are left to
the model, which reads this report first and then goes looking.

Available in scope from run_script: ``args``, ``tool(name, args)``, ``print``,
``result``.

The wiki and raw locations come from agent.yaml. run_script executes this in
the same process as the agent, so ``wiki_tools`` is already imported and
configured — asking it is what keeps this script and the tools looking at the
same tree. The ``wiki``/``raw`` args and the agent.yaml read below are only
fallbacks for running it under a different harness.
"""

import re
from pathlib import Path


def _layout():
    override = (args.get("wiki"), args.get("raw"))
    if all(override):
        return tuple(Path(p).expanduser().resolve() for p in override)
    try:
        import wiki_tools

        return wiki_tools.wiki_root(), wiki_tools.raw_root()
    except Exception:
        pass
    try:
        import sys

        sys.path.insert(0, str(Path.cwd()))
        import wiki_tools

        return wiki_tools.load_layout(args.get("config") or "agent.yaml")
    except Exception:
        base = Path(args.get("root") or ".").expanduser().resolve()
        return base / "wiki", base / "raw"


WIKI, RAW = _layout()
SPECIAL = {"index", "logs", "portrait"}
THIN_CHARS = 400

LINK_RE = re.compile(r"\[\[([^\]|#]+)")


def frontmatter(text):
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    meta = {}
    for line in text[text.find("\n", 3) + 1 : end].split("\n"):
        if ":" in line and not line.startswith((" ", "\t", "#")):
            key, value = line.split(":", 1)
            meta[key.strip()] = value.strip()
    return meta


pages = {}
for path in sorted(WIKI.rglob("*.md")):
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        continue
    slug = path.stem
    pages[slug] = {
        "path": path.relative_to(WIKI.parent),
        "text": text,
        "meta": frontmatter(text),
        "links": {m.strip() for m in LINK_RE.findall(text)},
    }

inbound = {slug: set() for slug in pages}
wanted = {}
for slug, page in pages.items():
    for target in page["links"]:
        if target in inbound:
            if target != slug:
                inbound[target].add(slug)
        else:
            wanted.setdefault(target, set()).add(slug)

index_text = pages.get("index", {}).get("text", "")
index_links = {m.strip() for m in LINK_RE.findall(index_text)}

orphans = sorted(
    slug for slug, sources in inbound.items()
    if not sources and slug not in SPECIAL
)
missing_from_index = sorted(
    slug for slug in pages
    if slug not in index_links and slug not in SPECIAL
)
index_ghosts = sorted(link for link in index_links if link not in pages)
thin = sorted(
    slug for slug, page in pages.items()
    if slug not in SPECIAL and len(page["text"]) < THIN_CHARS
)
no_frontmatter = sorted(
    slug for slug, page in pages.items()
    if slug not in SPECIAL and not page["meta"].get("title")
)
undated = sorted(
    slug for slug, page in pages.items()
    if slug not in SPECIAL and not page["meta"].get("updated")
)

source_pages = {
    slug for slug, page in pages.items() if page["meta"].get("type") == "source"
}
raw_files = {p.stem for p in RAW.iterdir() if p.is_file() and not p.name.startswith(".")} if RAW.is_dir() else set()
unfiled = sorted(raw_files - source_pages)

# The wiki is flat: a subdirectory means a page went somewhere the index and
# the [[slug]] namespace do not expect.
subdirs = sorted(
    str(d.relative_to(WIKI))
    for d in WIKI.iterdir()
    if d.is_dir() and not d.name.startswith(".")
)
untyped = sorted(
    slug for slug, page in pages.items()
    if slug not in SPECIAL and not page["meta"].get("type")
)


def section(title, items, render=lambda x: f"  - {x}"):
    if not items:
        return [f"{title}: none"]
    lines = [f"{title}: {len(items)}"]
    lines.extend(render(item) for item in items)
    return lines


report = [
    f"Structural lint over {WIKI} — {len(pages)} pages, "
    f"{len(raw_files)} raw sources in {RAW}",
    "",
]
report += section("ORPHANS (no page links to them)", orphans)
report += [""]
report += section(
    "WANTED (linked but missing)",
    sorted(wanted),
    lambda slug: f"  - {slug}  <- linked from {', '.join(sorted(wanted[slug]))}",
)
report += [""]
report += section("MISSING FROM INDEX", missing_from_index)
report += [""]
report += section("INDEX POINTS AT NOTHING", index_ghosts)
report += [""]
report += section(f"THIN (under {THIN_CHARS} chars)", thin)
report += [""]
report += section("NO TITLE IN FRONTMATTER", no_frontmatter)
report += [""]
report += section("NO updated: DATE", undated)
report += [""]
report += section("NO type: IN FRONTMATTER (it is what groups the index)", untyped)
report += [""]
report += section("SUBDIRECTORIES (the wiki is flat — move these up)", subdirs)
report += [""]
report += section("RAW SOURCES NEVER INGESTED", unfiled)

print("\n".join(report))

result = {
    "pages": len(pages),
    "orphans": len(orphans),
    "wanted": len(wanted),
    "index_drift": len(missing_from_index) + len(index_ghosts),
    "subdirectories": len(subdirs),
    "unfiled_sources": len(unfiled),
}
