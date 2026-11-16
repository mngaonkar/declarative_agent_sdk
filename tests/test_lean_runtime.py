"""Unit tests for the lean deliberative runtime (no network)."""

import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

from declarative_agent_sdk.agents.lean.runtime.loop import (
    LeanLoop,
    _close_open_tool_calls,
    _fill_missing_tool_responses,
    _parse_control_tags,
    _tool_chain_valid,
    _tool_result_failed,
)
from declarative_agent_sdk.agents.lean.runtime.skills import SkillRegistry, parse_frontmatter
from declarative_agent_sdk.agents.lean.runtime.tools import LeanToolRegistry
from declarative_agent_sdk.core.agent_factory import resolve_agent_framework, AgentFactory


class TestParseFrontmatter:
    def test_basic(self):
        text = "---\nname: led\ndescription: Control LED\n---\n\n# Body\nHello"
        meta, body = parse_frontmatter(text)
        assert meta["name"] == "led"
        assert meta["description"] == "Control LED"
        assert "Hello" in body


class TestControlTags:
    def test_decision_done(self):
        visible, decision, phase = _parse_control_tags(
            "All set.\n[[decision:done]]\n[[phase:done]]"
        )
        assert decision == "done"
        assert phase == "done"
        assert "[[" not in visible
        assert "All set" in visible

    def test_tool_failure_detect(self):
        assert _tool_result_failed("Error: boom")
        assert _tool_result_failed('{"success": false, "stderr": "x"}')
        assert not _tool_result_failed('{"success": true, "stdout": "ok"}')


class TestSkillRegistry:
    def test_discover(self, tmp_path: Path):
        skill_dir = tmp_path / "disk-space"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\nname: disk-space\ndescription: Check disk free space.\n---\n\n# Disk\nUse df -h.\n",
            encoding="utf-8",
        )
        reg = SkillRegistry(root=str(tmp_path))
        assert "disk-space" in reg.skills
        assert "disk free" in reg.catalog().lower() or "disk" in reg.catalog().lower()
        body = reg.render("disk-space")
        assert "df -h" in body


class TestLeanToolRegistry:
    def test_skill_tool(self, tmp_path: Path):
        skill_dir = tmp_path / "demo"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\nname: demo\ndescription: Demo skill.\n---\n\nDo the thing.\n",
            encoding="utf-8",
        )
        skills = SkillRegistry(root=str(tmp_path))
        tools = LeanToolRegistry(skills, workspace=str(tmp_path / "ws"))
        out = tools.invoke("Skill", {"name": "demo"})
        assert "Do the thing" in out
        assert "Skill" in tools.names()


