#!/usr/bin/env python3
"""
Test program for DiscordAgentServer.

Three modes, from "needs nothing" to "needs everything":

  selftest  (default)  Scripted fake agent + fake Discord.  No token, no API
                       key, no network.  Asserts the routing, streaming,
                       message-splitting and tool-approval behaviour and
                       prints a pass/fail report.

  local                Your real agent (from a YAML config) behind a fake
                       Discord channel.  You type queries in the terminal and
                       see exactly what the bot would post, including tool
                       approval prompts.  Needs an LLM key, no Discord token.

  connect              Logs in to Discord, reports the servers and channels the
                       bot can post in, logs out.  Checks the token, the
                       MESSAGE CONTENT intent and permissions — no agent, no
                       LLM key.  Needs DISCORD_BOT_TOKEN.

  live                 Your real agent connected to Discord for real.
                       Needs DISCORD_BOT_TOKEN and an LLM key.

Usage:
    python run_discord_bot.py
    python run_discord_bot.py --mode local   --config agent.yaml
    python run_discord_bot.py --mode connect
    python run_discord_bot.py --mode live    --config agent.yaml
"""

import argparse
import asyncio
import os
import sys
from typing import Any, List, Optional

from declarative_agent_sdk import DiscordAgentServer
from declarative_agent_sdk.discord_agent_server import (
    APPROVE_EMOJI,
    DENY_EMOJI,
    DISCORD_MESSAGE_LIMIT,
)

BOT_ID = "1000"


# ---------------------------------------------------------------------------
# Fake Discord objects — enough surface for DiscordAgentServer to run against
# ---------------------------------------------------------------------------

class FakeUser:
    def __init__(self, user_id: str, bot: bool = False) -> None:
        self.id = user_id
        self.bot = bot


class FakeMessage:
    def __init__(self, content, channel, author, guild="guild-1", mentions=None, msg_id="m"):
        self.content = content
        self.channel = channel
        self.author = author
        self.guild = guild
        self.mentions = mentions or []
        self.id = msg_id
        self.reactions: List[str] = []
        self.deleted = False

    async def add_reaction(self, emoji: str) -> None:
        self.reactions.append(emoji)

    async def remove_reaction(self, emoji: str, user: Any) -> None:
        if emoji in self.reactions:
            self.reactions.remove(emoji)

    async def clear_reactions(self) -> None:
        # Most bots are invited without Manage Messages; exercise that path.
        raise PermissionError("Missing Manage Messages")

    async def edit(self, content: Optional[str] = None) -> None:
        self.content = content
        self.channel.log.append(("edit", content))

    async def delete(self) -> None:
        self.deleted = True
        self.channel.log.append(("delete", self.content))


class FakeChannel:
    """Records everything the bot posts instead of talking to Discord."""

    def __init__(self, channel_id: str = "chan-1", echo: bool = False) -> None:
        self.id = channel_id
        self.echo = echo
        self.sent: List[FakeMessage] = []
        self.log: List[tuple] = []

    async def send(self, content: str) -> FakeMessage:
        message = FakeMessage(content, self, FakeUser(BOT_ID, bot=True), msg_id=f"s{len(self.sent)}")
        self.sent.append(message)
        self.log.append(("send", content))
        if self.echo:
            print(f"\n\033[96m[bot → #{self.id}]\033[0m {content}\n")
        return message

    def posted(self) -> List[str]:
        """Text of messages still standing (status messages get deleted)."""
        return [m.content for m in self.sent if not m.deleted]


class FakeClient:
    """
    Stands in for discord.Client.  `wait_for` drives tool approvals: the
    reaction waiter resolves from `reaction_provider`, the typed-reply waiter
    parks forever (the server cancels the loser of the race).
    """

    def __init__(self, reaction_provider=None, reply_text: Optional[str] = None) -> None:
        self.user = FakeUser(BOT_ID, bot=True)
        self._reaction_provider = reaction_provider
        self._reply_text = reply_text

    async def wait_for(self, event: str, check=None, timeout=None):
        if event == "raw_reaction_add" and self._reaction_provider is not None:
            emoji = await self._reaction_provider()
            return type("Payload", (), {
                "emoji": emoji, "user_id": "user-1", "message_id": "s0",
            })()
        if event == "message" and self._reply_text is not None:
            return FakeMessage(self._reply_text, FakeChannel("chan-1"), FakeUser("user-1"))
        await asyncio.Event().wait()   # never fires; cancelled by the server


