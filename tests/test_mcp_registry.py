"""
Browsing the official MCP Registry from the MCP tab (MCP-CAT-13): each result
becomes ready catalog specs; nothing is added until the user saves the form.
Samples follow the registry's real shapes (registry.modelcontextprotocol.io).
"""

import httpx
from starlette.testclient import TestClient

import declarative_agent_sdk.agent_board.server as server_module
from declarative_agent_sdk.agent_board import mcp_catalog
from declarative_agent_sdk.agent_board.mcp_catalog import registry_entry
from declarative_agent_sdk.agent_board.server import ControlPlane

AUTH = {"Authorization": "Bearer t0ken"}


def item(**server):
    server.setdefault("name", "io.github.someone/Some_Server")
    server.setdefault("version", "1.2.0")
    return {"server": server, "_meta": {"io.modelcontextprotocol.registry/official": {
        "status": "active"}}}


NPM = item(repository={"url": "https://github.com/someone/some-server", "source": "github"},
           packages=[{"registryType": "npm", "identifier": "@someone/some-server",
                      "version": "1.2.0", "transport": {"type": "stdio"},
                      "runtimeArguments": [{"type": "positional", "value": "-y"}],
                      "packageArguments": [{"type": "positional", "value": "mcp"},
                                           {"type": "named", "name": "--root",
                                            "isRequired": True, "valueHint": "path"}],
                      "environmentVariables": [
                          {"name": "API_KEY", "isRequired": True, "isSecret": True,
                           "description": "the key"},
                          {"name": "LOG_LEVEL", "default": "info"}]}])
PYPI = item(packages=[{"registryType": "pypi", "identifier": "github-mcp-server",
                       "version": "2.5.7", "transport": {"type": "stdio"}}])
OCI = item(packages=[{"registryType": "oci", "identifier": "ghcr.io/x/server:1.0.1",
                      "transport": {"type": "stdio"},
                      "environmentVariables": [{"name": "TOKEN", "isSecret": True}]}])
REMOTE = item(name="ai.example/search", remotes=[{
    "type": "streamable-http", "url": "https://example.ai/mcp",
    "headers": [{"name": "Authorization", "isSecret": True, "description": "Bearer key"}]}])


def only_option(entry):
    (option,) = entry["options"]
    return option


class TestConversion:
    def test_an_npm_package_runs_with_npx(self):
        option = only_option(registry_entry(NPM))
        spec = option["spec"]
        assert spec["command"] == "npx"
        assert spec["args"] == ["-y", "@someone/some-server@1.2.0", "mcp", "--root", "<path>"]
        assert spec["env"] == {"API_KEY": "${API_KEY}"}       # not the optional LOG_LEVEL
        assert {v["name"] for v in option["vars"]} == {"API_KEY", "LOG_LEVEL"}

    def test_a_pypi_package_runs_with_uvx_pinned(self):
        spec = only_option(registry_entry(PYPI))["spec"]
        assert spec["command"] == "uvx" and spec["args"] == ["github-mcp-server==2.5.7"]

    def test_a_docker_image_passes_its_variables(self):
        spec = only_option(registry_entry(OCI))["spec"]
        assert spec["command"] == "docker"
        assert spec["args"] == ["run", "-i", "--rm", "-e", "TOKEN", "ghcr.io/x/server:1.0.1"]
        assert spec["env"] == {"TOKEN": "${TOKEN}"}

    def test_a_hosted_server_gets_a_token_reference(self):
        option = only_option(registry_entry(REMOTE))
        assert option["spec"] == {"transport": "http", "url": "https://example.ai/mcp",
                                  "headers": {"Authorization": "Bearer ${SEARCH_TOKEN}"}}
        assert option["vars"][0]["name"] == "SEARCH_TOKEN"

    def test_what_cannot_run_says_why(self):
        entry = registry_entry(item(packages=[
            {"registryType": "npm", "identifier": "x", "transport": {"type": "sse"}},
            {"registryType": "nuget", "identifier": "y", "transport": {"type": "stdio"}}]))
        reasons = [o["unsupported"] for o in entry["options"]]
        assert "spec" not in entry["options"][0] and "start it yourself" in reasons[0]
        assert "nuget packages are not supported" in reasons[1]

    def test_every_offered_spec_passes_the_catalog_check(self):
        for sample in (NPM, PYPI, OCI, REMOTE):
            entry = registry_entry(sample)
            for option in entry["options"]:
                mcp_catalog.check(entry["slug"], option["spec"])

    def test_the_name_and_links(self):
        entry = registry_entry(NPM)
        assert entry["slug"] == "some_server"           # lower-cased, '_' kept
        assert entry["repository"] == "https://github.com/someone/some-server"
        bad = registry_entry(item(repository={"url": "javascript:alert(1)"}))
        assert bad["repository"] == ""