class TestLeanLoop:
    def _loop(self, tmp_path: Path, client, approval: bool = True, **kwargs) -> LeanLoop:
        skills = SkillRegistry(root=str(tmp_path / "skills"))
        (tmp_path / "skills").mkdir(exist_ok=True)
        tools = LeanToolRegistry(skills, workspace=str(tmp_path / "ws"))

        def add(x: int, y: int) -> int:
            """Add two integers."""
            return x + y

        tools.add_callable(add)
        return LeanLoop(
            client=client,
            tools=tools,
            skills=skills,
            tools_approval_required=approval,
            max_tool_iterations=kwargs.get("max_tool_iterations", 8),
            max_step_retries=kwargs.get("max_step_retries", 3),
            max_no_tool_continues=kwargs.get("max_no_tool_continues", 4),
        )

    def test_explicit_done_exits(self, tmp_path: Path):
        client = MagicMock()
        client.chat.return_value = {
            "role": "assistant",
            "content": "Hello there.\n[[decision:done]]",
        }
        loop = self._loop(tmp_path, client, approval=False)
        events = list(loop.run("hi", "s1"))
        assert any(e.kind == "final" and "Hello" in e.text for e in events)
        assert client.chat.call_count == 1

    def test_no_tool_without_decision_continues_then_done(self, tmp_path: Path):
        client = MagicMock()
        client.chat.side_effect = [
            {"role": "assistant", "content": "I will plan to greet you."},
            {
                "role": "assistant",
                "content": "Hello after reflecting.\n[[decision:done]]",
            },
        ]
        loop = self._loop(tmp_path, client, approval=False)
        events = list(loop.run("hi", "s1"))
        assert client.chat.call_count == 2
        assert any(e.kind == "status" and "plan" in e.text.lower() for e in events)
        assert any(e.kind == "final" and "Hello after" in e.text for e in events)

    def test_think_blocks_and_phase_surface_as_status(self, tmp_path: Path):
        client = MagicMock()
        client.chat.side_effect = [
            {
                "role": "assistant",
                "content": (
                    "<think>Need disk free space first.</think>\n"
                    "[[phase:plan]] Check df then summarize.\n"
                    "[[decision:continue]]"
                ),
            },
            {
                "role": "assistant",
                "content": (
                    "<think>Have enough info.</think>\n"
                    "Root is 22% used.\n[[decision:done]]"
                ),
            },
        ]
        loop = self._loop(tmp_path, client, approval=False)
        events = list(loop.run("disk?", "think1"))
        status_texts = [e.text for e in events if e.kind == "status"]
        assert any("Need disk free space" in t for t in status_texts)
        assert any("[Planning]" in t for t in status_texts)
        # Terminal <think> is shown; final is user-facing only
        assert any("Have enough info" in t for t in status_texts)
        finals = [e for e in events if e.kind == "final"]
        assert finals and "Root is 22%" in finals[0].text
        assert "<think>" not in finals[0].text

    def test_tool_approval_pause_and_resume(self, tmp_path: Path):
        client = MagicMock()
        client.chat.side_effect = [
            {
                "role": "assistant",
                "content": "[[phase:act]]",
                "tool_calls": [
                    {
                        "id": "tc1",
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
        loop = self._loop(tmp_path, client, approval=True)
        events = list(loop.run("add 2 and 3", "s1"))
        assert any(e.kind == "tool_approval" and e.tool_name == "add" for e in events)
        assert "s1" in loop._pending

        events2 = list(loop.resume("s1", approved=True))
        assert any(e.kind == "final" and "5" in e.text for e in events2)

    def test_auto_approve_skill_tool(self, tmp_path: Path):
        skills_root = tmp_path / "skills"
        skill_dir = skills_root / "demo"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: demo\ndescription: Demo.\n---\n\nBody.\n",
            encoding="utf-8",
        )
        client = MagicMock()
        client.chat.side_effect = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "tc1",
                        "function": {
                            "name": "Skill",
                            "arguments": '{"name": "demo"}',
                        },
                    }
                ],
            },
            {
                "role": "assistant",
                "content": "Loaded skill.\n[[decision:done]]",
            },
        ]
        skills = SkillRegistry(root=str(skills_root))
        tools = LeanToolRegistry(skills, workspace=str(tmp_path / "ws"))
        loop = LeanLoop(
            client=client,
            tools=tools,
            skills=skills,
            tools_approval_required=True,
            max_tool_iterations=5,
        )
        events = list(loop.run("use demo", "s2"))
        kinds = [e.kind for e in events]
        assert "tool_approval" not in kinds
        assert "final" in kinds

    def test_step_failures_force_ask(self, tmp_path: Path):
        client = MagicMock()

        def boom(**kwargs):
            return "Error: always fails"

        skills = SkillRegistry(root=str(tmp_path / "skills"))
        (tmp_path / "skills").mkdir(exist_ok=True)
        tools = LeanToolRegistry(skills, workspace=str(tmp_path / "ws"))
        tools.add(
            "boom",
            "Always fails",
            {},
            [],
            lambda a: boom(),
        )
        # 3 failing tool rounds then model must ask
        fail_call = {
            "role": "assistant",
            "content": "[[phase:act]]",
            "tool_calls": [
                {
                    "id": "t1",
                    "function": {"name": "boom", "arguments": "{}"},
                }
            ],
        }
        client.chat.side_effect = [
            fail_call,
            fail_call,
            fail_call,
            {
                "role": "assistant",
                "content": "I need help with boom.\n[[decision:ask]]",
            },
        ]
        loop = LeanLoop(
            client=client,
            tools=tools,
            skills=skills,
            tools_approval_required=False,
            max_tool_iterations=10,
            max_step_retries=3,
        )
        events = list(loop.run("do boom", "s3"))
        assert any(e.kind == "final" and "help" in e.text.lower() for e in events)
        assert any("Step failed" in (e.text or "") for e in events if e.kind == "status")

    def test_partial_tool_batch_is_closed_before_next_chat(self, tmp_path: Path):
        """
        If a multi-tool batch stops early (max step retries), remaining
        tool_call_ids must still get tool messages before the next model call.
        """
        client = MagicMock()
        skills = SkillRegistry(root=str(tmp_path / "skills"))
        (tmp_path / "skills").mkdir(exist_ok=True)
        tools = LeanToolRegistry(skills, workspace=str(tmp_path / "ws"))

        def boom(**kwargs):
            return "Error: always fails"

        tools.add("boom", "Always fails", {}, [], lambda a: boom())
        tools.add("second", "Would run second", {}, [], lambda a: "ok")

        # One assistant turn with TWO tool calls; first fails and hits
        # max_step_retries=1 so the second must be synthetically closed.
        multi = {
            "role": "assistant",
            "content": "[[phase:act]]",
            "tool_calls": [
                {
                    "id": "call_AXIbSl0R4zsQ2Ci7BKO601VS",
                    "function": {"name": "boom", "arguments": "{}"},
                },
                {
                    "id": "call_SECOND_TOOL_ID_0001",
                    "function": {"name": "second", "arguments": "{}"},
                },
            ],
        }
        client.chat.side_effect = [
            multi,
            {
                "role": "assistant",
                "content": "I need help after failures.\n[[decision:ask]]",
            },
        ]
        loop = LeanLoop(
            client=client,
            tools=tools,
            skills=skills,
            tools_approval_required=False,
            max_tool_iterations=5,
            max_step_retries=1,
        )
        events = list(loop.run("do things", "batch1"))
        assert client.chat.call_count == 2
        # Second chat request must include tool responses for BOTH ids.
        second_msgs = client.chat.call_args_list[1].kwargs.get("messages")
        if second_msgs is None:
            second_msgs = client.chat.call_args_list[1].args[0]
        tool_ids = {
            m.get("tool_call_id")
            for m in second_msgs
            if m.get("role") == "tool"
        }
        assert "call_AXIbSl0R4zsQ2Ci7BKO601VS" in tool_ids
        assert "call_SECOND_TOOL_ID_0001" in tool_ids
        assert _tool_chain_valid([m for m in second_msgs if m.get("role") != "system"])
        assert any(e.kind == "final" for e in events)

    def test_abandoned_approval_closes_tool_ids(self, tmp_path: Path):
        client = MagicMock()
        client.chat.side_effect = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_pending_1",
                        "function": {
                            "name": "add",
                            "arguments": '{"x": 1, "y": 2}',
                        },
                    }
                ],
            },
            {
                "role": "assistant",
                "content": "Fresh turn.\n[[decision:done]]",
            },
        ]
        loop = self._loop(tmp_path, client, approval=True)
        events = list(loop.run("add", "ab1"))
        assert any(e.kind == "tool_approval" for e in events)
        # User ignores approval and sends a new message.
        events2 = list(loop.run("never mind, just say hi", "ab1"))
        assert any(e.kind == "final" and "Fresh" in e.text for e in events2)
        second_msgs = client.chat.call_args_list[1].kwargs.get("messages")
        if second_msgs is None:
            second_msgs = client.chat.call_args_list[1].args[0]
        tool_ids = {
            m.get("tool_call_id")
            for m in second_msgs
            if m.get("role") == "tool"
        }
        assert "call_pending_1" in tool_ids

    def test_tool_failure_surfaces_reflection_and_retries(self, tmp_path: Path):
        client = MagicMock()
        skills = SkillRegistry(root=str(tmp_path / "skills"))
        (tmp_path / "skills").mkdir(exist_ok=True)
        tools = LeanToolRegistry(skills, workspace=str(tmp_path / "ws"))

        def slow_cmd() -> str:
            return "Error: Command timed out after 30 seconds"

        def fast_cmd() -> str:
            return "success: folder sizes 10G, 5G, 2G"

        tools.add_callable(slow_cmd)
        tools.add_callable(fast_cmd)

        client.chat.side_effect = [
            {
                "role": "assistant",
                "content": "Executing slow command to check sizes.",
                "tool_calls": [
                    {
                        "id": "tc1",
                        "function": {"name": "slow_cmd", "arguments": "{}"},
                    }
                ],
            },
            {
                "role": "assistant",
                "content": "The previous command timed out. Retrying with faster command.",
                "tool_calls": [
                    {
                        "id": "tc2",
                        "function": {"name": "fast_cmd", "arguments": "{}"},
                    }
                ],
            },
            {
                "role": "assistant",
                "content": "Here are the top 3 folders: 10G, 5G, 2G.",
            },
        ]

        loop = LeanLoop(
            client=client,
            tools=tools,
            skills=skills,
            tools_approval_required=True,
            max_step_retries=3,
        )

        events1 = list(loop.run("check disk", "s_fail"))
        assert any(e.kind == "tool_approval" and e.tool_name == "slow_cmd" for e in events1)

        events2 = list(loop.resume("s_fail", approved=True))
        status_texts = [e.text for e in events2 if e.kind == "status"]
        assert any("Step failed" in t and "Command timed out" in t for t in status_texts)
        assert any("previous command timed out" in t for t in status_texts)
        assert any(e.kind == "tool_approval" and e.tool_name == "fast_cmd" for e in events2)

        events3 = list(loop.resume("s_fail", approved=True))
        assert any(e.kind == "final" and "10G, 5G, 2G" in e.text for e in events3)


