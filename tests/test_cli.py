"""
Unit tests for the packaged CLI (declarative_agent_sdk.cli.main).

Nothing here contacts Discord or an LLM; the agent factory is never invoked.
"""

import pytest

from declarative_agent_sdk.cli.main import (
    DEFAULT_CONFIG_NAME,
    build_parser,
    cmd_a2a,
    cmd_version,
    load_dotenv_if_present,
    main,
    run,
    preflight,
    resolve_config_path,
)


# ---------------------------------------------------------------------------
# Entry-point shape — callers invoke this as an attribute chain
# ---------------------------------------------------------------------------

class TestEntryPointShape:
    def test_submodule_is_not_shadowed_by_the_function(self):
        """Callers run `declarative_agent_sdk.cli.main.main()`.

        Re-exporting `main` in cli/__init__.py would make `.main` resolve to the
        function, and the second `.main` would then raise AttributeError.
        """
        import declarative_agent_sdk.cli
        import declarative_agent_sdk.cli.main as main_module

        assert callable(main_module.main)
        # The attribute on the package must be the module, not the function.
        assert declarative_agent_sdk.cli.main is main_module

    def test_run_propagates_the_exit_code(self):
        """A caller that discards the return value still needs the exit code.

        run() must raise SystemExit; returning main()'s int would silently exit 0
        even when the command had failed.
        """
        with pytest.raises(SystemExit) as excinfo:
            run()
        # No argv -> argparse errors out with code 2.
        assert excinfo.value.code != 0

    def test_run_exits_zero_on_success(self, monkeypatch, capsys):
        monkeypatch.setattr("sys.argv", ["declarative-agent", "version"])
        with pytest.raises(SystemExit) as excinfo:
            run()
        assert excinfo.value.code == 0

    def test_console_script_target_is_importable(self):
        from importlib import import_module

        module = import_module("declarative_agent_sdk.cli.main")
        assert callable(getattr(module, "main"))


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

class TestParser:
    def test_subcommands_present(self):
        parser = build_parser()
        for cmd in ("discord", "a2a", "version"):
            assert parser.parse_args([cmd]).command == cmd

    def test_command_is_required(self):
        with pytest.raises(SystemExit):
            build_parser().parse_args([])

    def test_discord_defaults(self):
        args = build_parser().parse_args(["discord"])
        assert args.config is None
        assert args.check is False
        assert args.prefix == "!ask "
        assert args.log_level is None
        assert args.embed is False

    def test_log_level_is_upper_cased_and_validated(self):
        assert build_parser().parse_args(["discord", "--log-level", "debug"]).log_level == "DEBUG"
        with pytest.raises(SystemExit):
            build_parser().parse_args(["discord", "--log-level", "chatty"])

    def test_check_flag(self):
        assert build_parser().parse_args(["discord", "--check"]).check is True


class TestResolveConfigPath:
    def test_explicit_path_wins(self):
        assert resolve_config_path("/tmp/other.yaml").name == "other.yaml"

    def test_defaults_to_agent_yaml_in_cwd(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        resolved = resolve_config_path(None)
        assert resolved.name == DEFAULT_CONFIG_NAME
        assert resolved.parent == tmp_path


# ---------------------------------------------------------------------------
# Credential preflight
# ---------------------------------------------------------------------------

def _clear_keys(monkeypatch):
    for var in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY",
                "GEMINI_API_KEY", "TAVILY_API_KEY", "DISCORD_BOT_TOKEN"):
        monkeypatch.delenv(var, raising=False)


