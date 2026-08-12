"""
Unit tests for DiscordAgentServer.

Discord itself is never contacted — channels, messages and the client are
fakes, and the agent is a stub that yields scripted events.
"""

import asyncio

import pytest
from unittest.mock import MagicMock

from declarative_agent_sdk.transports.discord.server import (
    APPROVE_EMOJI,
    DENY_EMOJI,
    DISCORD_MESSAGE_LIMIT,
    DiscordAgentServer,
    _confirmation_request,
    _event_text,
    _is_final,
    _is_tool_status,
    dedupe_paths_by_content,
    extract_image_refs,
    format_working_status,
    resolve_image_files,
    split_message,
    strip_attached_refs_from_text,
    to_discord_markdown,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeEvent:
    """Agent event shaped like ADK/LangChain events."""

    def __init__(self, text="", is_final=False, tool_call=None, long_running=False):
        self.content = MagicMock() if (text or tool_call) else None
        if self.content is not None:
            part = MagicMock()
            part.text = text or None
            part.function_call = tool_call
            self.content.parts = [part]
        self.long_running_tool_ids = ["lr-1"] if long_running else []
        self.actions = MagicMock()
        self.actions.requested_tool_confirmations = [] if not long_running else ["c1"]
        self._is_final = is_final

    def is_final_response(self):
        return self._is_final


def _tool_call(call_id="fc-1", name="search", args=None):
    fc = MagicMock()
    fc.id = call_id
    fc.args = {"originalFunctionCall": {"name": name, "args": args or {"q": "news"}}}
    return fc


class FakeMessage:
    def __init__(self, content, channel, author_id="99", author_bot=False,
                 guild=object(), mentions=None, message_id="m1"):
        self.content = content
        self.channel = channel
        self.author = MagicMock()
        self.author.id = author_id
        self.author.bot = author_bot
        self.guild = guild
        self.mentions = mentions or []
        self.id = message_id
        self.reactions = []

    async def add_reaction(self, emoji):
        self.reactions.append(emoji)

    async def remove_reaction(self, emoji, user):
        if emoji in self.reactions:
            self.reactions.remove(emoji)

    async def clear_reactions(self):
        raise PermissionError("Missing Manage Messages")   # common bot setup

    async def edit(self, content=None):
        self.content = content

    async def delete(self):
        self.channel.deleted.append(self)


class FakeChannel:
    def __init__(self, channel_id="555"):
        self.id = channel_id
        self.sent = []
        self.deleted = []
        self.files_sent = []

    async def send(self, content=None, *, files=None, embed=None, **kwargs):
        msg = FakeMessage(content, self, message_id=f"sent-{len(self.sent)}")
        msg.files = files or []
        msg.embed = embed
        self.sent.append(msg)
        if files:
            self.files_sent.extend(files)
        return msg


class FakeAgent:
    """BaseAgent stand-in that replays scripted event batches."""

    name = "test_agent"

    def __init__(self, batches):
        # batches: list of event lists — one per run_query / tool_confirmation call
        self._batches = list(batches)
        self.queries = []
        self.confirmations = []

    def _next_batch(self):
        return self._batches.pop(0) if self._batches else []

    async def run_query(self, query, session_id=None):
        self.queries.append((query, session_id))
        for event in self._next_batch():
            yield event

    async def invoke(self, context):  # pragma: no cover - unused here
        return
        yield

    async def tool_confirmation(self, context_id, session_id, yes):
        self.confirmations.append((context_id, session_id, yes))
        for event in self._next_batch():
            yield event


def _make_server(agent=None, bot_user_id="1", **kwargs):
    server = DiscordAgentServer(agent or FakeAgent([]), token="fake-token", **kwargs)
    client = MagicMock()
    client.user = MagicMock()
    client.user.id = bot_user_id
    server._client = client
    return server


def _mention(user_id="1"):
    user = MagicMock()
    user.id = user_id
    return user


# ---------------------------------------------------------------------------
# to_discord_markdown
# ---------------------------------------------------------------------------

class TestExtractImageRefs:
    def test_attach_marker(self):
        text, refs = extract_image_refs(
            "Chart ready [[attach:attachments/plot.png]] thanks"
        )
        assert refs == ["attachments/plot.png"]
        assert "[[" not in text

    def test_markdown_image(self):
        text, refs = extract_image_refs("See ![plot](workspace/out.png) above")
        assert refs == ["workspace/out.png"]
        assert "plot" in text
        assert "![" not in text

    def test_url_and_path(self, tmp_path):
        img = tmp_path / "chart.png"
        img.write_bytes(b"\x89PNG\r\n\x1a\n")
        text = f"http://example.com/a.jpg and {img}"
        cleaned, refs = extract_image_refs(text)
        assert "http://example.com/a.jpg" in refs
        assert str(img) in refs or any(str(img) in r or r.endswith("chart.png") for r in refs)

    def test_resolve_local_file(self, tmp_path):
        img = tmp_path / "shot.png"
        # Valid PNG signature so magic-byte check accepts it
        img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
        paths, notes = resolve_image_files([str(img)])
        assert paths == [str(img.resolve())]
        assert notes == []

    def test_rejects_html_masquerading_as_jpg(self, tmp_path):
        fake = tmp_path / "page.jpg"
        fake.write_text("<!DOCTYPE html><html>not an image</html>")
        paths, notes = resolve_image_files([str(fake)])
        assert paths == []
        assert any("not a valid image" in n for n in notes)

    def test_strip_attached_refs_removes_url_and_path(self):
        text = (
            "See ![x](https://cdn.example.com/a.png) and "
            "attachments/a.png https://cdn.example.com/a.png done"
        )
        out = strip_attached_refs_from_text(
            text,
            ["https://cdn.example.com/a.png", "attachments/a.png"],
        )
        assert "cdn.example.com" not in out
        assert "attachments/a.png" not in out
        assert "done" in out

    def test_dedupe_same_content(self, tmp_path):
        a = tmp_path / "a.png"
        b = tmp_path / "b.png"
        data = b"\x89PNG\r\n\x1a\n" + b"\x01" * 20
        a.write_bytes(data)
        b.write_bytes(data)
        assert dedupe_paths_by_content([str(a), str(b)]) == [str(a)]


class TestToDiscordMarkdown:
    def test_passthrough_simple_markdown(self):
        text = "Here's **bold** and a list:\n- one\n- two"
        assert to_discord_markdown(text) == text

    def test_converts_gfm_table_to_code_block(self):
        text = (
            "Usage:\n"
            "| Volume | Used |\n"
            "| ------ | ---- |\n"
            "| Root   | 22%  |\n"
            "| Data   | 90%  |\n"
            "Done."
        )
        out = to_discord_markdown(text)
        assert "```" in out
        assert "Volume" in out
        assert "Root" in out
        assert "90%" in out
        assert "| ------ |" not in out
        assert out.startswith("Usage:")
        assert out.endswith("Done.")

    def test_leaves_markdown_images_for_attachment_pass(self):
        # Image markdown is handled by extract_image_refs / _send_reply,
        # not rewritten to links (attachments render in-channel).
        assert "![plot](https://x.test/a.png)" in to_discord_markdown(
            "See ![plot](https://x.test/a.png)"
        )

    def test_strips_html_tags(self):
        assert to_discord_markdown("Hello <b>world</b>") == "Hello world"

    def test_horizontal_rule(self):
        out = to_discord_markdown("above\n---\nbelow")
        assert "────────" in out
        assert "above" in out and "below" in out


# ---------------------------------------------------------------------------
# format_working_status / thinking
# ---------------------------------------------------------------------------

class TestFormatWorkingStatus:
    def test_tool_only(self):
        text = format_working_status(
            thinking_parts=[], tool_status="Calling tools: search"
        )
        assert text == "⏳ Calling tools: search"

    def test_thinking_only(self):
        text = format_working_status(thinking_parts=["Plan step 1"])
        assert "💭" in text
        assert "Thinking" in text
        assert "> Plan step 1" in text

    def test_combined(self):
        text = format_working_status(
            thinking_parts=["I will call search"],
            tool_status="Calling tools: search",
        )
        assert "💭" in text
        assert "⏳ Calling tools: search" in text

    def test_permanent_header(self):
        text = format_working_status(
            thinking_parts=["reasoned"], permanent=True
        )
        assert "Thought process" in text
        assert "Calling tools" not in text

    def test_is_tool_status(self):
        assert _is_tool_status("Calling tools: foo")
        assert _is_tool_status("Running tool: bar")
        assert not _is_tool_status("I will plan next")


# ---------------------------------------------------------------------------
# split_message
# ---------------------------------------------------------------------------

class TestSplitMessage:
    def test_short_text_is_single_chunk(self):
        assert split_message("hello") == ["hello"]

    def test_empty_text_yields_no_chunks(self):
        assert split_message("") == []

    def test_long_text_respects_limit(self):
        chunks = split_message("word " * 1000)
        assert len(chunks) > 1
        assert all(len(c) <= DISCORD_MESSAGE_LIMIT for c in chunks)

    def test_splits_on_newline_boundary(self):
        text = ("a" * 1500) + "\n" + ("b" * 1000)
        chunks = split_message(text)
        assert chunks[0] == "a" * 1500
        assert chunks[1] == "b" * 1000

    def test_unsplittable_text_is_hard_cut(self):
        chunks = split_message("x" * 4500)
        assert [len(c) for c in chunks] == [2000, 2000, 500]

    def test_no_content_is_lost(self):
        text = "line\n" * 900
        assert "".join(split_message(text)).replace("\n", "") == text.replace("\n", "")

    def test_closes_open_code_fence_across_chunks(self):
        # Long fenced block that must be split — each piece should be balanced.
        body = "\n".join(f"line {i} " + ("x" * 40) for i in range(80))
        text = f"```\n{body}\n```"
        chunks = split_message(text)
        assert len(chunks) > 1
        for chunk in chunks:
            fences = sum(1 for ln in chunk.splitlines() if ln.strip().startswith("```"))
            assert fences % 2 == 0, chunk[:80]


# ---------------------------------------------------------------------------
# Event helpers
# ---------------------------------------------------------------------------

class TestEventHelpers:
    def test_event_text_joins_parts(self):
        assert _event_text(FakeEvent(text="hello")) == "hello"

    def test_event_text_empty_when_no_content(self):
        assert _event_text(FakeEvent()) == ""

    def test_is_final_true_for_final_event(self):
        assert _is_final(FakeEvent(text="done", is_final=True)) is True

    def test_is_final_false_for_working_event(self):
        assert _is_final(FakeEvent(text="working")) is False

    def test_is_final_false_when_long_running_tools_pending(self):
        event = FakeEvent(text="x", is_final=True, long_running=True)
        assert _is_final(event) is False

    def test_confirmation_request_extracted(self):
        event = FakeEvent(tool_call=_tool_call("fc-9", "fetch"), long_running=True)
        request = _confirmation_request(event)
        assert request == {"id": "fc-9", "name": "fetch", "args": {"q": "news"}}

    def test_confirmation_request_none_for_plain_event(self):
        assert _confirmation_request(FakeEvent(text="hi")) is None


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------

class TestInit:
    def test_token_from_environment(self, monkeypatch):
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "env-token")
        server = DiscordAgentServer(FakeAgent([]))
        assert server._token == "env-token"

    def test_missing_token_raises(self, monkeypatch):
        monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
        with pytest.raises(ValueError, match="token is required"):
            DiscordAgentServer(FakeAgent([]))

    def test_invalid_session_scope_raises(self):
        with pytest.raises(ValueError, match="session_scope"):
            DiscordAgentServer(FakeAgent([]), token="t", session_scope="planet")


