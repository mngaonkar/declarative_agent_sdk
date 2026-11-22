"""
exec_command: how a model's command actually runs.

These cases are all taken from one real failure. An agent was asked to turn a
Markdown white paper into a PDF on a machine that had both pandoc and pdflatex
installed. It spent twenty minutes probing for a toolchain, concluded none
existed, and reported nothing. Every probe it ran was defeated here rather than
on the machine.
"""

import json

import pytest

from declarative_agent_sdk.tools.builtin.exec_tool import (
    _needs_a_shell,
    exec_command,
)


class TestShellSyntax:
    """
    Splitting a command with shell syntax into argv does not fail — it runs
    something else and reports that thing's exit code.
    """

    @pytest.mark.parametrize("command,why", [
        ("pwd && ls -la workspace", "&&"),
        ("ls || echo no", "||"),
        ("which pandoc; python3 -V", ";"),
        ("ps aux | grep python", "|"),
        ("ls x 2>/dev/null", ">"),
        ("cat < input.txt", "<"),
        ("echo $(date)", "$("),
        ("echo `date`", "`"),
        ("wc -c workspace/*.md", "*"),
        ("echo $HOME", "$H"),
        ("echo ${HOME}", "${"),
    ])
    def test_syntax_a_shell_has_to_carry_out_is_recognised(self, command, why):
        assert why in _needs_a_shell(command)

    @pytest.mark.parametrize("command", [
        "pandoc paper.md -o paper.pdf",
        "which pandoc",
        "ls -la /tmp",
        "sed -n '195,345p' paper.md",
    ])
    def test_a_plain_command_needs_no_shell(self, command):
        assert _needs_a_shell(command) == ""

    def test_a_builtin_is_not_a_program(self):
        """execvp cannot find `command`; only a shell has it."""
        assert "builtin" in _needs_a_shell("command -v brew")

    def test_unbalanced_quotes_are_reported_not_raised(self):
        assert _needs_a_shell('echo "unclosed') == "unbalanced quotes"


class TestAutoShell:
    def test_the_compound_command_that_defeated_the_agent_now_runs(self):
        """
        `pwd && ls -la workspace/whitepaper` ran as argv became `pwd` with
        three junk arguments: 'usage: pwd', exit 1, no output. The agent read
        that as a missing directory.
        """
        result = exec_command(command="pwd && echo second-half-ran")
        assert result["success"] is True
        assert result["shell"] is True
        assert "second-half-ran" in result["stdout"]

    def test_a_plain_command_still_avoids_the_shell(self):
        result = exec_command(command="echo plain")
        assert result["stdout"].strip() == "plain"
        assert result["shell"] is False

    def test_a_builtin_gets_a_shell(self):
        result = exec_command(command="command -v echo")
        assert result["success"] is True and result["shell"] is True

    def test_shell_true_is_still_honoured(self):
        result = exec_command(command="echo forced", shell=True)
        assert result["shell"] is True and result["stdout"].strip() == "forced"

    def test_shell_false_refuses_rather_than_mangles(self):
        """
        The caller asked for no shell and the command needs one. Running the
        first word with the rest as arguments would answer a question nobody
        asked, so it does not run at all.
        """
        result = exec_command(command="pwd && ls", shell=False)
        assert result["success"] is False
        assert "needs a shell" in result["error"]
        assert "shell=true" in result["error"]
        assert result["stdout"] == ""

    @pytest.mark.parametrize("value,expected_shell", [
        ("true", True), ("yes", True), ("1", True),
        ("auto", True), ("", True), ("none", True),
    ])
    def test_string_arguments_from_a_model_are_coerced(self, value, expected_shell):
        """Tool arguments arrive as JSON strings; 'auto' and '' mean unset."""
        result = exec_command(command="echo x && echo y", shell=value)
        assert result["shell"] is expected_shell

    def test_a_string_false_still_refuses(self):
        result = exec_command(command="echo x && echo y", shell="false")
        assert "needs a shell" in result["error"]


class TestPartialSuccess:
    def test_a_non_zero_exit_with_output_is_flagged(self):
        """
        `which pandoc wkhtmltopdf weasyprint md-to-pdf` printed
        /opt/homebrew/bin/pandoc and exited 1, because three of the four names
        were missing. The agent read success=false and concluded that no
        converter was installed — the answer was in the payload it discarded.
        """
        result = exec_command(command="which pandoc definitely-not-a-real-binary")
        if result["stdout"].strip():            # skip where pandoc is absent
            assert result["success"] is False
            assert "partial success" in result["note"]

    def test_a_clean_failure_carries_no_note(self):
        result = exec_command(command="which definitely-not-a-real-binary")
        assert result["success"] is False
        assert "note" not in result or not result["note"]

    def test_a_success_carries_no_note(self):
        assert "note" not in exec_command(command="echo ok")

    def test_the_result_is_json_serialisable(self):
        """It goes into the model's context as JSON."""
        json.dumps(exec_command(command="echo ok"))


class TestStillSafe:
    def test_a_missing_binary_is_reported_not_raised(self):
        result = exec_command(command="definitely-not-a-real-binary")
        assert result["success"] is False
        assert result["error"] or result["exit_code"] != 0

    def test_a_timeout_is_reported(self):
        result = exec_command(command="sleep 5", timeout=1)
        assert result["timed_out"] is True
        assert "timed out" in result["error"]

    def test_a_bad_timeout_does_not_run_the_command(self):
        result = exec_command(command="echo should-not-run", timeout="soon")
        assert result["stdout"] == "" and "timeout must be" in result["error"]
