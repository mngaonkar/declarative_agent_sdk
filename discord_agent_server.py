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
import os
import re
from typing import Any, Dict, Iterable, List, Optional

from declarative_agent_sdk.agent_logging import get_logger
from declarative_agent_sdk.base_agent import BaseAgent
from declarative_agent_sdk.utils import remove_think_content

logger = get_logger(__name__)

# Discord rejects messages longer than this.
DISCORD_MESSAGE_LIMIT = 2000

APPROVE_EMOJI = "\N{WHITE HEAVY CHECK MARK}"
DENY_EMOJI = "\N{CROSS MARK}"

# Typed replies accepted instead of a reaction.
APPROVE_WORDS = {"y", "yes", "ok", "okay", "approve", "approved", "go", "do it"}
DENY_WORDS = {"n", "no", "nope", "deny", "denied", "cancel", "stop"}


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


def to_discord_markdown(text: str) -> str:
    """
    Adapt common GitHub-flavored Markdown so Discord renders it cleanly.

    Discord already supports **bold**, *italic*, ``code``, fences, lists,
    quotes, spoilers, and (on modern clients) #/##/### headers.  What it does
    **not** support is the main source of "broken" agent replies:

    * pipe tables
    * HTML tags
    * image embeds (``![alt](url)``)
    * thematic breaks (``---``)

    Those are rewritten into Discord-friendly forms.  Native Discord markdown
    is left intact.
    """
    if not text:
        return text

    # Images → linked text (Discord won't inline the image in a normal message)
    text = _IMAGE_RE.sub(
        lambda m: f"[{m.group(1) or m.group(2)}]({m.group(2)})" if m.group(1) else m.group(2),
        text,
    )

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

        stream = self._agent.run_query(query, session_id)
        while stream is not None:
            pending_confirmation: Optional[Dict[str, Any]] = None

            async for event in stream:
                if _is_final(event):
                    text = remove_think_content(_event_text(event))
                    if not text:
                        continue
                    if self._format_markdown:
                        text = to_discord_markdown(text)
                    status_message = await self._clear_status(status_message)
                    await self._send_reply(channel, text)
                    answered = True
                    continue

                confirmation = _confirmation_request(event)
                if confirmation:
                    pending_confirmation = confirmation
                    continue

                if self._show_working_updates:
                    working_text = _event_text(event)
                    if working_text:
                        status_message = await self._update_status(
                            channel, status_message, f"⏳ {working_text}"
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

    async def _send(self, channel: Any, text: str) -> Any:
        """Send text to a channel, splitting it across the 2000-char limit."""
        sent = None
        for chunk in split_message(text):
            try:
                sent = await channel.send(chunk)
            except Exception as exc:
                logger.warning(f"Failed to send Discord message: {exc}")
                return None
        return sent

    async def _send_reply(self, channel: Any, text: str) -> Any:
        """
        Send a final agent answer.  Uses a Discord embed when
        ``reply_as_embed`` is set; otherwise plain messages (with splitting).
        """
        if not self._reply_as_embed:
            return await self._send(channel, text)

        discord = self._import_discord()
        # Embed description limit is 4096; split if needed
        embed_limit = 4096
        sent = None
        for chunk in split_message(text, limit=embed_limit):
            try:
                embed = discord.Embed(description=chunk)
                sent = await channel.send(embed=embed)
            except Exception as exc:
                logger.warning(f"Failed to send Discord embed reply, falling back to text: {exc}")
                return await self._send(channel, text)
        return sent

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
