#!/usr/bin/env python3
"""Command-line entry point for the Declarative Agent SDK.

    declarative-agent discord --config agent.yaml   # serve an agent on Discord
    declarative-agent discord --check               # verify the Discord side
    declarative-agent version

Credentials come from the environment, or from a .env next to the agent config
or in the working directory.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys
from pathlib import Path
from typing import Any, List, Optional

DEFAULT_CONFIG_NAME = "agent.yaml"

# Which environment variable each provider needs for its API key.
_PROVIDER_KEYS = {
    "google": ("GOOGLE_API_KEY", "GEMINI_API_KEY"),
    "google_genai": ("GOOGLE_API_KEY", "GEMINI_API_KEY"),
    "openai": ("OPENAI_API_KEY",),
    "anthropic": ("ANTHROPIC_API_KEY",),
    "vllm": (),      # local server; lean substitutes a placeholder key
    # litellm reads OPENAI_API_KEY and gets no placeholder — see
    # agents/lean/agent.py:_resolve_api_key.
    "litellm": ("OPENAI_API_KEY",),
}

_DEFAULT_PROVIDER = {"lean": "openai", "adk": "google", "deepagent": "anthropic"}


# ---------------------------------------------------------------------------
# Configuration and credentials
# ---------------------------------------------------------------------------

def resolve_config_path(explicit: Optional[str]) -> Path:
    """The agent YAML to use: --config, else ./agent.yaml."""
    if explicit:
        return Path(explicit).expanduser()
    return Path.cwd() / DEFAULT_CONFIG_NAME


def load_dotenv_if_present(env_file: Optional[str], config_path: Optional[Path]) -> None:
    """Load the first .env found: --env-file, the config's directory, then cwd."""
    candidates: List[Path] = []
    if env_file:
        candidates.append(Path(env_file).expanduser())
    if config_path is not None:
        candidates.append(config_path.expanduser().resolve().parent / ".env")
    candidates.append(Path.cwd() / ".env")

    found = next((p for p in candidates if p.is_file()), None)

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
    elif env_file:
        print(f"note: --env-file {env_file} does not exist", file=sys.stderr)


def preflight(config: dict, *, require_discord_token: bool = False) -> List[str]:
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
            f"      ...or point the config at a provider you have a key for."
        )

    if "tavily_search" in (config.get("tools") or []) and not os.environ.get("TAVILY_API_KEY"):
        problems.append(
            "Tool 'tavily_search' needs TAVILY_API_KEY.\n"
            "      export TAVILY_API_KEY=...   (or drop tavily_search from the tools list)"
        )

    if require_discord_token and not os.environ.get("DISCORD_BOT_TOKEN"):
        problems.append(
            "DISCORD_BOT_TOKEN is not set.\n"
            "      export DISCORD_BOT_TOKEN=...   "
            "(Developer portal -> your app -> Bot -> Reset Token)"
        )

    return problems


def load_checked_config(config_path: Path, *, require_discord_token: bool) -> Optional[dict]:
    """Load the config and verify credentials; None means "stop, told them why"."""
    from declarative_agent_sdk.core.agent_factory import AgentFactory

    try:
        # load_config applies the same relative-path resolution as from_yaml_file,
        # so instruction_file / skills_directory / workspace_directory come back
        # resolved against the config's own directory.
        config = AgentFactory.load_config(str(config_path))
    except FileNotFoundError:
        print(f"\nConfig not found: {config_path}\n"
              f"  Pass --config /path/to/agent.yaml, or run from a directory "
              f"containing {DEFAULT_CONFIG_NAME}.\n",
              file=sys.stderr)
        return None

    problems = preflight(config, require_discord_token=require_discord_token)
    if problems:
        print(f"\nCannot start — {config_path.name} is missing credentials:\n", file=sys.stderr)
        for problem in problems:
            print(f"  -  {problem}", file=sys.stderr)
        print(file=sys.stderr)
        return None
    return config


# ---------------------------------------------------------------------------
# discord --check : verify the Discord side without starting the agent
# ---------------------------------------------------------------------------

def check_discord() -> int:
    """Log in, report what the bot can see, log out."""
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
        # discord.py swallows exceptions raised in event handlers, so anything
        # that escapes here would leave the client connected and client.run()
        # blocking forever.  Always close.
        try:
            report_access(client)
            result["ok"] = True
        except Exception as exc:
            print(f"\nConnected, but could not report the bot's access: {exc}", file=sys.stderr)
        finally:
            await client.close()

    if _login(client, token) != 0:
        return 1
    return 0 if result["ok"] else 1


def report_access(client: Any) -> None:
    """Print the bot's identity and where it can post."""
    print(f"\nconnected as {client.user}  (id {client.user.id})")
    guilds = list(client.guilds)
    if not guilds:
        print("\n  This bot is not in any server yet — invite it to one first.")
    for guild in guilds:
        print(f"\n  server: {guild.name}")
        me = guild.me
        if me is None:
            # Possible when the members intent is off and the cache is cold.
            print("    cannot read this bot's own membership — skipping permission check")
            continue
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
                print(f"    ... and {len(usable) - 10} more")
        else:
            print("    no channels it can post in — check the role's permissions")
    print(f"\n  mention it as <@{client.user.id}> in any channel above, "
          "or DM it, once you start the bot.\n")