class TestToolChainHelpers:
    def test_fill_missing(self):
        msgs = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "a", "function": {"name": "t1", "arguments": "{}"}},
                    {"id": "b", "function": {"name": "t2", "arguments": "{}"}},
                ],
            },
            {"role": "tool", "tool_call_id": "a", "content": "ok"},
        ]
        _fill_missing_tool_responses(
            msgs,
            msgs[0]["tool_calls"],
            reason="skipped",
        )
        assert [m.get("tool_call_id") for m in msgs if m.get("role") == "tool"] == [
            "a",
            "b",
        ]
        assert _tool_chain_valid(msgs)

    def test_close_open_before_user(self):
        msgs = [
            {
                "role": "assistant",
                "tool_calls": [
                    {"id": "x", "function": {"name": "t", "arguments": "{}"}},
                ],
            },
        ]
        _close_open_tool_calls(msgs, reason="fix")
        msgs.append({"role": "user", "content": "nudge"})
        assert _tool_chain_valid(msgs)


class TestFactoryLean:
    def test_default_is_lean(self):
        assert resolve_agent_framework({}) == "lean"
        assert resolve_agent_framework({"agent_framework": "simple"}) == "lean"

    def test_routes_to_lean(self):
        fake = MagicMock(name="lean")
        with patch.object(AgentFactory, "_create_lean_agent", return_value=fake) as c:
            agent = AgentFactory.from_dict({"name": "n", "agent_framework": "lean"})
        c.assert_called_once()
        assert agent is fake