# ---------------------------------------------------------------------------
# extract_query — when should the bot answer?
# ---------------------------------------------------------------------------

class TestExtractQuery:
    def test_mention_triggers_and_is_stripped(self):
        server = _make_server()
        message = FakeMessage("<@1> what is the news?", FakeChannel(), mentions=[_mention("1")])
        assert server.extract_query(message) == "what is the news?"

    def test_other_user_mention_is_ignored(self):
        server = _make_server()
        message = FakeMessage("<@2> hello", FakeChannel(), mentions=[_mention("2")])
        assert server.extract_query(message) is None

    def test_plain_guild_message_ignored(self):
        server = _make_server()
        assert server.extract_query(FakeMessage("hello", FakeChannel())) is None

    def test_dm_triggers_without_mention(self):
        server = _make_server()
        message = FakeMessage("hello there", FakeChannel(), guild=None)
        assert server.extract_query(message) == "hello there"

    def test_dm_ignored_when_disabled(self):
        server = _make_server(respond_to_dms=False)
        message = FakeMessage("hello", FakeChannel(), guild=None)
        assert server.extract_query(message) is None

    def test_prefix_triggers_and_is_stripped(self):
        server = _make_server(command_prefix="!ask ")
        message = FakeMessage("!ask summarise this", FakeChannel())
        assert server.extract_query(message) == "summarise this"

    def test_respond_to_all_messages(self):
        server = _make_server(respond_to_all_messages=True)
        assert server.extract_query(FakeMessage("hi", FakeChannel())) == "hi"

    def test_allowed_channels_filter(self):
        server = _make_server(allowed_channels=["777"], respond_to_all_messages=True)
        assert server.extract_query(FakeMessage("hi", FakeChannel("555"))) is None
        assert server.extract_query(FakeMessage("hi", FakeChannel("777"))) == "hi"

    def test_allowed_channels_do_not_block_dms(self):
        server = _make_server(allowed_channels=["777"])
        message = FakeMessage("hi", FakeChannel("555"), guild=None)
        assert server.extract_query(message) == "hi"

    def test_mention_only_message_is_ignored(self):
        server = _make_server()
        message = FakeMessage("<@1>", FakeChannel(), mentions=[_mention("1")])
        assert server.extract_query(message) is None


