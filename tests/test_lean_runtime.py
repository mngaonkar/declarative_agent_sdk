"""Unit tests for the lean deliberative runtime (no network)."""

from pathlib import Path
from unittest.mock import MagicMock, patch

from declarative_agent_sdk.agents.lean.runtime.loop import (
    LeanLoop,
    _parse_control_tags,
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
