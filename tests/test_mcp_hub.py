"""
The MCP hub against a real MCP server (tests/mcp_server_fixture.py over stdio):
tools and names, results, errors, timeouts, failed servers, reconnecting,
shutdown, and the lean runtime using it all.
"""

import asyncio
import os
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("mcp", reason="the mcp extra is not installed")

from declarative_agent_sdk.tools.mcp import McpHub, parse_mcp_servers, tool_name  # noqa: E402

FIXTURE = str(Path(__file__).resolve().parent / "mcp_server_fixture.py")


def fixture_server(name="fx", **extra):
    return {name: {"transport": "stdio", "command": sys.executable, "args": [FIXTURE], **extra}}


def start(raw, workspace):
    return McpHub(parse_mcp_servers(raw, os.environ, str(workspace)), str(workspace)).start()


@pytest.fixture(scope="module")
def ws(tmp_path_factory):
    return tmp_path_factory.mktemp("mcp-ws")


@pytest.fixture(scope="module")
def hub(ws):
    hub = start(fixture_server(timeout=3), ws)
    yield hub
    hub.close()


class TestTools:
    def test_names_are_prefixed_by_server(self, hub):
        assert {t.name for t in hub.tools} == {
            "fx__echo", "fx__add", "fx__picture", "fx__wipe", "fx__slow", "fx__broken", "fx__die"}

    def test_names_are_safe_and_short(self):
        assert tool_name("gh", "repo.search/code") == "gh__repo_search_code"
        assert len(tool_name("server", "x" * 100)) == 64

    def test_schema_and_hints_come_from_the_server(self, hub):
        tools = {t.name: t for t in hub.tools}
        assert tools["fx__add"].input_schema["required"] == ["a", "b"]
        assert tools["fx__echo"].read_only and not tools["fx__echo"].destructive
        assert tools["fx__wipe"].destructive
        assert tools["fx__add"].description.startswith("[fx] Add two")

    def test_an_allowlist_keeps_only_those(self, ws):
        hub = start(fixture_server(tools=["echo", "nosuch"]), ws)
        try:
            assert [t.name for t in hub.tools] == ["fx__echo"]
        finally:
            hub.close()


class TestCalls:
    def test_text(self, hub):
        assert hub.call("fx__echo", {"text": "hi"}) == "echo: hi"
        assert hub.call("fx__add", {"a": 2, "b": 3}) == "5"

    def test_from_another_event_loop(self, hub):
        assert asyncio.run(hub.acall("fx__echo", {"text": "async"})) == "echo: async"

    def test_an_image_is_saved_and_linked(self, hub, ws):
        out = hub.call("fx__picture", {})
        assert out.startswith("[image/png, ") and "saved as picture-" in out
        saved = list((ws / "mcp" / "fx").glob("picture-*.png"))
        assert saved and saved[0].read_bytes().startswith(b"\x89PNG")

    def test_a_tool_error_is_an_error_result(self, hub):
        assert hub.call("fx__broken", {}).startswith("Error:")

    def test_a_slow_call_is_stopped(self, hub):
        out = hub.call("fx__slow", {"seconds": 6})
        assert out == "Error: fx__slow took longer than 3s and was stopped"
        assert hub.call("fx__echo", {"text": "still here"}) == "echo: still here"

    def test_an_unknown_tool(self, hub):
        with pytest.raises(Exception, match="no MCP tool"):
            hub.call("fx__nope", {})


class TestFailures:
    def test_a_server_that_will_not_start_is_left_out(self, ws):
        hub = start({
            **fixture_server("bad", env={"FIXTURE_FAIL_START": "1",
                                         "FIXTURE_STDERR": "missing API key"}),
            "nope": {"transport": "stdio", "command": "/no/such/binary"},
            **fixture_server("good", tools=["echo"]),
        }, ws)
        try:
            status = {s["name"]: s for s in hub.status()}
            assert not status["bad"]["connected"] and "missing API key" in status["bad"]["error"]
            assert not status["nope"]["connected"] and "No such file" in status["nope"]["error"]
            assert status["good"]["connected"] and [t.name for t in hub.tools] == ["good__echo"]
        finally:
            hub.close()

    def test_a_dead_server_is_reconnected(self, ws):
        hub = start(fixture_server(timeout=5), ws)
        try:
            assert hub.call("fx__die", {}).startswith("Error:")   # it died answering
            assert hub.call("fx__echo", {"text": "back"}) == "echo: back"
            assert hub.status()[0]["connected"]
        finally:
            hub.close()

    def test_close_stops_the_server_process(self, ws):
        hub = start(fixture_server("closer"), ws)
        hub.close()
        assert not hub.status()[0]["connected"]
        with pytest.raises(Exception, match="not running"):
            hub.call("closer__echo", {"text": "x"})