# ---------------------------------------------------------------------------
# Session mapping
# ---------------------------------------------------------------------------

class TestSessionId:
    def test_channel_scope(self):
        server = _make_server()
        assert server.session_id(FakeMessage("x", FakeChannel("42"))) == "discord-channel-42"

    def test_user_scope(self):
        server = _make_server(session_scope="user")
        message = FakeMessage("x", FakeChannel("42"), author_id="7")
        assert server.session_id(message) == "discord-user-7"

    def test_global_scope(self):
        server = _make_server(session_scope="global")
        assert server.session_id(FakeMessage("x", FakeChannel())) == "discord-test_agent"


# ---------------------------------------------------------------------------
# handle_message
# ---------------------------------------------------------------------------

class TestHandleMessage:
    async def test_final_response_is_posted(self):
        agent = FakeAgent([[FakeEvent(text="Here is the news.", is_final=True)]])
        server = _make_server(agent)
        channel = FakeChannel()
        await server.handle_message(
            FakeMessage("<@1> news?", channel, mentions=[_mention("1")])
        )
        assert [m.content for m in channel.sent] == ["Here is the news."]

    async def test_query_and_session_passed_to_agent(self):
        agent = FakeAgent([[FakeEvent(text="ok", is_final=True)]])
        server = _make_server(agent)
        await server.handle_message(
            FakeMessage("<@1> hello", FakeChannel("321"), mentions=[_mention("1")])
        )
        assert agent.queries == [("hello", "discord-channel-321")]

    async def test_messages_from_bots_are_ignored(self):
        agent = FakeAgent([[FakeEvent(text="ok", is_final=True)]])
        server = _make_server(agent)
        channel = FakeChannel()
        await server.handle_message(
            FakeMessage("<@1> hi", channel, author_bot=True, mentions=[_mention("1")])
        )
        assert channel.sent == []
        assert agent.queries == []

    async def test_bots_own_message_is_ignored(self):
        agent = FakeAgent([[FakeEvent(text="ok", is_final=True)]])
        server = _make_server(agent)
        channel = FakeChannel()
        await server.handle_message(
            FakeMessage("<@1> hi", channel, author_id="1", mentions=[_mention("1")])
        )
        assert agent.queries == []

    async def test_working_update_is_replaced_by_answer(self):
        agent = FakeAgent([[
            FakeEvent(text="Calling tools: search"),
            FakeEvent(text="Final answer.", is_final=True),
        ]])
        server = _make_server(agent)
        channel = FakeChannel()
        await server.handle_message(
            FakeMessage("<@1> go", channel, mentions=[_mention("1")])
        )
        assert channel.sent[0].content == "⏳ Calling tools: search"
        assert channel.sent[0] in channel.deleted
        assert channel.sent[1].content == "Final answer."

    async def test_model_thinking_is_shown_and_kept(self):
        agent = FakeAgent([[
            FakeEvent(text="I will search then summarize."),
            FakeEvent(text="Calling tools: search"),
            FakeEvent(text="Here is the summary.", is_final=True),
        ]])
        server = _make_server(agent)
        channel = FakeChannel()
        await server.handle_message(
            FakeMessage("<@1> go", channel, mentions=[_mention("1")])
        )
        # Live status was edited in place: final freeze + answer
        contents = [m.content for m in channel.sent]
        assert any("💭" in (c or "") and "I will search" in (c or "") for c in contents)
        assert any("Thought process" in (c or "") for c in contents)
        assert contents[-1] == "Here is the summary."
        # Frozen thinking is NOT deleted
        thinking_msgs = [m for m in channel.sent if m.content and "Thought process" in m.content]
        assert thinking_msgs
        assert thinking_msgs[0] not in channel.deleted

    async def test_working_updates_suppressed_when_disabled(self):
        agent = FakeAgent([[
            FakeEvent(text="thinking"),
            FakeEvent(text="Done.", is_final=True),
        ]])
        server = _make_server(agent, show_working_updates=False)
        channel = FakeChannel()
        await server.handle_message(
            FakeMessage("<@1> go", channel, mentions=[_mention("1")])
        )
        assert [m.content for m in channel.sent] == ["Done."]

    async def test_think_tags_stripped_from_answer(self):
        agent = FakeAgent([[
            FakeEvent(text="<think>internal reasoning</think>Public answer.", is_final=True),
        ]])
        server = _make_server(agent)
        channel = FakeChannel()
        await server.handle_message(
            FakeMessage("<@1> go", channel, mentions=[_mention("1")])
        )
        assert channel.sent[0].content == "Public answer."

    async def test_long_answer_is_split(self):
        agent = FakeAgent([[FakeEvent(text="y" * 3000, is_final=True)]])
        server = _make_server(agent)
        channel = FakeChannel()
        await server.handle_message(
            FakeMessage("<@1> go", channel, mentions=[_mention("1")])
        )
        assert len(channel.sent) == 2
        assert all(len(m.content) <= DISCORD_MESSAGE_LIMIT for m in channel.sent)

    async def test_empty_run_reports_no_response(self):
        server = _make_server(FakeAgent([[]]))
        channel = FakeChannel()
        await server.handle_message(
            FakeMessage("<@1> go", channel, mentions=[_mention("1")])
        )
        assert "without producing a response" in channel.sent[0].content

    async def test_agent_exception_is_reported(self):
        class ExplodingAgent(FakeAgent):
            async def run_query(self, query, session_id=None):
                raise RuntimeError("model unavailable")
                yield  # pragma: no cover

        server = _make_server(ExplodingAgent([]))
        channel = FakeChannel()
        await server.handle_message(
            FakeMessage("<@1> go", channel, mentions=[_mention("1")])
        )
        assert "model unavailable" in channel.sent[0].content

    async def test_same_session_turns_are_serialised(self):
        order = []

        class SlowAgent(FakeAgent):
            async def run_query(self, query, session_id=None):
                order.append(f"start:{query}")
                await asyncio.sleep(0.01)
                order.append(f"end:{query}")
                yield FakeEvent(text=f"done {query}", is_final=True)

        server = _make_server(SlowAgent([]))
        channel = FakeChannel()
        await asyncio.gather(
            server.handle_message(FakeMessage("<@1> a", channel, mentions=[_mention("1")])),
            server.handle_message(FakeMessage("<@1> b", channel, mentions=[_mention("1")])),
        )
        assert order == ["start:a", "end:a", "start:b", "end:b"]


