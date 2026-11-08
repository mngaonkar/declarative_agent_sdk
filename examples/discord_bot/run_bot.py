#!/usr/bin/env python3
"""Run this example agent as a Discord bot.

A thin wrapper around the packaged CLI, kept so the example runs straight from
a checkout.  Once the SDK is installed these are equivalent:

    python run_bot.py --check
    declarative-agent discord --config agent.yaml --check

Usage:
    cd examples/discord_bot
    export DISCORD_BOT_TOKEN="your-bot-token"
    export OPENAI_API_KEY="your-openai-api-key"
    python run_bot.py            # serve this agent on Discord
    python run_bot.py --check    # verify token, intent and permissions
"""

from __future__ import annotations

import sys
from pathlib import Path

# Add project root to sys.path if running directly from a checkout.
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from declarative_agent_sdk.cli.main import main

_HERE = Path(__file__).parent


if __name__ == "__main__":
    argv = sys.argv[1:]
    # Default to this example's config unless the caller named one.
    if not any(a == "--config" or a.startswith("--config=") for a in argv):
        argv = ["--config", str(_HERE / "agent.yaml")] + argv
    raise SystemExit(main(["discord"] + argv))
