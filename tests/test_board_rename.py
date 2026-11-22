"""
agent-board used to be agents-control. Agents installed then have
AGENTS_CONTROL_* in their .env, and a board set up then keeps its data in
~/.agents-control; both must keep working.
"""

from pathlib import Path

from declarative_agent_sdk.agent_board import store as store_mod
from declarative_agent_sdk.agent_board.server import ControlPlane
from declarative_agent_sdk.cli.main import build_parser
from declarative_agent_sdk.core.board_env import board_env
from declarative_agent_sdk.tools.builtin._board import Board
from declarative_agent_sdk.tools.tool_registry import ToolRegistry


class TestEnvNames:
    def test_new_name_wins(self, monkeypatch):
        monkeypatch.setenv("AGENT_BOARD_URL", "http://new:8900")
        monkeypatch.setenv("AGENTS_CONTROL_URL", "http://old:8900")
        assert board_env("URL") == "http://new:8900"

    def test_legacy_name_is_the_fallback(self, monkeypatch):
        monkeypatch.delenv("AGENT_BOARD_URL", raising=False)
        monkeypatch.delenv("AGENT_BOARD_TOKEN", raising=False)
        monkeypatch.setenv("AGENTS_CONTROL_URL", "http://old:8900/")
        monkeypatch.setenv("AGENTS_CONTROL_TOKEN", "t")
        board = Board()
        assert board.url == "http://old:8900" and board.token == "t"
        assert ToolRegistry.on_a_board()

    def test_neither_is_off_a_board(self, monkeypatch):
        for name in ("AGENT_BOARD_URL", "AGENTS_CONTROL_URL"):
            monkeypatch.delenv(name, raising=False)
        assert board_env("URL") == "" and not ToolRegistry.on_a_board()

    def test_the_board_seeds_both_names(self, tmp_path):
        plane = ControlPlane(db_path=str(tmp_path / "c.db"), token="tok",
                             public_url="http://10.0.0.1:8900")
        env = plane.agent_env("coder", "box")
        assert env["AGENT_BOARD_URL"] == env["AGENTS_CONTROL_URL"] == "http://10.0.0.1:8900"
        assert env["AGENT_BOARD_TOKEN"] == env["AGENTS_CONTROL_TOKEN"] == "tok"


class TestDataDir:
    def _homes(self, tmp_path, monkeypatch):
        monkeypatch.setattr(store_mod, "HOME_DIR", str(tmp_path / ".agent-board"))
        monkeypatch.setattr(store_mod, "LEGACY_HOME_DIR", str(tmp_path / ".agents-control"))
        return tmp_path / ".agent-board", tmp_path / ".agents-control"

    def test_new_install_uses_the_new_dir(self, tmp_path, monkeypatch):
        new, _ = self._homes(tmp_path, monkeypatch)
        assert store_mod.board_home() == new

    def test_an_old_board_keeps_its_data(self, tmp_path, monkeypatch):
        new, old = self._homes(tmp_path, monkeypatch)
        old.mkdir()
        (old / "control.db").write_text("")
        assert store_mod.default_db_path() == str(old / "control.db")

    def test_the_new_dir_wins_once_it_exists(self, tmp_path, monkeypatch):
        new, old = self._homes(tmp_path, monkeypatch)
        old.mkdir()
        (old / "control.db").write_text("")
        new.mkdir()
        assert store_mod.board_home() == new


class TestCommand:
    def test_board_and_the_old_control_name(self):
        parser = build_parser()
        for name in ("board", "control"):
            args = parser.parse_args([name, "--port", "8913"])
            assert args.port == 8913 and args.db is None


def test_the_installer_retires_the_old_service():
    script = (Path(__file__).resolve().parents[1] / "install.sh").read_text()
    assert 'LEGACY_LABEL="com.declarative-agent.control"' in script
    assert 'LEGACY_UNIT="agents-control"' in script
    assert script.count("remove_legacy_service") == 3     # defined, uninstall, install
