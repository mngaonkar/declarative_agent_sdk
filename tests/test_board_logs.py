"""The control plane's own log, and reading an agent's logs from the board."""

import logging

import pytest
from starlette.testclient import TestClient

from declarative_agent_sdk.agent_board import logbook
from declarative_agent_sdk.agent_board.machines import MachineDriver, MachineError
from declarative_agent_sdk.agent_board.server import ControlPlane

AUTH = {"Authorization": "Bearer t0ken"}


@pytest.fixture
def plane(tmp_path) -> ControlPlane:
    return ControlPlane(db_path=str(tmp_path / "api.db"), token="t0ken",
                        public_url="http://127.0.0.1:8900")


@pytest.fixture
def client(plane) -> TestClient:
    return TestClient(plane.app)


def _log(level, text, name="declarative_agent_sdk.agent_board.test"):
    logging.getLogger(name).log(level, text)


# ---------------------------------------------------------------------------
# the ring buffer
# ---------------------------------------------------------------------------

class TestLogBook:
    def test_pages_by_sequence(self):
        book = logbook.LogBook(capacity=10)
        for i in range(3):
            book.emit(logging.makeLogRecord({"msg": f"m{i}", "levelname": "INFO", "name": "x"}))
        first = book.read()
        assert [r["message"] for r in first["records"]] == ["m0", "m1", "m2"]
        assert book.read(after=first["last"])["records"] == []

    def test_level_and_text_filters(self):
        book = logbook.LogBook()
        for level, msg in (("INFO", "quiet"), ("WARNING", "install slow"), ("ERROR", "install broke")):
            book.emit(logging.makeLogRecord({"msg": msg, "levelname": level, "name": "x"}))
        assert [r["message"] for r in book.read(level="WARNING")["records"]] == \
            ["install slow", "install broke"]
        assert [r["message"] for r in book.read(query="BROKE")["records"]] == ["install broke"]

    def test_limit_keeps_the_newest_and_says_so(self):
        book = logbook.LogBook()
        for i in range(5):
            book.emit(logging.makeLogRecord({"msg": f"m{i}", "levelname": "INFO", "name": "x"}))
        page = book.read(limit=2)
        assert [r["message"] for r in page["records"]] == ["m3", "m4"]
        assert page["truncated"] is True

    def test_capacity_bounds_memory(self):
        book = logbook.LogBook(capacity=3)
        for i in range(10):
            book.emit(logging.makeLogRecord({"msg": f"m{i}", "levelname": "INFO", "name": "x"}))
        page = book.read()
        assert len(page["records"]) == 3 and page["last"] == 10

    def test_access_log_is_left_out(self):
        book = logbook.LogBook()
        record = logging.makeLogRecord({"msg": "GET /api/state 200", "levelname": "INFO",
                                        "name": "uvicorn.access"})
        assert book.filter(record) is False

    def test_tracebacks_are_kept(self):
        book = logbook.LogBook()
        try:
            raise RuntimeError("kaboom")
        except RuntimeError:
            import sys
            book.emit(logging.makeLogRecord({"msg": "failed", "levelname": "ERROR",
                                             "name": "x", "exc_info": sys.exc_info()}))
        assert "RuntimeError: kaboom" in book.read()["records"][0]["message"]


class TestInstall:
    def test_writes_the_file_beside_the_database(self, plane, tmp_path):
        _log(logging.WARNING, "written to disk")
        for handler in logging.getLogger().handlers:
            handler.flush()
        text = (tmp_path / "logs" / "control.log").read_text()
        assert "written to disk" in text

    def test_repeated_installs_attach_one_buffer(self, tmp_path):
        logbook.install(str(tmp_path / "a"))
        logbook.install(str(tmp_path / "a"))
        books = [h for h in logging.getLogger().handlers if isinstance(h, logbook.LogBook)]
        assert len(books) == 1


# ---------------------------------------------------------------------------
# /api/logs
# ---------------------------------------------------------------------------

