"""Unit tests for AgentFactory framework routing (agent_framework / backend)."""

import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from declarative_agent_sdk.core.agent_factory import AgentFactory, resolve_agent_framework


class TestResolveAgentFramework:
    def test_defaults_to_lean(self):
        assert resolve_agent_framework({}) == "lean"
        assert resolve_agent_framework({"name": "x"}) == "lean"

    def test_agent_framework_lean_aliases(self):
        for value in ("lean", "simple", "esp", "esp32", "native"):
            assert resolve_agent_framework({"agent_framework": value}) == "lean"

    def test_agent_framework_adk_aliases(self):
        for value in ("adk", "ADK", "google_adk", "google-adk"):
            assert resolve_agent_framework({"agent_framework": value}) == "adk"

    def test_agent_framework_deepagent_aliases(self):
        for value in ("deepagent", "deepagents", "langchain", "langgraph", "DeepAgent"):
            assert resolve_agent_framework({"agent_framework": value}) == "deepagent"

    def test_legacy_backend_key(self):
        assert resolve_agent_framework({"backend": "adk"}) == "adk"
        assert resolve_agent_framework({"backend": "langchain"}) == "deepagent"
        assert resolve_agent_framework({"backend": "deepagents"}) == "deepagent"
        assert resolve_agent_framework({"backend": "lean"}) == "lean"

    def test_agent_framework_wins_over_backend(self):
        assert resolve_agent_framework({
            "agent_framework": "deepagent",
            "backend": "adk",
        }) == "deepagent"

    def test_unknown_raises(self):
        with pytest.raises(ValueError, match="Unknown agent_framework"):
            resolve_agent_framework({"agent_framework": "crewai"})


class TestAgentFactoryRouting:
    def test_from_dict_routes_adk(self):
        fake = MagicMock(name="adk_agent")
        with patch.object(AgentFactory, "_create_adk_agent", return_value=fake) as create:
            agent = AgentFactory.from_dict({"name": "n", "agent_framework": "adk"})
        create.assert_called_once()
        assert agent is fake

    def test_from_dict_routes_deepagent(self):
        fake = MagicMock(name="deep_agent")
        with patch.object(AgentFactory, "_create_langchain_agent", return_value=fake) as create:
            agent = AgentFactory.from_dict({"name": "n", "agent_framework": "deepagent"})
        create.assert_called_once()
        assert agent is fake

    def test_from_dict_routes_legacy_backend_langchain(self):
        fake = MagicMock(name="deep_agent")
        with patch.object(AgentFactory, "_create_langchain_agent", return_value=fake) as create:
            agent = AgentFactory.from_dict({"name": "n", "backend": "langchain"})
        create.assert_called_once()
        assert agent is fake

    def test_from_dict_requires_name(self):
        with pytest.raises(ValueError, match="name"):
            AgentFactory.from_dict({"agent_framework": "deepagent"})

    def test_from_yaml_file_resolves_relative_paths(self, tmp_path: Path):
        instructions = tmp_path / "instructions.md"
        instructions.write_text("You are helpful.")
        yaml_path = tmp_path / "agent.yaml"
        yaml_path.write_text(textwrap.dedent("""\
            name: demo
            agent_framework: deepagent
            instruction_file: instructions.md
            workspace_directory: workspace
            provider: openai
            model: gpt-4o
        """))
        (tmp_path / "workspace").mkdir()

        captured = {}

        def fake_create(common):
            # Factory now passes CommonAgentConfig
            captured["instruction_file"] = common.instruction_file
            captured["workspace_directory"] = common.workspace_directory
            return MagicMock(name="agent")

        with patch.object(AgentFactory, "_create_langchain_agent", side_effect=fake_create):
            AgentFactory.from_yaml_file(str(yaml_path))

        assert Path(captured["instruction_file"]) == instructions.resolve()
        assert Path(captured["workspace_directory"]) == (tmp_path / "workspace").resolve()
