"""Tests for the canonical AgentEvent type and ADK mapping."""

from types import SimpleNamespace

from declarative_agent_sdk.agent_event import AgentEvent, from_adk_event


class TestAgentEvent:
    def test_final_text(self):
        e = AgentEvent.final_text("hello")
        assert e.kind == "text"
        assert e.is_final_response() is True
        assert e.content.parts[0].text == "hello"

    def test_status_not_final(self):
        e = AgentEvent.status("working")
        assert e.is_final_response() is False
        assert e.content.parts[0].text == "working"

    def test_tool_approval_duck_type(self):
        e = AgentEvent.tool_approval("fc-1", "exec_command", {"cmd": "df"})
        assert e.kind == "tool_approval"
        assert e.is_final_response() is False
        assert e.long_running_tool_ids == ["fc-1"]
        assert e.actions.requested_tool_confirmations == ["fc-1"]
        fc = e.content.parts[0].function_call
        assert fc.id == "fc-1"
        assert fc.args["originalFunctionCall"]["name"] == "exec_command"
        assert fc.args["originalFunctionCall"]["args"] == {"cmd": "df"}


class TestFromAdkEvent:
    def test_final_text(self):
        part = SimpleNamespace(text="answer", function_call=None)
        content = SimpleNamespace(parts=[part])
        event = SimpleNamespace(
            content=content,
            long_running_tool_ids=[],
            actions=SimpleNamespace(requested_tool_confirmations=[]),
            is_final_response=lambda: True,
        )
        mapped = from_adk_event(event)
        assert mapped is not None
        assert mapped.is_final_response()
        assert mapped.content.parts[0].text == "answer"

    def test_tool_confirmation(self):
        fc = SimpleNamespace(
            id="id-9",
            name="adk_request_confirmation",
            args={
                "originalFunctionCall": {
                    "name": "search",
                    "args": {"q": "news"},
                }
            },
        )
        part = SimpleNamespace(text=None, function_call=fc)
        event = SimpleNamespace(
            content=SimpleNamespace(parts=[part]),
            long_running_tool_ids=["id-9"],
            actions=SimpleNamespace(requested_tool_confirmations=["id-9"]),
            is_final_response=lambda: True,
        )
        mapped = from_adk_event(event)
        assert mapped is not None
        assert mapped.kind == "tool_approval"
        assert mapped.tool_name == "search"
        assert mapped.tool_args == {"q": "news"}
        assert mapped.is_final_response() is False

    def test_passthrough_agent_event(self):
        e = AgentEvent.final_text("x")
        assert from_adk_event(e) is e

    def test_empty_returns_none(self):
        event = SimpleNamespace(
            content=SimpleNamespace(parts=[]),
            long_running_tool_ids=[],
            actions=SimpleNamespace(requested_tool_confirmations=[]),
            is_final_response=lambda: False,
        )
        assert from_adk_event(event) is None