# ---------------------------------------------------------------------------
# Tool confirmation flow
# ---------------------------------------------------------------------------

class TestToolConfirmation:
    def _server_with_reaction(self, agent, emoji, requester_id="99"):
        """Server whose asker reacts with `emoji`; the typed-reply waiter hangs."""
        server = _make_server(agent)

        async def wait_for(event, check=None, timeout=None):
            if event != "raw_reaction_add":
                await asyncio.Event().wait()  # cancelled when the race resolves
            payload = MagicMock()
            payload.emoji = emoji
            payload.user_id = requester_id
            payload.message_id = "sent-0"
            return payload

        server._client.wait_for = wait_for
        return server

    def _server_with_reply(self, agent, text, requester_id="99"):
        """Server whose asker types `text`; the reaction waiter hangs."""
        server = _make_server(agent)

        async def wait_for(event, check=None, timeout=None):
            if event != "message":
                await asyncio.Event().wait()
            reply = MagicMock()
            reply.content = text
            reply.author.id = requester_id
            reply.channel.id = "555"
            return reply

        server._client.wait_for = wait_for
        return server

    async def test_approval_resumes_agent(self):
        agent = FakeAgent([
            [FakeEvent(tool_call=_tool_call("fc-1", "search"), long_running=True)],
            [FakeEvent(text="Found it.", is_final=True)],
        ])
        server = self._server_with_reaction(agent, APPROVE_EMOJI)
        channel = FakeChannel()
        await server.handle_message(
            FakeMessage("<@1> search", channel, mentions=[_mention("1")])
        )
        assert agent.confirmations == [("fc-1", "discord-channel-555", True)]
        assert channel.sent[-1].content == "Found it."

    async def test_denial_is_passed_to_agent(self):
        agent = FakeAgent([
            [FakeEvent(tool_call=_tool_call("fc-2", "delete"), long_running=True)],
            [FakeEvent(text="Cancelled.", is_final=True)],
        ])
        server = self._server_with_reaction(agent, DENY_EMOJI)
        channel = FakeChannel()
        await server.handle_message(
            FakeMessage("<@1> delete stuff", channel, mentions=[_mention("1")])
        )
        assert agent.confirmations == [("fc-2", "discord-channel-555", False)]

    async def test_prompt_shows_tool_name_and_reactions(self):
        agent = FakeAgent([
            [FakeEvent(tool_call=_tool_call("fc-3", "fetch_news"), long_running=True)],
            [FakeEvent(text="ok", is_final=True)],
        ])
        server = self._server_with_reaction(agent, APPROVE_EMOJI)
        channel = FakeChannel()
        await server.handle_message(
            FakeMessage("<@1> go", channel, mentions=[_mention("1")])
        )
        prompt = channel.sent[0]
        assert "fetch_news" in prompt.content
        # Seeded so they are clickable, then withdrawn once answered so the
        # counts do not read as a second vote.
        assert prompt.reactions == []

    async def test_seeds_both_reactions_before_deciding(self):
        seen = {}

        agent = FakeAgent([
            [FakeEvent(tool_call=_tool_call("fc-10", "search"), long_running=True)],
            [FakeEvent(text="ok", is_final=True)],
        ])
        server = _make_server(agent)

        async def wait_for(event, check=None, timeout=None):
            if event == "raw_reaction_add":
                seen["at_decision_time"] = list(server._prompt.reactions)
                payload = MagicMock()
                payload.emoji = APPROVE_EMOJI
                payload.user_id = "99"
                payload.message_id = "sent-0"
                return payload
            await asyncio.Event().wait()

        server._client.wait_for = wait_for
        channel = FakeChannel()

        original_send = channel.send

        async def send_and_track(content):
            message = await original_send(content)
            server._prompt = message
            return message

        channel.send = send_and_track
        await server.handle_message(
            FakeMessage("<@1> go", channel, mentions=[_mention("1")])
        )
        assert seen["at_decision_time"] == [APPROVE_EMOJI, DENY_EMOJI]

    async def test_clear_reactions_used_when_permitted(self):
        agent = FakeAgent([
            [FakeEvent(tool_call=_tool_call("fc-13", "search"), long_running=True)],
            [FakeEvent(text="ok", is_final=True)],
        ])
        server = self._server_with_reaction(agent, APPROVE_EMOJI)
        channel = FakeChannel()
        cleared = []
        original_send = channel.send

        async def send_with_manage_messages(content):
            message = await original_send(content)

            async def clear_reactions():
                cleared.append(True)
                message.reactions.clear()

            message.clear_reactions = clear_reactions
            return message

        channel.send = send_with_manage_messages
        await server.handle_message(
            FakeMessage("<@1> go", channel, mentions=[_mention("1")])
        )
        assert cleared == [True]
        assert channel.sent[0].reactions == []

    async def test_outcome_recorded_on_the_prompt(self):
        agent = FakeAgent([
            [FakeEvent(tool_call=_tool_call("fc-11", "search"), long_running=True)],
            [FakeEvent(text="ok", is_final=True)],
        ])
        server = self._server_with_reaction(agent, APPROVE_EMOJI)
        channel = FakeChannel()
        await server.handle_message(
            FakeMessage("<@1> go", channel, mentions=[_mention("1")])
        )
        assert "✅ Approved" in channel.sent[0].content

    async def test_timeout_recorded_on_the_prompt(self):
        agent = FakeAgent([
            [FakeEvent(tool_call=_tool_call("fc-12", "search"), long_running=True)],
            [FakeEvent(text="ok", is_final=True)],
        ])
        server = _make_server(agent, tool_confirmation_timeout=0.01)

        async def wait_for(_event, check=None, timeout=None):
            await asyncio.Event().wait()

        server._client.wait_for = wait_for
        channel = FakeChannel()
        await server.handle_message(
            FakeMessage("<@1> go", channel, mentions=[_mention("1")])
        )
        assert "Timed out" in channel.sent[0].content
        assert channel.sent[0].reactions == []

    async def test_typed_yes_approves(self):
        agent = FakeAgent([
            [FakeEvent(tool_call=_tool_call("fc-6", "search"), long_running=True)],
            [FakeEvent(text="Searched.", is_final=True)],
        ])
        server = self._server_with_reply(agent, "yes")
        channel = FakeChannel()
        await server.handle_message(
            FakeMessage("<@1> go", channel, mentions=[_mention("1")])
        )
        assert agent.confirmations == [("fc-6", "discord-channel-555", True)]

    async def test_typed_no_denies(self):
        agent = FakeAgent([
            [FakeEvent(tool_call=_tool_call("fc-7", "wipe"), long_running=True)],
            [FakeEvent(text="Skipped.", is_final=True)],
        ])
        server = self._server_with_reply(agent, "No")
        channel = FakeChannel()
        await server.handle_message(
            FakeMessage("<@1> go", channel, mentions=[_mention("1")])
        )
        assert agent.confirmations[0][2] is False

    async def test_decision_reply_is_not_treated_as_a_new_query(self):
        agent = FakeAgent([
            [FakeEvent(tool_call=_tool_call("fc-8", "search"), long_running=True)],
            [FakeEvent(text="Done.", is_final=True)],
        ])
        server = self._server_with_reply(agent, "yes")
        channel = FakeChannel()
        # Mid-approval, the asker's "yes" arrives through on_message as well.
        server._pending_decisions[channel.id] = "99"
        reply = FakeMessage("yes", channel, guild=None)
        await server.handle_message(reply)
        assert agent.queries == []

    async def test_reaction_from_another_user_is_ignored(self):
        agent = FakeAgent([
            [FakeEvent(tool_call=_tool_call("fc-9", "search"), long_running=True)],
            [FakeEvent(text="Denied.", is_final=True)],
        ])
        server = _make_server(agent, tool_confirmation_timeout=0.05)
        seen = []

        async def wait_for(event, check=None, timeout=None):
            if event == "raw_reaction_add":
                payload = MagicMock()
                payload.emoji = APPROVE_EMOJI
                payload.user_id = "someone-else"
                payload.message_id = "sent-0"
                seen.append(check(payload))
            await asyncio.Event().wait()

        server._client.wait_for = wait_for
        channel = FakeChannel()
        await server.handle_message(
            FakeMessage("<@1> go", channel, mentions=[_mention("1")])
        )
        assert seen == [False]                       # check rejected the stranger
        assert agent.confirmations[0][2] is False    # so the call timed out → denied

    async def test_timeout_denies_the_call(self):
        agent = FakeAgent([
            [FakeEvent(tool_call=_tool_call("fc-4", "wipe"), long_running=True)],
            [FakeEvent(text="Denied.", is_final=True)],
        ])
        server = _make_server(agent, tool_confirmation_timeout=0.01)

        async def wait_for(_event, check=None, timeout=None):
            await asyncio.Event().wait()

        server._client.wait_for = wait_for
        channel = FakeChannel()
        await server.handle_message(
            FakeMessage("<@1> go", channel, mentions=[_mention("1")])
        )
        assert agent.confirmations == [("fc-4", "discord-channel-555", False)]
        assert any("timed out" in m.content for m in channel.sent)

    async def test_agent_without_confirmation_support_is_reported(self):
        class NoConfirmAgent(FakeAgent):
            tool_confirmation = None

        agent = NoConfirmAgent([
            [FakeEvent(tool_call=_tool_call("fc-5", "search"), long_running=True)],
        ])
        server = self._server_with_reaction(agent, APPROVE_EMOJI)
        channel = FakeChannel()
        await server.handle_message(
            FakeMessage("<@1> go", channel, mentions=[_mention("1")])
        )
        assert any("does not support resuming" in m.content for m in channel.sent)
