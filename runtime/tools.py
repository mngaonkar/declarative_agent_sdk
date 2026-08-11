"""Lean tool registry — skills tools + callable bridges.

Skills supply *instructions*; these tools supply *capability*.
"""

from __future__ import annotations

import inspect
import json
import os
import traceback
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Union

from declarative_agent_sdk.agent_logging import get_logger
from declarative_agent_sdk.runtime.skills import SkillRegistry, parse_frontmatter

logger = get_logger(__name__)

# Tools that never need human approval (meta / discovery).
AUTO_APPROVE_TOOLS: Set[str] = {
    "Skill",
    "list_skills",
    "list_dir",
    "read_file",
}


class LeanToolRegistry:
    """OpenAI function schemas + handlers."""

    def __init__(
        self,
        skills: SkillRegistry,
        *,
        workspace: str = "workspace",
        mutable_prefixes: Optional[List[str]] = None,
    ) -> None:
        self.skills = skills
        self.workspace = str(Path(workspace).expanduser().resolve())
        os.makedirs(self.workspace, exist_ok=True)
        skills_root = str(Path(skills.root).resolve())
        self._mutable_prefixes = mutable_prefixes or [
            skills_root.rstrip("/") + "/",
            self.workspace.rstrip("/") + "/",
            "/tmp/",
        ]
        self._handlers: Dict[str, Callable[[Dict[str, Any]], Any]] = {}
        self._schemas: List[Dict[str, Any]] = []
        self._register_core()

    # ------------------------------------------------------------------ API

    def add(
        self,
        name: str,
        description: str,
        properties: Dict[str, Any],
        required: List[str],
        handler: Callable[[Dict[str, Any]], Any],
    ) -> None:
        self._schemas.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": description,
                    "parameters": {
                        "type": "object",
                        "properties": properties,
                        "required": required,
                    },
                },
            }
        )
        self._handlers[name] = handler

    def schemas(self) -> List[Dict[str, Any]]:
        return list(self._schemas)

    def names(self) -> List[str]:
        return list(self._handlers.keys())

    def invoke(self, name: str, arguments: Optional[Dict[str, Any]] = None) -> str:
        handler = self._handlers.get(name)
        if not handler:
            return f"Error: no such tool '{name}'"
        try:
            result = handler(arguments or {})
        except Exception as exc:
            logger.exception(f"tool '{name}' failed")
            return f"Error: {type(exc).__name__}: {exc}"
        if isinstance(result, str):
            return result
        try:
            return json.dumps(result, default=str)
        except Exception:
            return str(result)

    def add_callable(self, fn: Callable[..., Any], name: Optional[str] = None) -> None:
        """Register a Python function using its signature and docstring."""
        tool_name = name or fn.__name__
        if tool_name in self._handlers:
            logger.debug(f"tool '{tool_name}' already registered; skipping")
            return

        sig = inspect.signature(fn)
        properties: Dict[str, Any] = {}
        required: List[str] = []
        for pname, param in sig.parameters.items():
            if pname in ("self", "cls"):
                continue
            ann = param.annotation
            json_type = "string"
            if ann is int:
                json_type = "integer"
            elif ann is float:
                json_type = "number"
            elif ann is bool:
                json_type = "boolean"
            elif ann is dict or str(ann).startswith("typing.Dict") or str(ann).startswith("dict"):
                json_type = "object"
            elif ann is list or str(ann).startswith("typing.List") or str(ann).startswith("list"):
                json_type = "array"
            properties[pname] = {"type": json_type}
            if param.default is inspect.Parameter.empty:
                required.append(pname)

        doc = (inspect.getdoc(fn) or f"Call {tool_name}.").strip().split("\n\n")[0]
        annotations = {
            pname: param.annotation
            for pname, param in sig.parameters.items()
            if pname not in ("self", "cls")
        }

        def handler(args: Dict[str, Any]) -> Any:
            # Filter unknown keys; coerce JSON-ish values to annotated types
            # (models often send timeout=30 as the string "30").
            kwargs = {}
            for k, v in args.items():
                if k not in properties:
                    continue
                kwargs[k] = _coerce_arg(v, annotations.get(k))
            return fn(**kwargs)

        self.add(tool_name, doc, properties, required, handler)

    # ------------------------------------------------------------------ core tools

    def _register_core(self) -> None:
        self.add(
            "Skill",
            "Load the full instructions for an installed skill. Call this as "
            "soon as a skill's description looks relevant; the returned text "
            "tells you how to complete the task and lists bundled files.",
            {"name": {"type": "string", "description": "Skill name to load."}},
            ["name"],
            lambda a: self.skills.render(a.get("name", "")),
        )
        self.add(
            "list_skills",
            "List every installed skill with its description.",
            {},
            [],
            lambda a: self.skills.catalog(),
        )
        self.add(
            "list_dir",
            "List files and directories at a path.",
            {"path": {"type": "string", "description": "Directory path."}},
            ["path"],
            self._list_dir,
        )
        self.add(
            "read_file",
            "Read a UTF-8 text file.",
            {
                "path": {"type": "string", "description": "File path."},
                "max_bytes": {
                    "type": "integer",
                    "description": "Cap on bytes read (default 8000).",
                },
            },
            ["path"],
            self._read_file,
        )
        self.add(
            "write_file",
            "Create or overwrite a text file under the workspace or skills tree. "
            "For a new skill write skills/<name>/SKILL.md then optional scripts/.",
            {
                "path": {"type": "string", "description": "File path."},
                "content": {"type": "string", "description": "Full file contents."},
            },
            ["path", "content"],
            self._write_file,
        )
        self.add(
            "run_script",
            "Execute a .py script bundled with a skill. Prints and a `result` "
            "variable are returned. Scripts get `args` and `tool(name, args)` "
            "in scope to call other tools.",
            {
                "path": {"type": "string", "description": "Absolute path to a .py file."},
                "args": {
                    "type": "object",
                    "description": "Optional args available to the script as `args`.",
                },
            },
            ["path"],
            self._run_script,
        )

    def _is_mutable(self, path: str) -> bool:
        abs_path = str(Path(path).expanduser().resolve())
        for prefix in self._mutable_prefixes:
            if abs_path == prefix.rstrip("/") or abs_path.startswith(prefix):
                return True
        return False

    def _list_dir(self, a: Dict[str, Any]) -> str:
        path = a.get("path") or self.workspace
        try:
            entries = sorted(os.listdir(path))
        except OSError as exc:
            return f"Error listing {path}: {exc}"
        lines = []
        for name in entries[:200]:
            full = os.path.join(path, name)
            kind = "dir" if os.path.isdir(full) else "file"
            lines.append(f"{kind}\t{name}")
        return "\n".join(lines) or "(empty)"

    def _read_file(self, a: Dict[str, Any]) -> str:
        path = a.get("path", "")
        max_bytes = int(a.get("max_bytes") or 8000)
        try:
            with open(path, "rb") as f:
                data = f.read(max_bytes + 1)
        except OSError as exc:
            return f"Error reading {path}: {exc}"
        text = data[:max_bytes].decode("utf-8", errors="replace")
        if len(data) > max_bytes:
            text += "\n...[truncated]"
        return text

    def _write_file(self, a: Dict[str, Any]) -> str:
        path = a.get("path", "")
        content = a.get("content", "")
        if not path:
            return "Error: path is required"
        abs_path = str(Path(path).expanduser().resolve())
        if not self._is_mutable(abs_path):
            return (
                f"Error: path '{path}' is not writable. "
                f"Write under workspace ({self.workspace}) or the skills tree."
            )

        # Guardrails for skill manifests (ESP lesson)
        if abs_path.endswith("SKILL.md") or abs_path.endswith("/SKILL.md"):
            dir_name = Path(abs_path).parent.name
            problem = _validate_manifest(content, dir_name)
            if problem:
                return f"Error: invalid SKILL.md — {problem}"

        if abs_path.endswith(".py") and "/skills/" in abs_path.replace("\\", "/"):
            # refuse loose .py directly under skills root
            parent = Path(abs_path).parent
            if parent.name == "skills" or parent.resolve() == Path(self.skills.root).resolve():
                return (
                    "Error: do not put loose .py under skills/. "
                    "Use skills/<name>/scripts/foo.py with a SKILL.md."
                )

        try:
            Path(abs_path).parent.mkdir(parents=True, exist_ok=True)
            with open(abs_path, "w", encoding="utf-8") as f:
                f.write(content if isinstance(content, str) else str(content))
        except OSError as exc:
            return f"Error writing {path}: {exc}"

        # Reindex skills when a manifest lands
        if abs_path.endswith("SKILL.md"):
            self.skills.discover()
            return f"Wrote {abs_path} and reindexed skills ({len(self.skills.skills)} loaded)."
        return f"Wrote {abs_path} ({len(content)} chars)."

    def _run_script(self, a: Dict[str, Any]) -> str:
        path = a.get("path", "")
        if not path.endswith(".py"):
            return "Error: run_script only executes .py files"
        abs_path = str(Path(path).expanduser().resolve())
        if not os.path.isfile(abs_path):
            return f"Error: file not found: {path}"

        args = a.get("args") or {}
        captured: List[str] = []

        def _print(*items: Any, **kwargs: Any) -> None:
            captured.append(" ".join(str(x) for x in items))

        def tool(name: str, tool_args: Optional[Dict[str, Any]] = None) -> str:
            return self.invoke(name, tool_args or {})

        glb: Dict[str, Any] = {
            "__name__": "__skill_script__",
            "args": args,
            "tool": tool,
            "print": _print,
            "result": None,
        }
        try:
            with open(abs_path, encoding="utf-8") as f:
                source = f.read()
            exec(compile(source, abs_path, "exec"), glb, glb)  # noqa: S102 — intentional skill scripts
        except Exception:
            return f"Script error:\n{traceback.format_exc()}"

        parts = []
        if captured:
            parts.append("\n".join(captured))
        if glb.get("result") is not None:
            parts.append(f"result={glb['result']!r}")
        return "\n".join(parts) if parts else "(script finished with no output)"


