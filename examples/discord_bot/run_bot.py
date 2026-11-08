#!/usr/bin/env python3
"""Run an agent as a live Discord bot.

Usage:
    cd examples/discord_bot
    export DISCORD_BOT_TOKEN="your-bot-token"
    export OPENAI_API_KEY="your-openai-api-key"

    python run_bot.py                      # serve the agent on Discord
    python run_bot.py --check              # verify token, intent and permissions, then exit
    python run_bot.py --config other.yaml  # a different agent config
    python run_bot.py --log-level DEBUG    # verbose SDK logging

Credentials are read from the environment, or from a .env next to this file
or at the repository root.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys
from pathlib import Path
from typing import Any, List, Optional

# Add project root to sys.path if running directly
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_HERE = Path(__file__).parent


# ---------------------------------------------------------------------------
# Configuration and credentials
# ---------------------------------------------------------------------------

_PROVIDER_KEYS = {
    "google": ("GOOGLE_API_KEY", "GEMINI_API_KEY"),
    "google_genai": ("GOOGLE_API_KEY", "GEMINI_API_KEY"),
    "openai": ("OPENAI_API_KEY",),
    "anthropic": ("ANTHROPIC_API_KEY",),
    "vllm": (),      # local server, no key
    "litellm": (),
}

_DEFAULT_PROVIDER = {"lean": "openai", "adk": "google", "deepagent": "anthropic"}


def load_dotenv_if_present() -> None:
    """Pick up keys from a .env next to this example or at the repo root."""
    found = next((p for p in (_HERE / ".env", _ROOT / ".env") if p.is_file()), None)

    try:
        from dotenv import load_dotenv
    except ImportError:
        # Say so rather than silently ignoring a .env the user expects to be read.
        if found is not None:
            print(f"note: {found} is NOT being read — python-dotenv is not installed "
                  f"for {sys.executable}.\n"
                  f"      pip install python-dotenv, or export the keys yourself.",
                  file=sys.stderr)
        return

    if found is not None:
        load_dotenv(found)
        print(f"loaded environment from {found}")


def load_config(config_path: Path) -> dict:
    import yaml

    if not config_path.is_file():
        raise FileNotFoundError(f"Config not found: {config_path}")
    with open(config_path) as handle:
        return yaml.safe_load(handle) or {}


def preflight(config: dict) -> List[str]:
    """Return human-readable problems that would break the first model call."""
    from declarative_agent_sdk.core.agent_factory import resolve_agent_framework

    problems: List[str] = []

    try:
        framework = resolve_agent_framework(config)
    except ValueError as exc:
        problems.append(str(exc))
        framework = "lean"

    provider = str(config.get("provider") or _DEFAULT_PROVIDER[framework]).lower()
    model = config.get("model", "<default>")

    expected = _PROVIDER_KEYS.get(provider)
    if expected is None:
        problems.append(f"Unknown provider '{provider}' in the config.")
    elif expected and not any(os.environ.get(var) for var in expected):
        options = " or ".join(expected)
        problems.append(
            f"Provider '{provider}' (model {model}, agent_framework={framework}) needs {options}.\n"
            f"      export {expected[0]}=...\n"
            f"      …or edit the config to a provider you have a key for "
            f"(agent.yaml lists OpenAI, Anthropic, and vLLM alternatives)."
        )

    if "tavily_search" in (config.get("tools") or []) and not os.environ.get("TAVILY_API_KEY"):
        problems.append(
            "Tool 'tavily_search' needs TAVILY_API_KEY.\n"
            "      export TAVILY_API_KEY=...   (or drop tavily_search from the tools list)"
        )

    if not os.environ.get("DISCORD_BOT_TOKEN"):
        problems.append(
            "DISCORD_BOT_TOKEN is not set.\n"
            "      export DISCORD_BOT_TOKEN=...   (Developer portal → your app → Bot → Reset Token)"
        )

    return problems


def check_credentials(config_path: Path) -> Optional[dict]:
    """Load the config and verify credentials; None means "stop, told the user why"."""
    try:
        config = load_config(config_path)
    except FileNotFoundError:
        print(f"\nConfig not found: {config_path}\n"
              f"  Pass --config /path/to/agent.yaml, or run from the example directory.\n",
              file=sys.stderr)
        return None
    problems = preflight(config)
    if problems:
        print(f"\nCannot start — {config_path.name} is missing credentials:\n", file=sys.stderr)
        for problem in problems:
            print(f"  •  {problem}", file=sys.stderr)
        print(file=sys.stderr)
        return None
    return config


# ---------------------------------------------------------------------------
# --check — verify the Discord side without starting the agent
# ---------------------------------------------------------------------------

def check_discord() -> int:
    """
    Log in with the token, report what the bot can see, log out.  Verifies the
    token, the MESSAGE CONTENT intent and channel permissions without involving
    the agent or an LLM key.
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
              "or DM it, once you start the bot.\n")
        result["ok"] = True
        await client.close()

    if _login(client, token) != 0:
        return 1
    return 0 if result["ok"] else 1


