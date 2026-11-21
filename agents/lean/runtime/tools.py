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

from declarative_agent_sdk.core.agent_logging import get_logger
from declarative_agent_sdk.agents.lean.runtime.skills import SkillRegistry, parse_frontmatter
from declarative_agent_sdk.tools.tool_registry import ToolRegistry

logger = get_logger(__name__)

# Tools that never need human approval (meta / discovery).
#
# The two board tools are here on purpose. `list_agents` only reads a roster.
# `send_to_agent` posts a message to the board — which the agent can do anyway
# by writing @name in its reply, with no tool and no approval — so a click in
# front of it buys nothing and makes every hand-off tedious. What bounds a
# cascade is the channel's Stop and max_relay_hops, not an approval prompt.
AUTO_APPROVE_TOOLS: Set[str] = {
    "Skill",
    "list_skills",
    "list_dir",
    "read_file",
    "send_to_agent",
    "list_agents",
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
        # Sibling attachments/ dir (common for Discord file drops)
        self.attachments = str(
            (Path(self.workspace).parent / "attachments").resolve()
        )
        os.makedirs(self.attachments, exist_ok=True)
        skills_root = str(Path(skills.root).resolve())
        self._mutable_prefixes = mutable_prefixes or [
            skills_root.rstrip("/") + "/",
            self.workspace.rstrip("/") + "/",
            self.attachments.rstrip("/") + "/",
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
            "Create or overwrite a text file under workspace/, attachments/, or "
            "the skills tree. For a new skill write skills/<name>/SKILL.md. "
            "When saving a file for the user to see in Discord, write under "
            "attachments/ and include [[attach:attachments/filename.ext]] in "
            "your final answer so the bot uploads it. Keep each call to a few "
            "KB: write a long document in parts (the first with write_file, "
            "the rest with append=true). To change an existing file use "
            "edit_file; to save a file another agent shared use write_link — "
            "never retype either.",
            {
                "path": {"type": "string", "description": "File path."},
                "content": {
                    "type": "string",
                    "description": "File contents, or the next part when append is true.",
                },
                "append": {
                    "type": "boolean",
                    "description": "Add content to the end of the file instead of "
                                   "replacing it (default false).",
                },
            },
            ["path", "content"],
            self._write_file,
        )
        self.add(
            "edit_file",
            "Change part of an existing text file: replace old_text with "
            "new_text. Use this to revise a draft instead of rewriting the "
            "whole file — one call per change, each quoting just enough of the "
            "original to be unique. read_file first to quote it exactly.",
            {
                "path": {"type": "string", "description": "File path."},
                "old_text": {
                    "type": "string",
                    "description": "Exact text to replace, as it appears in the file.",
                },
                "new_text": {"type": "string", "description": "Replacement text."},
                "replace_all": {
                    "type": "boolean",
                    "description": "Replace every occurrence (default false: "
                                   "old_text must occur exactly once).",
                },
            },
            ["path", "old_text", "new_text"],
            self._edit_file,
        )
        self.add(
            "write_link",
            "Save a file from another agent's link (http://<agent>/files/<path>) "
            "straight to a file under workspace/. "
            "Use this instead of read_shared + write_file whenever you want to "
            "keep, edit or pass on a shared file: it is copied directly, so you "
            "never retype it. Returns a short receipt, not the content.",
            {
                "url": {"type": "string", "description": "The link exactly as given."},
                "path": {
                    "type": "string",
                    "description": "Where to save it, e.g. workspace/review.md.",
                },
                "overwrite": {
                    "type": "boolean",
                    "description": "Replace an existing file (default false).",
                },
            },
            ["url", "path"],
            self._write_link,
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
        self._register_shared_core()

    def _register_shared_core(self) -> None:
        """
        Bridge in the SDK-wide core tools (web_request, exec_command,
        send_to_agent, share_file, read_shared; list_agents on a board).

        Core rather than opt-in, so no agent config has to list them. The lean
        read_file / write_file registered above are sandboxed to the workspace
        and win: add_callable skips a name that is already taken.
        """
        ToolRegistry.register_built_in_tools()
        for name in ToolRegistry.core_tool_names():
            try:
                self.add_callable(ToolRegistry.get(name))
            except (ValueError, Exception) as exc:   # noqa: BLE001 - never fatal
                logger.warning(f"could not register core tool '{name}': {exc}")

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

    def _writable(self, path: str) -> "tuple[Optional[str], str]":
        """(error, absolute path) for a path a tool wants to write."""
        if not path:
            return "Error: path is required", ""
        abs_path = str(Path(path).expanduser().resolve())
        if not self._is_mutable(abs_path):
            return (
                f"Error: path '{path}' is not writable. "
                f"Write under workspace ({self.workspace}) or the skills tree."
            ), abs_path
        return None, abs_path

    def _write_file(self, a: Dict[str, Any]) -> str:
        path = a.get("path", "")
        content = a.get("content", "")
        if not isinstance(content, str):
            content = str(content)
        append = _truthy(a.get("append"))
        error, abs_path = self._writable(path)
        if error:
            return error

        # Guardrails for skill manifests (ESP lesson). An append is checked as
        # the file it will produce, not as the fragment being added.
        if abs_path.endswith("SKILL.md") or abs_path.endswith("/SKILL.md"):
            dir_name = Path(abs_path).parent.name
            whole = content
            if append and os.path.isfile(abs_path):
                with open(abs_path, encoding="utf-8", errors="replace") as f:
                    whole = f.read() + content
            problem = _validate_manifest(whole, dir_name)
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
            with open(abs_path, "a" if append else "w", encoding="utf-8") as f:
                f.write(content)
        except OSError as exc:
            return f"Error writing {path}: {exc}"

        # Reindex skills when a manifest lands
        if abs_path.endswith("SKILL.md"):
            self.skills.discover()
            return f"Wrote {abs_path} and reindexed skills ({len(self.skills.skills)} loaded)."
        if append:
            return (f"Appended {len(content)} chars to {abs_path} "
                    f"(now {os.path.getsize(abs_path):,} bytes).{_link_note(abs_path)}")
        return f"Wrote {abs_path} ({len(content)} chars).{_link_note(abs_path)}"

    def _edit_file(self, a: Dict[str, Any]) -> str:
        path = a.get("path", "")
        old = a.get("old_text", "")
        new = a.get("new_text", "")
        old = old if isinstance(old, str) else str(old)
        new = new if isinstance(new, str) else str(new)
        replace_all = _truthy(a.get("replace_all"))
        error, abs_path = self._writable(path)
        if error:
            return error
        if not old:
            return "Error: old_text is required (to add to the end, use write_file with append=true)"
        try:
            with open(abs_path, encoding="utf-8") as f:
                text = f.read()
        except FileNotFoundError:
            return f"Error: {path} does not exist — create it with write_file first"
        except (OSError, UnicodeDecodeError) as exc:
            return f"Error reading {path}: {exc}"

        count = text.count(old)
        if count == 0:
            return (f"Error: old_text was not found in {path}. It must match the "
                    "file exactly, including spaces and line breaks — read_file "
                    "it and copy the passage.")
        if count > 1 and not replace_all:
            return (f"Error: old_text occurs {count} times in {path}. Quote more "
                    "of the surrounding text so it is unique, or pass "
                    "replace_all=true to change every one.")
        updated = text.replace(old, new) if replace_all else text.replace(old, new, 1)

        if abs_path.endswith("SKILL.md"):
            problem = _validate_manifest(updated, Path(abs_path).parent.name)
            if problem:
                return f"Error: the edit would leave an invalid SKILL.md — {problem}"
        try:
            with open(abs_path, "w", encoding="utf-8") as f:
                f.write(updated)
        except OSError as exc:
            return f"Error writing {path}: {exc}"
        if abs_path.endswith("SKILL.md"):
            self.skills.discover()
        done = count if replace_all else 1
        return (f"Edited {abs_path}: replaced {done} occurrence{'s' if done != 1 else ''} "
                f"({len(text):,} → {len(updated):,} chars).{_link_note(abs_path)}")

    def _write_link(self, a: Dict[str, Any]) -> str:
        from declarative_agent_sdk.tools.builtin import _links

        path = a.get("path", "")
        error, abs_path = self._writable(path)
        if error:
            return error
        if os.path.isdir(abs_path):
            return f"Error: {path} is a directory; give a file name"
        if os.path.exists(abs_path) and not _truthy(a.get("overwrite")):
            return f"Error: {path} already exists; pass overwrite=true to replace it"
        if abs_path.endswith("SKILL.md"):
            return "Error: write_link does not write SKILL.md; use write_file so it is validated"
        try:
            data, kind = _links.download(str(a.get("url") or ""))
        except _links.LinkError as exc:
            return f"Error: {exc}"
        try:
            Path(abs_path).parent.mkdir(parents=True, exist_ok=True)
            with open(abs_path, "wb") as f:
                f.write(data)
        except OSError as exc:
            return f"Error writing {path}: {exc}"
        return _links.receipt(
            abs_path, data, kind,
            hint="The file is on disk: read_file it only if you need its content, "
                 "and change it with edit_file rather than rewriting it."
                 + _link_note(abs_path))

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


def _link_note(abs_path: str) -> str:
    """The file's link, when this agent serves its workspace and it is in it."""
    from declarative_agent_sdk.core import shares

    link = shares.link_for(abs_path)
    return f" Link for other agents: {link}" if link else ""


def _truthy(value: Any) -> bool:
    """A boolean argument as models send it: true, "true", 1, "yes"…"""
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "y")
    return bool(value)


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
