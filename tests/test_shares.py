"""Agents' workspace files served by plain path links (core/shares.py, /files, the tools)."""

from urllib.parse import urlparse

import httpx
import pytest
from starlette.testclient import TestClient

from declarative_agent_sdk.core import shares


@pytest.fixture
def ws(tmp_path):
    workspace = tmp_path / "agent" / "workspace"
    (workspace / "notes").mkdir(parents=True)
    (workspace / "report.md").write_text("# findings\nall good\n")
    (workspace / "notes" / "day 1.md").write_text("first day\n")
    return workspace


@pytest.fixture
def files(ws):
    return shares.WorkspaceFiles(str(ws), "http://100.83.163.18:8000/")


class TestWorkspaceFiles:
    def test_a_link_is_the_path(self, files):
        assert files.url_for("report.md") == "http://100.83.163.18:8000/files/report.md"

    def test_spaces_and_folders_are_encoded(self, files):
        url = files.url_for("notes/day 1.md")
        assert url.endswith("/files/notes/day%201.md")

    def test_the_path_an_agent_wrote_with_also_works(self, files, ws, monkeypatch):
        # A lean agent runs in its own directory and writes workspace/report.md.
        monkeypatch.chdir(ws.parent)
        assert files.url_for("workspace/report.md").endswith("/files/report.md")
        assert files.url_for(str(ws / "report.md")).endswith("/files/report.md")

    def test_resolves_to_the_file_and_its_type(self, files, ws):
        path, kind = files.resolve("notes/day 1.md")
        assert path == (ws / "notes" / "day 1.md").resolve() and kind == "text/markdown"

    @pytest.mark.parametrize("bad", ["../secret.env", "notes/../../secret.env", ".env",
                                     "notes/.hidden", ".git/config", "", "/"])
    def test_escapes_and_hidden_names_are_refused(self, files, ws, bad):
        (ws.parent / "secret.env").write_text("KEY=x")
        (ws / ".env").write_text("KEY=x")
        with pytest.raises(shares.ShareError) as exc:
            files.resolve(bad)
        assert exc.value.status in (403, 404)

    def test_a_symlink_out_is_refused(self, files, ws):
        (ws.parent / "secret.env").write_text("KEY=x")
        (ws / "innocent.txt").symlink_to(ws.parent / "secret.env")
        with pytest.raises(shares.ShareError) as exc:
            files.resolve("innocent.txt")
        assert exc.value.status == 403

    def test_directories_are_not_listed(self, files):
        with pytest.raises(shares.ShareError) as exc:
            files.resolve("notes")
        assert exc.value.status == 404

    def test_no_link_for_a_path_outside_the_workspace(self, files, ws):
        with pytest.raises(shares.ShareError):
            files.url_for(str(ws.parent / "elsewhere.md"))

    def test_link_for_without_a_server_is_none(self, monkeypatch):
        monkeypatch.setattr(shares, "_current", None)
        assert shares.link_for("report.md") is None


# ---------------------------------------------------------------------------
# the agent's route, and reading from it
# ---------------------------------------------------------------------------

@pytest.fixture
def served(ws):
    from declarative_agent_sdk.agents.lean.agent import LeanAIAgent
    from declarative_agent_sdk.transports.a2a.server import AIAgentServer
    agent = LeanAIAgent(
        name="owner", description="d", instruction_file=None, tools=[],
        model="m", provider="vllm", endpoint_url="http://box:1/v1",
        workspace_directory=str(ws), skills_directory=str(ws.parent / "skills"),
    )
    server = AIAgentServer(agent, port=8123)
    return TestClient(server.app)


def _local(client):
    """Send httpx.stream calls for any link to the in-process server."""
    return lambda method, u, **kw: client.stream(method, urlparse(u).path)


