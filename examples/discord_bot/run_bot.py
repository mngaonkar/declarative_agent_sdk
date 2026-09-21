#!/usr/bin/env python3
"""Run an agent as a live Discord bot.

Usage:
    cd examples/discord_bot
    export DISCORD_BOT_TOKEN="your-bot-token"
    export OPENAI_API_KEY="your-openai-api-key"
    python run_bot.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Add project root to sys.path if running directly
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# Load .env if present
for env_path in (Path(__file__).parent / ".env", _ROOT / ".env"):
    if env_path.is_file():
        try:
            from dotenv import load_dotenv
            load_dotenv(env_path)
        except ImportError:
            pass
        break

from declarative_agent_sdk import AgentFactory, AgentRegistry, DiscordAgentServer


def main():
    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token:
        print("ERROR: DISCORD_BOT_TOKEN environment variable not set.")
        print("Set your bot token with: export DISCORD_BOT_TOKEN='...'")
        sys.exit(1)

    config_path = Path(__file__).parent / "agent.yaml"
    print(f"Loading agent configuration from: {config_path}")
    agent = AgentFactory.from_yaml_file(str(config_path))
    AgentRegistry.register(agent, category="discord")

    print(f"Starting DiscordAgentServer for '{agent.name}'...")
    print("Bot will respond to @mentions, direct messages (DMs), and the '!ask ' prefix.")

    server = DiscordAgentServer(
        agent=agent,
        token=token,
        command_prefix="!ask ",
        activity_status=f"{agent.name} | mention me to ask",
    )
    server.run()


if __name__ == "__main__":
    main()
