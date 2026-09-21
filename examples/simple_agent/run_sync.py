#!/usr/bin/env python3
"""Run an agent synchronously using BaseAgent.run_sync().

Usage:
    cd examples/simple_agent
    export OPENAI_API_KEY=sk-...
    python run_sync.py
    python run_sync.py "What is 42 * 1337?"
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

from declarative_agent_sdk import AgentFactory


def main():
    config_file = Path(__file__).parent / "agent.yaml"
    query = sys.argv[1] if len(sys.argv) > 1 else "Introduce yourself in one concise sentence."

    print(f"Loading agent from: {config_file.name}")
    agent = AgentFactory.from_yaml_file(str(config_file))
    print(f"Agent '{agent.name}' loaded (runtime: {getattr(agent, '_framework', 'lean')})")
    print(f"\nUser Query: {query}\n" + "-" * 50)

    # Synchronous execution
    response = agent.run_sync(query)

    print("\nAgent Response:\n" + "=" * 50)
    print(response)
    print("=" * 50)


if __name__ == "__main__":
    main()