def attach_fake_client(server: DiscordAgentServer, reaction_provider=None,
                       reply_text: Optional[str] = None) -> FakeClient:
    client = FakeClient(reaction_provider, reply_text)
    server._client = client
    return client


def user_message(text: str, channel: FakeChannel, mention_bot: bool = True,
                 author_id: str = "user-1", guild: Any = "guild-1") -> FakeMessage:
    mentions = [FakeUser(BOT_ID)] if mention_bot else []
    content = f"<@{BOT_ID}> {text}" if mention_bot else text
    return FakeMessage(content, channel, FakeUser(author_id), guild=guild, mentions=mentions)


# ---------------------------------------------------------------------------
# Scripted agent for selftest mode
# ---------------------------------------------------------------------------

class ScriptedEvent:
    def __init__(self, text: str = "", final: bool = False, tool: Optional[dict] = None) -> None:
        self.long_running_tool_ids = ["lr"] if tool else []
        self.actions = type("A", (), {"requested_tool_confirmations": ["c"] if tool else []})()
        self._final = final

        if text or tool:
            function_call = None
            if tool:
                function_call = type("FC", (), {
                    "id": tool["id"],
                    "name": tool["name"],
                    "args": {"originalFunctionCall": {"name": tool["name"], "args": tool["args"]}},
                })()
            part = type("P", (), {"text": text or None, "function_call": function_call})()
            self.content = type("C", (), {"parts": [part]})()
        else:
            self.content = None

    def is_final_response(self) -> bool:
        return self._final


class ScriptedAgent:
    """BaseAgent stand-in that replays one event batch per call."""

    name = "scripted_agent"

    def __init__(self, batches: List[List[ScriptedEvent]]) -> None:
        self._batches = list(batches)
        self.queries: List[tuple] = []
        self.confirmations: List[tuple] = []

    def _next(self) -> List[ScriptedEvent]:
        return self._batches.pop(0) if self._batches else []

    async def run_query(self, query: str, session_id: Optional[str] = None):
        self.queries.append((query, session_id))
        for event in self._next():
            yield event

    async def invoke(self, context):  # not used by the Discord transport
        return
        yield

    async def tool_confirmation(self, context_id: str, session_id: str, yes: bool):
        self.confirmations.append((context_id, session_id, yes))
        for event in self._next():
            yield event


# ---------------------------------------------------------------------------
# selftest mode
# ---------------------------------------------------------------------------

class Report:
    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0

    def check(self, name: str, condition: bool, detail: str = "") -> None:
        if condition:
            self.passed += 1
            print(f"  \033[92m✓\033[0m {name}")
        else:
            self.failed += 1
            print(f"  \033[91m✗\033[0m {name}" + (f"\n      {detail}" if detail else ""))


