"""Anthropic-style progressive skills (from esp32s3-ai-agent).

Level 1  name + description  — always in the system prompt
Level 2  full SKILL.md body — loaded when the model calls Skill(name)
Level 3  bundled files      — via read_file / run_script
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from declarative_agent_sdk.agent_logging import get_logger

logger = get_logger(__name__)


def _strip_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def parse_frontmatter(text: str) -> Tuple[Dict[str, Any], str]:
    """Parse leading ``---`` YAML-ish frontmatter. Returns (meta, body)."""
    if not text.startswith("---"):
        return {}, text

    end = text.find("\n---", 3)
    if end == -1:
        return {}, text

    block = text[text.find("\n", 3) + 1 : end]
    body_start = text.find("\n", end + 1)
    body = text[body_start + 1 :] if body_start != -1 else ""

    meta: Dict[str, Any] = {}
    current_key: Optional[str] = None
    for raw in block.split("\n"):
        line = raw.rstrip()
        if not line.strip() or line.strip().startswith("#"):
            continue

        if line.startswith((" ", "\t")) and line.strip().startswith("- "):
            if current_key:
                meta.setdefault(current_key, [])
                if isinstance(meta[current_key], list):
                    meta[current_key].append(_strip_quotes(line.strip()[2:].strip()))
            continue

        if ":" not in line:
            continue

        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        current_key = key

        if not value:
            meta[key] = []
        elif value.startswith("[") and value.endswith("]"):
            inner = value[1:-1].strip()
            meta[key] = (
                [_strip_quotes(p.strip()) for p in inner.split(",") if p.strip()]
                if inner
                else []
            )
        else:
            meta[key] = _strip_quotes(value)

    return meta, body


class Skill:
    def __init__(
        self, name: str, path: str, description: str, meta: Dict[str, Any]
    ) -> None:
        self.name = name
        self.path = path
        self.description = description
        self.meta = meta

    def read(self) -> str:
        """Level 2: full SKILL.md body."""
        with open(os.path.join(self.path, "SKILL.md"), encoding="utf-8") as f:
            return parse_frontmatter(f.read())[1]

    def files(self, rel: str = "", depth: int = 0) -> List[Tuple[str, str, int]]:
        """Level 3: bundled files (rel path, abs path, size)."""
        found: List[Tuple[str, str, int]] = []
        base = os.path.join(self.path, rel) if rel else self.path
        if depth > 4:
            return found
        try:
            entries = os.listdir(base)
        except OSError:
            return found
        for entry in entries:
            child_rel = f"{rel}/{entry}" if rel else entry
            full = os.path.join(base, entry)
            if entry == "SKILL.md" and not rel:
                continue
            if os.path.isdir(full):
                found.extend(self.files(child_rel, depth + 1))
            else:
                try:
                    size = os.path.getsize(full)
                except OSError:
                    size = 0
                found.append((child_rel, full, size))
        return found


class SkillRegistry:
    """Discover skills and render progressive-disclosure payloads."""

    def __init__(self, root: str = "skills") -> None:
        self.root = str(Path(root).expanduser())
        self.skills: Dict[str, Skill] = {}
        self.discover()

    def discover(self) -> Dict[str, Skill]:
        self.skills = {}
        root = Path(self.root)
        if not root.is_dir():
            logger.info(f"[skills] no skills directory at {self.root}")
            return self.skills

        for entry in sorted(root.iterdir()):
            if not entry.is_dir():
                continue
            manifest = entry / "SKILL.md"
            if not manifest.is_file():
                continue
            try:
                # Bound the index read so huge bodies never enter RAM just to list.
                with open(manifest, encoding="utf-8") as f:
                    head = f.read(4096)
                meta, _ = parse_frontmatter(head)
            except Exception as exc:
                logger.warning(f"[skills] failed to index {entry.name}: {exc}")
                continue

            name = meta.get("name") or entry.name
            description = meta.get("description") or ""
            if not description:
                logger.warning(f"[skills] {name} has no description; skipping")
                continue
            self.skills[str(name)] = Skill(
                str(name), str(entry.resolve()), str(description), meta
            )

        logger.info(
            "[skills] loaded %d: %s"
            % (len(self.skills), ", ".join(sorted(self.skills)) or "none")
        )
        return self.skills

    def get(self, name: str) -> Optional[Skill]:
        return self.skills.get(name)

    def catalog(self) -> str:
        """Level 1 text for the system prompt."""
        if not self.skills:
            return "(no skills installed)"
        lines = [
            f"- {name}: {self.skills[name].description}"
            for name in sorted(self.skills)
        ]
        return "\n".join(lines)

    def render(self, name: str) -> str:
        """Level 2 payload returned by the Skill tool."""
        skill = self.get(name)
        if not skill:
            available = ", ".join(sorted(self.skills)) or "none"
            return f"No skill named '{name}'. Available skills: {available}"

        try:
            body = skill.read()
        except Exception as exc:
            return f"Failed to read skill '{name}': {exc}"

        out = [f"# Skill: {skill.name}\n", body.strip()]
        bundled = skill.files()
        if bundled:
            out.append("\n\n## Bundled files")
            out.append(
                "Read these with read_file, or execute .py scripts with "
                "run_script, using the exact absolute paths below."
            )
            for rel, full, size in bundled:
                out.append(f"- {rel} ({size} bytes) -> {full}")
        return "\n".join(out)
