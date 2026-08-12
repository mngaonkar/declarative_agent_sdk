"""Lean agent over A2A: invoke routing + AIAgentExecutor (no live LLM / network)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from declarative_agent_sdk.agents.lean.agent import LeanAIAgent, _a2a_tool_confirmation
from declarative_agent_sdk.agents.lean.runtime.loop import LeanLoop
from declarative_agent_sdk.agents.lean.runtime.skills import SkillRegistry
from declarative_agent_sdk.agents.lean.runtime.tools import LeanToolRegistry
from declarative_agent_sdk.core.agent_event import AgentEvent
from declarative_agent_sdk.transports.a2a.executor import AIAgentExecutor
from declarative_agent_sdk.transports.a2a.formatters.text_response_formatter import (
    TextResponseFormatter,
)
from a2a.types import TaskState


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _a2a_context(text: str = "", *, context_id: str = "ctx-1", data: Optional[dict] = None):
    """Minimal RequestContext-like object."""
    parts = []
    if text:
        part = MagicMock()
        part.WhichOneof = MagicMock(return_value="text")
        part.text = text
        part.data = None
        parts.append(part)
    if data is not None:
        part = MagicMock()
        part.WhichOneof = MagicMock(return_value="data")
        part.text = None
        part.data = data
        parts.append(part)

    message = MagicMock()
    message.parts = parts
    ctx = MagicMock()
    ctx.message = message
    ctx.context_id = context_id
    ctx.task_id = "task-1"
    return ctx


def _make_lean(tmp_path, client, approval: bool = True) -> LeanAIAgent:
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    (skills_dir / "demo").mkdir()
    (skills_dir / "demo" / "SKILL.md").write_text(
        "---\nname: demo\ndescription: Demo skill.\n---\n\nDo demo.\n",
        encoding="utf-8",
    )
    ws = tmp_path / "ws"
    ws.mkdir()

    with patch.object(LeanAIAgent, "__init__", lambda self, **kw: None):
        agent = LeanAIAgent.__new__(LeanAIAgent)

    agent.name = "lean_test"
    agent.description = "test"
    agent.tools_approval_required = approval
    agent.publish_url = None
    agent.output_key = None
    agent.input_key_map = {}

    skills = SkillRegistry(root=str(skills_dir))
    tools = LeanToolRegistry(skills, workspace=str(ws))

    def add(x: int, y: int) -> int:
        """Add two integers."""
        return x + y

    tools.add_callable(add)
    agent._skills = skills
    agent._tools = tools
    agent._client = client
    agent._loop = LeanLoop(
        client=client,
        tools=tools,
        skills=skills,
        instruction="Be brief.",
        tools_approval_required=approval,
        max_tool_iterations=8,
        max_step_retries=2,
        max_no_tool_continues=3,
    )
    agent.agent_card = SimpleNamespace(name="lean_test", supported_interfaces=[])
    return agent


# ---------------------------------------------------------------------------
# Confirmation parsing
# ---------------------------------------------------------------------------

class TestA2AConfirmationParse:
    def test_detects_confirmed_payload(self):
        msg = MagicMock()
        part = MagicMock()
        part.WhichOneof = MagicMock(return_value="data")
        part.data = {
            "function_response": {
                "id": "call_1",
                "name": "add",
                "response": {"confirmed": True},
            }
        }
        msg.parts = [part]
        assert _a2a_tool_confirmation(msg) == ("call_1", True)

    def test_plain_text_is_none(self):
        msg = MagicMock()
        part = MagicMock()
        part.WhichOneof = MagicMock(return_value="text")
        part.text = "hello"
        msg.parts = [part]
        assert _a2a_tool_confirmation(msg) is None


# ---------------------------------------------------------------------------
# Lean invoke
# ---------------------------------------------------------------------------

class TestLeanInvoke:
    @pytest.mark.asyncio
    async def test_text_query_streams_final(self, tmp_path):
        client = MagicMock()
        client.chat.return_value = {
            "role": "assistant",
            "content": "Hello over A2A.\n[[decision:done]]",
        }
        agent = _make_lean(tmp_path, client, approval=False)
        ctx = _a2a_context("hi there", context_id="sess-a")
        events = [e async for e in agent.invoke(ctx)]
        assert any(e.kind == "text" and e.is_final and "Hello" in e.text for e in events)
        assert client.chat.call_count == 1

    @pytest.mark.asyncio
    async def test_tool_approval_then_resume_via_a2a(self, tmp_path):
        client = MagicMock()
        client.chat.side_effect = [
            {
                "role": "assistant",
                "content": "[[phase:act]]",
                "tool_calls": [
                    {
                        "id": "call_add_1",
                        "type": "function",
                        "function": {
                            "name": "add",
                            "arguments": '{"x": 2, "y": 3}',
                        },
                    }
                ],
            },
            {
                "role": "assistant",
                "content": "Sum is 5.\n[[decision:done]]",
            },
        ]
        agent = _make_lean(tmp_path, client, approval=True)
        ctx = _a2a_context("add 2 and 3", context_id="sess-b")
        events = [e async for e in agent.invoke(ctx)]
        assert any(e.kind == "tool_approval" and e.tool_name == "add" for e in events)

        # Resume as A2A client would: function_response with confirmed=true
        resume_ctx = _a2a_context(
            data={
                "function_response": {
                    "id": "call_add_1",
                    "name": "add",
                    "response": {"confirmed": True},
                }
            },
            context_id="sess-b",
        )
        events2 = [e async for e in agent.invoke(resume_ctx)]
        assert any(e.is_final and "5" in (e.text or "") for e in events2)


# ---------------------------------------------------------------------------
# AIAgentExecutor + lean
# ---------------------------------------------------------------------------

class TestLeanA2AExecutor:
    @pytest.mark.asyncio
    async def test_executor_completes_on_final(self, tmp_path):
        client = MagicMock()
        client.chat.return_value = {
            "role": "assistant",
            "content": "All good.\n[[decision:done]]",
        }
        agent = _make_lean(tmp_path, client, approval=False)
        executor = AIAgentExecutor(agent, formatter=TextResponseFormatter())

        updates: List[Any] = []
        updater = MagicMock()
        updater.update_status = AsyncMock(
            side_effect=lambda state, message=None: updates.append((state, message))
        )
        updater.new_agent_message = MagicMock(side_effect=lambda parts=None: SimpleNamespace(parts=parts))
        updater.start_work = AsyncMock()

        ctx = _a2a_context("ping", context_id="sess-c")
        await executor._execute_implementation(ctx, updater)

        assert any(s == TaskState.TASK_STATE_COMPLETED for s, _ in updates)

    @pytest.mark.asyncio
    async def test_executor_input_required_on_tool_approval(self, tmp_path):
        client = MagicMock()
        client.chat.return_value = {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_x",
                    "function": {
                        "name": "add",
                        "arguments": '{"x": 1, "y": 1}',
                    },
                }
            ],
        }
        agent = _make_lean(tmp_path, client, approval=True)
        executor = AIAgentExecutor(agent, formatter=TextResponseFormatter())

        updates: List[Any] = []
        updater = MagicMock()
        updater.update_status = AsyncMock(
            side_effect=lambda state, message=None: updates.append((state, message))
        )
        updater.new_agent_message = MagicMock(
            side_effect=lambda parts=None: SimpleNamespace(parts=parts or [])
        )
        updater.start_work = AsyncMock()

        ctx = _a2a_context("add", context_id="sess-d")
        await executor._execute_implementation(ctx, updater)

        assert any(s == TaskState.TASK_STATE_INPUT_REQUIRED for s, _ in updates)