async def selftest() -> int:
    from declarative_agent_sdk import set_log_level

    # The failure-handling checks deliberately blow the agent up; keep the
    # expected tracebacks out of the report.
    set_log_level("CRITICAL")

    report = Report()
    print("\nDiscordAgentServer self-test (no Discord, no LLM)\n")

    # --- triggers -----------------------------------------------------------
    print("Triggers")

    agent = ScriptedAgent([[ScriptedEvent("Hello there.", final=True)]])
    server = DiscordAgentServer(agent, token="test-token")
    attach_fake_client(server)
    channel = FakeChannel()
    await server.handle_message(user_message("hi", channel))
    report.check("@mention gets a reply", channel.posted() == ["Hello there."], str(channel.posted()))
    report.check("mention stripped from query", agent.queries[0][0] == "hi", str(agent.queries))

    agent = ScriptedAgent([[ScriptedEvent("should not happen", final=True)]])
    server = DiscordAgentServer(agent, token="test-token")
    attach_fake_client(server)
    channel = FakeChannel()
    await server.handle_message(user_message("just chatting", channel, mention_bot=False))
    report.check("plain channel message ignored", channel.sent == [])

    agent = ScriptedAgent([[ScriptedEvent("DM reply.", final=True)]])
    server = DiscordAgentServer(agent, token="test-token")
    attach_fake_client(server)
    channel = FakeChannel("dm-1")
    await server.handle_message(user_message("hello", channel, mention_bot=False, guild=None))
    report.check("DM answered without mention", channel.posted() == ["DM reply."])

    agent = ScriptedAgent([[ScriptedEvent("Prefix reply.", final=True)]])
    server = DiscordAgentServer(agent, token="test-token", command_prefix="!ask ")
    attach_fake_client(server)
    channel = FakeChannel()
    await server.handle_message(
        FakeMessage("!ask what is up", channel, FakeUser("user-1"))
    )
    report.check("command prefix triggers", channel.posted() == ["Prefix reply."])
    report.check("prefix stripped from query", agent.queries[0][0] == "what is up", str(agent.queries))

    agent = ScriptedAgent([[ScriptedEvent("nope", final=True)]])
    server = DiscordAgentServer(agent, token="test-token")
    attach_fake_client(server)
    channel = FakeChannel()
    bot_msg = user_message("hi", channel, author_id="other-bot")
    bot_msg.author.bot = True
    await server.handle_message(bot_msg)
    report.check("messages from other bots ignored", channel.sent == [])

    # --- streaming and formatting ------------------------------------------
    print("\nStreaming & formatting")

    agent = ScriptedAgent([[
        ScriptedEvent("Calling tools: search_news"),
        ScriptedEvent("Here are the headlines.", final=True),
    ]])
    server = DiscordAgentServer(agent, token="test-token")
    attach_fake_client(server)
    channel = FakeChannel()
    await server.handle_message(user_message("news?", channel))
    report.check("working update posted", any("Calling tools" in c for _, c in channel.log))
    report.check("working update removed", channel.sent[0].deleted is True)
    report.check("final answer remains", channel.posted() == ["Here are the headlines."])

    agent = ScriptedAgent([[ScriptedEvent("<think>secret reasoning</think>Clean answer.", final=True)]])
    server = DiscordAgentServer(agent, token="test-token")
    attach_fake_client(server)
    channel = FakeChannel()
    await server.handle_message(user_message("think", channel))
    report.check("<think> content stripped", channel.posted() == ["Clean answer."], str(channel.posted()))

    long_text = "\n".join(f"line {i} " + "x" * 60 for i in range(120))
    agent = ScriptedAgent([[ScriptedEvent(long_text, final=True)]])
    server = DiscordAgentServer(agent, token="test-token")
    attach_fake_client(server)
    channel = FakeChannel()
    await server.handle_message(user_message("long", channel))
    chunks = channel.posted()
    report.check("long answer split into several messages", len(chunks) > 1, f"{len(chunks)} chunk(s)")
    report.check(
        "every chunk within Discord's limit",
        all(len(c) <= DISCORD_MESSAGE_LIMIT for c in chunks),
        str([len(c) for c in chunks]),
    )
    report.check("no text lost while splitting", "line 119" in chunks[-1])

    # --- sessions -----------------------------------------------------------
    print("\nSessions")

    agent = ScriptedAgent([
        [ScriptedEvent("a", final=True)],
        [ScriptedEvent("b", final=True)],
    ])
    server = DiscordAgentServer(agent, token="test-token")
    attach_fake_client(server)
    await server.handle_message(user_message("one", FakeChannel("chan-A")))
    await server.handle_message(user_message("two", FakeChannel("chan-B")))
    sessions = [session for _, session in agent.queries]
    report.check(
        "each channel gets its own session",
        sessions == ["discord-channel-chan-A", "discord-channel-chan-B"],
        str(sessions),
    )

    agent = ScriptedAgent([
        [ScriptedEvent("a", final=True)],
        [ScriptedEvent("b", final=True)],
    ])
    server = DiscordAgentServer(agent, token="test-token", session_scope="user")
    attach_fake_client(server)
    await server.handle_message(user_message("one", FakeChannel("chan-A"), author_id="u9"))
    await server.handle_message(user_message("two", FakeChannel("chan-B"), author_id="u9"))
    sessions = [session for _, session in agent.queries]
    report.check(
        "user scope keeps one session across channels",
        sessions == ["discord-user-u9", "discord-user-u9"],
        str(sessions),
    )

    # --- tool approval ------------------------------------------------------
    print("\nTool approval")

    tool = {"id": "fc-1", "name": "delete_files", "args": {"path": "/tmp/x"}}

    agent = ScriptedAgent([
        [ScriptedEvent(tool=tool)],
        [ScriptedEvent("Tool ran, done.", final=True)],
    ])
    server = DiscordAgentServer(agent, token="test-token")
    attach_fake_client(server, reaction_provider=lambda: _immediate(APPROVE_EMOJI))
    channel = FakeChannel()
    await server.handle_message(user_message("delete stuff", channel))
    prompt = channel.sent[0]
    report.check("approval prompt names the tool", "delete_files" in prompt.content)
    report.check("approval prompt shows the args", "/tmp/x" in prompt.content)
    report.check("bot's own reactions withdrawn once answered", prompt.reactions == [],
                 str(prompt.reactions))
    report.check("outcome recorded on the prompt", "✅ Approved" in prompt.content)
    report.check("approval forwarded to agent", agent.confirmations == [("fc-1", "discord-channel-chan-1", True)],
                 str(agent.confirmations))
    report.check("agent resumed after approval", channel.posted()[-1] == "Tool ran, done.")

    agent = ScriptedAgent([
        [ScriptedEvent(tool=tool)],
        [ScriptedEvent("Skipped that.", final=True)],
    ])
    server = DiscordAgentServer(agent, token="test-token")
    attach_fake_client(server, reaction_provider=lambda: _immediate(DENY_EMOJI))
    channel = FakeChannel()
    await server.handle_message(user_message("delete stuff", channel))
    report.check("denial forwarded to agent", agent.confirmations[0][2] is False, str(agent.confirmations))

    agent = ScriptedAgent([
        [ScriptedEvent(tool=tool)],
        [ScriptedEvent("Tool ran, done.", final=True)],
    ])
    server = DiscordAgentServer(agent, token="test-token")
    attach_fake_client(server, reply_text="yes")
    channel = FakeChannel()
    await server.handle_message(user_message("delete stuff", channel))
    report.check("typed 'yes' approves too", agent.confirmations[0][2] is True,
                 str(agent.confirmations))

    agent = ScriptedAgent([
        [ScriptedEvent(tool=tool)],
        [ScriptedEvent("Skipped.", final=True)],
    ])
    server = DiscordAgentServer(agent, token="test-token")
    attach_fake_client(server, reply_text="no")
    channel = FakeChannel()
    await server.handle_message(user_message("delete stuff", channel))
    report.check("typed 'no' denies", agent.confirmations[0][2] is False)

    agent = ScriptedAgent([[ScriptedEvent("should not run", final=True)]])
    server = DiscordAgentServer(agent, token="test-token")
    attach_fake_client(server)
    channel = FakeChannel()
    server._pending_decisions[channel.id] = "user-1"
    await server.handle_message(user_message("yes", channel, mention_bot=False, guild=None))
    report.check("approval reply is not re-sent as a new question",
                 agent.queries == [], str(agent.queries))

    agent = ScriptedAgent([
        [ScriptedEvent(tool=tool)],
        [ScriptedEvent("Denied by timeout.", final=True)],
    ])
    server = DiscordAgentServer(agent, token="test-token", tool_confirmation_timeout=0.01)
    attach_fake_client(server)  # no reaction provider → wait_for times out
    channel = FakeChannel()
    await server.handle_message(user_message("delete stuff", channel))
    report.check("timeout denies the call", agent.confirmations[0][2] is False)
    report.check("timeout explained in channel", any("timed out" in c for c in channel.posted()))

    # --- failure handling ---------------------------------------------------
    print("\nFailure handling")

    class BrokenAgent(ScriptedAgent):
        async def run_query(self, query, session_id=None):
            raise RuntimeError("model endpoint unreachable")
            yield  # pragma: no cover

    server = DiscordAgentServer(BrokenAgent([]), token="test-token")
    attach_fake_client(server)
    channel = FakeChannel()
    await server.handle_message(user_message("boom", channel))
    report.check("agent errors reported in channel",
                 any("model endpoint unreachable" in c for c in channel.posted()),
                 str(channel.posted()))

    server = DiscordAgentServer(ScriptedAgent([[]]), token="test-token")
    attach_fake_client(server)
    channel = FakeChannel()
    await server.handle_message(user_message("silence", channel))
    report.check("empty agent run reported",
                 any("without producing a response" in c for c in channel.posted()))

    try:
        os.environ.pop("DISCORD_BOT_TOKEN", None)
        DiscordAgentServer(ScriptedAgent([]))
        report.check("missing token raises ValueError", False, "no exception raised")
    except ValueError:
        report.check("missing token raises ValueError", True)

    print(f"\n{report.passed} passed, {report.failed} failed\n")
    return 1 if report.failed else 0