class TestPreflight:
    def test_lean_defaults_to_openai(self, monkeypatch):
        _clear_keys(monkeypatch)
        problems = preflight({"name": "a", "agent_framework": "lean"})
        assert any("OPENAI_API_KEY" in p for p in problems)

    def test_adk_defaults_to_google(self, monkeypatch):
        _clear_keys(monkeypatch)
        problems = preflight({"name": "a", "agent_framework": "adk"})
        assert any("GOOGLE_API_KEY" in p for p in problems)

    def test_deepagent_defaults_to_anthropic(self, monkeypatch):
        _clear_keys(monkeypatch)
        problems = preflight({"name": "a", "agent_framework": "deepagent"})
        assert any("ANTHROPIC_API_KEY" in p for p in problems)

    def test_satisfied_provider_is_not_reported(self, monkeypatch):
        _clear_keys(monkeypatch)
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        assert preflight({"name": "a", "provider": "openai"}) == []

    def test_vllm_needs_no_key(self, monkeypatch):
        _clear_keys(monkeypatch)
        assert preflight({"name": "a", "provider": "vllm"}) == []

    def test_litellm_needs_openai_key(self, monkeypatch):
        # lean's _resolve_api_key reads OPENAI_API_KEY for litellm and, unlike
        # vllm, gets no placeholder substitute.
        _clear_keys(monkeypatch)
        problems = preflight({"name": "a", "provider": "litellm"})
        assert any("OPENAI_API_KEY" in p for p in problems)

    def test_unknown_provider_is_reported(self, monkeypatch):
        _clear_keys(monkeypatch)
        problems = preflight({"name": "a", "provider": "mystery"})
        assert any("Unknown provider" in p for p in problems)

    def test_tavily_tool_requires_its_key(self, monkeypatch):
        _clear_keys(monkeypatch)
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        problems = preflight({"name": "a", "provider": "openai",
                              "tools": ["tavily_search"]})
        assert any("TAVILY_API_KEY" in p for p in problems)

    def test_discord_token_only_checked_when_requested(self, monkeypatch):
        _clear_keys(monkeypatch)
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        config = {"name": "a", "provider": "openai"}
        assert preflight(config) == []
        problems = preflight(config, require_discord_token=True)
        assert any("DISCORD_BOT_TOKEN" in p for p in problems)

    def test_invalid_framework_is_reported(self, monkeypatch):
        _clear_keys(monkeypatch)
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        problems = preflight({"name": "a", "agent_framework": "wat"})
        assert any("wat" in p for p in problems)


# ---------------------------------------------------------------------------
# .env discovery
# ---------------------------------------------------------------------------

class TestDotenv:
    def test_prefers_the_config_directory(self, tmp_path, monkeypatch, capsys):
        (tmp_path / ".env").write_text("FROM_CONFIG_DIR=1\n")
        monkeypatch.delenv("FROM_CONFIG_DIR", raising=False)
        load_dotenv_if_present(None, tmp_path / "agent.yaml")
        import os
        assert os.environ.get("FROM_CONFIG_DIR") == "1"
        assert "loaded environment from" in capsys.readouterr().out

    def test_explicit_env_file_wins(self, tmp_path, monkeypatch, capsys):
        (tmp_path / ".env").write_text("WHICH=config_dir\n")
        explicit = tmp_path / "custom.env"
        explicit.write_text("WHICH=explicit\n")
        monkeypatch.delenv("WHICH", raising=False)
        load_dotenv_if_present(str(explicit), tmp_path / "agent.yaml")
        import os
        assert os.environ.get("WHICH") == "explicit"

    def test_missing_explicit_file_is_reported(self, tmp_path, capsys):
        load_dotenv_if_present(str(tmp_path / "nope.env"), None)
        assert "does not exist" in capsys.readouterr().err

    def test_no_env_file_is_silent(self, tmp_path, capsys):
        load_dotenv_if_present(None, tmp_path / "agent.yaml")
        captured = capsys.readouterr()
        assert "loaded environment" not in captured.out


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

class TestCommands:
    def test_version_prints_and_succeeds(self, capsys):
        from declarative_agent_sdk.__version__ import __version__

        assert cmd_version(None) == 0
        assert __version__ in capsys.readouterr().out

    def test_version_through_main(self, capsys):
        assert main(["version"]) == 0
        assert "declarative-agent" in capsys.readouterr().out

    def test_a2a_stub_exits_two(self, capsys):
        assert cmd_a2a(None) == 2
        assert "not yet" in capsys.readouterr().err

    def test_missing_config_is_reported_not_raised(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)          # no agent.yaml here
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "t")
        assert main(["discord"]) == 1
        assert "Config not found" in capsys.readouterr().err