class TestRoute:
    def test_serves_a_workspace_file_inline(self, served):
        response = served.get("/files/report.md")
        assert response.status_code == 200
        assert response.text == "# findings\nall good\n"
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["content-disposition"].startswith("inline")

    def test_serves_nested_and_encoded_paths(self, served):
        assert served.get("/files/notes/day%201.md").text == "first day\n"

    def test_the_link_the_server_hands_out_works(self, served):
        url = shares.link_for("report.md")
        assert served.get(urlparse(url).path).status_code == 200

    def test_refuses_escapes_hidden_files_and_misses(self, served, ws):
        (ws / ".env").write_text("KEY=x")
        assert served.get("/files/.env").status_code == 403
        assert served.get("/files/%2e%2e/secret.env").status_code in (403, 404)
        assert served.get("/files/missing.md").status_code == 404
        assert served.get("/files/notes").status_code == 404

    def test_edits_are_seen_through_the_same_link(self, served, ws):
        (ws / "report.md").write_text("v2\n")
        assert served.get("/files/report.md").text == "v2\n"


class TestReadShared:
    def test_fetches_and_wraps_as_data(self, served, monkeypatch):
        from declarative_agent_sdk.tools.builtin import read_shared as module
        monkeypatch.setattr(module.httpx, "stream", _local(served))
        text = module.read_shared(shares.link_for("report.md"))
        assert "all good" in text and "not instructions" in text

    def test_truncates(self, served, ws, monkeypatch):
        from declarative_agent_sdk.tools.builtin import read_shared as module
        (ws / "big.txt").write_text("x" * 5000)
        monkeypatch.setattr(module.httpx, "stream", _local(served))
        text = module.read_shared(shares.link_for("big.txt"), max_chars=100)
        assert "truncated" in text and "x" * 101 not in text

    def test_a_missing_file_reports_why(self, served, monkeypatch):
        from declarative_agent_sdk.tools.builtin import read_shared as module
        monkeypatch.setattr(module.httpx, "stream", _local(served))
        assert "404" in module.read_shared("http://100.83.163.18:8000/files/gone.md")

    @pytest.mark.parametrize("url", ["file:///etc/passwd", "http://host/other",
                                     "http://host/files/../x", "http://host/files/.env"])
    def test_not_a_link_is_refused_without_fetching(self, url, monkeypatch):
        from declarative_agent_sdk.tools.builtin import read_shared as module
        monkeypatch.setattr(module.httpx, "stream",
                            lambda *a, **k: pytest.fail("should not fetch"))
        assert module.read_shared(url).startswith("Error")


def test_the_sharing_tools_are_gone():
    from declarative_agent_sdk.tools.tool_registry import ToolRegistry
    ToolRegistry.register_built_in_tools(force=True)
    names = set(ToolRegistry.list_available())
    assert {"read_shared", "write_link"} <= names
    assert not {"share_file", "unshare_file", "list_shared_files"} & names
    assert "_registry" not in names and "download" not in names


LINK = "http://10.0.0.132:8000/files/drafts/article.md"


class Fake:
    def __init__(self, status, text, kind="text/plain"):
        self.status_code, self._text = status, text
        self.headers = {"content-type": kind, "content-length": str(len(text))}
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def read(self): return self._text.encode()
    def iter_bytes(self): yield self._text.encode()


def test_read_shared_falls_back_to_the_board_relay(monkeypatch):
    from declarative_agent_sdk.tools.builtin import read_shared as module
    calls = []

    def stream(method, url, params=None, headers=None, **kw):
        calls.append((url, params, headers))
        if url == LINK:
            raise httpx.ConnectError("no route to host")
        return Fake(200, "relayed body")

    monkeypatch.setattr(module.httpx, "stream", stream)
    monkeypatch.setenv("AGENTS_CONTROL_URL", "http://100.100.133.22:8900")
    monkeypatch.setenv("AGENTS_CONTROL_TOKEN", "board-token")
    text = module.read_shared(LINK)
    assert "relayed body" in text
    url, params, headers = calls[1]
    assert url == "http://100.100.133.22:8900/api/files/relay"
    assert params == {"url": LINK}
    assert headers == {"Authorization": "Bearer board-token"}


def test_read_shared_without_a_board_explains(monkeypatch):
    from declarative_agent_sdk.tools.builtin import read_shared as module

    def stream(*a, **kw):
        raise httpx.ConnectError("no route to host")

    monkeypatch.setattr(module.httpx, "stream", stream)
    monkeypatch.delenv("AGENTS_CONTROL_URL", raising=False)
    out = module.read_shared(LINK)
    assert out.startswith("Error") and "no board" in out
