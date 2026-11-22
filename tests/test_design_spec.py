"""
docs/design maps 1:1 onto the code: every source file has exactly one
section, headed by its path in backticks, and every section names a file that
exists. See docs/design/README.md.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DESIGN = ROOT / "docs" / "design"
HEADING = re.compile(r"^## `([^`]+)`\s*$", re.MULTILINE)

# What counts as source. Examples, tests and generated files are not specced.
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "build", "dist", "__pycache__",
             "tests", "examples", "docs", ".github", ".claude"}
SOURCE_SUFFIXES = {".py", ".sh", ".ts", ".js", ".cjs", ".css", ".html"}
SOURCE_NAMES = {"Dockerfile"}


def _source_files():
    found = set()
    for path in ROOT.rglob("*"):
        rel = path.relative_to(ROOT)
        if any(part in SKIP_DIRS or part.endswith(".egg-info") for part in rel.parts):
            continue
        if path.is_file() and (path.suffix in SOURCE_SUFFIXES or path.name in SOURCE_NAMES):
            if path.name.endswith(".d.ts") or path.suffix == ".js" and "static" not in rel.parts:
                continue        # build output and vendored bundles
            found.add(rel.as_posix())
    return found


def _sections():
    sections = {}
    for spec in sorted(DESIGN.rglob("*.md")):
        for name in HEADING.findall(spec.read_text(encoding="utf-8")):
            sections.setdefault(name, []).append(spec.relative_to(ROOT).as_posix())
    return sections


def test_every_source_file_has_a_section():
    missing = sorted(_source_files() - set(_sections()))
    assert not missing, (
        "These files have no section in docs/design (add '## `<path>`' to the "
        "spec of their area):\n  " + "\n  ".join(missing))


def test_no_file_has_two_sections():
    doubled = {name: specs for name, specs in _sections().items() if len(specs) > 1}
    assert not doubled, f"Sections must be unique; found: {doubled}"


def test_every_section_names_a_real_file():
    gone = sorted(name for name in _sections() if not (ROOT / name).is_file())
    assert not gone, (
        "These spec sections name files that do not exist (renamed or "
        "deleted?):\n  " + "\n  ".join(gone))


def test_rule_ids_are_unique():
    seen = {}
    pattern = re.compile(r"^- ([A-Z0-9]+(?:-[A-Z0-9]+)*-\d+):", re.MULTILINE)
    for spec in DESIGN.rglob("*.md"):
        for rule in pattern.findall(spec.read_text(encoding="utf-8")):
            seen.setdefault(rule, []).append(spec.name)
    doubled = {rule: specs for rule, specs in seen.items() if len(specs) > 1}
    assert not doubled, f"Rule IDs must be unique; found: {doubled}"
