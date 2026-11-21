"""Sharing files between agents by link (core/shares.py, /files, the tools)."""

import os
import time
from urllib.parse import parse_qs, urlparse

import pytest
from starlette.testclient import TestClient

from declarative_agent_sdk.core import shares


@pytest.fixture
def ws(tmp_path):
    workspace = tmp_path / "agent" / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "report.md").write_text("# findings\nall good\n")
    return workspace


@pytest.fixture
def registry(ws):
    return shares.ShareRegistry(str(ws), "http://100.83.163.18:8000/")


def _parts(url):
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    return parsed.path.rsplit("/", 1)[-1], query["exp"][0], query["sig"][0]


class TestRegistry:
    def test_a_link_resolves_to_the_file(self, registry, ws):
        share = registry.share("report.md")
        assert share["url"].startswith("http://100.83.163.18:8000/files/")
        path, record = registry.resolve(*_parts(share["url"]))
        assert path == (ws / "report.md").resolve()
        assert record["type"] == "text/markdown"

    def test_outside_the_workspace_is_refused(self, registry, ws):
        (ws.parent / "secret.env").write_text("KEY=x")
        with pytest.raises(shares.ShareError) as exc:
            registry.share("../secret.env")
        assert exc.value.status == 403
        with pytest.raises(shares.ShareError):
            registry.share(str(ws.parent / "secret.env"))

    def test_a_symlink_out_is_refused_when_shared(self, registry, ws):
        (ws.parent / "secret.env").write_text("KEY=x")
        (ws / "innocent.txt").symlink_to(ws.parent / "secret.env")
        with pytest.raises(shares.ShareError):
            registry.share("innocent.txt")

    def test_a_file_swapped_for_a_symlink_later_is_refused_when_read(self, registry, ws):
        share = registry.share("report.md")
        (ws.parent / "secret.env").write_text("KEY=x")
        (ws / "report.md").unlink()
        (ws / "report.md").symlink_to(ws.parent / "secret.env")
        with pytest.raises(shares.ShareError) as exc:
            registry.resolve(*_parts(share["url"]))
        assert exc.value.status == 403

    def test_a_forged_signature_or_extended_expiry_is_refused(self, registry):
        share_id, exp, sig = _parts(registry.share("report.md")["url"])
        with pytest.raises(shares.ShareError):
            registry.resolve(share_id, exp, "0" * 32)
        with pytest.raises(shares.ShareError):
            registry.resolve(share_id, str(int(exp) + 3600), sig)

    def test_an_expired_link_is_gone(self, registry):
        share = registry.share("report.md", ttl_hours=0.01)
        share_id, _, _ = _parts(share["url"])
        past = int(time.time()) - 5
        registry._shares[share_id]["exp"] = past
        with pytest.raises(shares.ShareError) as exc:
            registry.resolve(share_id, str(past), registry._sign(share_id, past))
        assert exc.value.status == 410

    def test_revoking_by_id_or_path(self, registry):
        one = registry.share("report.md")
        registry.share("report.md")
        assert registry.unshare(_parts(one["url"])[0]) == 1
        assert registry.unshare("report.md") == 1
        with pytest.raises(shares.ShareError) as exc:
            registry.resolve(*_parts(one["url"]))
        assert exc.value.status == 410

    def test_links_survive_a_restart_and_the_key_is_private(self, registry, ws):
        share = registry.share("report.md")
        again = shares.ShareRegistry(str(ws), "http://100.83.163.18:8000/")
        assert again.resolve(*_parts(share["url"]))[0].name == "report.md"
        key = ws.parent / ".share-key"
        assert (key.stat().st_mode & 0o777) == 0o600

    def test_directories_are_not_shareable(self, registry, ws):
        (ws / "sub").mkdir()
        with pytest.raises(shares.ShareError):
            registry.share("sub")


