"""Run any BaseAgent as a Discord bot.

Mirrors AIAgentServer (which exposes an agent over the A2A protocol), but the
transport here is Discord: the bot listens for mentions / DMs / prefixed
commands, feeds the text to the agent and posts the agent's response back to
the originating channel.

Example:
    from declarative_agent_sdk import AgentFactory, DiscordAgentServer

    agent = AgentFactory.from_yaml_file('configs/agent.yaml')
    server = DiscordAgentServer(agent, token=os.environ["DISCORD_BOT_TOKEN"])
    server.run()

`discord.py` is an optional dependency:  pip install "declarative-agent-sdk[discord]"
The bot also needs the MESSAGE CONTENT privileged intent enabled in the
Discord developer portal, otherwise message text arrives empty.
"""

import asyncio
import json
import mimetypes
import os
import re
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import unquote, urlparse

from declarative_agent_sdk.core.agent_logging import get_logger
from declarative_agent_sdk.core.base_agent import BaseAgent
from declarative_agent_sdk.core.utils import remove_think_content

logger = get_logger(__name__)

# Discord rejects messages longer than this.
DISCORD_MESSAGE_LIMIT = 2000
# Non-boosted servers: 25 MiB is the usual bot upload cap; stay under it.
DISCORD_FILE_MAX_BYTES = 24 * 1024 * 1024
DISCORD_MAX_FILES_PER_MESSAGE = 10

APPROVE_EMOJI = "\N{WHITE HEAVY CHECK MARK}"
DENY_EMOJI = "\N{CROSS MARK}"

# Typed replies accepted instead of a reaction.
APPROVE_WORDS = {"y", "yes", "ok", "okay", "approve", "approved", "go", "do it"}
DENY_WORDS = {"n", "no", "nope", "deny", "denied", "cancel", "stop"}

_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")


# ---------------------------------------------------------------------------
# Event helpers — tolerate both ADK Events and LangChainEvents
# ---------------------------------------------------------------------------

def _event_text(event: Any) -> str:
    """Concatenate the text of every part carried by an agent event."""
    content = getattr(event, "content", None)
    parts = getattr(content, "parts", None) or []
    texts = [p.text for p in parts if getattr(p, "text", None)]
    return "\n".join(texts).strip()


def _is_final(event: Any) -> bool:
    """True when the event carries the agent's final answer."""
    is_final_response = getattr(event, "is_final_response", None)
    if not callable(is_final_response):
        return False
    return bool(is_final_response()) and not getattr(event, "long_running_tool_ids", None)


def _confirmation_request(event: Any) -> Optional[Dict[str, Any]]:
    """
    Return {"id", "name", "args"} when the event is a tool-confirmation request,
    otherwise None.  Shape matches what AIAgentExecutor extracts for A2A.
    """
    actions = getattr(event, "actions", None)
    requested = getattr(actions, "requested_tool_confirmations", None)
    if not requested and not getattr(event, "long_running_tool_ids", None):
        return None

    content = getattr(event, "content", None)
    parts = getattr(content, "parts", None) or []
    function_call = getattr(parts[0], "function_call", None) if parts else None
    if function_call is None:
        return None

    args = getattr(function_call, "args", None) or {}
    original = args.get("originalFunctionCall", {}) if isinstance(args, dict) else {}
    return {
        "id": getattr(function_call, "id", "") or "",
        "name": original.get("name", getattr(function_call, "name", "unknown")),
        "args": original.get("args", {}),
    }


# ---------------------------------------------------------------------------
# Message formatting
# ---------------------------------------------------------------------------

_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$")
_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
_HTML_TAG_RE = re.compile(r"</?[a-zA-Z][^>]*>")
_HR_RE = re.compile(r"^(?:\s*[-*_]){3,}\s*$", re.MULTILINE)
# Bare image URLs (optional query string).
_IMAGE_URL_RE = re.compile(
    r"(https?://[^\s<>`\"')\]]+\.(?:png|jpe?g|gif|webp|bmp)(?:\?[^\s<>`\"')\]]*)?)",
    re.IGNORECASE,
)
# Any path-like token ending in an image extension (quoted or bare).
# Covers attachments/foo.jpg, workspace/x.png, /abs/path.jpg, `file.png`.
_IMAGE_PATH_TOKEN_RE = re.compile(
    r"[`\"']?("
    r"(?:/(?!/)[^\s`\"'<>]+\.(?:png|jpe?g|gif|webp|bmp))"
    r"|(?:\./[^\s`\"'<>]+\.(?:png|jpe?g|gif|webp|bmp))"
    r"|(?:[A-Za-z]:\\[^\s`\"'<>]+\.(?:png|jpe?g|gif|webp|bmp))"
    r"|(?:(?:attachments|workspace|skills|tmp|photos|output|outputs|images|img)"
    r"/[^\s`\"'<>]+\.(?:png|jpe?g|gif|webp|bmp))"
    r"|(?:[A-Za-z0-9_.\-]+/(?:[A-Za-z0-9_./\-])+\.(?:png|jpe?g|gif|webp|bmp))"
    r")[`\"']?",
    re.IGNORECASE,
)