def _coerce_arg(value: Any, annotation: Any) -> Any:
    """Best-effort cast of model-provided JSON values to the tool's type hints."""
    if value is None or annotation is inspect.Parameter.empty or annotation is Any:
        return value

    origin = getattr(annotation, "__origin__", None)
    args = getattr(annotation, "__args__", ())

    # Optional[T] / Union[T, None]
    if origin is Union or str(origin) in ("typing.Union", "types.UnionType"):
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) == 1 and (type(None) in args or type(None) in getattr(annotation, "__args__", ())):
            if value is None or value == "":
                return None
            return _coerce_arg(value, non_none[0])

    # Unwrap typing.Optional written as Union
    if hasattr(annotation, "__args__") and type(None) in getattr(annotation, "__args__", ()):
        non_none = [a for a in annotation.__args__ if a is not type(None)]
        if len(non_none) == 1:
            if value is None or value == "":
                return None
            return _coerce_arg(value, non_none[0])

    try:
        if annotation is bool or annotation is bool:
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "yes", "y")
            return bool(value)
        if annotation is int:
            return int(float(value)) if not isinstance(value, bool) else int(value)
        if annotation is float:
            return float(value)
        if annotation is str:
            return value if isinstance(value, str) else str(value)
    except (TypeError, ValueError):
        return value
    return value


def _validate_manifest(content: str, dir_name: str) -> Optional[str]:
    if not content.lstrip().startswith("---"):
        return "it has no YAML frontmatter block"
    meta, body = parse_frontmatter(content)
    if not meta.get("name"):
        return "the frontmatter has no 'name' field"
    if not meta.get("description"):
        return "the frontmatter has no 'description' field"
    if meta["name"] != dir_name:
        return (
            f"name '{meta['name']}' does not match its directory '{dir_name}'; "
            "they must be identical"
        )
    if not body.strip():
        return "it has frontmatter but no instructions after it"
    return None
