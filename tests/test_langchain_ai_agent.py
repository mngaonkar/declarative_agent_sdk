"""
Unit tests for LangChainAIAgent.

All LLM and I/O calls are mocked — no API keys or network access required.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import BaseTool, StructuredTool

from declarative_agent_sdk.core.base_agent import BaseAgent
from declarative_agent_sdk.agents.deepagent.agent import (
    LangChainAIAgent,
    LangChainEvent,
    _message_text,
    _resolve_model,
    _to_lc_tool,
)


# ---------------------------------------------------------------------------
# Test fixtures / helpers
# ---------------------------------------------------------------------------

def _graph_mock(chunks: list):
    """
    Return a MagicMock whose .astream() is an async generator that yields
    the given chunks.  A new generator is created on every .astream() call,
    so the agent can be queried multiple times with the same mock.
    """
    async def _astream(*args, **kwargs):
        for chunk in chunks:
            yield chunk

    graph = MagicMock()
    graph.astream = _astream
    return graph


def _make_agent(chunks: list | None = None, name: str = "test_agent") -> LangChainAIAgent:
    """
    Build a LangChainAIAgent with all external dependencies mocked:
      - create_deep_agent  → fake graph
      - read_from_file     → static instruction string
      - ToolRegistry       → empty (no built-in tools)
    Uses /tmp as workspace so no temp-dir cleanup is needed.
    """
    if chunks is None:
        chunks = [{"model": {"messages": [AIMessage(content="Hello!")]}}]

    with (
        patch(
            "declarative_agent_sdk.agents.deepagent.agent.create_deep_agent",
            return_value=_graph_mock(chunks),
        ),
        patch(
            "declarative_agent_sdk.agents.deepagent.agent.read_from_file",
            return_value="You are a helpful assistant.",
        ),
        patch("declarative_agent_sdk.agents.deepagent.agent.ToolRegistry.register_built_in_tools"),
        patch("declarative_agent_sdk.agents.deepagent.agent.ToolRegistry.get_all", return_value=[]),
    ):
        return LangChainAIAgent(
            name=name,
            instruction_file="instructions.md",
            description="Test agent",
            model="claude-3-5-haiku-20241022",
            provider="anthropic",
            workspace_directory="/tmp",
            tools_approval_required=False,
        )


def _a2a_context(text: str, context_id: str = "ctx-001"):
    """Return a minimal mock of an A2A RequestContext with a single text part."""
    part = MagicMock()
    part.WhichOneof.return_value = "text"
    part.text = text

    message = MagicMock()
    message.parts = [part]

    ctx = MagicMock()
    ctx.message = message
    ctx.context_id = context_id
    return ctx


# ---------------------------------------------------------------------------
# LangChainEvent
# ---------------------------------------------------------------------------

class TestLangChainEvent:
    def test_final_event_is_final(self):
        event = LangChainEvent(text="done", is_final=True)
        assert event.is_final_response() is True

    def test_working_event_is_not_final(self):
        event = LangChainEvent(text="thinking...", is_final=False)
        assert event.is_final_response() is False

    def test_event_with_text_has_content(self):
        event = LangChainEvent(text="hello", is_final=True)
        assert event.content is not None
        assert event.content.parts[0].text == "hello"

    def test_event_without_text_has_no_content(self):
        event = LangChainEvent(is_final=False)
        assert event.content is None

    def test_event_always_has_empty_tool_ids(self):
        event = LangChainEvent(text="x")
        assert event.long_running_tool_ids == []

    def test_event_always_has_empty_confirmations(self):
        event = LangChainEvent(text="x")
        assert event.actions.requested_tool_confirmations == []


# ---------------------------------------------------------------------------
# _message_text
# ---------------------------------------------------------------------------

class TestMessageText:
    def test_plain_string(self):
        assert _message_text("Hello") == "Hello"

    def test_none_and_empty(self):
        assert _message_text(None) == ""
        assert _message_text([]) == ""

    def test_openai_style_text_blocks(self):
        content = [{
            "type": "text",
            "text": "Here's the current disk space usage:\n\n- **Root**: 22% used",
            "annotations": [],
            "id": "msg_01bca071ad62fb3b006a7b7b5e202881999a0de584d86b7347",
        }]
        assert _message_text(content) == (
            "Here's the current disk space usage:\n\n- **Root**: 22% used"
        )

    def test_multiple_text_blocks_joined(self):
        content = [
            {"type": "text", "text": "Part one."},
            {"type": "text", "text": "Part two."},
        ]
        assert _message_text(content) == "Part one.\nPart two."

    def test_skips_non_text_blocks(self):
        content = [
            {"type": "thinking", "thinking": "secret"},
            {"type": "text", "text": "Visible answer"},
            {"type": "tool_use", "name": "search", "id": "t1"},
        ]
        assert _message_text(content) == "Visible answer"

    def test_message_thinking_extracts_reasoning_blocks(self):
        from declarative_agent_sdk.agents.deepagent.agent import _message_thinking

        content = [
            {"type": "thinking", "thinking": "secret plan"},
            {"type": "text", "text": "Visible answer"},
        ]
        assert _message_thinking(content) == "secret plan"
        assert _message_text(content) == "Visible answer"

    def test_mixed_strings_and_blocks(self):
        content = ["Prefix", {"type": "text", "text": "body"}]
        assert _message_text(content) == "Prefix\nbody"

    def test_object_with_text_attr(self):
        block = type("Block", (), {"type": "text", "text": "from object"})()
        assert _message_text([block]) == "from object"


# ---------------------------------------------------------------------------
# _resolve_model
# ---------------------------------------------------------------------------

class TestResolveModel:
    def test_anthropic_returns_prefixed_string(self):
        result = _resolve_model("claude-3-5-sonnet-20241022", "anthropic")
        assert result == "anthropic:claude-3-5-sonnet-20241022"

    def test_openai_returns_prefixed_string(self):
        result = _resolve_model("gpt-4o", "openai")
        assert result == "openai:gpt-4o"

    def test_google_returns_prefixed_string(self):
        result = _resolve_model("gemini-2.0-flash", "google")
        assert result == "google_genai:gemini-2.0-flash"

    def test_google_genai_alias(self):
        result = _resolve_model("gemini-2.0-flash", "google_genai")
        assert result == "google_genai:gemini-2.0-flash"

    def test_prebuilt_model_returned_as_is(self):
        fake_model = MagicMock()
        result = _resolve_model(fake_model, "anthropic")
        assert result is fake_model


# ---------------------------------------------------------------------------
# _to_lc_tool
# ---------------------------------------------------------------------------

class TestToLcTool:
    def test_converts_callable_to_structured_tool(self):
        def add(x: int, y: int) -> int:
            """Add two numbers."""
            return x + y

        tool = _to_lc_tool(add)
        assert isinstance(tool, StructuredTool)
        assert tool.name == "add"

    def test_passes_through_existing_base_tool(self):
        existing = MagicMock(spec=BaseTool)
        result = _to_lc_tool(existing)
        assert result is existing

    def test_raises_on_non_callable(self):
        with pytest.raises(TypeError):
            _to_lc_tool(42)


# ---------------------------------------------------------------------------
# LangChainAIAgent — initialisation
# ---------------------------------------------------------------------------

class TestLangChainAIAgentInit:
    def test_is_base_agent_subclass(self):
        agent = _make_agent()
        assert isinstance(agent, BaseAgent)

    def test_name_and_description_set(self):
        agent = _make_agent(name="my_agent")
        assert agent.name == "my_agent"
        assert agent.description == "Test agent"

    def test_agent_card_created(self):
        agent = _make_agent()
        assert agent.agent_card is not None
        assert agent.agent_card.name == "test_agent"

    def test_create_deep_agent_called_with_system_prompt(self):
        with (
            patch(
                "declarative_agent_sdk.agents.deepagent.agent.create_deep_agent",
                return_value=_graph_mock([]),
            ) as mock_create,
            patch(
                "declarative_agent_sdk.agents.deepagent.agent.read_from_file",
                return_value="System instruction.",
            ),
            patch("declarative_agent_sdk.agents.deepagent.agent.ToolRegistry.register_built_in_tools"),
            patch("declarative_agent_sdk.agents.deepagent.agent.ToolRegistry.get_all", return_value=[]),
        ):
            LangChainAIAgent(
                name="agent",
                instruction_file="inst.md",
                model="claude-3-5-haiku-20241022",
                provider="anthropic",
                workspace_directory="/tmp",
            )
            mock_create.assert_called_once()
            _, kwargs = mock_create.call_args
            assert kwargs["system_prompt"] == "System instruction."
            assert kwargs["name"] == "agent"


# ---------------------------------------------------------------------------
# run_query
# ---------------------------------------------------------------------------

class TestRunQuery:
    async def test_yields_final_event_for_text_response(self):
        agent = _make_agent([
            {"model": {"messages": [AIMessage(content="The answer is 42.")]}},
        ])
        events = [e async for e in agent.run_query("What is the answer?")]
        assert len(events) == 1
        assert events[0].is_final_response() is True
        assert events[0].content.parts[0].text == "The answer is 42."

    async def test_yields_working_event_for_tool_call(self):
        tool_call_msg = AIMessage(content="")
        tool_call_msg.tool_calls = [{"name": "search", "args": {"q": "test"}, "id": "tc1"}]
        agent = _make_agent([
            {"model": {"messages": [tool_call_msg]}},
            {"tools": {"messages": [ToolMessage(content="result", tool_call_id="tc1")]}},
            {"model": {"messages": [AIMessage(content="Final answer.")]}},
        ])
        events = [e async for e in agent.run_query("Search something")]
        working = [e for e in events if not e.is_final_response()]
        final   = [e for e in events if e.is_final_response()]
        assert len(working) == 1
        assert "search" in working[0].content.parts[0].text
        assert len(final) == 1
        assert final[0].content.parts[0].text == "Final answer."

    async def test_no_events_when_graph_is_empty(self):
        agent = _make_agent(chunks=[])
        events = [e async for e in agent.run_query("anything")]
        assert events == []

    async def test_non_ai_messages_are_ignored(self):
        agent = _make_agent([
            {"tools": {"messages": [ToolMessage(content="ignored", tool_call_id="t1")]}},
        ])
        events = [e async for e in agent.run_query("question")]
        assert events == []

    async def test_none_node_output_is_skipped(self):
        """deepagents can stream {node: None} for middleware/routing nodes."""
        agent = _make_agent([
            {"middleware": None},
            {"model": {"messages": [AIMessage(content="Still works.")]}},
            {"other": None},
        ])
        events = [e async for e in agent.run_query("disk space")]
        assert len(events) == 1
        assert events[0].is_final_response() is True
        assert events[0].content.parts[0].text == "Still works."

    async def test_non_dict_chunk_is_skipped(self):
        agent = _make_agent([
            "not-a-dict",
            {"model": {"messages": [AIMessage(content="ok")]}},
        ])
        events = [e async for e in agent.run_query("hi")]
        assert len(events) == 1
        assert events[0].content.parts[0].text == "ok"

    async def test_list_content_blocks_become_plain_text(self):
        content = [{
            "type": "text",
            "text": "Disk free: 43Gi available",
            "annotations": [],
            "id": "msg_abc",
        }]
        agent = _make_agent([
            {"model": {"messages": [AIMessage(content=content)]}},
        ])
        events = [e async for e in agent.run_query("disk?")]
        assert len(events) == 1
        assert events[0].content.parts[0].text == "Disk free: 43Gi available"
        assert events[0].content.parts[0].text.startswith("Disk free")
        assert "type" not in events[0].content.parts[0].text

    async def test_interrupt_yields_tool_confirmation_event(self):
        from langgraph.types import Interrupt

        hitl = {
            "action_requests": [
                {"name": "exec_command", "args": {"command": "df -h"}, "description": "run"},
            ],
            "review_configs": [
                {"action_name": "exec_command", "allowed_decisions": ["approve", "reject"]},
            ],
        }
        tool_call_msg = AIMessage(content="")
        tool_call_msg.tool_calls = [
            {"name": "exec_command", "args": {"command": "df -h"}, "id": "tc1"}
        ]
        agent = _make_agent([
            {"model": {"messages": [tool_call_msg]}},
            {"__interrupt__": (Interrupt(value=hitl, id="intr-1"),)},
        ])
        events = [e async for e in agent.run_query("disk space", session_id="sess-1")]
        conf = [e for e in events if e.long_running_tool_ids]
        assert len(conf) == 1
        assert conf[0].actions.requested_tool_confirmations
        part = conf[0].content.parts[0]
        assert part.function_call is not None
        assert part.function_call.args["originalFunctionCall"]["name"] == "exec_command"
        assert agent._pending_hitl["sess-1"] == 1

    async def test_tool_confirmation_resumes_with_command(self):
        from langgraph.types import Command

        agent = _make_agent([
            {"model": {"messages": [AIMessage(content="Done after approval.")]}},
        ])
        agent._pending_hitl["sess-2"] = 1

        seen_input = {}

        async def _astream(input_data, config=None, stream_mode=None):
            seen_input["value"] = input_data
            for chunk in [{"model": {"messages": [AIMessage(content="Done after approval.")]}}]:
                yield chunk

        agent._graph.astream = _astream
        events = [
            e async for e in agent.tool_confirmation("ctx", "sess-2", yes=True)
        ]
        assert isinstance(seen_input["value"], Command)
        assert seen_input["value"].resume == {"decisions": [{"type": "approve"}]}
        assert any(e.is_final_response() for e in events)
        assert "sess-2" not in agent._pending_hitl


# ---------------------------------------------------------------------------
# tools_approval_required → interrupt_on
# ---------------------------------------------------------------------------

class TestToolApprovalWiring:
    def test_interrupt_on_passed_when_approval_required(self):
        with (
            patch(
                "declarative_agent_sdk.agents.deepagent.agent.create_deep_agent",
                return_value=_graph_mock([]),
            ) as mock_create,
            patch(
                "declarative_agent_sdk.agents.deepagent.agent.read_from_file",
                return_value="sys",
            ),
            patch("declarative_agent_sdk.agents.deepagent.agent.ToolRegistry.register_built_in_tools"),
            patch("declarative_agent_sdk.agents.deepagent.agent.ToolRegistry.get_all", return_value=[]),
        ):
            def fake_tool(x: str) -> str:
                """demo tool"""
                return x

            LangChainAIAgent(
                name="a",
                instruction_file="i.md",
                tools=[fake_tool],
                tools_approval_required=True,
                model="claude-3-5-haiku-20241022",
                provider="anthropic",
                workspace_directory="/tmp",
            )
            kwargs = mock_create.call_args.kwargs
            assert "interrupt_on" in kwargs
            assert "fake_tool" in kwargs["interrupt_on"]
            assert "execute" in kwargs["interrupt_on"]
            assert kwargs["interrupt_on"]["fake_tool"]["allowed_decisions"] == [
                "approve",
                "reject",
            ]

    def test_no_interrupt_on_when_approval_disabled(self):
        with (
            patch(
                "declarative_agent_sdk.agents.deepagent.agent.create_deep_agent",
                return_value=_graph_mock([]),
            ) as mock_create,
            patch(
                "declarative_agent_sdk.agents.deepagent.agent.read_from_file",
                return_value="sys",
            ),
            patch("declarative_agent_sdk.agents.deepagent.agent.ToolRegistry.register_built_in_tools"),
            patch("declarative_agent_sdk.agents.deepagent.agent.ToolRegistry.get_all", return_value=[]),
        ):
            LangChainAIAgent(
                name="a",
                instruction_file="i.md",
                tools_approval_required=False,
                model="claude-3-5-haiku-20241022",
                provider="anthropic",
                workspace_directory="/tmp",
            )
            kwargs = mock_create.call_args.kwargs
            assert "interrupt_on" not in kwargs


# ---------------------------------------------------------------------------
# run_query_and_collect
# ---------------------------------------------------------------------------

class TestRunQueryAndCollect:
    async def test_returns_final_response_text(self):
        agent = _make_agent([
            {"model": {"messages": [AIMessage(content="Collected answer.")]}},
        ])
        result = await agent.run_query_and_collect("Tell me something")
        assert result == "Collected answer."

    async def test_returns_empty_string_when_no_final_event(self):
        agent = _make_agent(chunks=[])
        result = await agent.run_query_and_collect("?")
        assert result == ""

    async def test_returns_first_final_text_ignoring_working(self):
        tool_call_msg = AIMessage(content="")
        tool_call_msg.tool_calls = [{"name": "look", "args": {}, "id": "t1"}]
        agent = _make_agent([
            {"model": {"messages": [tool_call_msg]}},
            {"model": {"messages": [AIMessage(content="Real answer.")]}},
        ])
        result = await agent.run_query_and_collect("Go")
        assert result == "Real answer."


# ---------------------------------------------------------------------------
# run_sync
# ---------------------------------------------------------------------------

class TestRunSync:
    def test_returns_string(self):
        agent = _make_agent([
            {"model": {"messages": [AIMessage(content="Sync response.")]}},
        ])
        result = agent.run_sync("Hello")
        assert result == "Sync response."

    def test_returns_empty_string_on_no_response(self):
        agent = _make_agent(chunks=[])
        assert agent.run_sync("Hello") == ""


# ---------------------------------------------------------------------------
# invoke  (A2A RequestContext)
# ---------------------------------------------------------------------------

class TestInvoke:
    async def test_invoke_extracts_text_and_yields_events(self):
        agent = _make_agent([
            {"model": {"messages": [AIMessage(content="A2A response.")]}},
        ])
        ctx = _a2a_context("Hello from A2A", context_id="ctx-abc")
        events = [e async for e in agent.invoke(ctx)]
        assert len(events) == 1
        assert events[0].is_final_response()
        assert events[0].content.parts[0].text == "A2A response."

    async def test_invoke_uses_context_id_as_session(self):
        captured = {}

        async def _astream_spy(input_data, config, **kwargs):
            captured["thread_id"] = config["configurable"]["thread_id"]
            yield {"model": {"messages": [AIMessage(content="ok")]}}

        agent = _make_agent()
        agent._graph.astream = _astream_spy

        ctx = _a2a_context("hi", context_id="session-xyz")
        [e async for e in agent.invoke(ctx)]
        assert captured["thread_id"] == "session-xyz"

    async def test_invoke_raises_when_message_has_no_text(self):
        agent = _make_agent()

        part = MagicMock()
        part.WhichOneof.return_value = "data"   # not "text"
        part.text = ""

        ctx = MagicMock()
        ctx.message = MagicMock()
        ctx.message.parts = [part]
        ctx.context_id = "ctx-1"

        with pytest.raises(ValueError, match="No text content"):
            [e async for e in agent.invoke(ctx)]

    async def test_invoke_concatenates_multiple_text_parts(self):
        captured_queries = []

        async def _spy(input_data, config, **kwargs):
            msgs = input_data.get("messages", [])
            if msgs:
                captured_queries.append(msgs[0].content)
            yield {"model": {"messages": [AIMessage(content="ok")]}}

        agent = _make_agent()
        agent._graph.astream = _spy

        part1 = MagicMock()
        part1.WhichOneof.return_value = "text"
        part1.text = "Hello"

        part2 = MagicMock()
        part2.WhichOneof.return_value = "text"
        part2.text = "world"

        ctx = MagicMock()
        ctx.message = MagicMock()
        ctx.message.parts = [part1, part2]
        ctx.context_id = "ctx-2"

        [e async for e in agent.invoke(ctx)]
        assert captured_queries[0] == "Hello world"