def _is_table_separator(line: str) -> bool:
    """True for GFM header-separator rows like ``| --- | :---: |``."""
    stripped = line.strip()
    if not stripped or not stripped.replace("|", "").replace("-", "").replace(":", "").replace(" ", ""):
        # only pipes/dashes/colons/spaces — but need at least one ---
        return bool(_TABLE_SEP_RE.match(line)) or (
            "|" in stripped and set(stripped) <= set("|-: ") and "---" in stripped.replace(" ", "")
        )
    return bool(_TABLE_SEP_RE.match(line))


def _parse_table_row(line: str) -> List[str]:
    cells = [c.strip() for c in line.strip().strip("|").split("|")]
    return cells


def _format_table_block(rows: List[List[str]]) -> str:
    """Render a GFM table as a monospace block Discord will display cleanly."""
    if not rows:
        return ""
    col_count = max(len(r) for r in rows)
    normalized = [r + [""] * (col_count - len(r)) for r in rows]
    widths = [
        max(len(normalized[r][c]) for r in range(len(normalized)))
        for c in range(col_count)
    ]

    def fmt(row: List[str]) -> str:
        return " | ".join(cell.ljust(widths[i]) for i, cell in enumerate(row))

    lines = [fmt(normalized[0])]
    lines.append("-+-".join("-" * w for w in widths))
    for row in normalized[1:]:
        lines.append(fmt(row))
    return "```\n" + "\n".join(lines) + "\n```"


def _looks_like_image_ref(ref: str) -> bool:
    lower = ref.split("?", 1)[0].lower()
    return any(lower.endswith(ext) for ext in _IMAGE_EXTS)


def _is_real_image_bytes(data: bytes) -> bool:
    """True when *data* looks like PNG/JPEG/GIF/WEBP (not HTML saved as .jpg)."""
    if not data or len(data) < 12:
        return False
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return True
    if data[:2] == b"\xff\xd8":
        return True
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return True
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return True
    if data[:2] == b"BM":
        return True
    # HTML/XML error pages often get saved as .jpg by tools
    head = data[:200].lstrip().lower()
    if head.startswith((b"<!doctype", b"<html", b"<?xml", b"<head")):
        return False
    return False


def _is_real_image_file(path: str) -> bool:
    try:
        with open(path, "rb") as f:
            return _is_real_image_bytes(f.read(64))
    except OSError:
        return False


def _search_bases() -> List[Path]:
    """Directories to search when resolving relative image paths."""
    cwd = Path.cwd()
    bases = [
        cwd,
        cwd / "attachments",
        cwd / "workspace",
        cwd / "examples" / "discord_bot",
        cwd / "examples" / "discord_bot" / "attachments",
        cwd / "examples" / "discord_bot" / "workspace",
    ]
    # Deduplicate while preserving order
    out: List[Path] = []
    seen: set = set()
    for b in bases:
        try:
            key = str(b.resolve())
        except OSError:
            key = str(b)
        if key not in seen:
            seen.add(key)
            out.append(b)
    return out


def _image_watch_dirs() -> List[Path]:
    """Folders where agents commonly write images during a turn."""
    dirs: List[Path] = []
    for base in _search_bases():
        for name in ("attachments", "workspace", "photos", "output", "outputs", "images"):
            dirs.append(base / name if base.name != name else base)
        # also watch base itself if it is one of those names
        if base.name in ("attachments", "workspace", "photos", "images"):
            dirs.append(base)
    out: List[Path] = []
    seen: set = set()
    for d in dirs:
        try:
            key = str(d.resolve()) if d.exists() else str(d)
        except OSError:
            key = str(d)
        if key not in seen:
            seen.add(key)
            out.append(d)
    return out


def snapshot_image_files(dirs: Optional[List[Path]] = None) -> Dict[str, float]:
    """Map absolute image path → mtime for dirs (shallow + one level)."""
    snap: Dict[str, float] = {}
    for d in dirs or _image_watch_dirs():
        if not d.is_dir():
            continue
        try:
            entries = list(d.iterdir())
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_file() and _looks_like_image_ref(entry.name):
                    snap[str(entry.resolve())] = entry.stat().st_mtime
                elif entry.is_dir():
                    for child in entry.iterdir():
                        if child.is_file() and _looks_like_image_ref(child.name):
                            snap[str(child.resolve())] = child.stat().st_mtime
            except OSError:
                continue
    return snap


def new_images_since(before: Dict[str, float], after: Optional[Dict[str, float]] = None) -> List[str]:
    """Return paths that are new or updated after *before* snapshot."""
    after = after if after is not None else snapshot_image_files()
    out: List[str] = []
    for path, mtime in after.items():
        prev = before.get(path)
        if prev is None or mtime > prev + 0.01:
            if _is_real_image_file(path):
                out.append(path)
    return out