class TestControlLogsApi:
    def test_needs_the_token(self, client):
        assert client.get("/api/logs").status_code == 401

    def test_returns_new_records_only(self, client):
        _log(logging.WARNING, "before-marker")
        last = client.get("/api/logs", headers=AUTH).json()["last"]
        _log(logging.WARNING, "after-marker")
        page = client.get("/api/logs", params={"after": last}, headers=AUTH).json()
        messages = [r["message"] for r in page["records"]]
        assert any("after-marker" in m for m in messages)
        assert not any("before-marker" in m for m in messages)

    def test_bad_after_is_a_400(self, client):
        assert client.get("/api/logs", params={"after": "x"}, headers=AUTH).status_code == 400

    def test_changes_are_logged_with_their_outcome(self, client):
        last = client.get("/api/logs", headers=AUTH).json()["last"]
        client.post("/api/channels", json={"name": "ops"}, headers=AUTH)
        page = client.get("/api/logs", params={"after": last, "q": "/api/channels"},
                          headers=AUTH).json()
        assert any("POST /api/channels -> 200" in r["message"] for r in page["records"])

    def test_failures_are_logged(self, client):
        last = client.get("/api/logs", headers=AUTH).json()["last"]
        client.get("/api/agents/999/logs", headers=AUTH)
        page = client.get("/api/logs", params={"after": last, "level": "WARNING"},
                          headers=AUTH).json()
        assert any("/api/agents/999/logs -> 404" in r["message"] for r in page["records"])

    def test_unexpected_errors_are_a_logged_500(self, client, plane, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("store exploded")
        monkeypatch.setattr(plane.store, "agent", boom)
        last = client.get("/api/logs", headers=AUTH).json()["last"]
        reply = client.get("/api/agents/1/logs", headers=AUTH)
        assert reply.status_code == 500 and "store exploded" in reply.json()["error"]
        page = client.get("/api/logs", params={"after": last, "level": "ERROR"},
                          headers=AUTH).json()
        assert any("RuntimeError: store exploded" in r["message"] for r in page["records"])


# ---------------------------------------------------------------------------
# an agent's logs
# ---------------------------------------------------------------------------

LOCAL = {"name": "here", "is_local": 1, "host": "localhost"}


@pytest.fixture
def installed(tmp_path):
    root = tmp_path / "agents"
    prefix = root / "coder"
    (prefix / "logs").mkdir(parents=True)
    (prefix / "logs" / "coder.err.log").write_text(
        "".join(f"err {i}\n" for i in range(50)))
    (prefix / "logs" / "coder.log").write_text("out line\n")
    (root / "supervisor").mkdir()
    (root / "supervisor" / "supervisord.log").write_text("spawned: 'coder'\n")
    return str(prefix)


class TestAgentLogs:
    def test_tails_stderr_by_default(self, installed):
        result = MachineDriver().agent_logs(LOCAL, installed, "coder", lines=3)
        assert result["exists"] and result["text"].splitlines() == ["err 47", "err 48", "err 49"]
        assert result["path"].endswith("/logs/coder.err.log")

    def test_other_streams(self, installed):
        driver = MachineDriver()
        assert driver.agent_logs(LOCAL, installed, "coder", "stdout")["text"] == "out line\n"
        assert "spawned" in driver.agent_logs(LOCAL, installed, "coder", "supervisor")["text"]

    def test_missing_file_is_reported_not_raised(self, installed):
        result = MachineDriver().agent_logs(LOCAL, installed, "other")
        assert result["exists"] is False and result["text"] == ""

    def test_unknown_stream_is_refused(self, installed):
        with pytest.raises(MachineError):
            MachineDriver().agent_logs(LOCAL, installed, "coder", "passwd")

    def test_relative_prefix_is_refused(self):
        with pytest.raises(MachineError):
            MachineDriver().agent_logs(LOCAL, "relative/dir", "coder")

    def test_api(self, client, plane, installed):
        machine = plane.store.add_machine("here", "localhost", is_local=True)
        agent = plane.store.upsert_agent(machine["id"], {
            "name": "coder", "prefix": installed, "url": "http://a", "ready": 1})
        reply = client.get(f"/api/agents/{agent['id']}/logs",
                           params={"stream": "stderr", "lines": 2}, headers=AUTH)
        data = reply.json()
        assert reply.status_code == 200, data
        assert data["text"].splitlines() == ["err 48", "err 49"]
        assert data["machine"] == "here" and data["agent"] == "coder"


class TestInstallFailuresAreLogged:
    def test_installer_stderr_lands_in_the_log(self, monkeypatch):
        driver = MachineDriver()

        def run(machine, script, stdin=None, timeout=None, stdin_bytes=None):
            if "install.sh" in script:
                raise MachineError("command failed on 'omarchy' (exit 1)",
                                   stderr="created venv\nerror: installing 'x.whl' failed\n    uv: no space")
            return ""

        monkeypatch.setattr(driver, "run", run)
        monkeypatch.setattr(driver, "push_scripts", lambda *a, **k: None)
        monkeypatch.setattr(driver, "_script_path", lambda *a: "install.sh")
        book = logbook.book()
        last = book.read()["last"]
        with pytest.raises(MachineError):
            driver.create_agent({"name": "omarchy", "host": "h"}, name="probe",
                                sdk_source="git")
        messages = "\n".join(r["message"] for r in book.read(after=last)["records"])
        assert "install of 'probe' on 'omarchy' failed" in messages
        assert "uv: no space" in messages


# ---------------------------------------------------------------------------
# downloads
# ---------------------------------------------------------------------------

class TestDownloads:
    def test_control_log_is_an_attachment(self, client):
        _log(logging.WARNING, "download-me")
        reply = client.get("/api/logs/download", headers=AUTH)
        assert reply.status_code == 200
        assert reply.headers["content-disposition"].startswith(
            'attachment; filename="agent-board-')
        assert "download-me" in reply.text

    def test_control_log_needs_the_token(self, client):
        assert client.get("/api/logs/download").status_code == 401

    def test_control_log_falls_back_to_the_buffer(self, client, plane, monkeypatch):
        monkeypatch.setattr(plane.logbook, "file_path", None)
        _log(logging.WARNING, "only-in-memory")
        reply = client.get("/api/logs/download", headers=AUTH)
        assert "only-in-memory" in reply.text

    def test_agent_log_whole_file(self, client, plane, installed):
        machine = plane.store.add_machine("here", "localhost", is_local=True)
        agent = plane.store.upsert_agent(machine["id"], {
            "name": "coder", "prefix": installed, "url": "http://a", "ready": 1})
        reply = client.get(f"/api/agents/{agent['id']}/logs/download",
                           params={"stream": "stderr"}, headers=AUTH)
        assert reply.status_code == 200
        assert reply.text.splitlines() == [f"err {i}" for i in range(50)]
        assert 'filename="coder.here-stderr-' in reply.headers["content-disposition"]
        assert "x-log-truncated" not in reply.headers

    def test_agent_log_missing_is_a_404(self, client, plane, installed):
        machine = plane.store.add_machine("here", "localhost", is_local=True)
        agent = plane.store.upsert_agent(machine["id"], {
            "name": "ghost", "prefix": installed, "url": "http://a", "ready": 1})
        reply = client.get(f"/api/agents/{agent['id']}/logs/download", headers=AUTH)
        assert reply.status_code == 404 and "does not exist" in reply.json()["error"]

    def test_large_files_are_capped_and_flagged(self, installed, monkeypatch):
        driver = MachineDriver()
        monkeypatch.setattr(MachineDriver, "LOG_DOWNLOAD_BYTES", 16)
        result = driver.agent_log_file(LOCAL, installed, "coder", "stderr")
        assert result["truncated"] is True and len(result["text"].encode()) == 16
        assert result["size"] > 16