# ---------------------------------------------------------------------------
# a model call that times out
# ---------------------------------------------------------------------------

class TestLLMTimeout:
    """
    A read timeout used to escape `chat()` as a bare TimeoutError. The loop
    only catches LLMError, so the exception tore through the generator and
    ended the turn with a generic "could not complete this request" — losing
    both the reason and anything the agent had already said.
    """

    @staticmethod
    def _client(timeout=300):
        from declarative_agent_sdk.agents.lean.runtime.llm import LeanLLMClient
        return LeanLLMClient(model="m", api_key="k",
                             base_url="http://127.0.0.1:9/v1", timeout=timeout)

    def test_a_read_timeout_becomes_a_readable_llm_error(self, monkeypatch):
        from declarative_agent_sdk.agents.lean.runtime import llm

        def boom(*args, **kwargs):
            raise TimeoutError("timed out")

        monkeypatch.setattr(llm.urllib.request, "urlopen", boom)
        with pytest.raises(llm.LLMError) as excinfo:
            self._client(300).chat([{"role": "user", "content": "hi"}])

        message = str(excinfo.value)
        assert "timed out after 300s" in message
        assert "request_timeout" in message          # says what to change

    def test_it_is_not_retried(self, monkeypatch):
        """Three attempts would spend the budget three times over."""
        from declarative_agent_sdk.agents.lean.runtime import llm

        calls = []

        def boom(*args, **kwargs):
            calls.append(1)
            raise TimeoutError("timed out")

        monkeypatch.setattr(llm.urllib.request, "urlopen", boom)
        with pytest.raises(llm.LLMError):
            self._client().chat([{"role": "user", "content": "hi"}])
        assert len(calls) == 1

    def test_a_connect_timeout_is_distinguished(self, monkeypatch):
        from declarative_agent_sdk.agents.lean.runtime import llm

        def boom(*args, **kwargs):
            raise llm.urllib.error.URLError(TimeoutError("timed out"))

        monkeypatch.setattr(llm.urllib.request, "urlopen", boom)
        with pytest.raises(llm.LLMError, match="connection timed out"):
            self._client(120).chat([{"role": "user", "content": "hi"}])

    def test_the_loop_ends_the_turn_cleanly(self, monkeypatch, tmp_path):
        """
        The point of wrapping it: the turn finishes with an error event instead
        of the exception escaping, so the transcript keeps what came before.
        """
        from declarative_agent_sdk.agents.lean.runtime.llm import LLMError
        from declarative_agent_sdk.agents.lean.runtime.loop import LeanLoop
        from declarative_agent_sdk.agents.lean.runtime.skills import SkillRegistry
        from declarative_agent_sdk.agents.lean.runtime.tools import LeanToolRegistry

        skills = SkillRegistry(root=str(tmp_path / "skills"))
        tools = LeanToolRegistry(skills, workspace=str(tmp_path / "ws"))

        class TimingOutClient:
            def chat(self, messages, tools=None):
                raise LLMError("the model call timed out after 300s. …")

        loop = LeanLoop(client=TimingOutClient(), tools=tools, skills=skills,
                        instruction="", tools_approval_required=False)
        events = list(loop.run("write the paper", "session-1"))

        assert events, "the turn produced nothing at all"
        assert events[-1].kind == "error"
        assert "timed out after 300s" in events[-1].text