def _login(client: Any, token: str) -> int:
    """Blocking client.run with Discord's setup failures explained."""
    import discord

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
    return 0


# ---------------------------------------------------------------------------
# Serving
# ---------------------------------------------------------------------------

async def serve(server: Any) -> int:
    """Run the bot until Discord drops out or the process is signalled."""
    import discord

    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:      # Windows
            pass

    bot = asyncio.create_task(server.start())
    signalled = asyncio.create_task(stop.wait())
    try:
        done, _ = await asyncio.wait({bot, signalled}, return_when=asyncio.FIRST_COMPLETED)

        if bot in done:
            bot.result()                 # re-raise whatever ended the connection
            print("Discord connection closed.")
            return 1

        print("\nstopping…")
        await server.close()
        try:
            await asyncio.wait_for(bot, timeout=10)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
        return 0
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
    finally:
        signalled.cancel()
        # Idempotent, and required on the error paths above so discord.py's
        # HTTP session does not leak ("Unclosed connector").
        try:
            await server.close()
        except Exception:
            pass
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.remove_signal_handler(sig)
            except (NotImplementedError, RuntimeError):
                pass


def run_bot(config_path: Path) -> int:
    from declarative_agent_sdk import AgentFactory, AgentRegistry, DiscordAgentServer
    from declarative_agent_sdk.core.agent_factory import resolve_agent_framework

    config = check_credentials(config_path)
    if config is None:
        return 1

    framework = resolve_agent_framework(config)
    print(f"Loading agent configuration from: {config_path}")
    agent = AgentFactory.from_yaml_file(str(config_path))
    # Required for ADK: before_model_callback resolves context via AgentRegistry.
    # Harmless for lean and deepagent; keep for a single code path.
    AgentRegistry.register(agent, category="discord")

    server = DiscordAgentServer(
        agent=agent,
        token=os.environ["DISCORD_BOT_TOKEN"],
        command_prefix="!ask ",
        activity_status=f"{agent.name} | mention me to ask",
        # Convert GFM tables/HTML to Discord-friendly markdown; set
        # reply_as_embed=True if you prefer answers in an embed card.
        format_markdown=True,
        reply_as_embed=False,
    )

    print(f"Connecting agent '{agent.name}' (agent_framework={framework}) to Discord…")
    print("It will respond to @mentions, direct messages (DMs), and the '!ask ' prefix.")
    print("Ctrl-C to stop.")
    return asyncio.run(serve(server))


# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Serve a declarative agent as a Discord bot")
    parser.add_argument("--config", default=str(_HERE / "agent.yaml"),
                        help="Agent YAML config (default: agent.yaml next to this script)")
    parser.add_argument("--check", action="store_true",
                        help="Verify the token, intent and channel permissions, then exit")
    parser.add_argument("--log-level", default=None,
                        help="SDK log level: DEBUG, INFO, WARNING, ERROR (default: INFO)")
    args = parser.parse_args()

    load_dotenv_if_present()

    from declarative_agent_sdk.core.agent_logging import setup_logging
    setup_logging(**({"level": args.log_level.upper()} if args.log_level else {}))

    if args.check:
        return check_discord()
    return run_bot(Path(args.config))


if __name__ == "__main__":
    raise SystemExit(main())
