#!/usr/bin/env python3
"""Interactive A2A client for the lean agent server.

Usage:
    # terminal 1
    python run_server.py

    # terminal 2
    python run_client.py
    # or: AGENT_URL=http://127.0.0.1:8000 python run_client.py
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import uuid
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("a2a_lean_client")

from google.protobuf.json_format import MessageToDict

from a2a.types import TaskState
from declarative_agent_sdk.transports.a2a.client import AIAgentClient


def _extract_function_id(parts) -> str | None:
    for part in parts:
        if part.HasField("data"):
            d = MessageToDict(part.data)
            if isinstance(d, dict):
                fn_id = d.get("function_response", {}).get("id")
                if fn_id:
                    return fn_id
    return None


def _unpack_event(event):
    if event.HasField("task"):
        t = event.task
        return t.status.state, list(t.status.message.parts), t.id, t.context_id
    if event.HasField("status_update"):
        su = event.status_update
        return su.status.state, list(su.status.message.parts), su.task_id, su.context_id
    return None, [], None, None


def _part_text(parts) -> str:
    texts = []
    for p in parts or []:
        if getattr(p, "text", None):
            texts.append(p.text)
    return "\n".join(texts).strip()


async def handle_events(client: AIAgentClient, event_stream) -> None:
    async for event in event_stream:
        state, parts, task_id, context_id = _unpack_event(event)
        if state is None:
            continue

        if state == TaskState.TASK_STATE_COMPLETED:
            text = _part_text(parts) or "(empty)"
            print(f"\nAgent: {text}\n")

        elif state == TaskState.TASK_STATE_INPUT_REQUIRED:
            fn_id = _extract_function_id(parts)
            if not fn_id:
                logger.warning("INPUT_REQUIRED but no function_id in parts")
                continue
            d = (
                MessageToDict(parts[0].data)
                if parts and parts[0].HasField("data")
                else {}
            )
            fr = d.get("function_response") or {} if isinstance(d, dict) else {}
            fn_name = fr.get("name", "unknown tool")
            args = fr.get("args") or {}
            print(f"\nTool approval requested: {fn_name}({args})")
            answer = input("Approve? [y/N]: ").strip().lower()
            approved = answer in ("y", "yes")
            await handle_events(
                client,
                client.send_tool_confirmation(task_id, context_id, fn_id, approved),
            )

        elif state == TaskState.TASK_STATE_FAILED:
            print(f"\nAgent failed: {_part_text(parts) or 'unknown error'}\n")

        elif state == TaskState.TASK_STATE_WORKING:
            status = _part_text(parts)
            if status:
                print(f"  … {status[:200]}")


async def main() -> None:
    agent_url = os.environ.get("AGENT_URL", "http://127.0.0.1:8000").rstrip("/")
    client = AIAgentClient(agent_url=agent_url)
    context_id = uuid.uuid4().hex
    print(f"A2A client → {agent_url}")
    print(f"context_id={context_id}")
    print("Type a message (or 'exit'). Try: 'How much free disk space is there?'\n")

    while True:
        try:
            query = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if query.lower() in ("exit", "quit", "q"):
            break
        if not query:
            continue
        try:
            await handle_events(client, client.run(query, context_id))
        except Exception as exc:
            logger.error("Request failed: %s", exc)


if __name__ == "__main__":
    asyncio.run(main())