class TestHttp:
    @pytest.fixture(scope="class")
    def http_server(self):
        import socket
        import subprocess
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        process = subprocess.Popen(
            [sys.executable, FIXTURE],
            env={**os.environ, "FIXTURE_HTTP_PORT": str(port), "FIXTURE_TOKEN": "fx-token"},
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.time() + 20
        while time.time() < deadline:
            with socket.socket() as attempt:
                if attempt.connect_ex(("127.0.0.1", port)) == 0:
                    break
            time.sleep(0.2)
        yield f"http://127.0.0.1:{port}/mcp"
        process.terminate()
        process.wait(timeout=10)

    def test_streamable_http_with_a_token_from_env(self, http_server, ws, monkeypatch):
        monkeypatch.setenv("FX_TOKEN", "fx-token")
        hub = start({"web": {"transport": "http", "url": http_server,
                             "headers": {"Authorization": "Bearer ${FX_TOKEN}"}}}, ws)
        try:
            assert hub.status()[0]["connected"], hub.status()[0]["error"]
            assert hub.call("web__add", {"a": 1, "b": 1}) == "2"
        finally:
            hub.close()

    def test_a_wrong_token_is_a_server_that_did_not_start(self, http_server, ws):
        hub = start({"web": {"transport": "http", "url": http_server, "startup_timeout": 10,
                             "headers": {"Authorization": "Bearer wrong"}}}, ws)
        try:
            status = hub.status()[0]
            assert not status["connected"] and status["error"]
            assert "wrong" not in status["where"]
        finally:
            hub.close()


class TestLean:
    @pytest.fixture
    def agent(self, tmp_path):
        from declarative_agent_sdk.core.agent_factory import AgentFactory
        (tmp_path / "agent.yaml").write_text(f"""
name: mcp-lean
agent_framework: lean
provider: vllm
model: m
endpoint: {{url: "http://127.0.0.1:9/v1"}}
workspace_directory: ws
skills_directory: skills
mcp_servers:
  fx:
    transport: stdio
    command: {sys.executable}
    args: ["{FIXTURE}"]
    approval: destructive
  spare:
    transport: stdio
    command: nothing
    enabled: false
""")
        agent = AgentFactory.from_yaml_file(str(tmp_path / "agent.yaml"))
        yield agent
        agent.mcp.close()

    def test_tools_are_registered_and_called(self, agent):
        assert "fx__add" in agent._tools.names()
        assert agent._tools.invoke("fx__add", {"a": 40, "b": 2}) == "42"
        schema = next(s for s in agent._tools.schemas() if s["function"]["name"] == "fx__add")
        assert schema["function"]["parameters"]["required"] == ["a", "b"]

    def test_destructive_asks_and_the_rest_do_not(self, agent):
        mcp = [n for n in agent._tools.names() if n.startswith("fx__")]
        assert [n for n in mcp if agent._loop._needs_approval(n)] == ["fx__wipe"]

    def test_the_a2a_server_reports_it(self, agent):
        from starlette.testclient import TestClient
        from declarative_agent_sdk.transports.a2a.server import AIAgentServer
        body = TestClient(AIAgentServer(agent, port=8126).app).get("/mcp").json()
        (server,) = body["servers"]
        assert server["name"] == "fx" and server["connected"] and "add" in server["tools"]

    def test_no_servers_no_hub(self, tmp_path):
        from declarative_agent_sdk.core.agent_factory import AgentFactory
        agent = AgentFactory.from_dict({
            "name": "plain", "agent_framework": "lean", "provider": "vllm", "model": "m",
            "endpoint": {"url": "http://127.0.0.1:9/v1"},
            "workspace_directory": str(tmp_path / "ws"),
            "skills_directory": str(tmp_path / "skills")})
        assert agent.mcp is None
