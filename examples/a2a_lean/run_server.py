#!/usr/bin/env python3
"""Serve the lean agent over A2A (JSON-RPC + agent card).

Usage:
    cd examples/a2a_lean
    export OPENAI_API_KEY=...
    python run_server.py
    # optional: HOST=127.0.0.1 PORT=8000 python run_server.py

Agent card:  http://127.0.0.1:8000/.well-known/agent-card.json  (or /agent-card)
JSON-RPC:    http://127.0.0.1:8000/
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Allow running from this directory without installing the package editable.
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# Load .env from example dir or repo root if present.
for env_path in (Path(__file__).parent / ".env", _ROOT / ".env"):
    if env_path.is_file():
        try:
            from dotenv import load_dotenv

            load_dotenv(env_path)
        except ImportError:
            pass
        break

from declarative_agent_sdk import AgentFactory, AgentRegistry, AIAgentServer
from declarative_agent_sdk.core.agent_logging import get_logger

logger = get_logger(__name__)


def main() -> None:
    os.chdir(Path(__file__).resolve().parent)
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))

    # Let AIAgentServer attach a local interface for this bind address.
    # (publish_url in agent.yaml is optional; empty interfaces → server fills them.)
    agent = AgentFactory.from_yaml_file("agent.yaml")
    if getattr(agent, "agent_card", None) is not None:
        interfaces = getattr(agent.agent_card, "supported_interfaces", None)
        if interfaces is not None:
            try:
                del interfaces[:]
            except Exception:
                pass

    AgentRegistry.register(agent, category="a2a")
    logger.info(
        "Starting lean A2A server name=%s framework=lean host=%s port=%s",
        agent.name,
        host,
        port,
    )
    print(f"Lean A2A agent '{agent.name}' on http://{host}:{port}/")
    print("Ctrl+C to stop. In another terminal: python run_client.py")
    AIAgentServer(agent, host=host, port=port).run()


if __name__ == "__main__":
    main()