# ---------------------------------------------------------------------------
# the output cap
# ---------------------------------------------------------------------------

class TestOutputCap:
    """
    agent.yaml without max_tokens left the request with no cap at all: the
    factory passed None explicitly, which beat LeanAIAgent's own default of
    4096, and `_payload` omits the key when it is None. A model then free-ran
    until it chose to stop — seen at 19,672 tokens for a one-line question.
    """

    @staticmethod
    def _agent(tmp_path, **extra):
        from declarative_agent_sdk.core.agent_factory import AgentFactory

        return AgentFactory.from_dict({
            "name": "probe", "agent_framework": "lean", "provider": "vllm",
            "model": "m", "endpoint": {"url": "http://127.0.0.1:9/v1"},
            "skills_directory": str(tmp_path / "skills"),
            "workspace_directory": str(tmp_path / "ws"),
            **extra,
        })

    def test_the_runtime_default_applies_when_yaml_is_silent(self, tmp_path):
        agent = self._agent(tmp_path)
        assert agent._client.max_tokens == 4096

    def test_the_request_carries_a_cap(self, tmp_path):
        agent = self._agent(tmp_path)
        payload = agent._client._payload([{"role": "user", "content": "x"}], None)
        assert payload["max_tokens"] == 4096

    def test_yaml_still_wins(self, tmp_path):
        agent = self._agent(tmp_path, max_tokens=1024)
        assert agent._client.max_tokens == 1024

    def test_endpoint_max_tokens_is_honoured(self, tmp_path):
        agent = self._agent(tmp_path, endpoint={"url": "http://127.0.0.1:9/v1",
                                                "max_tokens": 2048})
        assert agent._client.max_tokens == 2048


# ---------------------------------------------------------------------------
# aged tool payloads
# ---------------------------------------------------------------------------