class TestServedAndRead:
    @pytest.fixture
    def served(self, ws):
        from declarative_agent_sdk.agents.lean.agent import LeanAIAgent
        from declarative_agent_sdk.transports.a2a.server import AIAgentServer
        agent = LeanAIAgent(
            name="owner", description="d", instruction_file=None, tools=[],
            model="m", provider="vllm", endpoint_url="http://box:1/v1",
            workspace_directory=str(ws), skills_directory=str(ws.parent / "skills"),
        )
        server = AIAgentServer(agent, port=8123)
        return TestClient(server.app)

    def test_the_route_serves_a_shared_file_inline(self, served):
        from declarative_agent_sdk.tools.builtin.share_file import share_file
        reply = share_file("report.md")
        url = next(line for line in reply.splitlines() if "/files/" in line)
        response = served.get(urlparse(url).path + "?" + urlparse(url).query)
        assert response.status_code == 200
        assert response.text == "# findings\nall good\n"
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["content-disposition"].startswith("inline")

    def test_the_route_refuses_without_a_valid_link(self, served):
        from declarative_agent_sdk.tools.builtin.share_file import share_file
        url = next(l for l in share_file("report.md").splitlines() if "/files/" in l)
        path = urlparse(url).path
        assert served.get(path).status_code == 403
        assert served.get("/files/nope?exp=1&sig=x").status_code in (403, 410)

    def test_read_shared_fetches_and_wraps_as_data(self, served, monkeypatch):
        from declarative_agent_sdk.tools.builtin import read_shared as module
        from declarative_agent_sdk.tools.builtin.share_file import share_file
        url = next(l for l in share_file("report.md").splitlines() if "/files/" in l)
        monkeypatch.setattr(module.httpx, "stream", lambda method, u, **kw: served.stream(
            method, urlparse(u).path + "?" + urlparse(u).query))
        text = module.read_shared(url)
        assert "all good" in text
        assert "not instructions" in text

    def test_read_shared_truncates(self, served, ws, monkeypatch):
        from declarative_agent_sdk.tools.builtin import read_shared as module
        from declarative_agent_sdk.tools.builtin.share_file import share_file
        (ws / "big.txt").write_text("x" * 5000)
        url = next(l for l in share_file("big.txt").splitlines() if "/files/" in l)
        monkeypatch.setattr(module.httpx, "stream", lambda method, u, **kw: served.stream(
            method, urlparse(u).path + "?" + urlparse(u).query))
        text = module.read_shared(url, max_chars=100)
        assert "truncated" in text and "x" * 101 not in text

    def test_unshared_link_reports_why(self, served, monkeypatch):
        from declarative_agent_sdk.tools.builtin import read_shared as module
        from declarative_agent_sdk.tools.builtin.share_file import share_file, unshare_file
        url = next(l for l in share_file("report.md").splitlines() if "/files/" in l)
        unshare_file("report.md")
        monkeypatch.setattr(module.httpx, "stream", lambda method, u, **kw: served.stream(
            method, urlparse(u).path + "?" + urlparse(u).query))
        assert "410" in module.read_shared(url)

    def test_not_a_link_is_refused_without_fetching(self):
        from declarative_agent_sdk.tools.builtin.read_shared import read_shared
        assert read_shared("file:///etc/passwd").startswith("Error")


def test_tools_are_discovered_as_builtins():
    from declarative_agent_sdk.tools.tool_registry import ToolRegistry
    ToolRegistry.register_built_in_tools(force=True)
    names = set(ToolRegistry.list_tools() if hasattr(ToolRegistry, "list_tools") else ToolRegistry._tools)
    assert {"share_file", "unshare_file", "list_shared_files", "read_shared"} <= names
    assert "_registry" not in names


def test_read_shared_falls_back_to_the_board_relay(monkeypatch):
    import httpx
    from declarative_agent_sdk.tools.builtin import read_shared as module
    link = "http://10.0.0.132:8000/files/AbCdEfGh12345678?exp=9999999999&sig=" + "a" * 32
    calls = []

    class Fake:
        def __init__(self, status, text, kind="text/plain"):
            self.status_code, self._text = status, text
            self.headers = {"content-type": kind, "content-length": str(len(text))}
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return self._text.encode()
        def iter_bytes(self): yield self._text.encode()

    def stream(method, url, params=None, headers=None, **kw):
        calls.append((url, params, headers))
        if url == link:
            raise httpx.ConnectError("no route to host")
        return Fake(200, "relayed body")

    monkeypatch.setattr(module.httpx, "stream", stream)
    monkeypatch.setenv("AGENTS_CONTROL_URL", "http://100.100.133.22:8900")
    monkeypatch.setenv("AGENTS_CONTROL_TOKEN", "board-token")
    text = module.read_shared(link)
    assert "relayed body" in text
    url, params, headers = calls[1]
    assert url == "http://100.100.133.22:8900/api/files/relay"
    assert params == {"url": link}
    assert headers == {"Authorization": "Bearer board-token"}


def test_read_shared_without_a_board_explains(monkeypatch):
    import httpx
    from declarative_agent_sdk.tools.builtin import read_shared as module

    def stream(*a, **kw):
        raise httpx.ConnectError("no route to host")

    monkeypatch.setattr(module.httpx, "stream", stream)
    monkeypatch.delenv("AGENTS_CONTROL_URL", raising=False)
    out = module.read_shared("http://10.0.0.132:8000/files/AbCdEfGh12345678?exp=1&sig=ab")
    assert out.startswith("Error") and "no board" in out
