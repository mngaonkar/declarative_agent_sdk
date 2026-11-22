"""
agent-board and MCP (MCP-BOARD-1..4): reading each agent's /mcp, telling
agents about MCP tools, the 'safe' level using tool hints, and probing a
machine for npx / uvx.
"""

import asyncio

import httpx
import pytest
from starlette.testclient import TestClient

from declarative_agent_sdk.agent_board import metrics
from declarative_agent_sdk.agent_board.machines import MachineDriver
from declarative_agent_sdk.agent_board.router import ChannelRouter, EventBus, PendingApproval
from declarative_agent_sdk.agent_board.server import ControlPlane
from declarative_agent_sdk.agent_board.store import Store

AUTH = {"Authorization": "Bearer t0ken"}

GITHUB = {
    "name": "github", "transport": "http", "where": "http: https://api.example/mcp",
    "connected": True, "tools": ["search_code", "delete_repo", "get_file"],
    "tool_hints": {
        "github__search_code": {"read_only": True, "destructive": False, "needs_approval": True},
        "github__delete_repo": {"read_only": False, "destructive": True, "needs_approval": True},
        "github__get_file": {"read_only": False, "destructive": False, "needs_approval": True},
    },
    "error": "", "approval": "true",
}
DOWN = {"name": "files", "transport": "stdio", "where": "stdio: npx fs", "connected": False,
        "tools": [], "tool_hints": {}, "error": "FileNotFoundError: npx", "approval": "false"}


@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path / "board.db"))


@pytest.fixture
def board(store):
    machine = store.add_machine("box", "localhost", is_local=True)
    coder = store.upsert_agent(machine["id"], {"name": "coder", "url": "http://127.0.0.1:8001/",
                                               "ready": True})
    scout = store.upsert_agent(machine["id"], {"name": "scout", "url": "http://127.0.0.1:8002/",
                                               "ready": True})
    channel = store.add_channel("dev")
    store.join(channel["id"], coder["id"])
    store.join(channel["id"], scout["id"])
    router = ChannelRouter(store, EventBus())
    router.remember_mcp({coder["id"]: {"supported": True, "servers": [GITHUB, DOWN]}})
    return router, store, channel["id"], coder, scout


class TestReadingMcp:
    def _client(self, handler):
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    def test_servers_from_the_route(self):
        async def go():
            async with self._client(lambda r: httpx.Response(
                    200, json={"agent": "coder", "servers": [GITHUB]})) as client:
                return await metrics.agent_mcp(client, "http://coder:8001/")
        report = asyncio.run(go())
        assert report["supported"] and report["servers"][0]["name"] == "github"

    def test_an_agent_from_before_mcp(self):
        async def go():
            async with self._client(lambda r: httpx.Response(404)) as client:
                return await metrics.agent_mcp(client, "http://coder:8001/")
        report = asyncio.run(go())
        assert not report["supported"] and "reinstall" in report["error"]

    def test_a_stopped_agent(self):
        def refuse(request):
            raise httpx.ConnectError("refused")

        async def go():
            async with self._client(refuse) as client:
                return await metrics.agent_mcp(client, "http://coder:8001/")
        report = asyncio.run(go())
        assert not report["supported"] and report["servers"] == []

    def test_the_metrics_poll_returns_and_remembers_it(self, tmp_path, monkeypatch):
        plane = ControlPlane(db_path=str(tmp_path / "p.db"), token="t0ken")
        machine = plane.store.add_machine("box", "localhost", is_local=True)
        agent = plane.store.upsert_agent(machine["id"], {"name": "coder", "url": "http://c:1/"})

        async def no_context(agents, timeout=0):
            return {}

        async def mcp(agents, timeout=0):
            return {agent["id"]: {"supported": True, "servers": [GITHUB]}}

        monkeypatch.setattr(metrics, "contexts_for", no_context)
        monkeypatch.setattr(metrics, "mcps_for", mcp)
        body = TestClient(plane.app).get("/api/agents/metrics", headers=AUTH).json()
        assert body["mcp"][str(agent["id"])]["servers"][0]["name"] == "github"
        assert plane.router.mcp[agent["id"]][0]["name"] == "github"

    def test_an_unreachable_agent_keeps_what_was_last_read(self, board):
        router, _, _, coder, _ = board
        router.remember_mcp({coder["id"]: {"supported": False, "servers": [], "error": "down"}})
        assert router.mcp[coder["id"]][0]["name"] == "github"