async def _immediate(emoji: str) -> str:
    return emoji


# ---------------------------------------------------------------------------
# Credential preflight — fail before the REPL rather than mid-conversation
# ---------------------------------------------------------------------------

# provider → (env vars, any one of which is enough)
_PROVIDER_KEYS = {
    "google": ("GOOGLE_API_KEY", "GEMINI_API_KEY"),
    "google_genai": ("GOOGLE_API_KEY", "GEMINI_API_KEY"),
    "openai": ("OPENAI_API_KEY",),
    "anthropic": ("ANTHROPIC_API_KEY",),
    "vllm": (),      # local server, no key
    "litellm": (),
}


def load_config(config_path: str) -> dict:
    import yaml

    if not os.path.exists(config_path):
        raise FileNotFoundError(f"Config not found: {config_path}")
    with open(config_path) as handle:
        return yaml.safe_load(handle) or {}


def load_dotenv_if_present() -> None:
    """Pick up keys from a .env next to the example or at the repo root."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    here = os.path.dirname(os.path.abspath(__file__))
    for candidate in (os.path.join(here, ".env"), os.path.join(here, "..", "..", ".env")):
        if os.path.exists(candidate):
            load_dotenv(candidate)
            print(f"loaded environment from {os.path.normpath(candidate)}")


def preflight(config: dict) -> List[str]:
    """Return human-readable problems that would break the first model call."""
    problems: List[str] = []

    backend = str(config.get("backend", "adk")).lower()
    default_provider = "anthropic" if backend in ("langchain", "deepagents") else "google"
    provider = str(config.get("provider") or default_provider).lower()
    model = config.get("model", "<default>")

    expected = _PROVIDER_KEYS.get(provider)
    if expected is None:
        problems.append(f"Unknown provider '{provider}' in the config.")
    elif expected and not any(os.environ.get(var) for var in expected):
        options = " or ".join(expected)
        problems.append(
            f"Provider '{provider}' (model {model}) needs {options}.\n"
            f"      export {expected[0]}=...\n"
            f"      …or edit {config.get('name', 'the agent')}'s config to a provider you have a key for "
            f"(agent.yaml lists OpenAI and vLLM alternatives)."
        )

    if "tavily_search" in (config.get("tools") or []) and not os.environ.get("TAVILY_API_KEY"):
        problems.append(
            "Tool 'tavily_search' needs TAVILY_API_KEY.\n"
            "      export TAVILY_API_KEY=...   (or drop tavily_search from the tools list)"
        )

    return problems


def check_credentials(config_path: str) -> Optional[dict]:
    """Load the config and verify credentials; None means "stop, told the user why"."""
    load_dotenv_if_present()
    config = load_config(config_path)
    problems = preflight(config)
    if problems:
        print(f"\nCannot start — {config_path} is missing credentials:\n", file=sys.stderr)
        for problem in problems:
            print(f"  •  {problem}", file=sys.stderr)
        print(
            "\nNo keys handy? Run the self-test instead — it exercises the whole "
            "Discord path with a stub agent:\n    python run_discord_bot.py\n",
            file=sys.stderr,
        )
        return None
    return config


# ---------------------------------------------------------------------------
# local mode — real agent, fake Discord, terminal input
# ---------------------------------------------------------------------------

async def local(config_path: str) -> int:
    from declarative_agent_sdk import AgentFactory, AgentRegistry

    if check_credentials(config_path) is None:
        return 1

    agent = AgentFactory.from_yaml_file(config_path)
    # Required: the ADK before_model_callback resolves the agent's context
    # through AgentRegistry, so an unregistered agent fails on its first call.
    AgentRegistry.register(agent, category="discord")

    server = DiscordAgentServer(agent, token="local-test-token")

    async def ask_terminal() -> str:
        answer = await asyncio.to_thread(input, "  approve tool call? [y/N]: ")
        return APPROVE_EMOJI if answer.strip().lower().startswith("y") else DENY_EMOJI

    attach_fake_client(server, reaction_provider=ask_terminal)
    channel = FakeChannel("local", echo=True)

    print(f"\nLocal Discord simulation for agent '{agent.name}'. Ctrl-C or 'quit' to exit.\n")
    while True:
        try:
            text = await asyncio.to_thread(input, "you > ")
        except (EOFError, KeyboardInterrupt):
            break
        text = text.strip()
        if not text:
            continue
        if text in ("quit", "exit"):
            break
        await server.handle_message(user_message(text, channel))

    print("\nbye")
    return 0


# ---------------------------------------------------------------------------
# connect mode — real Discord, no agent
# ---------------------------------------------------------------------------

def connect() -> int:
    """
    Log in to Discord with the token, report what the bot can see, log out.
    Verifies the token, the MESSAGE CONTENT intent and channel permissions
    without involving the agent or an LLM key.
    """
    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token:
        print("DISCORD_BOT_TOKEN is not set — export it first.", file=sys.stderr)
        return 1

    try:
        import discord
    except ImportError:
        print('discord.py is not installed — pip install "declarative-agent-sdk[discord]"',
              file=sys.stderr)
        return 1

    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents)
    result = {"ok": False}

    @client.event
    async def on_ready() -> None:
        print(f"\nconnected as {client.user}  (id {client.user.id})")
        guilds = list(client.guilds)
        if not guilds:
            print("\n  This bot is not in any server yet — use the invite URL "
                  "from the README to add it to one.")
        for guild in guilds:
            print(f"\n  server: {guild.name}")
            me = guild.me
            usable = [
                channel for channel in guild.text_channels
                if channel.permissions_for(me).send_messages
                and channel.permissions_for(me).read_message_history
            ]
            if usable:
                for channel in usable[:10]:
                    reactions = channel.permissions_for(me).add_reactions
                    note = "" if reactions else "   (cannot add reactions — tool approval won't work here)"
                    print(f"    can post in #{channel.name}{note}")
                if len(usable) > 10:
                    print(f"    … and {len(usable) - 10} more")
            else:
                print("    no channels it can post in — check the role's permissions")
        print(f"\n  mention it as <@{client.user.id}> in any channel above, "
              "or DM it, once you start live mode.\n")
        result["ok"] = True
        await client.close()

    try:
        client.run(token)
    except discord.PrivilegedIntentsRequired:
        print(
            "\nDiscord refused the MESSAGE CONTENT intent.\n"
            "  Developer portal → your app → Bot → Privileged Gateway Intents →\n"
            "  enable MESSAGE CONTENT INTENT, then save and retry.\n",
            file=sys.stderr,
        )
        return 1
    except discord.LoginFailure:
        print("\nDiscord rejected the token. Copy a fresh one from "
              "Developer portal → Bot → Reset Token.\n", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"\nCould not connect: {exc}\n", file=sys.stderr)
        return 1

    return 0 if result["ok"] else 1


# ---------------------------------------------------------------------------
# live mode — real Discord
# ---------------------------------------------------------------------------

def live(config_path: str) -> int:
    from declarative_agent_sdk import AgentFactory, AgentRegistry

    if check_credentials(config_path) is None:
        return 1

    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token:
        print("DISCORD_BOT_TOKEN is not set — export it before running live mode.", file=sys.stderr)
        return 1

    agent = AgentFactory.from_yaml_file(config_path)
    # Required: see the comment in local() — the agent must be in the registry.
    AgentRegistry.register(agent, category="discord")

    server = DiscordAgentServer(
        agent,
        token=token,
        command_prefix="!ask ",
        activity_status=f"{agent.name} — mention me",
    )
    print(f"Connecting agent '{agent.name}' to Discord… (Ctrl-C to stop)")
    server.run()
    return 0


# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Test program for DiscordAgentServer")
    parser.add_argument("--mode", choices=("selftest", "local", "connect", "live"),
                        default="selftest")
    parser.add_argument("--config", default="agent.yaml",
                        help="Agent YAML config (local and live modes)")
    args = parser.parse_args()

    if args.mode == "selftest":
        return asyncio.run(selftest())
    if args.mode == "local":
        return asyncio.run(local(args.config))
    if args.mode == "connect":
        load_dotenv_if_present()
        return connect()
    return live(args.config)


if __name__ == "__main__":
    raise SystemExit(main())