def extract_image_refs(text: str) -> Tuple[str, List[str]]:
    """
    Find image markdown / URLs / local paths in *text*.

    Returns ``(cleaned_text, refs)`` where refs are unique sources in order.
    Markdown image syntax is removed from the text (files will be attached);
    bare URLs/paths are left in place so the message still documents the source.
    """
    if not text:
        return text, []

    refs: List[str] = []
    seen: set = set()

    def _add(ref: str) -> None:
        ref = (ref or "").strip().strip("<>\"'`")
        if not ref or not _looks_like_image_ref(ref):
            return
        if ref.startswith("//"):
            return
        if ref not in seen:
            seen.add(ref)
            refs.append(ref)

    # 1) ![alt](src) — strip from text; attach instead
    def _md_sub(match: re.Match) -> str:
        _add(match.group(2))
        alt = (match.group(1) or "").strip()
        return alt

    cleaned = _IMAGE_RE.sub(_md_sub, text)

    # 2) bare image URLs
    for match in _IMAGE_URL_RE.finditer(cleaned):
        _add(match.group(1))

    # 3) path-like tokens (attachments/foo.jpg, workspace/x.png, /abs/…)
    for match in _IMAGE_PATH_TOKEN_RE.finditer(cleaned):
        _add(match.group(1))

    return cleaned, refs


def resolve_local_image_path(ref: str) -> Optional[Path]:
    """Resolve a local image ref against cwd and common project dirs."""
    raw = os.path.expanduser((ref or "").strip())
    if not raw or raw.startswith(("http://", "https://")):
        return None

    candidates: List[Path] = []
    p = Path(raw)
    if p.is_absolute():
        candidates.append(p)
    else:
        for base in _search_bases():
            candidates.append(base / raw)
            # also try basename under attachments/
            candidates.append(base / "attachments" / Path(raw).name)
            candidates.append(base / Path(raw).name)

    for cand in candidates:
        try:
            resolved = cand.resolve()
        except OSError:
            continue
        if resolved.is_file():
            return resolved
    return None


def resolve_image_files(
    refs: List[str],
    *,
    max_files: int = DISCORD_MAX_FILES_PER_MESSAGE,
    max_bytes: int = DISCORD_FILE_MAX_BYTES,
) -> Tuple[List[str], List[str]]:
    """
    Resolve refs to local filesystem paths ready for ``discord.File``.

    Downloads http(s) images to temp files. Skips HTML/error pages masquerading
    as images. Returns ``(local_paths, notes)``.
    """
    local_paths: List[str] = []
    notes: List[str] = []
    temp_dir = tempfile.mkdtemp(prefix="discord_imgs_")
    seen_resolved: set = set()

    for ref in refs:
        if len(local_paths) >= max_files:
            notes.append(
                f"(skipped remaining images — Discord max {max_files} per message)"
            )
            break

        if ref.startswith(("http://", "https://")):
            path = _download_image(ref, temp_dir, max_bytes=max_bytes)
            if path and path not in seen_resolved:
                if not _is_real_image_file(path):
                    notes.append(
                        f"Downloaded file is not a valid image (got HTML/error?): {ref}"
                    )
                    try:
                        os.remove(path)
                    except OSError:
                        pass
                    continue
                seen_resolved.add(path)
                local_paths.append(path)
            elif not path:
                notes.append(f"Could not fetch image: {ref}")
            continue

        candidate = resolve_local_image_path(ref)
        if candidate is None:
            notes.append(f"Image not found: {ref}")
            continue
        key = str(candidate)
        if key in seen_resolved:
            continue
        try:
            size = candidate.stat().st_size
        except OSError as exc:
            notes.append(f"Cannot read {ref}: {exc}")
            continue
        if size == 0:
            notes.append(f"Empty image file: {ref}")
            continue
        if size > max_bytes:
            notes.append(
                f"Image too large for Discord upload ({size // (1024 * 1024)} MiB, "
                f"limit ~{max_bytes // (1024 * 1024)} MiB): {ref}"
            )
            continue
        if not _is_real_image_file(key):
            notes.append(
                f"File is not a valid image (HTML/error page saved as image?): {ref}"
            )
            continue
        seen_resolved.add(key)
        local_paths.append(key)

    return local_paths, notes


def _download_image(url: str, dest_dir: str, *, max_bytes: int) -> Optional[str]:
    """Download *url* into *dest_dir*; return local path or None."""
    try:
        parsed = urlparse(url)
        name = Path(unquote(parsed.path)).name or "image"
        if not any(name.lower().endswith(ext) for ext in _IMAGE_EXTS):
            name = name + ".png"
        name = re.sub(r"[^\w.\-]+", "_", name)[:120]
        dest = os.path.join(dest_dir, name)
        base, ext = os.path.splitext(dest)
        n = 0
        while os.path.exists(dest):
            n += 1
            dest = f"{base}_{n}{ext}"

        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (compatible; declarative-agent-sdk-discord/1.0)"
                ),
                "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
            },
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=45) as resp:
            data = resp.read(max_bytes + 1)
            content_type = (resp.headers.get("Content-Type") or "").lower()
        if len(data) > max_bytes:
            logger.warning(f"Downloaded image exceeds size cap: {url}")
            return None
        if not data:
            return None
        if "text/html" in content_type or not _is_real_image_bytes(data):
            logger.warning(
                f"URL did not return image bytes (content-type={content_type!r}): {url}"
            )
            return None
        if not any(dest.lower().endswith(ext) for ext in _IMAGE_EXTS):
            guess = mimetypes.guess_extension(content_type.split(";")[0].strip())
            if guess and guess.lower() in _IMAGE_EXTS:
                dest = dest + guess
        with open(dest, "wb") as f:
            f.write(data)
        return dest
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
        logger.warning(f"Failed to download image {url}: {exc}")
        return None
    except Exception as exc:
        logger.warning(f"Unexpected error downloading {url}: {exc}")
        return None