class TestRoute:
    def _board(self, tmp_path, monkeypatch, handler):
        seen = []

        def record(request):
            seen.append(request)
            return handler(request)

        original = server_module.httpx.AsyncClient
        monkeypatch.setattr(server_module.httpx, "AsyncClient",
                            lambda **kw: original(transport=httpx.MockTransport(record)))
        plane = ControlPlane(db_path=str(tmp_path / "b.db"), token="t0ken")
        return TestClient(plane.app), seen

    def test_search_returns_entries_and_the_next_page(self, tmp_path, monkeypatch):
        client, seen = self._board(tmp_path, monkeypatch, lambda r: httpx.Response(
            200, json={"servers": [NPM, REMOTE], "metadata": {"nextCursor": "abc"}}))
        body = client.get("/api/mcp/registry", params={"search": "github"}, headers=AUTH).json()
        assert [s["slug"] for s in body["servers"]] == [registry_entry(NPM)["slug"], "search"]
        assert body["next"] == "abc"
        sent = seen[0].url
        assert sent.host == "registry.modelcontextprotocol.io"
        assert sent.params["search"] == "github" and sent.params["version"] == "latest"

    def test_registry_down_is_a_502_with_the_reason(self, tmp_path, monkeypatch):
        client, _ = self._board(tmp_path, monkeypatch, lambda r: httpx.Response(503))
        reply = client.get("/api/mcp/registry", headers=AUTH)
        assert reply.status_code == 502 and "MCP Registry" in reply.json()["error"]

    def test_needs_the_token(self, tmp_path, monkeypatch):
        client, seen = self._board(tmp_path, monkeypatch, lambda r: httpx.Response(200, json={}))
        assert client.get("/api/mcp/registry").status_code == 401 and seen == []


class TestOfficial:
    """MCP-CAT-14: only servers published under their vendor's own namespace."""

    def test_publisher_needs_the_exact_namespace(self):
        assert mcp_catalog.publisher("io.github.github/github-mcp-server") == "GitHub"
        assert mcp_catalog.publisher("com.stripe/mcp") == "Stripe"
        assert mcp_catalog.publisher("io.github.GitHub30/zenn-mcp-server") == ""
        assert mcp_catalog.publisher("com.thenextgennexus/github-mcp-server") == ""
        assert mcp_catalog.publisher("io.github.ClickHouse/mcp-clickhouse") == "ClickHouse"

    def test_an_entry_carries_its_publisher(self):
        assert registry_entry(item(name="com.notion/mcp"))["publisher"] == "Notion"
        assert registry_entry(NPM)["publisher"] == ""

    def _board(self, tmp_path, monkeypatch, fail=()):
        """A registry answering each namespace search with its servers, plus look-alikes."""
        asked = []

        def answer(request):
            search = request.url.params.get("search", "")
            asked.append(search)
            if search in fail:
                return httpx.Response(503)
            servers = [item(name=f"{search}/server", description="a server"),
                       item(name=f"{search}-copycat/server")]
            if search == "io.github.github":
                servers.append(item(name="io.github.GitHub30/note-mcp-server"))
            return httpx.Response(200, json={"servers": servers, "metadata": {}})

        original = server_module.httpx.AsyncClient
        monkeypatch.setattr(server_module.httpx, "AsyncClient",
                            lambda **kw: original(transport=httpx.MockTransport(answer)))
        plane = ControlPlane(db_path=str(tmp_path / "b.db"), token="t0ken")
        return TestClient(plane.app), asked

    def test_an_empty_search_lists_every_vendors_servers_only(self, tmp_path, monkeypatch):
        client, asked = self._board(tmp_path, monkeypatch)
        body = client.get("/api/mcp/registry", params={"official": "1"}, headers=AUTH).json()
        names = {s["name"] for s in body["servers"]}
        assert "io.github.github/server" in names and "com.stripe/server" in names
        assert not any("copycat" in n or "GitHub30" in n for n in names)
        assert set(asked) == set(mcp_catalog.OFFICIAL_PUBLISHERS)

    def test_it_is_cached(self, tmp_path, monkeypatch):
        client, asked = self._board(tmp_path, monkeypatch)
        client.get("/api/mcp/registry", params={"official": "1"}, headers=AUTH)
        first = len(asked)
        client.get("/api/mcp/registry", params={"official": "1", "search": "stripe"},
                   headers=AUTH)
        assert len(asked) == first

    def test_search_words_match_outside_the_namespace(self, tmp_path, monkeypatch):
        client, _ = self._board(tmp_path, monkeypatch)
        body = client.get("/api/mcp/registry", params={"official": "1", "search": "github"},
                          headers=AUTH).json()
        assert [s["publisher"] for s in body["servers"]][0] == "GitHub"
        # io.github.redis/server is not a match just for its namespace.
        assert "io.github.redis/server" not in {s["name"] for s in body["servers"]}

    def test_one_vendor_failing_hides_only_that_vendor(self, tmp_path, monkeypatch):
        client, _ = self._board(tmp_path, monkeypatch, fail=("com.stripe",))
        body = client.get("/api/mcp/registry", params={"official": "1"}, headers=AUTH).json()
        publishers = {s["publisher"] for s in body["servers"]}
        assert "Stripe" not in publishers and "GitHub" in publishers