def _login(client: Any, token: str) -> int:
    """Blocking client.run with Discord's setup failures explained."""
    import discord

    try:
        # log_handler=None: without it discord.py installs its own root handler
        # on top of the SDK's and every line is printed twice.
        client.run(token, log_handler=None)
    except discord.PrivilegedIntentsRequired:
        print(
            "\nDiscord refused the MESSAGE CONTENT intent.\n"
            "  Developer portal -> your app -> Bot -> Privileged Gateway Intents ->\n"
            "  enable MESSAGE CONTENT INTENT, then save and retry.\n",
            file=sys.stderr,
        )
        return 1
    except discord.LoginFailure:
        print("\nDiscord rejected the token. Copy a fresh one from "
              "Developer portal -> Bot -> Reset Token.\n", file=sys.stderr)
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

        print("\nstopping...")
        await server.close()
        try:
            await asyncio.wait_for(bot, timeout=10)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
        return 0
    except discord.PrivilegedIntentsRequired:
        print(
            "\nDiscord refused the MESSAGE CONTENT intent.\n"
            "  Developer portal -> your app -> Bot -> Privileged Gateway Intents ->\n"
            "  enable MESSAGE CONTENT INTENT, then save and retry.\n",
            file=sys.stderr,
        )
        return 1
    except discord.LoginFailure:
        print("\nDiscord rejected the token. Copy a fresh one from "
              "Developer portal -> Bot -> Reset Token.\n", file=sys.stderr)
        return 1
    except Exception as exc:
        # Network drop, gateway 5xx, ... — name it instead of dumping a traceback.
        print(f"\nDiscord connection failed: {type(exc).__name__}: {exc}\n", file=sys.stderr)
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


def cmd_discord(args: argparse.Namespace) -> int:
    from declarative_agent_sdk import AgentFactory, AgentRegistry, DiscordAgentServer
    from declarative_agent_sdk.core.agent_factory import resolve_agent_framework

    if args.check:
        return check_discord()

    config_path = resolve_config_path(args.config)
    config = load_checked_config(config_path, require_discord_token=True)
    if config is None:
        return 1

    framework = resolve_agent_framework(config)
    print(f"Loading agent configuration from: {config_path}")
    agent = AgentFactory.from_dict(config)
    # Required for ADK: before_model_callback resolves context via AgentRegistry.
    # Harmless for lean and deepagent; keep for a single code path.
    AgentRegistry.register(agent, category="discord")

    server = DiscordAgentServer(
        agent=agent,
        token=os.environ["DISCORD_BOT_TOKEN"],
        command_prefix=args.prefix,
        activity_status=args.activity or f"{agent.name} | mention me to ask",
        format_markdown=True,
        reply_as_embed=args.embed,
        # So files the agent writes are found regardless of the working directory.
        workspace_dir=config.get("workspace_directory"),
    )

    print(f"Connecting agent '{agent.name}' (agent_framework={framework}) to Discord...")
    print(f"It will respond to @mentions, direct messages (DMs), "
          f"and the '{args.prefix}' prefix.")
    print("Ctrl-C to stop.")
    return asyncio.run(serve(server))


def cmd_version(_args: argparse.Namespace) -> int:
    from declarative_agent_sdk.__version__ import __version__

    print(f"declarative-agent {__version__} (python {sys.version.split()[0]})")
    return 0


def cmd_a2a(args: argparse.Namespace) -> int:
    print("The a2a transport is not yet exposed through this CLI.\n"
          "Use declarative_agent_sdk.AIAgentServer directly for now.",
          file=sys.stderr)
    return 2


# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="declarative-agent",
        description="Serve a YAML-defined agent over a transport.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    discord_cmd = sub.add_parser("discord", help="Serve an agent as a Discord bot")
    discord_cmd.add_argument("--config",
                             help=f"Agent YAML config (default: ./{DEFAULT_CONFIG_NAME})")
    discord_cmd.add_argument("--check", action="store_true",
                             help="Verify the token, intent and channel permissions, then exit")
    discord_cmd.add_argument("--env-file",
                             help="Explicit .env to load (default: beside the config, then ./.env)")
    discord_cmd.add_argument("--prefix", default="!ask ",
                             help="Command prefix that also triggers the agent (default: '!ask ')")
    discord_cmd.add_argument("--activity", default=None,
                             help="Text shown as the bot's 'Playing ...' status")
    discord_cmd.add_argument("--embed", action="store_true",
                             help="Post answers as an embed card instead of plain messages")
    discord_cmd.add_argument("--log-level", default=None,
                             choices=("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"),
                             type=str.upper,
                             help="SDK log level (default: INFO, or $SDK_LOG_LEVEL)")
    discord_cmd.set_defaults(func=cmd_discord)

    a2a_cmd = sub.add_parser("a2a", help="Serve an agent over A2A / JSON-RPC (not yet wired up)")
    a2a_cmd.add_argument("--config", help=f"Agent YAML config (default: ./{DEFAULT_CONFIG_NAME})")
    a2a_cmd.set_defaults(func=cmd_a2a)

    version_cmd = sub.add_parser("version", help="Print the SDK version")
    version_cmd.set_defaults(func=cmd_version)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command != "version":
        load_dotenv_if_present(getattr(args, "env_file", None),
                               resolve_config_path(getattr(args, "config", None)))

    if getattr(args, "log_level", None):
        # force=True is required: importing the SDK already configured logging,
        # so setup_logging() would otherwise early-return and do nothing.
        from declarative_agent_sdk.core.agent_logging import setup_logging
        setup_logging(level=args.log_level, force=True)

    return args.func(args)


def run() -> None:
    """Entry point for callers that ignore ``main()``'s return value.

    A caller that invokes ``module.attr()`` and discards the result would make
    the process always exit 0 no matter what went wrong.  Raising ``SystemExit``
    carries the code out.  Setuptools' console script wraps ``main()`` in
    ``sys.exit()`` itself, so it can use either.
    """
    raise SystemExit(main())


if __name__ == "__main__":
    run()
