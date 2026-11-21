"""Unit tests for ToolRegistry name resolution and unknown-name warnings."""

import logging

from declarative_agent_sdk.cli.main import preflight_warnings
from declarative_agent_sdk.core.agent_factory import AgentFactory
from declarative_agent_sdk.tools.tool_registry import ToolRegistry


class TestRegisterBuiltInTools:
    def test_loads_builtins(self):
        ToolRegistry.register_built_in_tools()
        for name in ("exec_command", "web_request", "read_file", "write_file"):
            assert name in ToolRegistry.list_available()

    def test_is_idempotent(self, caplog):
        ToolRegistry.register_built_in_tools()
        with caplog.at_level(logging.INFO):
            ToolRegistry.register_built_in_tools()
        assert "built-in tools" not in caplog.text


class TestResolve:
    def test_resolves_names_to_callables(self):
        resolved = ToolRegistry.resolve(["exec_command", "web_request"])
        assert [fn.__name__ for fn in resolved] == ["exec_command", "web_request"]

    def test_passes_callables_through(self):
        def my_tool() -> str:
            return "x"

        assert ToolRegistry.resolve([my_tool]) == [my_tool]

    def test_unknown_name_warns_and_is_dropped(self, caplog):
        with caplog.at_level(logging.WARNING):
            resolved = ToolRegistry.resolve(["totally_bogus", "exec_command"])
        assert [fn.__name__ for fn in resolved] == ["exec_command"]
        assert "Unknown tool 'totally_bogus'" in caplog.text
        assert "exec_command" in caplog.text  # available-tools list

    def test_typo_gets_a_suggestion(self, caplog):
        with caplog.at_level(logging.WARNING):
            ToolRegistry.resolve(["web_requests"], agent_name="demo")
        assert "Did you mean 'web_request'?" in caplog.text
        assert "for agent 'demo'" in caplog.text

    def test_keep_unresolved_retains_the_string(self, caplog):
        with caplog.at_level(logging.WARNING):
            resolved = ToolRegistry.resolve(["nope"], keep_unresolved=True)
        assert resolved == ["nope"]
        assert "Unknown tool 'nope'" in caplog.text

    def test_empty_and_none(self):
        assert ToolRegistry.resolve(None) == []
        assert ToolRegistry.resolve([]) == []


class TestCoreTools:
    def test_core_tools_off_a_board(self, monkeypatch):
        monkeypatch.delenv("AGENTS_CONTROL_URL", raising=False)
        names = [fn.__name__ for fn in ToolRegistry.core_tools()]
        assert names == ["read_file", "write_file", "web_request", "exec_command", "send_to_agent", "share_file", "read_shared"]

    def test_core_tools_need_no_registration_call(self):
        ToolRegistry.register_built_in_tools(force=True)
        assert ToolRegistry.core_tools()


class TestDedupe:
    def test_keeps_first_occurrence_by_name(self):
        def a() -> None: ...
        def b() -> None: ...

        assert ToolRegistry.dedupe([a, b, a]) == [a, b]

    def test_falls_back_to_identity_for_unnamed(self):
        tool = object()
        assert ToolRegistry.dedupe([tool, tool]) == [tool]


class TestSkillToolLookup:
    """`tools:` is the allowlist for every non-core tool, skill tools included."""

    def test_listed_skill_tool_resolves_from_extra(self, caplog):
        def my_skill_tool() -> str:
            return "ok"

        with caplog.at_level(logging.WARNING):
            resolved = ToolRegistry.resolve(
                ["my_skill_tool", "web_request"],
                agent_name="demo",
                extra={"my_skill_tool": my_skill_tool},
            )
        assert resolved[0] is my_skill_tool
        assert resolved[1].__name__ == "web_request"
        assert "Unknown tool" not in caplog.text

    def test_warning_lists_skill_tools_as_available(self, caplog):
        def my_skill_tool() -> str:
            return "ok"

        with caplog.at_level(logging.WARNING):
            ToolRegistry.resolve(
                ["my_skil_tool"], extra={"my_skill_tool": my_skill_tool}
            )
        assert "Did you mean 'my_skill_tool'?" in caplog.text


class TestFactoryPassthrough:
    """The runtime resolves names; the factory must not pre-resolve or warn."""

    def test_names_pass_through_untouched(self, caplog):
        with caplog.at_level(logging.WARNING):
            assert AgentFactory._resolve_tool_names(
                ["exec_command", "a_skill_tool"], "demo"
            ) == ["exec_command", "a_skill_tool"]
        assert "Unknown tool" not in caplog.text

    def test_empty_list(self):
        assert AgentFactory._resolve_tool_names([], "demo") == []
        assert AgentFactory._resolve_tool_names(None, "demo") == []


class TestPreflightWarnings:
    def test_clean_config_has_no_warnings(self):
        assert preflight_warnings({"name": "a", "tools": ["exec_command"]}) == []

    def test_no_tools_key(self):
        assert preflight_warnings({"name": "a"}) == []

    def test_bad_name_is_reported_once(self):
        warnings = preflight_warnings(
            {"name": "a", "tools": ["exec_comand", "web_request"]}
        )
        assert len(warnings) == 1
        assert "exec_comand" in warnings[0]
        assert "Did you mean 'exec_command'?" in warnings[0]


class TestPreflightSkillDeferral:
    def test_skips_check_when_skills_declared(self):
        config = {"name": "a", "skills": ["toc"], "tools": ["toc_generate"]}
        assert preflight_warnings(config) == []

    def test_still_checks_without_skills(self):
        config = {"name": "a", "tools": ["toc_generate"]}
        assert len(preflight_warnings(config)) == 1