class TestPrompt:
    def test_an_agent_is_told_its_mcp_tools(self, board):
        router, *_ = board
        block = router._self_block("coder.box")
        assert "MCP tools, named <server>__<tool>: github (search_code, delete_repo, get_file)" in block
        sentence = block.split("You also have MCP tools")[1].split(".")[0]
        assert "files" not in sentence                    # not connected: not offered

    def test_peers_see_who_has_which_servers(self, board):
        router, _, channel_id, *_ = board
        roster = router._roster_block(channel_id, "scout.box")
        assert "@coder.box" in roster and "MCP: github" in roster

    def test_no_servers_no_line(self, board):
        router, *_ = board
        assert "MCP tools" not in router._self_block("scout.box")

    def test_a_long_tool_list_is_cut(self, board):
        router, _, _, coder, _ = board
        many = dict(GITHUB, tools=[f"t{i}" for i in range(20)])
        router.remember_mcp({coder["id"]: {"supported": True, "servers": [many]}})
        assert "t11, +8 more" in router._self_block("coder.box")


class TestSafeLevel:
    @staticmethod
    def _decide(router, channel_id, tool, agent="coder.box"):
        approval = PendingApproval(channel_id, agent, tool, {}, task_id="t",
                                   context_id="c", function_id="f")
        return router._auto_decision(channel_id, approval)

    def test_read_only_is_approved(self, board):
        router, store, channel_id, *_ = board
        store.set_auto_approve(channel_id, "safe")
        assert self._decide(router, channel_id, "github__search_code") is True

    def test_destructive_goes_to_a_person(self, board):
        router, store, channel_id, *_ = board
        store.set_auto_approve(channel_id, "safe")
        assert self._decide(router, channel_id, "github__delete_repo") is None

    def test_no_hints_is_like_any_other_tool(self, board):
        router, store, channel_id, *_ = board
        store.set_auto_approve(channel_id, "safe")
        assert self._decide(router, channel_id, "github__get_file") is True
        assert self._decide(router, channel_id, "github__unknown") is True

    def test_shell_is_still_refused(self, board):
        router, store, channel_id, *_ = board
        store.set_auto_approve(channel_id, "safe")
        assert self._decide(router, channel_id, "exec_command") is False

    def test_all_approves_destructive_too(self, board):
        router, store, channel_id, *_ = board
        store.set_auto_approve(channel_id, "all")
        assert self._decide(router, channel_id, "github__delete_repo") is True

    def test_another_agents_hints_do_not_apply(self, board):
        router, store, channel_id, *_ = board
        store.set_auto_approve(channel_id, "safe")
        assert self._decide(router, channel_id, "github__delete_repo", agent="scout.box") is True


class TestRunners:
    def test_this_machine_is_probed_for_them(self):
        result = MachineDriver().probe({"name": "here", "is_local": 1, "host": "localhost"})
        assert result["ok"] and set(result["runners"]) <= {"npx", "uvx"}

    def test_they_are_stored_with_the_machine(self, store):
        machine = store.add_machine("box", "localhost", is_local=True)
        store.set_machine_status(machine["id"], "ready", "", "Darwin", [], ["uvx"])
        assert store.machine(machine["id"])["runners"] == ["uvx"]
        # A status update without a probe keeps what was found.
        store.set_machine_status(machine["id"], "ready")
        assert store.machine(machine["id"])["runners"] == ["uvx"]

    def test_an_old_database_gets_the_column(self, tmp_path):
        import sqlite3
        path = tmp_path / "old.db"
        Store(str(path))
        db = sqlite3.connect(path)
        db.execute("ALTER TABLE machines DROP COLUMN runners")
        db.commit()
        db.close()
        machine = Store(str(path)).add_machine("box", "localhost", is_local=True)
        assert Store(str(path)).machine(machine["id"])["runners"] == []
