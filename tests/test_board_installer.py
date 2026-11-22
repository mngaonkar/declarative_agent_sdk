"""
The one-line installer for agent-board (install.sh at the repo root).

Only --dry-run and option checking are exercised: a real run installs a uv
tool and a launchd/systemd service, which a test must not do.
"""

import os
import subprocess
from pathlib import Path

import pytest

from declarative_agent_sdk.agent_board import store as store_module

INSTALLER = Path(__file__).resolve().parents[1] / "install.sh"


def _run(tmp_path, *args, stdin=False):
    env = {"HOME": str(tmp_path), "PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    if stdin:   # exactly how `curl … | bash -s -- ARGS` runs it
        return subprocess.run(["bash", "-s", "--", *args], input=INSTALLER.read_text(),
                              capture_output=True, text=True, env=env, timeout=60)
    return subprocess.run(["bash", str(INSTALLER), *args], capture_output=True,
                          text=True, env=env, timeout=60)


def test_dry_run_from_a_pipe_changes_nothing(tmp_path):
    result = _run(tmp_path, "--dry-run", stdin=True)
    assert result.returncode == 0, result.stderr
    assert "uv tool install --force --python 3.14" in result.stderr
    assert "git+https://github.com/mngaonkar/declarative_agent_sdk.git@master" in result.stderr
    assert "nothing was changed" in result.stderr
    assert list(tmp_path.iterdir()) == []            # not even ~/.agent-board


def test_ref_and_port_are_used(tmp_path):
    result = _run(tmp_path, "--dry-run", "--ref", "v1.0.0", "--port", "9100")
    assert "@v1.0.0" in result.stderr and ":9100" in result.stderr


@pytest.mark.parametrize("args, message", [
    (["--port", "abc"], "--port must be a number"),
    (["--host", "a;b"], "--host is not an address"),
    (["--bogus"], "unknown option"),
])
def test_bad_options_are_refused(tmp_path, args, message):
    result = _run(tmp_path, *args)
    assert result.returncode != 0 and message in result.stderr


def test_a_truncated_download_runs_nothing(tmp_path):
    # Everything is inside main(), called on the last line: a cut-off script
    # defines nothing runnable and exits without doing any step.
    text = INSTALLER.read_text()
    cut = text[: len(text) // 2]
    env = {"HOME": str(tmp_path), "PATH": os.environ.get("PATH", "")}
    result = subprocess.run(["bash", "-s"], input=cut, capture_output=True, text=True,
                            env=env, timeout=30)
    assert "installing" not in result.stderr and list(tmp_path.iterdir()) == []


def test_help(tmp_path):
    result = _run(tmp_path, "--help")
    assert result.returncode == 0 and "--uninstall" in result.stderr


def test_an_installed_control_plane_installs_agents_from_git(tmp_path, monkeypatch):
    """An installed board has no working tree to build a 'local' wheel from."""
    from starlette.testclient import TestClient
    from declarative_agent_sdk.agent_board import server as server_module
    from declarative_agent_sdk.agent_board.server import ControlPlane

    monkeypatch.setattr(server_module, "running_from_checkout", lambda: False)
    plane = ControlPlane(db_path=str(tmp_path / "c.db"), token="t",
                         public_url="http://127.0.0.1:8900")
    machine = plane.store.add_machine("here", "localhost", is_local=True)
    calls = {}
    monkeypatch.setattr(plane.driver, "create_agent", lambda m, **kw: calls.update(kw) or {
        "name": kw["name"], "ready": True, "url": "http://127.0.0.1:8001/"})
    reply = TestClient(plane.app).post(
        "/api/agents", json={"machine_id": machine["id"], "name": "a", "sdk_source": "local"},
        headers={"Authorization": "Bearer t"})
    assert reply.status_code == 200, reply.text
    assert calls["sdk_source"] == "git"


def test_building_a_local_wheel_without_a_checkout_says_why(tmp_path, monkeypatch):
    from declarative_agent_sdk.agent_board import machines
    fake = tmp_path / "site-packages" / "declarative_agent_sdk" / "agent_board" / "machines.py"
    fake.parent.mkdir(parents=True)
    fake.write_text("")
    monkeypatch.setattr(machines, "__file__", str(fake))
    with pytest.raises(machines.MachineError) as exc:
        machines.MachineDriver().build_wheel()
    assert "installed as a package" in str(exc.value) and "'git'" in str(exc.value)
