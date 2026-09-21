#!/usr/bin/env python3
"""Run an agent with async streaming and interactive tool approval.

Usage:
    cd examples/simple_agent
    export OPENAI_API_KEY=sk-...
    python run_stream.py
    python run_stream.py "Run a command to list current directory files"
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
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

from declarative_agent_sdk import AgentFactory, BaseAgent


async def process_turn(agent: BaseAgent, query: str, session_id: str):
    """Execute a single query, handling streaming events and tool confirmations."""
    print(f"\nUser: {query}\n" + "-" * 50)

    pending_confirmation = False

    async for event in agent.run_query(query, session_id=session_id):
        # 1. Tool approval pause
        if event.long_running_tool_ids or event.kind == "tool_approval":
            call_id = (
                event.long_running_tool_ids[0]
                if event.long_running_tool_ids
                else (event.tool_call_id or "unknown")
            )
            name = event.tool_name or "tool"
            args = event.tool_args or {}

            print(f"\n⚠️  [TOOL APPROVAL REQUIRED]")
            print(f"    Tool: {name}")
            print(f"    Args: {args}")

            decision = input("    Approve execution? [y/N]: ").strip().lower()
            approved = decision in ("y", "yes")

            print(f"    Decision: {'Approved ✅' if approved else 'Denied ❌'}")
            print("    Resuming agent...\n")

            # Resume the agent loop
            async for resume_ev in agent.tool_confirmation(call_id, session_id, yes=approved):
                if resume_ev.kind == "status":
                    print(f"    [status] {resume_ev.text}")
                elif resume_ev.is_final_response():
                    text = (
                        resume_ev.content.parts[0].text
                        if resume_ev.content and resume_ev.content.parts
                        else resume_ev.text
                    )
                    print(f"\nAssistant:\n{text}\n")
            pending_confirmation = True
            break

        # 2. Intermediate status or thinking
        elif event.kind == "status":
            print(f"    💭 {event.text}")

        # 3. Final response
        elif event.is_final_response():
            text = (
                event.content.parts[0].text
                if event.content and event.content.parts
                else event.text
            )
            print(f"\nAssistant:\n{text}\n")


async def main():
    config_file = Path(__file__).parent / "agent.yaml"
    agent = AgentFactory.from_yaml_file(str(config_file))
    session_id = f"cli-session-{uuid.uuid4().hex[:8]}"

    print(f"Loaded agent: '{agent.name}' (session: {session_id})")

    # If query passed as CLI argument, run once and exit
    if len(sys.argv) > 1:
        query = " ".join(sys.argv[1:])
        await process_turn(agent, query, session_id)
        return

    # Interactive REPL mode
    print("\nInteractive Chat (type 'exit' or 'quit' to stop):")
    print("=" * 50)
    while True:
        try:
            query = input("\nYou: ").strip()
            if not query:
                continue
            if query.lower() in ("exit", "quit"):
                print("Goodbye!")
                break
            await process_turn(agent, query, session_id)
        except (KeyboardInterrupt, EOFError):
            print("\nExiting.")
            break


if __name__ == "__main__":
    asyncio.run(main())
