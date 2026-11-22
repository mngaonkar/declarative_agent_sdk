"""
A tool call whose arguments are not valid JSON must not poison the session.

The incident (amd-gpu-machine, #Research): a 2-bit quantized model emitted a
write_file call with broken JSON. The loop parsed it as {}, asked a person to
approve that empty call, ran it ("path is required"), and kept the broken
arguments in the history. llama.cpp re-parses every past tool call on each
request, so the next request — and every one after — failed with "the
model's tool call was malformed", although the model's newest output was fine.
"""

import json

from declarative_agent_sdk.agents.lean.runtime.llm import MalformedToolCall
from declarative_agent_sdk.agents.lean.runtime.loop import (
    UNREADABLE_ARGS,
    LeanLoop,
    _repair_tool_calls,
    _unreadable_chars,
)
from declarative_agent_sdk.agents.lean.runtime.skills import SkillRegistry
from declarative_agent_sdk.agents.lean.runtime.tools import LeanToolRegistry

BROKEN = '{"path": "workspace/report.md", "content": "# Report\\n\\nThe \\"fast path'  # cut off


def call(name, arguments, call_id="c1"):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}


class LlamaCpp:
    """Answers in order, but refuses any request whose history holds unparseable arguments."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.requests = []

    def chat(self, messages, tools=None):
        self.requests.append(json.loads(json.dumps(messages)))
        for message in messages:
            for past in message.get("tool_calls") or []:
                try:
                    json.loads(past["function"]["arguments"])
                except (TypeError, ValueError):
                    raise MalformedToolCall("failed to parse tool call arguments", chars=892)
        return self.replies.pop(0)


def loop(tmp_path, client, approval=False):
    (tmp_path / "skills").mkdir(exist_ok=True)
    skills = SkillRegistry(root=str(tmp_path / "skills"))
    tools = LeanToolRegistry(skills, workspace=str(tmp_path / "ws"))
    return LeanLoop(client=client, tools=tools, skills=skills,
                    tools_approval_required=approval, max_tool_iterations=8)


class TestRepair:
    def test_broken_arguments_become_a_valid_marker(self):
        calls = [call("write_file", BROKEN)]
        assert _repair_tool_calls(calls) == 1
        assert json.loads(calls[0]["function"]["arguments"]) == {UNREADABLE_ARGS: len(BROKEN)}
        assert _unreadable_chars(calls[0]) == len(BROKEN)

    def test_good_arguments_are_left_alone(self):
        calls = [call("read_file", '{"path": "a.md"}'), call("list_dir", "", "c2")]
        assert _repair_tool_calls(calls) == 0
        assert calls[0]["function"]["arguments"] == '{"path": "a.md"}'
        assert _unreadable_chars(calls[0]) is None

    def test_a_non_object_is_broken_and_a_dict_is_serialised(self):
        calls = [call("x", "[1, 2]"), call("y", {"a": 1}, "c2")]
        assert _repair_tool_calls(calls) == 1
        assert calls[1]["function"]["arguments"] == '{"a": 1}'

    def test_a_real_argument_named_like_the_marker_is_not_mistaken(self):
        mixed = call("x", json.dumps({UNREADABLE_ARGS: 3, "path": "a"}))
        assert _unreadable_chars(mixed) is None


class TestTheIncident:
    def test_the_session_recovers_instead_of_failing_every_request(self, tmp_path):
        target = str(tmp_path / "ws" / "report.md")
        client = LlamaCpp(
            {"role": "assistant", "content": "", "tool_calls": [call("write_file", BROKEN)]},
            {"role": "assistant", "content": "", "tool_calls": [
                call("write_file", json.dumps({"path": target, "content": "# Report\n"}), "c2")]},
            {"role": "assistant", "content": "Saved report.md.\n[[decision:done]]"},
        )
        events = list(loop(tmp_path, client).run("write the report", "s1"))
        assert not any(e.kind == "error" for e in events), [e.text for e in events]
        assert any(e.kind == "final" and "Saved report.md" in e.text for e in events)
        assert (tmp_path / "ws" / "report.md").read_text() == "# Report\n"
        # The model was told why, and nothing ran for the broken call.
        told = [m for m in client.requests[1] if m.get("role") == "tool"]
        assert "could not be read" in told[0]["content"]
        assert any(e.kind == "status" and "arrived unreadable" in e.text for e in events)

    def test_no_approval_is_asked_for_an_unreadable_call(self, tmp_path):
        client = LlamaCpp(
            {"role": "assistant", "content": "", "tool_calls": [call("write_file", BROKEN)]},
            {"role": "assistant", "content": "I will retry in parts.\n[[decision:done]]"},
        )
        events = list(loop(tmp_path, client, approval=True).run("write it", "s1"))
        assert not any(e.kind == "tool_approval" for e in events)
        assert any(e.kind == "final" for e in events)

    def test_a_later_message_in_the_same_session_still_works(self, tmp_path):
        client = LlamaCpp(
            {"role": "assistant", "content": "", "tool_calls": [call("write_file", BROKEN)]},
            {"role": "assistant", "content": "Could not save it.\n[[decision:done]]"},
            {"role": "assistant", "content": "Hello again.\n[[decision:done]]"},
        )
        agent_loop = loop(tmp_path, client)
        list(agent_loop.run("write it", "s1"))
        events = list(agent_loop.run("hi", "s1"))
        assert any(e.kind == "final" and "Hello again" in e.text for e in events)

    def test_it_gives_up_after_repeated_unreadable_calls(self, tmp_path):
        broken = {"role": "assistant", "content": "", "tool_calls": [call("write_file", BROKEN)]}
        client = LlamaCpp(*[dict(broken) for _ in range(5)])
        for reply in client.replies:
            reply["tool_calls"] = [call("write_file", BROKEN)]
        events = list(loop(tmp_path, client).run("write it", "s1"))
        errors = [e for e in events if e.kind == "error"]
        assert errors and "kept arriving unreadable" in errors[-1].text
        assert len(client.requests) == 3            # two redos, then it stops

    def test_an_already_broken_history_is_repaired_before_sending(self, tmp_path):
        client = LlamaCpp({"role": "assistant", "content": "Fine.\n[[decision:done]]"})
        agent_loop = loop(tmp_path, client)
        agent_loop._history["s1"] = [
            {"role": "user", "content": "earlier"},
            {"role": "assistant", "content": "", "tool_calls": [call("write_file", BROKEN, "old")]},
            {"role": "tool", "tool_call_id": "old", "content": "Error: path is required"},
            {"role": "assistant", "content": "Sorry."},
        ]
        events = list(agent_loop.run("again", "s1"))
        assert any(e.kind == "final" and "Fine." in e.text for e in events)