class TestToolPayloadElision:
    """
    An agent that writes files carries their contents twice: on disk, and in
    write_file's arguments, which are re-sent with every later model call. One
    researcher held the same 25 KB paper in context three times — 23,660 tokens
    of a 97k prompt, and the reason it kept reciting the paper instead of
    answering the question.
    """

    PAPER = "# Small LLMs on Small GPUs\n" + ("Body text. " * 2500)   # ~27 KB

    def _loop(self, tmp_path, **kwargs):
        from declarative_agent_sdk.agents.lean.runtime.loop import LeanLoop
        from declarative_agent_sdk.agents.lean.runtime.skills import SkillRegistry
        from declarative_agent_sdk.agents.lean.runtime.tools import LeanToolRegistry

        skills = SkillRegistry(root=str(tmp_path / "skills"))
        tools = LeanToolRegistry(skills, workspace=str(tmp_path / "ws"))
        return LeanLoop(client=object(), tools=tools, skills=skills,
                        instruction="", tools_approval_required=False, **kwargs)

    def _write_call(self, path, content, call_id="c1"):
        import json
        return {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": call_id,
                "function": {"name": "write_file",
                             "arguments": json.dumps({"path": path, "content": content})},
            }],
        }

    def _history(self, turns=4):
        history = [{"role": "user", "content": "write the paper"}]
        for i in range(turns):
            history.append(self._write_call(f"workspace/p{i}.md", self.PAPER, f"c{i}"))
            history.append({"role": "tool", "tool_call_id": f"c{i}",
                            "content": f"wrote {len(self.PAPER)} bytes"})
        return history

    def test_old_file_contents_are_elided(self, tmp_path):
        loop = self._loop(tmp_path, tool_payload_keep=1, tool_payload_max_chars=2000)
        history = self._history(turns=4)
        before = sum(len(str(m)) for m in history)

        removed = loop._elide_old_payloads(history)

        after = sum(len(str(m)) for m in history)
        assert removed > 0
        assert after < before / 2, f"only shrank from {before} to {after}"

    def test_the_path_survives(self, tmp_path):
        """It is what lets the agent read the file back."""
        import json

        loop = self._loop(tmp_path, tool_payload_keep=0, tool_payload_max_chars=2000)
        history = self._history(turns=2)
        loop._elide_old_payloads(history)

        args = json.loads(history[1]["tool_calls"][0]["function"]["arguments"])
        assert args["path"] == "workspace/p0.md"
        assert "elided" in args["content"]
        assert str(len(self.PAPER)) in args["content"]   # says how much went

    def test_recent_interactions_are_untouched(self, tmp_path):
        """An agent editing a file now still needs to see what it just wrote."""
        import json

        loop = self._loop(tmp_path, tool_payload_keep=2, tool_payload_max_chars=2000)
        history = self._history(turns=4)
        loop._elide_old_payloads(history)

        newest = json.loads(history[-2]["tool_calls"][0]["function"]["arguments"])
        oldest = json.loads(history[1]["tool_calls"][0]["function"]["arguments"])
        assert newest["content"] == self.PAPER
        assert "elided" in oldest["content"]

    def test_it_is_idempotent(self, tmp_path):
        """
        The property that makes it cache-safe: a prefix cache is a prefix match,
        so an elision recomputed each turn would invalidate the cache each turn.
        """
        import copy

        loop = self._loop(tmp_path, tool_payload_keep=1, tool_payload_max_chars=2000)
        history = self._history(turns=4)

        loop._elide_old_payloads(history)
        once = copy.deepcopy(history)
        assert loop._elide_old_payloads(history) == 0, "a second pass changed bytes"
        assert history == once

    def test_the_tool_chain_stays_valid(self, tmp_path):
        from declarative_agent_sdk.agents.lean.runtime.loop import _tool_chain_valid

        loop = self._loop(tmp_path, tool_payload_keep=0, tool_payload_max_chars=2000)
        history = self._history(turns=3)
        loop._elide_old_payloads(history)
        assert _tool_chain_valid(history)
        for message in history:
            for call in message.get("tool_calls") or []:
                assert call["id"]                      # ids are what pair them

    def test_long_tool_results_are_shortened_with_a_head(self, tmp_path):
        loop = self._loop(tmp_path, tool_payload_keep=0, tool_payload_max_chars=500)
        history = [
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "function": {"name": "exec_command",
                                          "arguments": '{"command": "ls"}'}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "x" * 9000},
        ]
        loop._elide_old_payloads(history)
        content = history[1]["content"]
        assert len(content) < 500
        assert content.startswith("xxx")               # a head is kept
        assert "elided" in content

    def test_zero_disables_it(self, tmp_path):
        import copy

        loop = self._loop(tmp_path, tool_payload_max_chars=0)
        history = self._history(turns=4)
        untouched = copy.deepcopy(history)
        assert loop._elide_old_payloads(history) == 0
        assert history == untouched

    def test_small_payloads_are_left_alone(self, tmp_path):
        import copy

        loop = self._loop(tmp_path, tool_payload_keep=0, tool_payload_max_chars=2000)
        history = [
            self._write_call("workspace/note.md", "short note"),
            {"role": "tool", "tool_call_id": "c1", "content": "wrote 10 bytes"},
        ]
        untouched = copy.deepcopy(history)
        assert loop._elide_old_payloads(history) == 0
        assert history == untouched

    def test_arguments_that_are_not_json_still_shrink(self, tmp_path):
        loop = self._loop(tmp_path, tool_payload_keep=0, tool_payload_max_chars=500)
        history = [
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "function": {"name": "odd", "arguments": "y" * 9000}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "ok"},
        ]
        loop._elide_old_payloads(history)
        assert len(history[0]["tool_calls"][0]["function"]["arguments"]) < 500

    def test_conversation_is_never_touched(self, tmp_path):
        """It sheds payload, not dialogue — the opposite of lowering history_limit."""
        import copy

        loop = self._loop(tmp_path, tool_payload_keep=0, tool_payload_max_chars=200)
        long_answer = "Here is my reasoning. " * 200
        history = [
            {"role": "user", "content": "what do you think?"},
            {"role": "assistant", "content": long_answer},
        ]
        untouched = copy.deepcopy(history)
        assert loop._elide_old_payloads(history) == 0
        assert history == untouched