def to_discord_markdown(text: str) -> str:
    """
    Adapt common GitHub-flavored Markdown so Discord renders it cleanly.

    Discord already supports **bold**, *italic*, ``code``, fences, lists,
    quotes, spoilers, and (on modern clients) #/##/### headers.  What it does
    **not** support is the main source of "broken" agent replies:

    * pipe tables
    * HTML tags
    * thematic breaks (``---``)

    Markdown images are handled separately in ``_send_reply`` (uploaded as
    attachments). Remaining image markdown is left alone here.
    """
    if not text:
        return text

    # Strip simple HTML tags agents sometimes emit
    text = _HTML_TAG_RE.sub("", text)

    # Horizontal rules → a light separator Discord will show as plain text
    text = _HR_RE.sub("────────", text)

    # Convert contiguous GFM tables into monospace blocks
    lines = text.splitlines()
    out: List[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if _TABLE_ROW_RE.match(line):
            table_lines = [line]
            j = i + 1
            while j < len(lines) and (
                _TABLE_ROW_RE.match(lines[j]) or _is_table_separator(lines[j])
            ):
                table_lines.append(lines[j])
                j += 1
            # Need a real table: header + separator + ≥0 body rows
            body_rows: List[List[str]] = []
            header: Optional[List[str]] = None
            saw_sep = False
            for tl in table_lines:
                if _is_table_separator(tl):
                    saw_sep = True
                    continue
                cells = _parse_table_row(tl)
                if header is None:
                    header = cells
                else:
                    body_rows.append(cells)
            if header is not None and (saw_sep or body_rows):
                out.append(_format_table_block([header] + body_rows))
                i = j
                continue
            # Not a table after all — emit lines as-is
            out.extend(table_lines)
            i = j
            continue
        out.append(line)
        i += 1

    # Collapse runs of blank lines left by stripped HTML / tables
    cleaned: List[str] = []
    blank = False
    for line in out:
        if line.strip() == "":
            if not blank:
                cleaned.append("")
            blank = True
        else:
            cleaned.append(line)
            blank = False
    return "\n".join(cleaned).strip()


def split_message(text: str, limit: int = DISCORD_MESSAGE_LIMIT) -> List[str]:
    """
    Split text into Discord-sized chunks, preferring line boundaries and
    falling back to word boundaries before cutting a word in half.

    Avoids splitting in the middle of a fenced code block when a line break
    outside the fence is available, so markdown fences stay balanced.
    """
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    chunks: List[str] = []
    remaining = text

    while len(remaining) > limit:
        window = remaining[:limit]
        # Prefer a newline that keeps ``` fence parity even inside the chunk
        last_safe_newline = -1
        search_from = 0
        while True:
            nl = window.find("\n", search_from)
            if nl < 0:
                break
            candidate = window[:nl]
            fence_lines = sum(
                1 for ln in candidate.splitlines() if ln.strip().startswith("```")
            )
            if fence_lines % 2 == 0:
                last_safe_newline = nl
            search_from = nl + 1

        if last_safe_newline > 0:
            split_at = last_safe_newline
        else:
            split_at = window.rfind("\n")
            if split_at <= 0:
                split_at = window.rfind(" ")
            if split_at <= 0:
                split_at = limit

        chunk = remaining[:split_at].rstrip()
        # If we still leave an open fence, close it and reopen on the next chunk
        fence_lines = sum(1 for ln in chunk.splitlines() if ln.strip().startswith("```"))
        reopen = ""
        if fence_lines % 2 == 1:
            chunk = chunk + "\n```"
            reopen = "```\n"

        if chunk:
            chunks.append(chunk)
        remaining = reopen + remaining[split_at:].lstrip()

    if remaining:
        chunks.append(remaining)
    return chunks


def _decision_from_text(text: str) -> Optional[bool]:
    """Map a typed reply to approve / deny, or None when it is neither."""
    normalized = (text or "").strip().lower().rstrip("!.")
    if normalized in APPROVE_WORDS:
        return True
    if normalized in DENY_WORDS:
        return False
    return None


def _format_tool_prompt(request: Dict[str, Any]) -> str:
    """Human-readable approval prompt for a pending tool call."""
    try:
        args = json.dumps(request.get("args", {}), indent=2, default=str)
    except (TypeError, ValueError):
        args = str(request.get("args", {}))
    if len(args) > 1000:
        args = args[:1000] + "\n… (truncated)"
    return (
        f"🔧 The agent wants to run tool **{request.get('name', 'unknown')}**:\n"
        f"```json\n{args}\n```\n"
        f"React with {APPROVE_EMOJI} to approve or {DENY_EMOJI} to deny "
        f"— or just reply `yes` / `no`."
    )


# ---------------------------------------------------------------------------
# DiscordAgentServer
# ---------------------------------------------------------------------------

class DiscordAgentServer:
    """Serve a BaseAgent (AIAgent, LangChainAIAgent, …) as a Discord bot."""

    def __init__(
        self,
        agent: BaseAgent,
        token: Optional[str] = None,
        *,
        respond_to_mentions: bool = True,
        respond_to_dms: bool = True,
        respond_to_all_messages: bool = False,
        command_prefix: Optional[str] = None,
        allowed_channels: Optional[Iterable[Any]] = None,
        session_scope: str = "channel",
        show_working_updates: bool = True,
        tool_confirmation_timeout: float = 120.0,
        activity_status: Optional[str] = None,
        format_markdown: bool = True,
        reply_as_embed: bool = False,
    ) -> None:
        """
        Args:
            agent: Any BaseAgent implementation.
            token: Bot token.  Falls back to the DISCORD_BOT_TOKEN env var.
            respond_to_mentions: Reply when the bot is @-mentioned in a guild.
            respond_to_dms: Reply to direct messages.
            respond_to_all_messages: Reply to every message in allowed channels,
                without needing a mention or prefix.
            command_prefix: Optional prefix (e.g. "!ask ") that also triggers
                the agent; the prefix is stripped before the query is sent.
            allowed_channels: Channel IDs the bot is allowed to answer in.
                None means all channels.  Does not restrict DMs.
            session_scope: Conversation memory granularity — "channel"
                (default), "user", or "global".
            show_working_updates: Post/edit a status message while the agent
                works (tool calls, intermediate steps).
            tool_confirmation_timeout: Seconds to wait for a reaction when the
                agent asks to confirm a tool call.  A timeout denies the call.
            activity_status: Text shown as the bot's "Playing …" status.
            format_markdown: Rewrite agent GFM (tables, HTML, images, ``---``)
                into forms Discord renders cleanly.  Discord-native markdown
                (bold, lists, fences, headers) is left alone.  Default True.
            reply_as_embed: Post final answers as a Discord embed (description
                field).  Often looks cleaner; limited to ~4096 chars per embed
                chunk.  Default False (plain messages, 2000-char chunks).

        Raises:
            ValueError: If no token is supplied and DISCORD_BOT_TOKEN is unset,
                or if session_scope is not a known value.
        """
        if session_scope not in ("channel", "user", "global"):
            raise ValueError(
                f"Unknown session_scope '{session_scope}' — expected 'channel', 'user' or 'global'"
            )

        self._agent = agent
        self._token = token or os.environ.get("DISCORD_BOT_TOKEN")
        if not self._token:
            raise ValueError(
                "A Discord bot token is required — pass token=... or set DISCORD_BOT_TOKEN"
            )

        self._respond_to_mentions = respond_to_mentions
        self._respond_to_dms = respond_to_dms
        self._respond_to_all_messages = respond_to_all_messages
        self._command_prefix = command_prefix
        self._allowed_channels = {str(c) for c in allowed_channels} if allowed_channels else None
        self._session_scope = session_scope
        self._show_working_updates = show_working_updates
        self._tool_confirmation_timeout = tool_confirmation_timeout
        self._activity_status = activity_status
        self._format_markdown = format_markdown
        self._reply_as_embed = reply_as_embed

        self._client: Any = None
        self._session_locks: Dict[str, asyncio.Lock] = {}
        # channel id → asker id, while that channel waits on a tool approval
        self._pending_decisions: Dict[Any, Any] = {}

    # ------------------------------------------------------------------
    # Client construction
    # ------------------------------------------------------------------

    @staticmethod
    def _import_discord() -> Any:
        try:
            import discord  # type: ignore
        except ImportError as exc:  # pragma: no cover - depends on environment
            raise ImportError(
                "discord.py is not installed. Install it with "
                "'pip install \"declarative-agent-sdk[discord]\"' to run the Discord bot."
            ) from exc
        return discord

    def build_client(self) -> Any:
        """Create the discord.Client and wire up its event handlers."""
        discord = self._import_discord()

        intents = discord.Intents.default()
        intents.message_content = True
        client = discord.Client(intents=intents)

        @client.event
        async def on_ready() -> None:
            logger.info(f"Discord bot connected as {client.user} — serving agent '{self._agent.name}'")
            if self._activity_status:
                try:
                    await client.change_presence(activity=discord.Game(name=self._activity_status))
                except Exception as exc:
                    logger.warning(f"Failed to set Discord presence: {exc}")

        @client.event
        async def on_message(message: Any) -> None:
            try:
                await self.handle_message(message)
            except Exception as exc:
                logger.exception(f"Unhandled error while processing Discord message: {exc}")

        self._client = client
        return client

    # ------------------------------------------------------------------
    # Message routing
    # ------------------------------------------------------------------

    def _bot_user_id(self) -> Optional[str]:
        user = getattr(self._client, "user", None)
        return str(user.id) if user is not None and getattr(user, "id", None) is not None else None

    def _is_allowed_channel(self, message: Any) -> bool:
        if self._allowed_channels is None:
            return True
        channel_id = getattr(getattr(message, "channel", None), "id", None)
        return str(channel_id) in self._allowed_channels

    def extract_query(self, message: Any) -> Optional[str]:
        """
        Return the query the agent should answer, or None when the bot should
        stay silent for this message.
        """
        content = (getattr(message, "content", "") or "").strip()
        is_dm = getattr(message, "guild", None) is None

        if is_dm:
            if not self._respond_to_dms:
                return None
            return self._strip_mentions(content) or None

        if not self._is_allowed_channel(message):
            return None

        bot_id = self._bot_user_id()
        mentioned = bot_id is not None and any(
            str(getattr(user, "id", "")) == bot_id for user in getattr(message, "mentions", []) or []
        )
        if self._respond_to_mentions and mentioned:
            return self._strip_mentions(content) or None

        if self._command_prefix and content.startswith(self._command_prefix):
            return content[len(self._command_prefix):].strip() or None

        if self._respond_to_all_messages:
            return self._strip_mentions(content) or None

        return None

    def _strip_mentions(self, content: str) -> str:
        """Remove the bot's own mention from the message text."""
        bot_id = self._bot_user_id()
        if bot_id:
            content = re.sub(rf"<@!?{bot_id}>", " ", content)
        return content.strip()

    def session_id(self, message: Any) -> str:
        """Map a Discord message to the agent session it belongs to."""
        if self._session_scope == "user":
            return f"discord-user-{getattr(getattr(message, 'author', None), 'id', 'unknown')}"
        if self._session_scope == "global":
            return f"discord-{self._agent.name}"
        return f"discord-channel-{getattr(getattr(message, 'channel', None), 'id', 'unknown')}"

    def _lock_for(self, session_id: str) -> asyncio.Lock:
        lock = self._session_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._session_locks[session_id] = lock
        return lock

    def _answers_pending_confirmation(self, message: Any) -> bool:
        """True when this message is the asker answering a pending tool prompt."""
        channel_id = getattr(getattr(message, "channel", None), "id", None)
        asker_id = self._pending_decisions.get(channel_id)
        if asker_id is None:
            return False
        if getattr(getattr(message, "author", None), "id", None) != asker_id:
            return False
        return _decision_from_text(getattr(message, "content", "")) is not None

    async def handle_message(self, message: Any) -> None:
        """Entry point for every message the bot sees."""
        author = getattr(message, "author", None)
        if getattr(author, "bot", False):
            return
        bot_id = self._bot_user_id()
        if bot_id is not None and str(getattr(author, "id", "")) == bot_id:
            return

        if self._answers_pending_confirmation(message):
            # "yes" / "no" belongs to the tool prompt this channel is waiting
            # on, not to a new question.
            return

        query = self.extract_query(message)
        if not query:
            return

        session_id = self.session_id(message)
        logger.info(f"Discord query in session '{session_id}': {query}")

        async with self._lock_for(session_id):
            try:
                await self._run_agent_turn(message, query, session_id)
            except Exception as exc:
                logger.exception(f"Agent run failed: {exc}")
                await self._send(message.channel, f"⚠️ Agent error: {exc}")

    # ------------------------------------------------------------------
    # Agent turn
    # ------------------------------------------------------------------

    async def _run_agent_turn(self, message: Any, query: str, session_id: str) -> None:
        channel = message.channel
        status_message: Any = None
        answered = False
        # Collect image refs from the whole turn (tool status + final). Agents
        # often print "saved to attachments/foo.jpg" only in tool output.
        turn_image_refs: List[str] = []
        # Tool stdout is NOT streamed to Discord — snapshot image dirs so we
        # still pick up files the agent wrote under attachments/workspace.
        images_before = snapshot_image_files()

        def _harvest_images(raw: str) -> None:
            if not raw:
                return
            _, refs = extract_image_refs(raw)
            for ref in refs:
                if ref not in turn_image_refs:
                    turn_image_refs.append(ref)

        stream = self._agent.run_query(query, session_id)
        while stream is not None:
            pending_confirmation: Optional[Dict[str, Any]] = None

            async for event in stream:
                event_text = _event_text(event)
                _harvest_images(event_text)

                if _is_final(event):
                    text = remove_think_content(event_text)
                    # Files created during the turn
                    for path in new_images_since(images_before):
                        if path not in turn_image_refs:
                            turn_image_refs.append(path)
                    if not text and not turn_image_refs:
                        continue
                    if self._format_markdown and text:
                        text = to_discord_markdown(text)
                    status_message = await self._clear_status(status_message)
                    await self._send_reply(
                        channel, text or "", extra_image_refs=turn_image_refs
                    )
                    answered = True
                    continue

                confirmation = _confirmation_request(event)
                if confirmation:
                    # Tool args sometimes include output paths
                    try:
                        _harvest_images(json.dumps(confirmation.get("args") or {}))
                    except (TypeError, ValueError):
                        pass
                    pending_confirmation = confirmation
                    continue

                if self._show_working_updates:
                    if event_text:
                        status_message = await self._update_status(
                            channel, status_message, f"⏳ {event_text}"
                        )

            stream = None
            if pending_confirmation:
                status_message = await self._clear_status(status_message)
                approved = await self._ask_tool_confirmation(message, pending_confirmation)
                resume = getattr(self._agent, "tool_confirmation", None)
                if resume is None:
                    await self._send(
                        channel,
                        "⚠️ This agent requested tool approval but does not support resuming it.",
                    )
                    break
                stream = resume(pending_confirmation["id"], session_id, approved)

        await self._clear_status(status_message)
        if not answered:
            await self._send(channel, "🤔 The agent finished without producing a response.")

    async def _ask_tool_confirmation(self, message: Any, request: Dict[str, Any]) -> bool:
        """
        Ask the requester to approve a tool call, by ✅ / ❌ reaction on the
        prompt or by replying "yes" / "no" in the channel.  Whichever arrives
        first wins; no answer within the timeout denies the call.
        """
        channel = message.channel
        prompt = await self._send(channel, _format_tool_prompt(request))
        if prompt is None:
            return False

        try:
            await prompt.add_reaction(APPROVE_EMOJI)
            await prompt.add_reaction(DENY_EMOJI)
        except Exception as exc:
            # Missing "Add Reactions" permission — the typed reply still works.
            logger.warning(f"Could not add confirmation reactions: {exc}")

        approved = await self._await_decision(message, prompt)
        logger.info(f"Tool '{request.get('name')}' approved={approved}")

        await self._close_prompt(prompt, approved)

        if approved is None:
            await self._send(channel, "⌛ Tool approval timed out — the call was denied.")
            return False
        return approved

    async def _close_prompt(self, prompt: Any, approved: Optional[bool]) -> None:
        """
        Retire an answered prompt: strip the ✅/❌ voting buttons so the counts
        do not linger, and record the outcome in the message itself.
        """
        if not await self._try(prompt, "clear_reactions"):
            # No "Manage Messages" — at least take back the two the bot seeded,
            # leaving only the asker's own click.
            me = getattr(self._client, "user", None)
            for emoji in (APPROVE_EMOJI, DENY_EMOJI):
                await self._try(prompt, "remove_reaction", emoji, me)

        outcome = {True: "✅ Approved", False: "❌ Denied", None: "⌛ Timed out — denied"}[approved]
        await self._try(prompt, "edit", content=f"{prompt.content}\n\n**{outcome}**")

    @staticmethod
    async def _try(target: Any, method: str, *args: Any, **kwargs: Any) -> bool:
        """Best-effort Discord call; False when unsupported or not permitted."""
        operation = getattr(target, method, None)
        if operation is None:
            return False
        try:
            await operation(*args, **kwargs)
            return True
        except Exception as exc:
            logger.debug(f"Discord call {method} failed: {exc}")
            return False

    async def _await_decision(self, message: Any, prompt: Any) -> Optional[bool]:
        """
        Wait for the asker's decision.  Returns True/False, or None on timeout.

        Listens for `raw_reaction_add` rather than `reaction_add` so the answer
        is picked up even when the prompt has fallen out of discord.py's
        message cache, and races it against a typed reply in the same channel.
        """
        requester_id = getattr(getattr(message, "author", None), "id", None)
        prompt_id = getattr(prompt, "id", None)
        channel_id = getattr(getattr(message, "channel", None), "id", None)

        def reaction_check(payload: Any) -> bool:
            if getattr(payload, "message_id", None) != prompt_id:
                return False
            if getattr(payload, "user_id", None) != requester_id:
                logger.info(
                    "Ignoring tool approval reaction from a different user "
                    f"({getattr(payload, 'user_id', None)}) — only the asker can approve"
                )
                return False
            return str(getattr(payload, "emoji", "")) in (APPROVE_EMOJI, DENY_EMOJI)

        def reply_check(reply: Any) -> bool:
            return (
                getattr(getattr(reply, "channel", None), "id", None) == channel_id
                and getattr(getattr(reply, "author", None), "id", None) == requester_id
                and _decision_from_text(getattr(reply, "content", "")) is not None
            )

        async def by_reaction() -> Optional[bool]:
            payload = await self._client.wait_for("raw_reaction_add", check=reaction_check)
            return str(getattr(payload, "emoji", "")) == APPROVE_EMOJI

        async def by_reply() -> Optional[bool]:
            reply = await self._client.wait_for("message", check=reply_check)
            return _decision_from_text(getattr(reply, "content", ""))

        self._pending_decisions[channel_id] = requester_id
        waiters = [asyncio.ensure_future(by_reaction()), asyncio.ensure_future(by_reply())]
        try:
            done, _pending = await asyncio.wait(
                waiters,
                timeout=self._tool_confirmation_timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            self._pending_decisions.pop(channel_id, None)
            for waiter in waiters:
                if not waiter.done():
                    waiter.cancel()

        if not done:
            return None
        return done.pop().result()

    # ------------------------------------------------------------------
    # Discord I/O
    # ------------------------------------------------------------------

    async def _send(
        self,
        channel: Any,
        text: str,
        *,
        files: Optional[List[Any]] = None,
    ) -> Any:
        """Send text to a channel, splitting it across the 2000-char limit.

        When *files* is provided, attachments go on the **first** chunk only
        (Discord requires a non-empty message or at least one file).
        """
        chunks = split_message(text) if text else []
        if not chunks and files:
            chunks = [""]  # attachment-only message
        if not chunks:
            return None

        sent = None
        for i, chunk in enumerate(chunks):
            kwargs: Dict[str, Any] = {}
            if chunk:
                kwargs["content"] = chunk
            if i == 0 and files:
                kwargs["files"] = files
            if not kwargs:
                continue
            try:
                sent = await channel.send(**kwargs)
            except Exception as exc:
                logger.warning(f"Failed to send Discord message: {exc}")
                # Retry text without files if attachment upload failed
                if i == 0 and files and "content" in kwargs:
                    try:
                        sent = await channel.send(content=kwargs["content"])
                        logger.warning("Sent text without image attachments after upload failure")
                    except Exception as exc2:
                        logger.warning(f"Fallback text send also failed: {exc2}")
                        return None
                else:
                    return None
        return sent

    async def _send_reply(
        self,
        channel: Any,
        text: str,
        *,
        extra_image_refs: Optional[List[str]] = None,
    ) -> Any:
        """
        Send a final agent answer, uploading any local/remote images found
        in the text (and *extra_image_refs* from the turn) as Discord
        file attachments so they render in-channel.
        """
        cleaned, refs = extract_image_refs(text or "")
        # Merge turn-wide refs (tool logs often have paths the final text omits)
        for ref in extra_image_refs or []:
            if ref not in refs:
                refs.append(ref)

        logger.info(f"Discord image refs for reply: {refs or '(none)'}")
        local_paths, notes = resolve_image_files(refs) if refs else ([], [])
        if local_paths:
            logger.info(f"Resolved image files for upload: {local_paths}")
        if notes:
            logger.warning(f"Image resolve notes: {notes}")
            note_block = "\n".join(f"_{n}_" for n in notes)
            cleaned = (cleaned + "\n\n" + note_block).strip() if cleaned else note_block

        discord_files: List[Any] = []
        open_handles: List[Any] = []
        if local_paths:
            discord = self._import_discord()
            for path in local_paths:
                try:
                    # Open binary handles so discord.py always has seekable data
                    handle = open(path, "rb")
                    open_handles.append(handle)
                    discord_files.append(
                        discord.File(handle, filename=Path(path).name)
                    )
                except Exception as exc:
                    logger.warning(
                        f"Could not open image for Discord upload ({path}): {exc}"
                    )
            if discord_files:
                logger.info(
                    f"Attaching {len(discord_files)} image(s) to Discord reply: "
                    f"{[Path(p).name for p in local_paths[: len(discord_files)]]}"
                )

        try:
            if self._reply_as_embed and not discord_files:
                discord = self._import_discord()
                embed_limit = 4096
                sent = None
                for chunk in split_message(cleaned, limit=embed_limit) or [""]:
                    try:
                        embed = discord.Embed(description=chunk or None)
                        if refs and len(refs) == 1 and refs[0].startswith("http"):
                            embed.set_image(url=refs[0])
                        sent = await channel.send(embed=embed)
                    except Exception as exc:
                        logger.warning(
                            f"Failed to send Discord embed reply, falling back to text: {exc}"
                        )
                        return await self._send(channel, cleaned, files=None)
                return sent

            return await self._send(
                channel,
                cleaned,
                files=discord_files or None,
            )
        finally:
            for handle in open_handles:
                try:
                    handle.close()
                except Exception:
                    pass

    async def _update_status(self, channel: Any, status_message: Any, text: str) -> Any:
        """Post the working status, or edit the existing status message in place."""
        text = split_message(text)[0]
        try:
            if status_message is None:
                return await channel.send(text)
            await status_message.edit(content=text)
            return status_message
        except Exception as exc:
            logger.warning(f"Failed to update Discord status message: {exc}")
            return status_message

    async def _clear_status(self, status_message: Any) -> Any:
        """Best-effort removal of the working status message."""
        if status_message is None:
            return None
        try:
            await status_message.delete()
        except Exception as exc:
            logger.debug(f"Could not delete status message: {exc}")
        return None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Connect to Discord and block until the bot is stopped."""
        client = self.build_client()
        try:
            client.run(self._token)
        except Exception as exc:
            logger.error(f"Error running Discord bot: {exc}")

    async def start(self) -> None:
        """Async variant of run() for embedding in an existing event loop."""
        client = self.build_client()
        await client.start(self._token)

    async def close(self) -> None:
        """Disconnect the bot."""
        if self._client is not None:
            await self._client.close()
