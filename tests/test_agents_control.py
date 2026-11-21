"""Unit tests for agents-control: store, machine driver, router, API, peer tool."""

import asyncio
import json
import logging
import os
import time
from pathlib import Path

import httpx
import pytest
from a2a.types import TaskState
from starlette.testclient import TestClient

from declarative_agent_sdk.agents_control.machines import (
    MachineDriver,
    MachineError,
    _env_file_lines,
    _parse_json,
)
from declarative_agent_sdk.agents_control.router import (
    ChannelRouter,
    EventBus,
    mentions,
    resolve_member,
)
from declarative_agent_sdk.agents_control import metrics
from declarative_agent_sdk.agents_control.server import ControlPlane
from declarative_agent_sdk.agents_control.store import Store


@pytest.fixture
def store(tmp_path) -> Store:
    return Store(str(tmp_path / "control.db"))


@pytest.fixture
def plane(tmp_path) -> ControlPlane:
    return ControlPlane(
        db_path=str(tmp_path / "api.db"),
        token="t0ken",
        public_url="http://127.0.0.1:8900",
    )


@pytest.fixture
def client(plane) -> TestClient:
    return TestClient(plane.app)


AUTH = {"Authorization": "Bearer t0ken"}


# ---------------------------------------------------------------------------
# store
# ---------------------------------------------------------------------------

class TestStore:
    def test_settings_are_seeded(self, store):
        settings = store.settings()
        assert settings["default_framework"] == "lean"
        # web_request, exec_command, send_to_agent ... are core, so the
        # default extra-tools list is empty
        assert settings["default_tools"] == ""

    def test_update_settings_ignores_unknown_keys(self, store):
        updated = store.update_settings({"default_model": "gpt-4o", "nonsense": 1})
        assert updated["default_model"] == "gpt-4o"
        assert "nonsense" not in updated

    def test_settings_survive_reopen(self, store, tmp_path):
        store.update_settings({"default_model": "kept"})
        store.close()
        assert Store(str(tmp_path / "control.db")).setting("default_model") == "kept"

    def test_agent_identity_is_machine_plus_name(self, store):
        one = store.add_machine("one", "h1")
        two = store.add_machine("two", "h2")
        store.upsert_agent(one["id"], {"name": "worker", "url": "http://a"})
        store.upsert_agent(two["id"], {"name": "worker", "url": "http://b"})
        assert len(store.agents()) == 2
        assert {a["url"] for a in store.agents_named("worker")} == {"http://a", "http://b"}

    def test_upsert_is_idempotent_and_updates(self, store):
        machine = store.add_machine("m", "h")
        store.upsert_agent(machine["id"], {"name": "a", "url": "http://old", "ready": 0})
        agent = store.upsert_agent(machine["id"], {"name": "a", "url": "http://new", "ready": 1})
        assert len(store.agents()) == 1
        assert agent["url"] == "http://new" and agent["ready"] == 1

    def test_upsert_keeps_tools_when_absent(self, store):
        machine = store.add_machine("m", "h")
        store.upsert_agent(machine["id"], {"name": "a", "tools": "web_request"})
        # A --list refresh carries no tools field; it must not wipe what we know.
        agent = store.upsert_agent(machine["id"], {"name": "a", "url": "http://x"})
        assert agent["tools"] == "web_request"

    def test_prune_drops_agents_gone_from_the_machine(self, store):
        machine = store.add_machine("m", "h")
        store.upsert_agent(machine["id"], {"name": "keep"})
        store.upsert_agent(machine["id"], {"name": "gone"})
        assert store.prune_agents(machine["id"], ["keep"]) == 1
        assert [a["name"] for a in store.agents()] == ["keep"]

    def test_deleting_a_machine_cascades(self, store):
        machine = store.add_machine("m", "h")
        store.upsert_agent(machine["id"], {"name": "a"})
        store.delete_machine(machine["id"])
        assert store.agents() == []

    def test_deleting_a_channel_cascades_messages(self, store):
        channel = store.add_channel("c")
        store.add_message(channel["id"], "human", "you", "hi")
        store.delete_channel(channel["id"])
        assert store.messages(channel["id"]) == []

    def test_membership_and_roster(self, store):
        machine = store.add_machine("m", "h")
        agent = store.upsert_agent(machine["id"], {"name": "a"})
        channel = store.add_channel("dev")
        store.join(channel["id"], agent["id"])
        assert [m["name"] for m in store.members(channel["id"])] == ["a"]
        assert [c["name"] for c in store.channels_for_agent_name("a")] == ["dev"]
        store.leave(channel["id"], agent["id"])
        assert store.members(channel["id"]) == []

    def test_messages_paginate_in_order(self, store):
        channel = store.add_channel("c")
        for i in range(5):
            store.add_message(channel["id"], "human", "you", f"m{i}")
        assert [m["text"] for m in store.messages(channel["id"])] == \
               ["m0", "m1", "m2", "m3", "m4"]
        after = store.messages(channel["id"], after_id=3)
        assert [m["text"] for m in after] == ["m3", "m4"]

    def test_recent_transcript_is_newest_last(self, store):
        channel = store.add_channel("c")
        store.add_message(channel["id"], "human", "you", "first")
        store.add_message(channel["id"], "agent", "bot", "second")
        transcript = store.recent_transcript(channel["id"], limit=2)
        assert transcript == "you: first\nbot: second"


# ---------------------------------------------------------------------------
# mentions / router
# ---------------------------------------------------------------------------

class TestMentions:
    def test_extracts_in_order_without_duplicates(self):
        assert mentions("@a and @b, then @a again") == ["a", "b"]

    def test_allows_dashes_and_underscores(self):
        assert mentions("@web-crawler @db_agent") == ["web-crawler", "db_agent"]

    def test_ignores_emails_and_bare_at(self):
        assert mentions("plain text, @ alone") == []

    def test_empty(self):
        assert mentions("") == [] and mentions(None) == []


class TestRouterDispatchRules:
    @pytest.fixture
    def setup(self, store):
        bus = EventBus()
        router = ChannelRouter(store, bus)
        machine = store.add_machine("m", "h")
        agent = store.upsert_agent(machine["id"], {"name": "scout", "url": "http://agent"})
        channel = store.add_channel("dev")
        store.join(channel["id"], agent["id"])
        return store, router, channel, agent

    @pytest.mark.asyncio
    async def test_no_mention_dispatches_nothing(self, setup):
        store, router, channel, _ = setup
        result = await router.post_human(channel["id"], "just thinking out loud")
        assert result["dispatched"] == []
        assert [m["text"] for m in store.messages(channel["id"])] == ["just thinking out loud"]

    @pytest.mark.asyncio
    async def test_unknown_mention_explains_itself(self, setup):
        store, router, channel, _ = setup
        result = await router.post_human(channel["id"], "@ghost you there?")
        assert result["dispatched"] == []
        assert "not in this channel" in store.messages(channel["id"])[-1]["text"]

    @pytest.mark.asyncio
    async def test_halted_channel_refuses_dispatch(self, setup):
        store, router, channel, _ = setup
        store.set_halted(channel["id"], True)
        result = await router.post_human(channel["id"], "@scout go")
        assert result["dispatched"] == []
        assert "halted" in store.messages(channel["id"])[-1]["text"]

    @pytest.mark.asyncio
    async def test_mentioned_member_is_dispatched(self, setup, monkeypatch):
        store, router, channel, _ = setup
        seen = {}

        async def fake_dispatch(channel_id, agent, text, sender, **kwargs):
            seen.update(agent=agent["name"], text=text, sender=sender)

        monkeypatch.setattr(router, "dispatch", fake_dispatch)
        result = await router.post_human(channel["id"], "@scout look into this")
        assert result["dispatched"] == ["scout.m"]      # dispatch reports the FQN
        # give the spawned task a turn to run
        import asyncio
        await asyncio.sleep(0)
        assert seen["agent"] == "scout"

    @pytest.mark.asyncio
    async def test_agent_without_url_is_reported(self, setup):
        store, router, channel, _ = setup
        machine = store.machines()[0]
        broken = store.upsert_agent(machine["id"], {"name": "nourl", "url": ""})
        await router.dispatch(channel["id"], broken, "hello", "you")
        assert "no A2A url" in store.messages(channel["id"])[-1]["text"]

    def test_prompt_carries_transcript_and_roster(self, setup):
        store, router, channel, _ = setup
        store.add_message(channel["id"], "human", "you", "earlier line")
        prompt = router._prompt_for(channel["id"], "scout", "new question", "you")
        assert "#dev" in prompt
        assert "@scout" in prompt
        assert "earlier line" in prompt
        assert "new question" in prompt

    def test_halt_is_recorded_and_announced(self, setup):
        store, router, channel, _ = setup
        result = router.halt(channel["id"], True)
        assert result["halted"] is True
        assert store.is_halted(channel["id"]) is True
        assert "halted" in store.messages(channel["id"])[-1]["text"].lower()
        router.halt(channel["id"], False)
        assert store.is_halted(channel["id"]) is False

    def test_resolve_unknown_approval_is_false(self, setup):
        _, router, _, _ = setup
        assert router.resolve_approval("nope", True) is False


class TestEventBus:
    def test_publish_reaches_subscribers(self):
        bus = EventBus()
        queue = bus.subscribe()
        bus.publish({"type": "x"})
        assert queue.get_nowait() == {"type": "x"}

    def test_unsubscribe_stops_delivery(self):
        bus = EventBus()
        queue = bus.subscribe()
        bus.unsubscribe(queue)
        bus.publish({"type": "x"})
        assert queue.empty()

    def test_slow_subscriber_is_dropped_not_blocking(self):
        bus = EventBus(max_queue=1)
        queue = bus.subscribe()
        bus.publish({"n": 1})
        bus.publish({"n": 2})          # would block a naive implementation
        assert queue.qsize() == 1


# ---------------------------------------------------------------------------
# machine driver
# ---------------------------------------------------------------------------

class TestMachineDriver:
    def test_local_detection(self):
        driver = MachineDriver()
        assert driver._is_local({"is_local": 1, "host": "anything"})
        assert driver._is_local({"host": "localhost"})
        assert not driver._is_local({"host": "10.0.0.5"})

    def test_ssh_command_includes_key_user_and_port(self):
        driver = MachineDriver()
        cmd = driver._ssh_command({
            "host": "box", "ssh_user": "ubuntu", "ssh_key": "~/.ssh/k", "ssh_port": 2222,
        })
        assert cmd[0] == "ssh"
        assert "-i" in cmd and "-p" in cmd and "2222" in cmd
        assert cmd[-1] == "ubuntu@box"
        assert "BatchMode=yes" in cmd            # never hang on a password prompt

    def test_ssh_command_without_user(self):
        assert MachineDriver()._ssh_command({"host": "box"})[-1] == "box"

    def test_probe_reports_unreachable_without_raising(self):
        driver = MachineDriver()
        result = driver.probe({"name": "ghost", "host": "203.0.113.9", "ssh_user": "x"})
        assert result["ok"] is False and result["error"]

    def test_probe_local_machine(self):
        result = MachineDriver().probe({"name": "here", "is_local": 1, "host": "localhost"})
        assert result["ok"] is True
        assert result["os"] in ("Darwin", "Linux")

    def test_run_raises_machine_error_on_failure(self):
        with pytest.raises(MachineError) as excinfo:
            MachineDriver().run({"name": "here", "is_local": 1}, "exit 7")
        assert excinfo.value.code == 7

    def test_exit_3_with_json_is_a_result_not_a_failure(self):
        # install.sh exits 3 when an agent installed but cannot start.
        out = MachineDriver().run(
            {"name": "here", "is_local": 1}, 'echo \'{"ready":false}\'; exit 3'
        )
        assert json.loads(out)["ready"] is False


class TestEnvFile:
    def test_maps_provider_to_key_name(self):
        lines = _env_file_lines("anthropic", "sk-x", None)
        assert lines == ["ANTHROPIC_API_KEY=sk-x"]

    def test_vllm_needs_no_key(self):
        assert _env_file_lines("vllm", "ignored", None) == []

    def test_control_handshake_is_included(self):
        lines = _env_file_lines("openai", "", {"AGENTS_CONTROL_URL": "http://c", "EMPTY": ""})
        assert lines == ["AGENTS_CONTROL_URL=http://c"]


class TestParseJson:
    def test_plain_object(self):
        assert _parse_json('{"a": 1}') == {"a": 1}

    def test_ignores_a_shell_banner(self):
        assert _parse_json('Welcome to box!\n{"a": 1}') == {"a": 1}

    def test_ignores_trailing_output(self):
        assert _parse_json('{"a": 1}\nlogout') == {"a": 1}

    def test_list_mode(self):
        assert _parse_json('[{"name": "a"}]', expect_list=True) == [{"name": "a"}]

    def test_garbage_is_empty(self):
        assert _parse_json("nothing here") is None
        assert _parse_json("nothing here", expect_list=True) == []


# ---------------------------------------------------------------------------
# HTTP API
# ---------------------------------------------------------------------------

class TestAuth:
    def test_api_requires_the_token(self, client):
        assert client.get("/api/state").status_code == 401
        assert client.get("/api/state", headers=AUTH).status_code == 200

    def test_wrong_token_is_rejected(self, client):
        assert client.get(
            "/api/state", headers={"Authorization": "Bearer wrong"}
        ).status_code == 401

    def test_query_token_works_for_the_event_stream(self, client):
        # EventSource cannot set headers, so the token may ride in the query.
        assert client.get("/api/state?token=t0ken").status_code == 200

    def test_board_and_static_are_public(self, client):
        assert client.get("/").status_code == 200
        assert client.get("/healthz").status_code == 200
        assert client.get("/static/app.js").status_code == 200

    def test_a_tokenless_plane_is_open(self, tmp_path):
        plane = ControlPlane(db_path=str(tmp_path / "open.db"), token="")
        assert TestClient(plane.app).get("/api/state").status_code == 200

    def test_refuses_public_bind_without_a_token(self, tmp_path):
        plane = ControlPlane(db_path=str(tmp_path / "x.db"), token="")
        with pytest.raises(RuntimeError, match="refusing to bind"):
            plane.run(host="0.0.0.0", port=8901)


class TestChannelApi:
    def test_create_list_delete(self, client):
        created = client.post("/api/channels", json={"name": "dev", "topic": "t"}, headers=AUTH)
        assert created.status_code == 200
        channel_id = created.json()["channel"]["id"]
        assert client.get("/api/state", headers=AUTH).json()["channels"][0]["name"] == "dev"
        assert client.delete(f"/api/channels/{channel_id}", headers=AUTH).status_code == 200
        assert client.get("/api/state", headers=AUTH).json()["channels"] == []

    def test_duplicate_name_is_rejected(self, client):
        client.post("/api/channels", json={"name": "dev"}, headers=AUTH)
        again = client.post("/api/channels", json={"name": "dev"}, headers=AUTH)
        assert again.status_code == 400

    def test_name_is_required(self, client):
        assert client.post("/api/channels", json={}, headers=AUTH).status_code == 400

    def test_leading_hash_is_stripped(self, client):
        created = client.post("/api/channels", json={"name": "#dev"}, headers=AUTH)
        assert created.json()["channel"]["name"] == "dev"

    def test_unknown_channel_is_404(self, client):
        assert client.get("/api/channels/999/messages", headers=AUTH).status_code == 404

    def test_post_message_and_read_back(self, client):
        channel_id = client.post(
            "/api/channels", json={"name": "dev"}, headers=AUTH
        ).json()["channel"]["id"]
        posted = client.post(
            f"/api/channels/{channel_id}/messages", json={"text": "hello board"}, headers=AUTH
        )
        assert posted.json()["dispatched"] == []
        messages = client.get(
            f"/api/channels/{channel_id}/messages", headers=AUTH
        ).json()["messages"]
        assert messages[0]["text"] == "hello board"

    def test_halt_round_trip(self, client):
        channel_id = client.post(
            "/api/channels", json={"name": "dev"}, headers=AUTH
        ).json()["channel"]["id"]
        assert client.post(
            f"/api/channels/{channel_id}/halt", json={"halted": True}, headers=AUTH
        ).json()["halted"] is True
        assert client.get(
            f"/api/channels/{channel_id}/messages", headers=AUTH
        ).json()["halted"] is True


class TestMachineApi:
    def test_register_local_machine_probes_it(self, client):
        response = client.post("/api/machines", json={"name": "here", "is_local": True},
                               headers=AUTH)
        assert response.status_code == 200
        assert response.json()["probe"]["ok"] is True
        assert response.json()["machine"]["status"] in ("ready", "no-uv")

    def test_host_is_required_for_a_remote(self, client):
        assert client.post("/api/machines", json={"name": "x"}, headers=AUTH).status_code == 400

    def test_forgetting_a_machine_keeps_agents_running(self, client, plane):
        machine_id = client.post(
            "/api/machines", json={"name": "here", "is_local": True}, headers=AUTH
        ).json()["machine"]["id"]
        plane.store.upsert_agent(machine_id, {"name": "a", "url": "http://a"})
        assert client.delete(f"/api/machines/{machine_id}", headers=AUTH).status_code == 200
        # record gone, nothing was torn down on the box
        assert plane.store.agents() == []

    def test_unknown_machine_is_404(self, client):
        assert client.post("/api/machines/999/test", headers=AUTH).status_code == 404


class TestPeerApi:
    @pytest.fixture
    def board(self, client, plane):
        machine = plane.store.add_machine("m", "h")
        alice = plane.store.upsert_agent(machine["id"], {"name": "alice", "url": "http://alice"})
        bob = plane.store.upsert_agent(machine["id"], {"name": "bob", "url": "http://bob"})
        channel = plane.store.add_channel("dev")
        plane.store.join(channel["id"], alice["id"])
        plane.store.join(channel["id"], bob["id"])
        return plane, channel

    def test_roster_excludes_the_caller(self, client, board):
        roster = client.get("/api/peer/roster?agent=alice", headers=AUTH).json()["roster"]
        assert [r["name"] for r in roster] == ["bob"]
        assert roster[0]["url"] == "http://bob"

    def test_preflight_allows_then_refuses(self, client, board):
        plane, channel = board
        assert client.get("/api/peer/preflight?channel=dev",
                          headers=AUTH).json()["allowed"] is True
        plane.store.set_halted(channel["id"], True)
        refused = client.get("/api/peer/preflight?channel=dev", headers=AUTH).json()
        assert refused["allowed"] is False and refused["reason"]

    def test_peer_log_lands_in_the_transcript(self, client, board):
        plane, channel = board
        response = client.post("/api/peer/log", json={
            "channel": "dev", "from": "alice", "to": "bob", "text": "@bob ping",
        }, headers=AUTH)
        assert response.status_code == 200
        message = plane.store.messages(channel["id"])[-1]
        # recorded as full addresses, even though the tool reported bare names
        assert message["sender"] == "alice.m" and message["target"] == "bob.m"

    def test_peer_log_needs_a_known_channel(self, client, board):
        assert client.post("/api/peer/log", json={
            "channel": "nope", "from": "a", "text": "x",
        }, headers=AUTH).status_code == 404

    def test_peer_endpoints_need_the_token(self, client, board):
        assert client.get("/api/peer/roster?agent=alice").status_code == 401
        assert client.post("/api/peer/log", json={}).status_code == 401


class TestSettingsApi:
    def test_round_trip(self, client):
        response = client.put("/api/settings", json={"default_model": "gpt-4o"}, headers=AUTH)
        assert response.json()["settings"]["default_model"] == "gpt-4o"
        assert client.get("/api/settings",
                          headers=AUTH).json()["settings"]["default_model"] == "gpt-4o"


# ---------------------------------------------------------------------------
# the peer tool
# ---------------------------------------------------------------------------

class TestSendToAgentTool:
    """
    The tool posts to the board; the board relays. It never calls a peer.
    """

    @staticmethod
    def _module():
        # tools/builtin/__init__.py rebinds the submodule name to the function
        # it exports, so `import ... as mod` yields the function. import_module
        # returns the real module, which is what monkeypatching needs.
        import importlib
        return importlib.import_module(
            "declarative_agent_sdk.tools.builtin.send_to_agent"
        )

    @classmethod
    def _tool(cls):
        return cls._module().send_to_agent

    def test_registered_as_a_core_builtin(self):
        from declarative_agent_sdk.tools.tool_registry import ToolRegistry
        ToolRegistry.register_built_in_tools()
        assert "send_to_agent" in ToolRegistry.list_available()
        assert "send_to_agent" in ToolRegistry.CORE_TOOL_NAMES

    def test_board_plumbing_is_not_offered_as_tools(self):
        """_board.py is a private module: its helpers must not become tools."""
        from declarative_agent_sdk.tools.tool_registry import ToolRegistry
        ToolRegistry.register_built_in_tools()
        available = ToolRegistry.list_available()
        for helper in ("board", "get", "post_to_board", "roster"):
            assert helper not in available

    def test_rejects_an_empty_target(self, monkeypatch):
        monkeypatch.setenv("AGENTS_CONTROL_URL", "http://c")
        assert "no agent named" in self._tool()("", "hi")

    def test_rejects_an_empty_message(self, monkeypatch):
        monkeypatch.setenv("AGENTS_CONTROL_URL", "http://c")
        assert "message is empty" in self._tool()("bob", "  ")

    def test_explains_a_missing_board(self, monkeypatch):
        monkeypatch.delenv("AGENTS_CONTROL_URL", raising=False)
        assert "AGENTS_CONTROL_URL" in self._tool()("bob", "hello")

    def test_unreachable_board_is_reported(self, monkeypatch):
        monkeypatch.setenv("AGENTS_CONTROL_URL", "http://127.0.0.1:1")
        monkeypatch.setenv("AGENT_NAME", "alice")
        assert "could not post to the board" in self._tool()("bob", "hello")

    def test_posts_the_mention_and_reports_delivery(self, monkeypatch):
        mod = self._module()
        monkeypatch.setenv("AGENTS_CONTROL_URL", "http://c")
        monkeypatch.setenv("AGENT_QUALIFIED_NAME", "alice.here")
        sent = {}

        def fake_post(link, text, channel=""):
            sent.update(text=text, channel=channel, who=link.name)
            return {"channel": "dev", "relayed": ["bob.here"], "members": ["bob.here"]}

        monkeypatch.setattr(mod, "post_to_board", fake_post)
        reply = self._tool()("@bob.here", "please check the logs")

        # the message goes out as a mention, which is what the board relays on
        assert sent["text"] == "@bob.here please check the logs"
        assert sent["who"] == "alice.here"
        assert "Delivered to @bob.here" in reply
        # and the model is told not to wait, because the answer is a new turn
        assert "end your turn" in reply

    def test_reports_an_unmatched_mention_with_the_roster(self, monkeypatch):
        mod = self._module()
        monkeypatch.setenv("AGENTS_CONTROL_URL", "http://c")
        monkeypatch.setattr(mod, "post_to_board", lambda *a, **k: {
            "channel": "dev", "relayed": [], "members": ["bob.here", "carol.box"],
        })
        reply = self._tool()("ghost", "hi")
        assert "did not match a member" in reply
        assert "@bob.here" in reply and "@carol.box" in reply

    def test_halted_channel_is_reported_as_refused(self, monkeypatch):
        """The board refuses to relay; the tool must not read that as 'no such member'."""
        mod = self._module()
        monkeypatch.setenv("AGENTS_CONTROL_URL", "http://c")
        monkeypatch.setattr(mod, "post_to_board", lambda *a, **k: {
            "channel": "dev", "relayed": [], "halted": True, "members": ["bob.here"],
        })
        reply = self._tool()("bob.here", "keep going")
        assert "Refused" in reply and "halted" in reply
        assert "do not retry" in reply

    def test_board_errors_surface_as_text(self, monkeypatch):
        mod = self._module()
        monkeypatch.setenv("AGENTS_CONTROL_URL", "http://c")

        def boom(*args, **kwargs):
            raise mod.BoardError("'alice' is in several channels (dev, ops) — name one")

        monkeypatch.setattr(mod, "post_to_board", boom)
        assert "several channels" in self._tool()("bob", "hi")


class TestListAgentsTool:
    """Discovery: the roster an agent can ask for."""

    @staticmethod
    def _module():
        import importlib
        return importlib.import_module(
            "declarative_agent_sdk.tools.builtin.list_agents"
        )

    def test_registered_as_a_non_core_builtin(self):
        from declarative_agent_sdk.tools.tool_registry import ToolRegistry
        ToolRegistry.register_built_in_tools()
        assert "list_agents" in ToolRegistry.list_available()
        assert "list_agents" not in ToolRegistry.CORE_TOOL_NAMES

    def test_explains_a_missing_board(self, monkeypatch):
        monkeypatch.delenv("AGENTS_CONTROL_URL", raising=False)
        assert "not on a board" in self._module().list_agents()

    def test_lists_addressable_members_with_detail(self, monkeypatch):
        mod = self._module()
        monkeypatch.setenv("AGENTS_CONTROL_URL", "http://c")
        monkeypatch.setattr(mod, "roster", lambda link, channel="", scope="channel": [
            {"qualified": "bob.here", "description": "reads logs",
             "model": "gpt-4o-mini", "channel": "dev", "ready": True},
        ])
        out = mod.list_agents()
        assert "@bob.here" in out and "reads logs" in out and "in #dev" in out

    def test_marks_an_agent_that_is_not_running(self, monkeypatch):
        mod = self._module()
        monkeypatch.setenv("AGENTS_CONTROL_URL", "http://c")
        monkeypatch.setattr(mod, "roster", lambda link, channel="", scope="channel": [
            {"qualified": "bob.here", "ready": False, "channel": "dev"},
        ])
        assert "NOT RUNNING" in mod.list_agents()

    def test_says_so_when_nobody_shares_a_channel(self, monkeypatch):
        mod = self._module()
        monkeypatch.setenv("AGENTS_CONTROL_URL", "http://c")
        monkeypatch.setattr(mod, "roster", lambda link, channel="", scope="channel": [])
        out = mod.list_agents()
        assert "nobody to address" in out
        assert "inventing a name" in out

    def test_include_all_separates_the_unreachable(self, monkeypatch):
        mod = self._module()
        monkeypatch.setenv("AGENTS_CONTROL_URL", "http://c")
        monkeypatch.setenv("AGENT_QUALIFIED_NAME", "alice.here")

        def fake_roster(link, channel="", scope="channel"):
            if scope == "all":
                return [
                    {"qualified": "bob.here"}, {"qualified": "carol.box"},
                    {"qualified": "alice.here"},
                ]
            return [{"qualified": "bob.here", "channel": "dev"}]

        monkeypatch.setattr(mod, "roster", fake_roster)
        out = mod.list_agents(include_all=True)
        assert "cannot address" in out
        assert "@carol.box" in out.split("cannot address")[1]   # listed as unreachable
        assert "@alice.here" not in out                         # never lists itself

    def test_board_error_is_reported(self, monkeypatch):
        mod = self._module()
        monkeypatch.setenv("AGENTS_CONTROL_URL", "http://c")

        def boom(*args, **kwargs):
            raise mod.BoardError("could not read /api/peer/roster: 401")

        monkeypatch.setattr(mod, "roster", boom)
        assert "401" in mod.list_agents()


# ---------------------------------------------------------------------------
# agent.yaml editing
# ---------------------------------------------------------------------------

SAMPLE_YAML = """# Generated by install.sh for agent 'coder' on 2026-11-13 06:50:23Z.
# Re-running without --force keeps this file.

name: coder
description: Lean A2A agent coder

agent_framework: lean

instruction_file: instructions.md
skills_directory: skills
workspace_directory: workspace

provider: vllm
model: qwen2.5-coder:latest
endpoint:
  url: http://10.0.0.147:30081/v1

tools:
  - web_request
  - exec_command

tools_approval_required: true
"""


class TestConfigParsing:
    def test_properties_are_flattened_for_the_form(self):
        from declarative_agent_sdk.agents_control import config

        props = config.properties(config.parse(SAMPLE_YAML))
        assert props["model"] == "qwen2.5-coder:latest"
        assert props["endpoint.url"] == "http://10.0.0.147:30081/v1"
        assert props["tools"] == "web_request, exec_command"
        assert props["tools_approval_required"] is True
        assert props["temperature"] == ""        # absent keys come back blank

    def test_bad_yaml_is_rejected(self):
        from declarative_agent_sdk.agents_control import config

        with pytest.raises(config.ConfigError, match="not valid YAML"):
            config.parse("name: [unclosed")

    def test_a_scalar_document_is_not_a_config(self):
        from declarative_agent_sdk.agents_control import config

        with pytest.raises(config.ConfigError, match="must be a mapping"):
            config.parse("just a string")

    def test_leading_comments_are_identified(self):
        from declarative_agent_sdk.agents_control import config

        header = config.leading_comments(SAMPLE_YAML)
        assert header.startswith("# Generated by install.sh")
        assert "name: coder" not in header


class TestConfigMerge:
    def test_updates_a_scalar_and_keeps_the_header(self):
        from declarative_agent_sdk.agents_control import config

        merged = config.merge(SAMPLE_YAML, {"model": "gpt-4o-mini"})
        data = config.parse(merged)
        assert data["model"] == "gpt-4o-mini"
        assert data["name"] == "coder"            # untouched keys survive
        assert merged.startswith("# Generated by install.sh")

    def test_nested_endpoint_url(self):
        from declarative_agent_sdk.agents_control import config

        merged = config.merge(SAMPLE_YAML, {"endpoint.url": "http://new:8000/v1"})
        assert config.parse(merged)["endpoint"]["url"] == "http://new:8000/v1"

    def test_empty_value_removes_the_key(self):
        from declarative_agent_sdk.agents_control import config

        merged = config.merge(SAMPLE_YAML, {"endpoint.url": ""})
        assert "endpoint" not in config.parse(merged)

    def test_tools_accept_a_comma_string(self):
        from declarative_agent_sdk.agents_control import config

        merged = config.merge(SAMPLE_YAML, {"tools": "web_request, send_to_agent"})
        assert config.parse(merged)["tools"] == ["web_request", "send_to_agent"]

    def test_bool_round_trip(self):
        from declarative_agent_sdk.agents_control import config

        merged = config.merge(SAMPLE_YAML, {"tools_approval_required": False})
        assert config.parse(merged)["tools_approval_required"] is False

    def test_numbers_are_typed_not_stringified(self):
        from declarative_agent_sdk.agents_control import config

        data = config.parse(config.merge(
            SAMPLE_YAML, {"temperature": "0.2", "max_tokens": "2048"}))
        assert data["temperature"] == 0.2 and isinstance(data["max_tokens"], int)

    def test_a_non_number_is_refused(self):
        from declarative_agent_sdk.agents_control import config

        with pytest.raises(config.ConfigError, match="must be a number"):
            config.merge(SAMPLE_YAML, {"temperature": "warm"})

    def test_unknown_form_keys_are_ignored(self):
        from declarative_agent_sdk.agents_control import config

        merged = config.merge(SAMPLE_YAML, {"evil": "value"})
        assert "evil" not in config.parse(merged)

    def test_a_bad_choice_is_refused(self):
        from declarative_agent_sdk.agents_control import config

        with pytest.raises(config.ConfigError, match="must be one of"):
            config.merge(SAMPLE_YAML, {"agent_framework": "nonsense"})


class TestConfigValidation:
    def test_renaming_is_refused(self):
        from declarative_agent_sdk.agents_control import config

        with pytest.raises(config.ConfigError, match="cannot be changed"):
            config.prepare(SAMPLE_YAML, "coder",
                           raw=SAMPLE_YAML.replace("name: coder", "name: renamed"))

    def test_missing_name_is_refused(self):
        from declarative_agent_sdk.agents_control import config

        with pytest.raises(config.ConfigError, match="'name' is required"):
            config.prepare(SAMPLE_YAML, "coder", raw="model: x\n")

    def test_unknown_tool_warns_but_saves(self):
        from declarative_agent_sdk.agents_control import config

        text, warnings = config.prepare(
            SAMPLE_YAML, "coder", updates={"tools": "web_request, nonexistent_tool"})
        assert "nonexistent_tool" in text
        assert any("nonexistent_tool" in w for w in warnings)

    def test_approval_off_with_exec_command_warns(self):
        from declarative_agent_sdk.agents_control import config

        _, warnings = config.prepare(
            SAMPLE_YAML, "coder", updates={"tools_approval_required": False})
        assert any("without asking" in w for w in warnings)

    def test_vllm_without_endpoint_warns(self):
        from declarative_agent_sdk.agents_control import config

        _, warnings = config.prepare(SAMPLE_YAML, "coder", updates={"endpoint.url": ""})
        assert any("endpoint.url" in w for w in warnings)

    def test_clean_edit_has_no_warnings(self):
        from declarative_agent_sdk.agents_control import config

        _, warnings = config.prepare(SAMPLE_YAML, "coder", updates={"model": "gpt-4o"})
        assert warnings == []

    def test_nothing_to_save_is_an_error(self):
        from declarative_agent_sdk.agents_control import config

        with pytest.raises(config.ConfigError, match="nothing to save"):
            config.prepare(SAMPLE_YAML, "coder")


class TestAgentFilesOnDisk:
    """The driver's read/write/restart path, exercised on this machine."""

    @pytest.fixture
    def installed(self, tmp_path):
        prefix = tmp_path / "agent-dir"
        prefix.mkdir()
        (prefix / "agent.yaml").write_text(SAMPLE_YAML)
        (prefix / "instructions.md").write_text("# coder\n\nBe direct.\n")
        return {"name": "here", "is_local": 1, "host": "localhost"}, str(prefix)

    def test_reads_both_files_in_one_round_trip(self, installed):
        machine, prefix = installed
        files = MachineDriver().read_agent_files(machine, prefix)
        assert "name: coder" in files["yaml"]
        assert files["instructions"].startswith("# coder")

    def test_missing_instructions_is_not_an_error(self, installed, tmp_path):
        machine, prefix = installed
        Path(prefix, "instructions.md").unlink()
        files = MachineDriver().read_agent_files(machine, prefix)
        assert files["instructions"] == "" and "name: coder" in files["yaml"]

    def test_write_keeps_a_backup(self, installed):
        machine, prefix = installed
        result = MachineDriver().write_agent_files(
            machine, prefix, yaml_text="name: coder\nmodel: new\n")
        assert "agent.yaml" in result["written"]
        assert Path(prefix, "agent.yaml").read_text() == "name: coder\nmodel: new\n"
        # the previous config is recoverable after a bad edit
        assert "qwen2.5-coder" in Path(prefix, "agent.yaml.bak").read_text()

    def test_write_adds_a_trailing_newline(self, installed):
        machine, prefix = installed
        MachineDriver().write_agent_files(machine, prefix, yaml_text="name: coder")
        assert Path(prefix, "agent.yaml").read_text().endswith("\n")

    def test_instructions_only_write_leaves_yaml_alone(self, installed):
        machine, prefix = installed
        result = MachineDriver().write_agent_files(
            machine, prefix, instructions="new prompt")
        assert result["written"] == ["instructions.md"]
        assert "qwen2.5-coder" in Path(prefix, "agent.yaml").read_text()

    def test_a_relative_prefix_is_refused(self, installed):
        machine, _ = installed
        with pytest.raises(MachineError, match="absolute install directory"):
            MachineDriver().read_agent_files(machine, "relative/dir")

    def test_an_empty_prefix_is_refused(self, installed):
        machine, _ = installed
        with pytest.raises(MachineError, match="absolute install directory"):
            MachineDriver().write_agent_files(machine, "", yaml_text="x: 1")

    def test_unsupported_supervisor_action_is_refused(self, installed):
        machine, prefix = installed
        with pytest.raises(MachineError, match="unsupported supervisor action"):
            MachineDriver().supervisor_action(machine, prefix, "coder", "destroy")

    def test_missing_supervisord_reports_a_hint_not_a_crash(self, installed):
        machine, prefix = installed
        result = MachineDriver().supervisor_action(machine, prefix, "coder", "status")
        assert result["ok"] is False and "launchagent" in result["hint"]


class TestAgentConfigApi:
    @pytest.fixture
    def agent(self, client, plane, tmp_path):
        prefix = tmp_path / "coder"
        prefix.mkdir()
        (prefix / "agent.yaml").write_text(SAMPLE_YAML)
        (prefix / "instructions.md").write_text("# coder\n")
        machine = plane.store.add_machine("here", "localhost", is_local=True)
        return plane.store.upsert_agent(machine["id"], {
            "name": "coder", "prefix": str(prefix), "url": "http://a", "ready": 1,
        })

    def test_get_returns_yaml_properties_and_schema(self, client, agent):
        data = client.get(f"/api/agents/{agent['id']}/config", headers=AUTH).json()
        assert "name: coder" in data["yaml"]
        assert data["properties"]["model"] == "qwen2.5-coder:latest"
        assert any(p["key"] == "tools" for p in data["schema"])
        assert data["instructions"].startswith("# coder")

    def test_get_needs_the_token(self, client, agent):
        assert client.get(f"/api/agents/{agent['id']}/config").status_code == 401

    def test_unknown_agent_is_404(self, client):
        assert client.get("/api/agents/999/config", headers=AUTH).status_code == 404

    def test_put_properties_updates_the_file_and_the_record(self, client, plane, agent):
        response = client.put(f"/api/agents/{agent['id']}/config", json={
            "properties": {"model": "gpt-4o-mini", "provider": "openai",
                           "endpoint.url": ""},
            "restart": False,
        }, headers=AUTH)
        assert response.status_code == 200
        assert "gpt-4o-mini" in response.json()["yaml"]
        # the store now matches what the agent will read
        assert plane.store.agent(agent["id"])["model"] == "gpt-4o-mini"
        assert "endpoint" not in response.json()["yaml"]

    def test_get_reports_key_status_never_the_key(self, client, agent):
        Path(agent["prefix"], ".env").write_text("OPENAI_API_KEY=sk-secret\n")
        response = client.get(f"/api/agents/{agent['id']}/config", headers=AUTH)
        data = response.json()
        assert data["key_name"] == "OPENAI_API_KEY"          # vllm reads this one
        assert data["env_names"] == ["OPENAI_API_KEY"]
        assert "sk-secret" not in response.text

    def test_put_with_a_key_lands_under_the_new_provider(self, client, agent):
        response = client.put(f"/api/agents/{agent['id']}/config", json={
            "properties": {"provider": "anthropic"}, "api_key": "sk-ant-1",
            "restart": False,
        }, headers=AUTH)
        assert response.status_code == 200
        assert "sk-ant-1" not in response.text
        env = Path(agent["prefix"], ".env").read_text()
        assert "ANTHROPIC_API_KEY=sk-ant-1" in env
        assert "sk-ant-1" not in Path(agent["prefix"], "agent.yaml").read_text()

    def test_put_with_a_bad_key_writes_nothing(self, client, agent):
        before = Path(agent["prefix"], "agent.yaml").read_text()
        response = client.put(f"/api/agents/{agent['id']}/config", json={
            "properties": {"model": "changed"}, "api_key": "has space",
            "restart": False,
        }, headers=AUTH)
        assert response.status_code == 400
        assert Path(agent["prefix"], "agent.yaml").read_text() == before
        assert not Path(agent["prefix"], ".env").exists()

    def test_put_raw_yaml_is_written_verbatim(self, client, agent, tmp_path):
        raw = "# my own comment\nname: coder\nprovider: openai\nmodel: gpt-4o\n"
        response = client.put(f"/api/agents/{agent['id']}/config",
                              json={"yaml": raw, "restart": False}, headers=AUTH)
        assert response.status_code == 200
        assert response.json()["yaml"] == raw
        assert "# my own comment" in Path(agent["prefix"], "agent.yaml").read_text()

    def test_put_rejects_a_rename(self, client, agent):
        response = client.put(f"/api/agents/{agent['id']}/config", json={
            "yaml": SAMPLE_YAML.replace("name: coder", "name: other"), "restart": False,
        }, headers=AUTH)
        assert response.status_code == 400
        assert "cannot be changed" in response.json()["error"]

    def test_put_rejects_bad_yaml_without_touching_the_file(self, client, agent):
        before = Path(agent["prefix"], "agent.yaml").read_text()
        response = client.put(f"/api/agents/{agent['id']}/config",
                              json={"yaml": "name: [oops", "restart": False}, headers=AUTH)
        assert response.status_code == 400
        assert Path(agent["prefix"], "agent.yaml").read_text() == before

    def test_warnings_reach_the_caller(self, client, agent):
        response = client.put(f"/api/agents/{agent['id']}/config", json={
            "properties": {"tools": "web_request, made_up_tool"}, "restart": False,
        }, headers=AUTH)
        assert any("made_up_tool" in w for w in response.json()["warnings"])

    def test_failed_restart_is_reported_as_a_warning(self, client, agent):
        # No supervisord in a tmp dir, so the restart cannot succeed.
        response = client.put(f"/api/agents/{agent['id']}/config", json={
            "properties": {"model": "gpt-4o"}, "restart": True,
        }, headers=AUTH)
        assert response.status_code == 200
        assert any("restart failed" in w for w in response.json()["warnings"])

    def test_instructions_are_saved(self, client, agent):
        client.put(f"/api/agents/{agent['id']}/config", json={
            "instructions": "# new prompt\n", "properties": {}, "yaml": SAMPLE_YAML,
            "restart": False,
        }, headers=AUTH)
        assert Path(agent["prefix"], "instructions.md").read_text() == "# new prompt\n"

    def test_action_endpoint_reports_status(self, client, agent):
        response = client.post(f"/api/agents/{agent['id']}/action",
                               json={"action": "status"}, headers=AUTH)
        assert response.status_code == 200 and response.json()["ok"] is False

    def test_action_rejects_an_unknown_verb(self, client, agent):
        assert client.post(f"/api/agents/{agent['id']}/action",
                           json={"action": "rm -rf"}, headers=AUTH).status_code == 502


# ---------------------------------------------------------------------------
# fully qualified names: <agent>.<machine>
# ---------------------------------------------------------------------------

class TestQualifiedNames:
    @pytest.fixture
    def two_machines(self, store):
        """The same agent name on two machines — the case FQNs exist for."""
        here = store.add_machine("here", "localhost", is_local=True)
        box = store.add_machine("build-box", "10.0.0.9")
        a = store.upsert_agent(here["id"], {"name": "coder", "url": "http://here"})
        b = store.upsert_agent(box["id"], {"name": "coder", "url": "http://box"})
        c = store.upsert_agent(box["id"], {"name": "scout", "url": "http://scout"})
        channel = store.add_channel("dev")
        for agent in (a, b, c):
            store.join(channel["id"], agent["id"])
        return store, channel

    def test_every_agent_row_carries_its_fqn(self, two_machines):
        store, _ = two_machines
        assert {a["qualified"] for a in store.agents()} == {
            "coder.here", "coder.build-box", "scout.build-box"
        }

    def test_members_carry_their_fqn(self, two_machines):
        store, channel = two_machines
        assert "coder.here" in {m["qualified"] for m in store.members(channel["id"])}

    def test_lookup_by_fqn(self, two_machines):
        store, _ = two_machines
        assert store.agent_by_qualified("coder.build-box")["url"] == "http://box"
        assert store.agent_by_qualified("CODER.HERE")["url"] == "http://here"

    def test_lookup_needs_both_halves(self, two_machines):
        store, _ = two_machines
        assert store.agent_by_qualified("coder") is None
        assert store.agent_by_qualified(".here") is None

    def test_same_name_on_two_machines_is_allowed(self, two_machines):
        store, _ = two_machines
        assert len(store.agents_named("coder")) == 2

    def test_channels_for_agent_by_either_form(self, two_machines):
        store, _ = two_machines
        assert len(store.channels_for_agent_name("coder")) == 1        # bare: any machine
        assert len(store.channels_for_agent_name("coder.here")) == 1   # qualified: that one
        assert store.channels_for_agent_name("coder.nowhere") == []


class TestQualifiedMentions:
    MEMBERS = [
        {"name": "coder", "qualified": "coder.here"},
        {"name": "coder", "qualified": "coder.build-box"},
        {"name": "scout", "qualified": "scout.here"},
    ]

    def test_mention_pattern_accepts_a_machine_part(self):
        assert mentions("@coder.build-box please look") == ["coder.build-box"]

    def test_a_trailing_full_stop_is_not_part_of_the_name(self):
        assert mentions("over to @coder.") == ["coder"]
        assert mentions("ask @scout.here.") == ["scout.here"]

    def test_mixed_forms_in_one_line(self):
        assert mentions("@coder.here and @scout") == ["coder.here", "scout"]

    def test_fqn_resolves_exactly(self):
        member, error = resolve_member(self.MEMBERS, "coder.build-box")
        assert member["qualified"] == "coder.build-box" and error == ""

    def test_fqn_is_case_insensitive(self):
        member, _ = resolve_member(self.MEMBERS, "CODER.Here")
        assert member["qualified"] == "coder.here"

    def test_unambiguous_bare_name_still_works(self):
        member, error = resolve_member(self.MEMBERS, "scout")
        assert member["qualified"] == "scout.here" and error == ""

    def test_ambiguous_bare_name_lists_the_options(self):
        member, error = resolve_member(self.MEMBERS, "coder")
        assert member is None
        assert "ambiguous" in error
        assert "@coder.here" in error and "@coder.build-box" in error

    def test_unknown_name_says_so(self):
        member, error = resolve_member(self.MEMBERS, "ghost")
        assert member is None and "not in this channel" in error

    def test_leading_at_is_tolerated(self):
        member, _ = resolve_member(self.MEMBERS, "@scout.here")
        assert member["qualified"] == "scout.here"


class TestQualifiedDispatch:
    @pytest.mark.asyncio
    async def test_ambiguous_mention_is_refused_not_guessed(self, store):
        bus, router = EventBus(), None
        router = ChannelRouter(store, bus)
        here = store.add_machine("here", "localhost", is_local=True)
        box = store.add_machine("box", "10.0.0.9")
        channel = store.add_channel("dev")
        for machine in (here, box):
            agent = store.upsert_agent(machine["id"], {"name": "coder", "url": "http://x"})
            store.join(channel["id"], agent["id"])

        result = await router.post_human(channel["id"], "@coder go")
        assert result["dispatched"] == []
        last = store.messages(channel["id"])[-1]["text"]
        assert "ambiguous" in last and "@coder.box" in last

    @pytest.mark.asyncio
    async def test_qualified_mention_picks_the_right_machine(self, store, monkeypatch):
        router = ChannelRouter(store, EventBus())
        here = store.add_machine("here", "localhost", is_local=True)
        box = store.add_machine("box", "10.0.0.9")
        channel = store.add_channel("dev")
        for machine, url in ((here, "http://here"), (box, "http://box")):
            agent = store.upsert_agent(machine["id"], {"name": "coder", "url": url})
            store.join(channel["id"], agent["id"])

        seen = {}

        async def fake_dispatch(channel_id, agent, text, sender, **kwargs):
            seen["url"] = agent["url"]

        monkeypatch.setattr(router, "dispatch", fake_dispatch)
        result = await router.post_human(channel["id"], "@coder.box go")
        assert result["dispatched"] == ["coder.box"]
        import asyncio
        await asyncio.sleep(0)
        assert seen["url"] == "http://box"

    def test_the_prompt_names_members_by_fqn(self, store):
        router = ChannelRouter(store, EventBus())
        machine = store.add_machine("here", "localhost", is_local=True)
        agent = store.upsert_agent(machine["id"], {"name": "coder", "url": "http://x"})
        channel = store.add_channel("dev")
        store.join(channel["id"], agent["id"])
        prompt = router._prompt_for(channel["id"], "coder.here", "hi", "you")
        # its own name in the identity line, and no self-entry in the roster
        assert "You are 'coder.here'" in prompt
        assert "only member" in prompt


class TestUniqueNames:
    """The UI and the API both refuse a name that is already taken."""

    def test_duplicate_machine_name_is_rejected(self, client):
        first = client.post("/api/machines", json={"name": "here", "is_local": True},
                            headers=AUTH)
        assert first.status_code == 200
        again = client.post("/api/machines", json={"name": "here", "host": "10.0.0.9"},
                            headers=AUTH)
        assert again.status_code == 400
        assert "already registered" in again.json()["error"]

    def test_duplicate_differing_only_in_case_is_rejected(self, client):
        client.post("/api/machines", json={"name": "Box", "host": "10.0.0.9"}, headers=AUTH)
        again = client.post("/api/machines", json={"name": "box", "host": "10.0.0.10"},
                            headers=AUTH)
        assert again.status_code == 400
        assert "already registered" in again.json()["error"]

    def test_a_dot_in_a_machine_name_is_rejected(self, client):
        # It would make '<agent>.<machine>' unparseable.
        response = client.post("/api/machines", json={"name": "box.local", "host": "h"},
                               headers=AUTH)
        assert response.status_code == 400
        assert "agent address" in response.json()["error"]

    def test_a_space_in_a_machine_name_is_rejected(self, client):
        assert client.post("/api/machines", json={"name": "build box", "host": "h"},
                           headers=AUTH).status_code == 400

    def test_duplicate_agent_on_the_same_machine_is_rejected(self, client, plane):
        machine = client.post("/api/machines", json={"name": "here", "is_local": True},
                              headers=AUTH).json()["machine"]
        plane.store.upsert_agent(machine["id"], {"name": "coder", "url": "http://x"})
        response = client.post("/api/agents",
                               json={"machine_id": machine["id"], "name": "coder"},
                               headers=AUTH)
        assert response.status_code == 400
        assert "coder.here' already exists" in response.json()["error"]

    def test_an_invalid_agent_name_is_rejected_before_provisioning(self, client):
        machine = client.post("/api/machines", json={"name": "here", "is_local": True},
                              headers=AUTH).json()["machine"]
        response = client.post("/api/agents",
                               json={"machine_id": machine["id"], "name": "My Agent"},
                               headers=AUTH)
        assert response.status_code == 400
        assert "not valid" in response.json()["error"]

    def test_roster_reports_qualified_names(self, client, plane):
        here = plane.store.add_machine("here", "localhost", is_local=True)
        box = plane.store.add_machine("box", "10.0.0.9")
        channel = plane.store.add_channel("dev")
        for machine in (here, box):
            agent = plane.store.upsert_agent(machine["id"], {"name": "coder", "url": "http://x"})
            plane.store.join(channel["id"], agent["id"])

        roster = client.get("/api/peer/roster?agent=coder.here", headers=AUTH).json()["roster"]
        # the caller is excluded by its own FQN, the peer is named by its FQN
        assert [r["qualified"] for r in roster] == ["coder.box"]
        assert roster[0]["machine"] == "box"


# ---------------------------------------------------------------------------
# the board's Markdown renderer (static/md.js)
# ---------------------------------------------------------------------------

class TestBoardMarkdown:
    """
    Runs the renderer's own suite through node.

    Agents answer in Markdown, so the board renders it — and the text comes
    from a model, which makes escaping a security property rather than a
    nicety. The suite covers both, and is kept with the file it tests.
    """

    def test_md_js_suite_passes(self):
        import shutil
        import subprocess

        node = shutil.which("node")
        if node is None:
            pytest.skip("node is not installed; run agents_control/static/md.test.cjs by hand")

        suite = Path(__file__).resolve().parents[1] / "agents_control" / "static" / "md.test.cjs"
        assert suite.is_file(), f"missing {suite}"
        result = subprocess.run(
            [node, str(suite)], capture_output=True, text=True, timeout=60
        )
        assert result.returncode == 0, (
            f"md.js suite failed:\n{result.stdout[-4000:]}\n{result.stderr[-2000:]}"
        )
        assert "all md.js tests passed" in result.stdout


class TestSenderQualification:
    """A bare sender name is recorded as a full address when it is unambiguous."""

    @pytest.fixture
    def board(self, client, plane):
        machine = plane.store.add_machine("here", "localhost", is_local=True)
        agent = plane.store.upsert_agent(machine["id"], {"name": "coder", "url": "http://a"})
        channel = plane.store.add_channel("dev")
        plane.store.join(channel["id"], agent["id"])
        return plane, channel, machine

    def test_a_bare_sender_is_stored_qualified(self, client, board):
        """An agent whose .env predates AGENT_QUALIFIED_NAME reports a bare name."""
        plane, channel, _ = board
        client.post("/api/peer/log", json={
            "channel": "dev", "from": "coder", "to": "you", "text": "hi",
        }, headers=AUTH)
        message = plane.store.messages(channel["id"])[-1]
        assert message["sender"] == "coder.here"

    def test_an_already_qualified_sender_is_untouched(self, client, board):
        plane, channel, _ = board
        client.post("/api/peer/log", json={
            "channel": "dev", "from": "coder.here", "to": "you", "text": "hi",
        }, headers=AUTH)
        assert plane.store.messages(channel["id"])[-1]["sender"] == "coder.here"

    def test_an_ambiguous_bare_sender_is_left_alone(self, client, board):
        """Two agents share the name, so there is nothing to resolve it to."""
        plane, channel, _ = board
        other = plane.store.add_machine("box", "10.0.0.9")
        plane.store.upsert_agent(other["id"], {"name": "coder", "url": "http://b"})
        client.post("/api/peer/log", json={
            "channel": "dev", "from": "coder", "to": "you", "text": "hi",
        }, headers=AUTH)
        assert plane.store.messages(channel["id"])[-1]["sender"] == "coder"

    def test_system_is_not_qualified(self, client, board):
        plane, channel, _ = board
        client.post("/api/peer/log", json={
            "channel": "dev", "from": "system", "text": "note", "kind": "system",
        }, headers=AUTH)
        assert plane.store.messages(channel["id"])[-1]["sender"] == "system"

    def test_dispatch_records_the_qualified_sender(self, store):
        """The agent-reply path already speaks FQNs end to end."""
        router = ChannelRouter(store, EventBus())
        machine = store.add_machine("here", "localhost", is_local=True)
        agent = store.upsert_agent(machine["id"], {"name": "coder", "url": ""})
        channel = store.add_channel("dev")
        store.join(channel["id"], agent["id"])
        import asyncio
        asyncio.run(router.dispatch(channel["id"], store.agent(agent["id"]), "hi", "you"))
        # no url, so it reports that — naming the agent by its full address
        assert "@coder.here" in store.messages(channel["id"])[-1]["text"]


# ---------------------------------------------------------------------------
# board relay: agents talk by mentioning, the hub delivers
# ---------------------------------------------------------------------------

class TestBoardRelay:
    @pytest.fixture
    def board(self, store):
        router = ChannelRouter(store, EventBus())
        machine = store.add_machine("here", "localhost", is_local=True)
        channel = store.add_channel("dev")
        agents = {}
        for name in ("alice", "bob", "carol"):
            agent = store.upsert_agent(
                machine["id"], {"name": name, "url": f"http://{name}", "ready": 1})
            store.join(channel["id"], agent["id"])
            agents[name] = agent
        return store, router, channel, agents

    @pytest.fixture
    def spy(self, monkeypatch):
        """Capture dispatches instead of calling an agent over A2A."""
        sent = []

        def install(router):
            async def fake_dispatch(channel_id, agent, text, sender, depth=0, **kwargs):
                sent.append({
                    "to": agent["qualified"], "sender": sender,
                    "text": text, "depth": depth,
                })
            monkeypatch.setattr(router, "dispatch", fake_dispatch)
            return sent

        return install

    @pytest.mark.asyncio
    async def test_a_mention_is_delivered_to_that_agent(self, board, spy):
        store, router, channel, _ = board
        sent = spy(router)
        relayed = await router.relay(channel["id"], "alice.here", "@bob.here take a look")
        assert relayed == ["bob.here"]
        await asyncio.sleep(0)
        assert sent[0]["to"] == "bob.here"
        assert sent[0]["sender"] == "alice.here"      # attributed to the agent

    @pytest.mark.asyncio
    async def test_several_mentions_reach_everyone_named(self, board, spy):
        store, router, channel, _ = board
        sent = spy(router)
        relayed = await router.relay(channel["id"], "alice.here", "@bob and @carol please")
        assert sorted(relayed) == ["bob.here", "carol.here"]
        await asyncio.sleep(0)
        assert len(sent) == 2

    @pytest.mark.asyncio
    async def test_a_self_mention_is_dropped(self, board, spy):
        """It carries no information and would loop forever."""
        store, router, channel, _ = board
        sent = spy(router)
        relayed = await router.relay(channel["id"], "alice.here", "@alice.here keep going")
        assert relayed == []
        await asyncio.sleep(0)
        assert sent == []

    @pytest.mark.asyncio
    async def test_prose_that_is_not_a_member_is_ignored_quietly(self, board, spy):
        store, router, channel, _ = board
        sent = spy(router)
        relayed = await router.relay(channel["id"], "alice.here", "email me @ work")
        assert relayed == []
        # no system noise for something that was probably not an address at all
        assert store.messages(channel["id"]) == []

    @pytest.mark.asyncio
    async def test_a_reply_with_no_mention_relays_nothing(self, board, spy):
        store, router, channel, _ = board
        spy(router)
        assert await router.relay(channel["id"], "alice.here", "all done, no idea") == []

    @pytest.mark.asyncio
    async def test_a_halted_channel_refuses_to_relay(self, board, spy):
        """The kill switch is authoritative now: the hub is in every hop."""
        store, router, channel, _ = board
        sent = spy(router)
        store.set_halted(channel["id"], True)
        relayed = await router.relay(channel["id"], "alice.here", "@bob.here carry on")
        assert relayed == []
        await asyncio.sleep(0)
        assert sent == []
        assert "halted" in store.messages(channel["id"])[-1]["text"]

    @pytest.mark.asyncio
    async def test_hops_are_unlimited_by_default(self, board, spy):
        store, router, channel, _ = board
        sent = spy(router)
        assert store.setting("max_relay_hops") == 0
        relayed = await router.relay(channel["id"], "alice.here", "@bob.here go", depth=99)
        assert relayed == ["bob.here"]
        await asyncio.sleep(0)
        assert sent[0]["depth"] == 99

    @pytest.mark.asyncio
    async def test_a_hop_budget_stops_the_cascade_when_set(self, board, spy):
        store, router, channel, _ = board
        sent = spy(router)
        store.update_settings({"max_relay_hops": 2})
        assert await router.relay(channel["id"], "alice.here", "@bob.here go", depth=2) \
            == ["bob.here"]
        assert await router.relay(channel["id"], "alice.here", "@bob.here go", depth=3) == []
        await asyncio.sleep(0)
        assert len(sent) == 1
        assert "max_relay_hops is 2" in store.messages(channel["id"])[-1]["text"]

    @pytest.mark.asyncio
    async def test_depth_grows_one_hop_per_reply(self, board, monkeypatch):
        """A reply's own mentions are relayed one level deeper than the reply."""
        store, router, channel, _ = board
        depths = []

        async def fake_dispatch(channel_id, agent, text, sender, depth=0, **kwargs):
            depths.append(depth)

        monkeypatch.setattr(router, "dispatch", fake_dispatch)
        await router.relay(channel["id"], "alice.here", "@bob.here one", depth=1)
        await router.relay(channel["id"], "bob.here", "@carol.here two", depth=2)
        await asyncio.sleep(0)
        assert depths == [1, 2]


class TestBoardSayApi:
    """POST /api/board/say — what an agent's tool call lands on."""

    @pytest.fixture
    def board(self, client, plane):
        machine = plane.store.add_machine("here", "localhost", is_local=True)
        channel = plane.store.add_channel("dev")
        for name in ("alice", "bob"):
            agent = plane.store.upsert_agent(
                machine["id"], {"name": name, "url": f"http://{name}", "ready": 1})
            plane.store.join(channel["id"], agent["id"])
        return plane, channel, machine

    def test_posts_and_reports_who_it_reached(self, client, board, monkeypatch):
        plane, channel, _ = board
        dispatched = []

        async def fake_dispatch(channel_id, agent, text, sender, depth=0, **kwargs):
            dispatched.append(agent["qualified"])

        monkeypatch.setattr(plane.router, "dispatch", fake_dispatch)
        response = client.post("/api/board/say", json={
            "from": "alice", "text": "@bob.here please look", "channel": "dev",
        }, headers=AUTH)
        assert response.status_code == 200
        assert response.json()["relayed"] == ["bob.here"]
        assert response.json()["members"] == ["alice.here", "bob.here"]
        # and it is in the transcript, attributed to the full address
        assert plane.store.messages(channel["id"])[0]["sender"] == "alice.here"

    def test_channel_is_inferred_when_there_is_only_one(self, client, board, monkeypatch):
        plane, _, _ = board
        monkeypatch.setattr(plane.router, "dispatch",
                            lambda *a, **k: asyncio.sleep(0))
        response = client.post("/api/board/say",
                               json={"from": "alice", "text": "@bob hi"}, headers=AUTH)
        assert response.status_code == 200 and response.json()["channel"] == "dev"

    def test_several_channels_need_one_named(self, client, board):
        plane, _, machine = board
        other = plane.store.add_channel("ops")
        alice = plane.store.agent_by_qualified("alice.here")
        plane.store.join(other["id"], alice["id"])
        response = client.post("/api/board/say",
                               json={"from": "alice", "text": "@bob hi"}, headers=AUTH)
        assert response.status_code == 400
        assert "several channels" in response.json()["error"]

    def test_an_agent_in_no_channel_has_nowhere_to_post(self, client, plane):
        machine = plane.store.add_machine("here", "localhost", is_local=True)
        plane.store.upsert_agent(machine["id"], {"name": "loner", "url": "http://x"})
        response = client.post("/api/board/say",
                               json={"from": "loner", "text": "@bob hi"}, headers=AUTH)
        assert response.status_code == 404
        assert "nowhere to post" in response.json()["error"]

    def test_an_unknown_channel_is_404(self, client, board):
        response = client.post("/api/board/say", json={
            "from": "alice", "text": "hi", "channel": "nope",
        }, headers=AUTH)
        assert response.status_code == 404

    def test_text_is_required(self, client, board):
        assert client.post("/api/board/say", json={"from": "alice"},
                           headers=AUTH).status_code == 400

    def test_it_needs_the_token(self, client, board):
        assert client.post("/api/board/say",
                           json={"from": "alice", "text": "hi"}).status_code == 401

    def test_halted_channel_records_but_does_not_relay(self, client, board):
        plane, channel, _ = board
        plane.store.set_halted(channel["id"], True)
        response = client.post("/api/board/say", json={
            "from": "alice", "text": "@bob.here go", "channel": "dev",
        }, headers=AUTH)
        assert response.json()["relayed"] == []
        assert response.json()["halted"] is True


class TestRosterScopes:
    def test_scope_all_lists_every_agent_on_the_board(self, client, plane):
        here = plane.store.add_machine("here", "localhost", is_local=True)
        box = plane.store.add_machine("box", "10.0.0.9")
        plane.store.upsert_agent(here["id"], {"name": "alice", "url": "http://a"})
        plane.store.upsert_agent(box["id"], {"name": "bob", "url": "http://b"})
        # neither is in a channel, so the channel scope sees nothing
        assert client.get("/api/peer/roster?agent=alice.here",
                          headers=AUTH).json()["roster"] == []
        everyone = client.get("/api/peer/roster?scope=all", headers=AUTH).json()["roster"]
        assert sorted(r["qualified"] for r in everyone) == ["alice.here", "bob.box"]

    def test_channel_scope_carries_what_discovery_needs(self, client, plane):
        machine = plane.store.add_machine("here", "localhost", is_local=True)
        channel = plane.store.add_channel("dev")
        for name, desc in (("alice", "asks"), ("bob", "reads logs")):
            agent = plane.store.upsert_agent(machine["id"], {
                "name": name, "url": "http://x", "description": desc,
                "model": "gpt-4o-mini", "ready": 1,
            })
            plane.store.join(channel["id"], agent["id"])
        roster = client.get("/api/peer/roster?agent=alice.here", headers=AUTH).json()["roster"]
        assert len(roster) == 1
        entry = roster[0]
        assert entry["qualified"] == "bob.here"
        assert entry["description"] == "reads logs"
        assert entry["model"] == "gpt-4o-mini"
        assert entry["ready"] is True


# ---------------------------------------------------------------------------
# telling a stale server from a current one
# ---------------------------------------------------------------------------

class TestServerVersion:
    def test_healthz_is_public_and_reports_the_revision(self, client):
        """Public on purpose: an old server may 401 or 404 everything else."""
        response = client.get("/healthz")          # no token
        assert response.status_code == 200
        payload = response.json()
        assert payload["ok"] is True
        assert payload["api_revision"] >= 1
        assert payload["version"]
        assert payload["started"]

    def test_the_board_and_the_server_agree_on_the_revision(self):
        """
        static/app.js is read from disk per request while the Python routes are
        loaded at startup, so the two can drift. The board warns when the
        server is behind — which only works if both constants are in step.
        """
        import re

        from declarative_agent_sdk.agents_control import API_REVISION

        app_js = (
            Path(__file__).resolve().parents[1]
            / "agents_control" / "static" / "app.js"
        ).read_text()
        match = re.search(r"const EXPECTS_API = (\d+);", app_js)
        assert match, "EXPECTS_API is missing from app.js"
        assert int(match.group(1)) == API_REVISION, (
            f"app.js expects api {match.group(1)} but API_REVISION is "
            f"{API_REVISION} — bump both together"
        )

    def test_a_route_that_does_not_exist_is_a_bare_404(self, client):
        """
        What a stale server does, and why the board explains it: no error body,
        because the router never reached a handler.
        """
        response = client.get("/api/agents/1/was-never-a-route", headers=AUTH)
        assert response.status_code == 404
        # not even JSON: Starlette's router answered, so there is no handler and
        # no error field for the board to show. Hence the explanatory message.
        with pytest.raises(Exception):
            response.json()

    def test_a_handled_404_does_carry_an_error(self, client):
        response = client.get("/api/agents/999/config", headers=AUTH)
        assert response.status_code == 404
        assert response.json()["error"] == "no such agent"


# ---------------------------------------------------------------------------
# the env file handed to install.sh
# ---------------------------------------------------------------------------

STUB_INSTALLER = '''#!/usr/bin/env bash
# Stands in for install.sh: checks --env-file the way the real one does.
set -euo pipefail
NAME=""; ENV_FILE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --name) NAME="$2"; shift 2 ;;
    --env-file) ENV_FILE="$2"; shift 2 ;;
    --json|--no-approval) shift ;;
    *) shift 2 ;;
  esac
done
if [ -n "$ENV_FILE" ]; then
  [ -f "$ENV_FILE" ] || { printf 'error: --env-file not found: %s\\n' "$ENV_FILE" >&2; exit 1; }
  cp "$ENV_FILE" "${ENV_FILE}.seen"
fi
printf '{"name":"%s","ready":true,"url":"http://127.0.0.1:8001/","port":8001}\\n' "$NAME"
'''


class TestEnvFileHandoff:
    """
    Regression: the staged env file's path must survive the trip to install.sh.

    It was passed through shlex.quote, which single-quotes it — and no shell
    expands $HOME or ~ inside single quotes, so install.sh stopped with
    "--env-file not found" on every machine with a key to pass.
    """

    @pytest.fixture
    def stub(self, tmp_path, monkeypatch):
        deploy = tmp_path / "deploy"
        deploy.mkdir()
        for name in ("install.sh", "uninstall.sh"):
            script = deploy / name
            script.write_text(STUB_INSTALLER)
            script.chmod(0o755)
        monkeypatch.setenv("AGENTS_CONTROL_DEPLOY_DIR", str(deploy))
        return deploy

    @staticmethod
    def _capture(driver_cls=MachineDriver):
        """A driver that records commands instead of running them."""
        sent = []

        class Spy(driver_cls):
            def run(self, machine, script, stdin=None, timeout=None):
                sent.append(script)
                if "--list" in script:
                    return "[]"
                if "--json" in script:
                    return '{"name":"researcher","ready":true}'
                return ""

            def push_scripts(self, machine, force=False):
                pass

        return Spy(), sent

    REMOTE = {"id": 1, "name": "nomarchy", "host": "nomarchy", "ssh_user": "mg"}

    def test_env_path_is_not_single_quoted(self, stub):
        driver, sent = self._capture()
        driver.create_agent(self.REMOTE, name="researcher", api_key="sk-x")
        install = next(c for c in sent if "install.sh" in c)
        assert '--env-file "$HOME/.agents-control/env.researcher"' in install
        assert "--env-file '" not in install     # the bug

    def test_the_file_is_written_where_install_sh_will_look(self, stub):
        driver, sent = self._capture()
        driver.create_agent(self.REMOTE, name="researcher", api_key="sk-x")
        write = next(c for c in sent if "cat >" in c)
        install = next(c for c in sent if "install.sh" in c)
        written = write.split("cat > ")[1].strip()
        assert f"--env-file {written}" in install   # same token, both sides

    def test_no_tilde_reaches_install_sh(self, stub):
        driver, sent = self._capture()
        driver.create_agent(self.REMOTE, name="researcher", api_key="sk-x")
        assert "~" not in next(c for c in sent if "install.sh" in c)

    def test_the_secret_never_appears_in_argv(self, stub):
        driver, sent = self._capture()
        driver.create_agent(self.REMOTE, name="researcher", api_key="sk-secret-value")
        assert all("sk-secret-value" not in command for command in sent)

    def test_no_env_file_flag_when_there_is_nothing_to_seed(self, stub):
        driver, sent = self._capture()
        driver.create_agent(self.REMOTE, name="researcher", provider="vllm")
        assert "--env-file" not in next(c for c in sent if "install.sh" in c)

    def test_a_real_run_finds_the_file(self, stub, tmp_path, monkeypatch):
        """End to end against the stub, which fails exactly as install.sh did."""
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        (tmp_path / "home").mkdir()
        driver = MachineDriver()
        local = {"id": 1, "name": "here", "is_local": 1, "host": "localhost"}
        result = driver.create_agent(
            local, name="researcher", api_key="sk-test",
            control_env={"AGENTS_CONTROL_URL": "http://127.0.0.1:8900"},
        )
        assert result["name"] == "researcher" and result["ready"] is True

        # the stub copied what it was given: the key and the handshake arrived
        seen = tmp_path / "home" / ".agents-control" / "env.researcher.seen"
        assert seen.is_file()
        body = seen.read_text()
        assert "OPENAI_API_KEY=sk-test" in body
        assert "AGENTS_CONTROL_URL=http://127.0.0.1:8900" in body

    def test_the_staged_copy_is_removed_afterwards(self, stub, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        (tmp_path / "home").mkdir()
        local = {"id": 1, "name": "here", "is_local": 1, "host": "localhost"}
        MachineDriver().create_agent(local, name="researcher", api_key="sk-test")
        staged = tmp_path / "home" / ".agents-control" / "env.researcher"
        assert not staged.exists(), "the key must not be left lying about"

    def test_a_home_with_a_space_still_works(self, stub, tmp_path, monkeypatch):
        home = tmp_path / "my home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        local = {"id": 1, "name": "here", "is_local": 1, "host": "localhost"}
        result = MachineDriver().create_agent(local, name="researcher", api_key="sk-test")
        assert result["ready"] is True

    def test_staged_paths_are_quoted_for_both_kinds_of_machine(self):
        driver = MachineDriver()
        _, remote = driver._staged_env_path(self.REMOTE, "researcher")
        _, local = driver._staged_env_path({"is_local": 1, "host": "localhost"}, "researcher")
        assert remote.startswith('"') and remote.endswith('"')
        assert local.startswith('"') and local.endswith('"')
        assert "$HOME" in remote and "$HOME" not in local


# ---------------------------------------------------------------------------
# which SDK an agent runs
# ---------------------------------------------------------------------------

class TestSdkSource:
    """
    install.sh installs from GitHub by default, so an agent created from
    unpushed work would quietly run whatever is on the branch instead. The
    'local' source builds a wheel from this working tree and ships it.
    """

    REMOTE = {"id": 1, "name": "nomarchy", "host": "nomarchy", "ssh_user": "mg"}

    @staticmethod
    def _capture():
        sent = []

        class Spy(MachineDriver):
            uploaded = 0

            def run(self, machine, script, stdin=None, timeout=None, stdin_bytes=None):
                sent.append({"script": script, "bytes": stdin_bytes})
                if stdin_bytes is not None:
                    self.uploaded = len(stdin_bytes)
                if "--list" in script:
                    return "[]"
                if "wc -c" in script:
                    # answer as the far side would, so the integrity check passes
                    return str(self.uploaded)
                if "--json" in script:
                    return '{"name":"researcher","ready":true}'
                return ""

            def push_scripts(self, machine, force=False):
                pass

        return Spy(), sent

    def test_git_is_the_explicit_alternative(self, tmp_path, monkeypatch):
        driver, sent = self._capture()
        driver.create_agent(self.REMOTE, name="researcher", sdk_source="git")
        install = next(s["script"] for s in sent if "install.sh" in s["script"])
        assert "--source wheel" not in install
        assert "--wheel" not in install

    def test_a_git_ref_is_passed_through(self):
        driver, sent = self._capture()
        driver.create_agent(self.REMOTE, name="researcher", sdk_source="git",
                            sdk_ref="feature-branch")
        install = next(s["script"] for s in sent if "install.sh" in s["script"])
        assert "--ref feature-branch" in install

    def test_local_builds_and_sends_a_wheel(self, monkeypatch, tmp_path):
        driver, sent = self._capture()
        wheel = tmp_path / "declarative_agent_sdk-0.9.2-py3-none-any.whl"
        wheel.write_bytes(b"PK\x03\x04 pretend wheel")
        monkeypatch.setattr(driver, "build_wheel", lambda force=False: wheel)

        driver.create_agent(self.REMOTE, name="researcher", sdk_source="local")

        # sent byte for byte: a wheel does not survive text re-encoding
        payload = next(s["bytes"] for s in sent if s["bytes"])
        assert payload == wheel.read_bytes()

        install = next(s["script"] for s in sent if "install.sh" in s["script"])
        assert '--source wheel --wheel "$HOME/.agents-control/wheels/' in install

    def test_a_truncated_transfer_is_caught(self, monkeypatch, tmp_path):
        wheel = tmp_path / "x.whl"
        wheel.write_bytes(b"0123456789")

        class Liar(MachineDriver):
            def run(self, machine, script, stdin=None, timeout=None, stdin_bytes=None):
                return "3" if "wc -c" in script else ""

            def push_scripts(self, machine, force=False):
                pass

        with pytest.raises(MachineError, match="truncated"):
            Liar().push_wheel(self.REMOTE, wheel)

    def test_a_local_machine_needs_no_transfer(self, tmp_path):
        wheel = tmp_path / "x.whl"
        wheel.write_bytes(b"PK")
        path = MachineDriver().push_wheel(
            {"is_local": 1, "host": "localhost"}, wheel)
        assert str(wheel) in path

    def test_the_wheel_carries_todays_code(self):
        """
        The point of the feature: the built wheel must contain what git does
        not have yet.
        """
        import zipfile

        wheel = MachineDriver().build_wheel(force=True)
        names = zipfile.ZipFile(wheel).namelist()
        for expected in (
            "declarative_agent_sdk/agents_control/router.py",
            "declarative_agent_sdk/tools/builtin/send_to_agent.py",
            "declarative_agent_sdk/tools/builtin/list_agents.py",
            "declarative_agent_sdk/agents_control/static/app.js",
        ):
            assert expected in names, f"{expected} missing from the wheel"

    def test_the_api_defaults_to_the_working_tree(self, client, plane, monkeypatch):
        """A create with no sdk_source must not silently fall back to git."""
        machine = client.post("/api/machines", json={"name": "here", "is_local": True},
                              headers=AUTH).json()["machine"]
        seen = {}

        def fake_create(machine, **kwargs):
            seen.update(kwargs)
            return {"name": kwargs["name"], "ready": True, "url": "http://x"}

        monkeypatch.setattr(plane.driver, "create_agent", fake_create)
        client.post("/api/agents",
                    json={"machine_id": machine["id"], "name": "fresh"}, headers=AUTH)
        assert seen["sdk_source"] == "local"
        assert plane.store.setting("default_sdk_source") == "local"

    def test_the_setting_can_be_changed_to_git(self, client, plane, monkeypatch):
        plane.store.update_settings({"default_sdk_source": "git"})
        machine = client.post("/api/machines", json={"name": "here", "is_local": True},
                              headers=AUTH).json()["machine"]
        seen = {}
        monkeypatch.setattr(plane.driver, "create_agent",
                            lambda machine, **kw: seen.update(kw) or
                            {"name": kw["name"], "ready": True, "url": "http://x"})
        client.post("/api/agents",
                    json={"machine_id": machine["id"], "name": "fresh"}, headers=AUTH)
        assert seen["sdk_source"] == "git"


# ---------------------------------------------------------------------------
# long model calls
# ---------------------------------------------------------------------------

class TestRequestTimeout:
    """
    The lean client's per-call timeout was hard-wired to 120s with no way to
    change it from agent.yaml. A local model asked to write a document hits it
    every time, and the board only shows "the agent could not complete this
    request" — the TimeoutError is in the agent's log.
    """

    def test_the_client_default_suits_a_local_model(self):
        from declarative_agent_sdk.agents.lean.runtime.llm import DEFAULT_REQUEST_TIMEOUT
        assert DEFAULT_REQUEST_TIMEOUT >= 300

    def test_endpoint_timeout_is_parsed(self):
        from declarative_agent_sdk.core.agent_config import parse_common_config

        common = parse_common_config(
            {"name": "a", "endpoint": {"url": "http://h/v1", "timeout": 900}},
            framework="lean", default_model="m", default_provider="vllm",
        )
        assert common.request_timeout == 900

    def test_root_request_timeout_is_parsed(self):
        from declarative_agent_sdk.core.agent_config import parse_common_config

        common = parse_common_config(
            {"name": "a", "request_timeout": 600},
            framework="lean", default_model="m", default_provider="vllm",
        )
        assert common.request_timeout == 600

    def test_unset_leaves_the_client_default(self):
        from declarative_agent_sdk.core.agent_config import parse_common_config

        common = parse_common_config(
            {"name": "a"}, framework="lean", default_model="m", default_provider="vllm")
        assert common.request_timeout is None

    def test_it_reaches_the_llm_client(self, tmp_path):
        """End to end: YAML value → LeanAIAgent → LeanLLMClient.timeout."""
        from declarative_agent_sdk.core.agent_factory import AgentFactory

        config = {
            "name": "slow", "agent_framework": "lean", "provider": "vllm",
            "model": "big-local-model",
            "endpoint": {"url": "http://127.0.0.1:9/v1", "timeout": 1200},
            "skills_directory": str(tmp_path / "skills"),
            "workspace_directory": str(tmp_path / "ws"),
        }
        agent = AgentFactory.from_dict(config)
        assert agent._client.timeout == 1200.0

    def test_the_default_applies_when_yaml_is_silent(self, tmp_path):
        from declarative_agent_sdk.agents.lean.runtime.llm import DEFAULT_REQUEST_TIMEOUT
        from declarative_agent_sdk.core.agent_factory import AgentFactory

        agent = AgentFactory.from_dict({
            "name": "plain", "agent_framework": "lean", "provider": "vllm",
            "model": "m", "endpoint": {"url": "http://127.0.0.1:9/v1"},
            "skills_directory": str(tmp_path / "skills"),
            "workspace_directory": str(tmp_path / "ws"),
        })
        assert agent._client.timeout == DEFAULT_REQUEST_TIMEOUT

    def test_the_config_editor_exposes_it(self):
        from declarative_agent_sdk.agents_control import config

        assert any(p["key"] == "request_timeout" for p in config.EDITABLE_PROPERTIES)
        text = config.merge("name: a\n", {"request_timeout": "900"})
        assert config.parse(text)["request_timeout"] == 900.0

    def test_the_board_waits_longer_than_one_call(self, store):
        """A turn is several calls, so the dispatch timeout must exceed one."""
        from declarative_agent_sdk.agents.lean.runtime.llm import DEFAULT_REQUEST_TIMEOUT

        assert store.setting("agent_timeout_seconds") > DEFAULT_REQUEST_TIMEOUT


class TestFailureHint:
    """A2A hides the exception text, so the board says which log holds it."""

    def test_the_hint_names_the_log_and_machine(self, store):
        router = ChannelRouter(store, EventBus())
        machine = store.add_machine("macbook-pro", "localhost", is_local=True)
        agent = store.upsert_agent(machine["id"], {
            "name": "researcher", "url": "http://x",
            "prefix": "/Users/me/.declarative-agent/researcher",
        })
        hint = router._where_to_look(store.agent(agent["id"]))
        assert "/Users/me/.declarative-agent/researcher/logs/researcher.err.log" in hint
        assert "macbook-pro" in hint

    def test_no_prefix_means_no_hint(self, store):
        router = ChannelRouter(store, EventBus())
        machine = store.add_machine("here", "localhost", is_local=True)
        agent = store.upsert_agent(machine["id"], {"name": "a", "url": "http://x"})
        assert router._where_to_look(store.agent(agent["id"])) == ""

    @pytest.mark.asyncio
    async def test_a_failed_turn_points_at_the_log(self, store, monkeypatch):
        router, agent, channel = _polling_board(
            store, monkeypatch,
            tasks=[_fake_task("failed", "The agent could not complete this request.")],
            prefix="/opt/researcher",
        )
        await router.dispatch(channel["id"], agent, "go", "you")
        last = store.messages(channel["id"])[-1]["text"]
        assert "could not complete" in last
        assert "/opt/researcher/logs/researcher.err.log" in last


class TestBoardScriptWiring:
    """
    Static checks on app.js that catch the mistakes a browser only reveals by
    doing nothing at all.
    """

    @staticmethod
    def _static(name: str) -> str:
        return (
            Path(__file__).resolve().parents[1] / "agents_control" / "static" / name
        ).read_text()

    def test_template_ids_are_not_queried_on_the_document(self):
        """
        An id declared inside <template> does not exist until the clone is
        inserted, so `$('#that-id')` returns null and the handler throws —
        which is how the Create agent button came to do nothing.
        """
        import re

        html = self._static("index.html")
        app_js = self._static("app.js")

        template_ids = set()
        for block in re.findall(r"<template\b.*?</template>", html, re.S):
            template_ids.update(re.findall(r'id="([^"]+)"', block))
        assert template_ids, "no template ids found — has the markup changed?"

        offenders = [
            tid for tid in template_ids
            if re.search(rf"""\$\(\s*['"]#{re.escape(tid)}['"]\s*\)""", app_js)
            or re.search(rf"""getElementById\(\s*['"]{re.escape(tid)}['"]""", app_js)
        ]
        assert not offenders, (
            "these ids live inside a <template> but are queried on the document: "
            f"{offenders} — scope the lookup to the cloned form instead"
        )

    def test_every_form_field_the_script_reads_exists(self):
        """
        A renamed input would otherwise fail only when the form is opened.

        Two sources are legitimate: a `name=` in the markup, and a key in the
        config editor's schema — those inputs are built at runtime from
        EDITABLE_PROPERTIES rather than written in HTML.
        """
        import re

        from declarative_agent_sdk.agents_control.config import EDITABLE_PROPERTIES

        html = self._static("index.html")
        app_js = self._static("app.js")

        declared = set(re.findall(r'name="([^"]+)"', html))
        declared |= {prop["key"] for prop in EDITABLE_PROPERTIES}
        used = set(re.findall(r"form\.elements\.([A-Za-z_][A-Za-z0-9_]*)", app_js))
        missing = sorted(used - declared)
        assert not missing, f"app.js reads form fields nothing declares: {missing}"

    def test_uncaught_failures_are_surfaced(self):
        app_js = self._static("app.js")
        assert "addEventListener('error'" in app_js
        assert "addEventListener('unhandledrejection'" in app_js

    def test_the_host_picker_is_wired_to_the_machine_select(self):
        app_js = self._static("app.js")
        assert "form.elements.machine_id.onchange" in app_js
        assert "fillHosts()" in app_js
        assert "bind_host_custom" in app_js

    def test_the_picker_can_rescan_a_machine(self):
        """A machine registered before addresses were collected has none stored."""
        app_js = self._static("app.js")
        assert "/interfaces`" in app_js
        assert "rescanHosts" in app_js


# ---------------------------------------------------------------------------
# a timeout that explains itself, and settings that lag the shipped defaults
# ---------------------------------------------------------------------------

class TestDispatchFailureMessages:
    """
    Dispatch submits and polls, so a client timeout now means a poll went
    unanswered — a blocked process, not a slow model. The message says so.
    """

    def test_an_unanswered_poll_points_at_the_process(self):
        from a2a.client.errors import A2AClientTimeoutError

        message = ChannelRouter._explain_dispatch_failure(
            "reviewer.omarchy", A2AClientTimeoutError("Client Request timed out"), 1800)
        assert "poll" in message
        assert "Poll request timeout" in message

    def test_a_plain_timeout_error_is_recognised_too(self):
        message = ChannelRouter._explain_dispatch_failure("a.b", TimeoutError(), 900)
        assert "poll" in message

    def test_other_failures_keep_their_detail(self):
        from a2a.client.errors import AgentCardResolutionError

        message = ChannelRouter._explain_dispatch_failure(
            "reviewer.omarchy",
            AgentCardResolutionError("All connection attempts failed"), 900)
        assert "AgentCardResolutionError" in message
        assert "All connection attempts failed" in message
        assert "poll" not in message

    @pytest.mark.asyncio
    async def test_a_slow_agent_is_followed_then_released(self, store, monkeypatch):
        """
        The budget bounds the agent's work. When it runs out the task is left
        alone — not cancelled — and the channel says which task it was.
        """
        router, agent, channel = _polling_board(
            store, monkeypatch,
            tasks=[_fake_task("working", "thinking"), _fake_task("working", "thinking")],
        )
        store.update_settings({
            "agent_timeout_seconds": 0, "poll_interval_seconds": 1,
        })
        await router.dispatch(channel["id"], agent, "go", "you")
        last = store.messages(channel["id"])[-1]["text"]
        assert "still working after 0s" in last
        assert "has not been cancelled" in last


class TestSettingsDrift:
    """
    Seeding only fills missing keys, so a default raised later never reaches an
    existing store. The board shows the difference instead of overwriting a
    value that may have been chosen on purpose.
    """

    def test_the_api_reports_the_shipped_defaults(self, client):
        payload = client.get("/api/settings", headers=AUTH).json()
        assert "defaults" in payload
        assert payload["defaults"]["agent_timeout_seconds"] == 1800

    def test_state_carries_them_too(self, client):
        assert "defaults" in client.get("/api/state", headers=AUTH).json()

    def test_an_old_value_is_kept_not_overwritten(self, tmp_path):
        """A store created with 300 still has 300 after reopening."""
        path = str(tmp_path / "old.db")
        store = Store(path)
        store.update_settings({"agent_timeout_seconds": 300})
        store.close()
        assert Store(path).setting("agent_timeout_seconds") == 300

    def test_the_board_surfaces_the_difference(self):
        app_js = (
            Path(__file__).resolve().parents[1]
            / "agents_control" / "static" / "app.js"
        ).read_text()
        assert "markDrift" in app_js
        assert "state.defaults" in app_js


# ---------------------------------------------------------------------------
# tool approval across the A2A boundary
# ---------------------------------------------------------------------------

class TestApprovalFlow:
    """
    The approval is read from a polled `input-required` state, so it surfaces
    whenever the agent asks — it no longer depends on an SSE stream surviving
    the wait. Answering it resumes the same task.
    """

    @pytest.fixture
    def wired(self, store, monkeypatch):
        router, agent, channel = _polling_board(
            store, monkeypatch,
            tasks=[
                _fake_task("working", "Calling tools: web_request"),
                _fake_task("input", "", approval=("fn-1", "web_request",
                                                  {"url": "https://example.com"})),
            ],
            after_confirmation=[_fake_task("completed", "Here is the research.")],
        )
        return router, agent, channel, router._calls

    @pytest.mark.asyncio
    async def test_an_approved_call_resumes_the_task(self, wired):
        router, agent, channel, calls = wired

        async def approve(channel_id, name, approval):
            approval.resolve(True)
            return True

        router._await_decision = approve
        await router.dispatch(channel["id"], agent, "research this", "you")

        assert ("confirm", True) in calls
        texts = [m["text"] for m in router.store.messages(channel["id"])]
        assert "Here is the research." in texts

    @pytest.mark.asyncio
    async def test_the_request_is_announced_in_the_channel(self, wired):
        router, agent, channel, _ = wired
        router._await_decision = lambda *a: _resolved(False)
        await router.dispatch(channel["id"], agent, "research this", "you")
        texts = " ".join(m["text"] for m in router.store.messages(channel["id"]))
        assert "wants to run web_request" in texts
        assert "example.com" in texts

    @pytest.mark.asyncio
    async def test_a_denial_still_resumes_the_task(self, wired):
        """The agent is told no, rather than being left waiting."""
        router, agent, channel, calls = wired
        router._await_decision = lambda *a: _resolved(False)
        await router.dispatch(channel["id"], agent, "research this", "you")
        assert ("confirm", False) in calls

    @pytest.mark.asyncio
    async def test_a_timeout_denies_and_says_so(self, wired):
        router, agent, channel, calls = wired
        router.store.update_settings({"approval_timeout_seconds": 1})

        async def never_clicked(channel_id, name, approval):
            return await ChannelRouter._await_decision(router, channel_id, name, approval)

        router._await_decision = never_clicked
        await router.dispatch(channel["id"], agent, "research this", "you")

        texts = " ".join(m["text"] for m in router.store.messages(channel["id"]))
        assert "timed out" in texts and "denied" in texts
        assert ("confirm", False) in calls

    @pytest.mark.asyncio
    async def test_the_human_wait_does_not_spend_the_agent_budget(self, store, monkeypatch):
        """A person thinking for a while must not end the turn."""
        router, agent, channel = _polling_board(
            store, monkeypatch,
            tasks=[_fake_task("input", "", approval=("fn-1", "web_request", {}))],
            after_confirmation=[_fake_task("completed", "done")],
        )
        store.update_settings({"agent_timeout_seconds": 1, "poll_interval_seconds": 1})

        async def slow_human(channel_id, name, approval):
            await asyncio.sleep(1.2)          # longer than the whole budget
            approval.resolve(True)
            return True

        router._await_decision = slow_human
        await router.dispatch(channel["id"], agent, "go", "you")
        assert "done" in [m["text"] for m in store.messages(channel["id"])]

    @pytest.mark.asyncio
    async def test_the_pending_record_is_cleared(self, wired):
        """A stale entry leaves an Approve button that answers nothing."""
        router, agent, channel, _ = wired

        async def click(channel_id, name, approval):
            approval.resolve(True)
            return await ChannelRouter._await_decision(router, channel_id, name, approval)

        router._await_decision = click
        await router.dispatch(channel["id"], agent, "research this", "you")
        assert router.pending == {}

    @pytest.mark.asyncio
    async def test_nothing_is_left_pending_if_the_turn_breaks(self, wired):
        router, agent, channel, _ = wired
        router._await_decision = lambda *a: _resolved(True)
        await router.dispatch(channel["id"], agent, "research this", "you")
        assert router.pending == {}

    @pytest.mark.asyncio
    async def test_an_unreadable_request_is_reported(self, store, monkeypatch):
        """Skipping it silently left the agent waiting and the channel empty."""
        router, agent, channel = _polling_board(
            store, monkeypatch,
            tasks=[_fake_task("input", "", approval=(None, "", {}))],
        )
        await router.dispatch(channel["id"], agent, "go", "you")
        assert "could not be read" in store.messages(channel["id"])[-1]["text"]

    def test_the_approval_carries_what_resuming_needs(self):
        from declarative_agent_sdk.agents_control.router import PendingApproval

        approval = PendingApproval(
            1, "a.b", "web_request", {}, task_id="t", context_id="c", function_id="f")
        assert (approval.task_id, approval.context_id, approval.function_id) == \
               ("t", "c", "f")


class TestPollingDispatch:
    """What submit-and-poll buys: progress while the agent is quiet."""

    @pytest.mark.asyncio
    async def test_a_long_quiet_task_still_completes(self, store, monkeypatch):
        """
        The case pattern A could not survive: minutes of silence. Polling does
        not care how long a model call takes.
        """
        router, agent, channel = _polling_board(
            store, monkeypatch,
            tasks=[_fake_task("submitted", ""),
                   _fake_task("working", ""),
                   _fake_task("working", ""),
                   _fake_task("completed", "the white paper")],
        )
        store.update_settings({"poll_interval_seconds": 1, "poll_max_interval_seconds": 1})
        await router.dispatch(channel["id"], agent, "write it", "you")
        assert "the white paper" in [m["text"] for m in store.messages(channel["id"])]

    @pytest.mark.asyncio
    async def test_status_text_is_streamed_to_the_board(self, store, monkeypatch):
        router, agent, channel = _polling_board(
            store, monkeypatch,
            tasks=[_fake_task("working", "Calling tools: web_request"),
                   _fake_task("completed", "done")],
        )
        seen = []
        router.bus.publish = lambda event: seen.append(event)
        store.update_settings({"poll_interval_seconds": 1})
        await router.dispatch(channel["id"], agent, "go", "you")
        assert any(e.get("type") == "status" and "web_request" in e.get("text", "")
                   for e in seen)

    @pytest.mark.asyncio
    async def test_an_answer_in_an_artifact_is_not_lost(self, store, monkeypatch):
        """A2A lets the answer live in an artifact rather than the status."""
        router, agent, channel = _polling_board(
            store, monkeypatch,
            tasks=[_fake_task("completed", "", artifact="from the artifact")],
        )
        await router.dispatch(channel["id"], agent, "go", "you")
        assert "from the artifact" in [m["text"] for m in store.messages(channel["id"])]

    @pytest.mark.asyncio
    async def test_a_silent_completion_says_so(self, store, monkeypatch):
        router, agent, channel = _polling_board(
            store, monkeypatch, tasks=[_fake_task("completed", "")])
        await router.dispatch(channel["id"], agent, "go", "you")
        assert "without saying anything" in store.messages(channel["id"])[-1]["text"]

    @pytest.mark.asyncio
    async def test_a_rejected_task_is_reported(self, store, monkeypatch):
        router, agent, channel = _polling_board(
            store, monkeypatch, tasks=[_fake_task("rejected", "no thanks")])
        await router.dispatch(channel["id"], agent, "go", "you")
        assert "failed: no thanks" in store.messages(channel["id"])[-1]["text"]

    @pytest.mark.asyncio
    async def test_a_cancelled_task_is_reported(self, store, monkeypatch):
        router, agent, channel = _polling_board(
            store, monkeypatch, tasks=[_fake_task("canceled", "")])
        await router.dispatch(channel["id"], agent, "go", "you")
        assert "cancelled" in store.messages(channel["id"])[-1]["text"]

    @pytest.mark.asyncio
    async def test_the_poll_interval_backs_off(self, store, monkeypatch):
        """Backoff keeps a long task from hammering the agent."""
        router, agent, channel = _polling_board(
            store, monkeypatch,
            tasks=[_fake_task("working", "")] * 4 + [_fake_task("completed", "ok")])
        slept = []
        real_sleep = asyncio.sleep

        async def record(delay):
            slept.append(delay)
            await real_sleep(0)

        monkeypatch.setattr(asyncio, "sleep", record)
        store.update_settings({"poll_interval_seconds": 2, "poll_max_interval_seconds": 4})
        await router.dispatch(channel["id"], agent, "go", "you")
        assert slept[0] == 2
        assert slept[1] > slept[0]
        assert max(slept) <= 4


# ---------------------------------------------------------------------------
# helpers for the submit-and-poll dispatch
# ---------------------------------------------------------------------------

_STATES = {
    "submitted": TaskState.TASK_STATE_SUBMITTED,
    "working": TaskState.TASK_STATE_WORKING,
    "completed": TaskState.TASK_STATE_COMPLETED,
    "failed": TaskState.TASK_STATE_FAILED,
    "rejected": TaskState.TASK_STATE_REJECTED,
    "canceled": TaskState.TASK_STATE_CANCELED,
    "input": TaskState.TASK_STATE_INPUT_REQUIRED,
    "auth": TaskState.TASK_STATE_AUTH_REQUIRED,
}


class _FakePart:
    def __init__(self, text=""):
        self.text = text


class _FakeTask:
    """Enough of a2a's Task for the router: status.state, status.message.parts."""

    def __init__(self, state, text="", approval=None, artifact=None, task_id="task-1"):
        self.id = task_id
        self.context_id = "ctx-1"
        self.approval = approval
        parts = [_FakePart(text)] if text else []
        self.status = type("Status", (), {
            "state": _STATES[state],
            "message": type("Msg", (), {"parts": parts})(),
        })()
        if artifact:
            self.artifacts = [type("Artifact", (), {"parts": [_FakePart(artifact)]})()]
        else:
            self.artifacts = []


def _fake_task(state, text="", approval=None, artifact=None):
    return _FakeTask(state, text, approval=approval, artifact=artifact)


def _polling_board(store, monkeypatch, tasks, after_confirmation=None, prefix=""):
    """
    A router whose AIAgentClient returns a scripted sequence of tasks.

    `tasks` is walked by submit() then get_task(); `after_confirmation` takes
    over once a tool approval has been answered.
    """
    import declarative_agent_sdk.agents_control.router as mod

    queue = list(tasks)
    resumed = list(after_confirmation or [])
    calls = []

    class FakeClient:
        def __init__(self, agent_url, timeout=0, poll_timeout=0):
            calls.append(("client", agent_url))

        async def submit(self, prompt, context_id):
            calls.append(("submit", context_id))
            return queue.pop(0)

        async def get_task(self, task_id, history_length=0):
            calls.append(("poll", task_id))
            return queue.pop(0) if queue else _fake_task("completed", "")

        async def submit_tool_confirmation(self, task_id, context_id, function_id,
                                           approved=False):
            calls.append(("confirm", approved))
            queue.clear()
            queue.extend(resumed or [_fake_task("completed", "")])
            return queue.pop(0)

    monkeypatch.setattr(mod, "AIAgentClient", FakeClient)
    monkeypatch.setattr(
        mod, "_part_text",
        lambda parts: " ".join(p.text for p in parts if getattr(p, "text", "")))

    # The approval payload rides on the scripted task rather than on protobuf
    # parts, so the parser is replaced with one that reads it.
    current_approval = {"value": (None, "", {})}
    monkeypatch.setattr(mod, "_approval_request",
                        lambda parts: current_approval["value"])

    router = mod.ChannelRouter(store, mod.EventBus())
    router._calls = calls
    router._approval_slot = current_approval

    machine = store.add_machine("here", "localhost", is_local=True)
    agent = store.upsert_agent(machine["id"], {
        "name": "researcher", "url": "http://127.0.0.1:8000/", "ready": 1,
        "prefix": prefix,
    })
    channel = store.add_channel("dev")
    store.join(channel["id"], agent["id"])

    # whichever scripted task carries an approval decides what the parser sees
    for task in tasks:
        if getattr(task, "approval", None):
            current_approval["value"] = task.approval
            break

    return router, store.agent(agent["id"]), channel


async def _resolved(value):
    """A stand-in for _await_decision that answers immediately."""
    return value


# ---------------------------------------------------------------------------
# the board tools arrive on their own
# ---------------------------------------------------------------------------

class TestBoardToolsAreCore:
    """
    An agent that can be addressed as @name should be able to answer in kind
    without anyone listing a tool for it. Asking people to opt in was a trap:
    the symptom was an agent that could not bring in a peer and said nothing
    about why.
    """

    @staticmethod
    def _registry():
        from declarative_agent_sdk.tools.tool_registry import ToolRegistry
        return ToolRegistry

    def test_they_are_core_on_a_board(self, monkeypatch):
        monkeypatch.setenv("AGENTS_CONTROL_URL", "http://127.0.0.1:8900")
        names = self._registry().core_tool_names()
        assert "send_to_agent" in names and "list_agents" in names

    def test_list_agents_is_absent_off_a_board(self, monkeypatch):
        """Off a board list_agents could only report that there is no board."""
        monkeypatch.delenv("AGENTS_CONTROL_URL", raising=False)
        names = self._registry().core_tool_names()
        assert "list_agents" not in names
        assert names == ["read_file", "write_file", "web_request", "exec_command", "send_to_agent", "share_file", "read_shared"]

    def test_core_tools_returns_the_callables(self, monkeypatch):
        monkeypatch.setenv("AGENTS_CONTROL_URL", "http://127.0.0.1:8900")
        got = [fn.__name__ for fn in self._registry().core_tools()]
        assert got == ["read_file", "write_file", "web_request", "exec_command", "send_to_agent", "share_file", "read_shared", "list_agents"]

    def test_the_lean_runtime_registers_them(self, tmp_path, monkeypatch):
        from declarative_agent_sdk.agents.lean.runtime.skills import SkillRegistry
        from declarative_agent_sdk.agents.lean.runtime.tools import LeanToolRegistry

        monkeypatch.setenv("AGENTS_CONTROL_URL", "http://127.0.0.1:8900")
        registry = LeanToolRegistry(
            SkillRegistry(root=str(tmp_path / "skills")), workspace=str(tmp_path / "ws"))
        assert "send_to_agent" in registry.names()
        assert "list_agents" in registry.names()

    def test_the_lean_runtime_leaves_them_out_off_a_board(self, tmp_path, monkeypatch):
        from declarative_agent_sdk.agents.lean.runtime.skills import SkillRegistry
        from declarative_agent_sdk.agents.lean.runtime.tools import LeanToolRegistry

        monkeypatch.delenv("AGENTS_CONTROL_URL", raising=False)
        registry = LeanToolRegistry(
            SkillRegistry(root=str(tmp_path / "skills")), workspace=str(tmp_path / "ws"))
        assert "send_to_agent" in registry.names()
        assert "list_agents" not in registry.names()

    def test_listing_them_anyway_does_not_duplicate(self, tmp_path, monkeypatch):
        """An existing agent.yaml still naming them must keep working."""
        from declarative_agent_sdk.agents.lean.runtime.skills import SkillRegistry
        from declarative_agent_sdk.agents.lean.runtime.tools import LeanToolRegistry
        from declarative_agent_sdk.tools.tool_registry import ToolRegistry

        monkeypatch.setenv("AGENTS_CONTROL_URL", "http://127.0.0.1:8900")
        ToolRegistry.register_built_in_tools()
        registry = LeanToolRegistry(
            SkillRegistry(root=str(tmp_path / "skills")), workspace=str(tmp_path / "ws"))
        registry.add_callable(ToolRegistry.get("send_to_agent"))

        schemas = [s["function"]["name"] for s in registry.schemas()]
        assert schemas.count("send_to_agent") == 1

    def test_neither_needs_approval(self):
        """
        send_to_agent posts what the agent could write as @name anyway, so a
        click in front of it buys nothing; list_agents only reads a roster.
        """
        from declarative_agent_sdk.agents.lean.runtime.tools import AUTO_APPROVE_TOOLS

        assert {"send_to_agent", "list_agents"} <= AUTO_APPROVE_TOOLS

    def test_the_prompt_mentions_them(self, store):
        router = ChannelRouter(store, EventBus())
        machine = store.add_machine("here", "localhost", is_local=True)
        agent = store.upsert_agent(machine["id"], {"name": "a", "url": "http://x"})
        channel = store.add_channel("dev")
        store.join(channel["id"], agent["id"])
        prompt = router._prompt_for(channel["id"], "a.here", "hi", "you")
        assert "send_to_agent" in prompt and "list_agents" in prompt


# ---------------------------------------------------------------------------
# work produced by a turn that then failed
# ---------------------------------------------------------------------------

class TestSubstantiveText:
    """
    The lean loop sends substantive text as a WORKING update whenever it
    expects to continue — an answer, an outline, a hand-off. Shown as a status
    line it was truncated to 400 characters, never stored, and any @mention in
    it never relayed. Anything long enough to be the agent talking is a
    message.
    """

    OUTLINE = "Working title: Small LLMs on Small GPUs. " * 12

    @pytest.mark.asyncio
    async def test_a_long_update_becomes_a_board_message(self, store, monkeypatch):
        router, agent, channel = _polling_board(
            store, monkeypatch,
            tasks=[_fake_task("working", self.OUTLINE),
                   _fake_task("completed", "")])
        store.update_settings({"poll_interval_seconds": 1})
        await router.dispatch(channel["id"], agent, "write it", "you")

        messages = store.messages(channel["id"])
        assert any(m["sender_kind"] == "agent" and "Small LLMs" in m["text"]
                   for m in messages)

    @pytest.mark.asyncio
    async def test_it_is_not_truncated(self, store, monkeypatch):
        router, agent, channel = _polling_board(
            store, monkeypatch,
            tasks=[_fake_task("working", self.OUTLINE), _fake_task("completed", "")])
        store.update_settings({"poll_interval_seconds": 1})
        await router.dispatch(channel["id"], agent, "go", "you")
        posted = [m for m in store.messages(channel["id"]) if m["sender_kind"] == "agent"]
        assert len(posted[0]["text"]) == len(self.OUTLINE.strip())

    @pytest.mark.asyncio
    async def test_a_mention_inside_it_relays(self, store, monkeypatch):
        """The hand-off that silently never happened."""
        hand_off = ("Outline is ready. @reviewer.here here is the 1-page outline "
                    "for the white paper. " + "Detail. " * 40)
        router, agent, channel = _polling_board(
            store, monkeypatch,
            tasks=[_fake_task("working", hand_off), _fake_task("completed", "")])
        machine = store.machines()[0]
        reviewer = store.upsert_agent(machine["id"], {
            "name": "reviewer", "url": "http://reviewer", "ready": 1})
        store.join(channel["id"], reviewer["id"])
        store.update_settings({"poll_interval_seconds": 1})

        relayed = []
        original = router.dispatch

        async def watched(channel_id, member, text, sender, depth=0):
            if member["name"] == "reviewer":
                relayed.append(sender)
                return
            await original(channel_id, member, text, sender, depth)

        router.dispatch = watched
        await watched(channel["id"], agent, "write it", "you")
        await asyncio.sleep(0)
        assert relayed == ["researcher.here"]

    @pytest.mark.asyncio
    async def test_a_short_update_stays_a_status_line(self, store, monkeypatch):
        """'Calling tools: web_request' is a progress note, not the agent talking."""
        router, agent, channel = _polling_board(
            store, monkeypatch,
            tasks=[_fake_task("working", "Calling tools: web_request"),
                   _fake_task("completed", "done")])
        events = []
        router.bus.publish = lambda event: events.append(event)
        store.update_settings({"poll_interval_seconds": 1})
        await router.dispatch(channel["id"], agent, "go", "you")

        assert any(e.get("type") == "status" for e in events)
        texts = [m["text"] for m in store.messages(channel["id"])]
        assert "Calling tools: web_request" not in texts

    @pytest.mark.asyncio
    async def test_the_final_answer_does_not_repeat_itself(self, store, monkeypatch):
        """The loop often repeats its last text as the final answer."""
        router, agent, channel = _polling_board(
            store, monkeypatch,
            tasks=[_fake_task("working", self.OUTLINE),
                   _fake_task("completed", self.OUTLINE)])
        store.update_settings({"poll_interval_seconds": 1})
        await router.dispatch(channel["id"], agent, "go", "you")
        posted = [m for m in store.messages(channel["id"])
                  if m["sender_kind"] == "agent" and "Small LLMs" in m["text"]]
        assert len(posted) == 1

    @pytest.mark.asyncio
    async def test_work_posted_before_a_failure_stays(self, store, monkeypatch):
        router, agent, channel = _polling_board(
            store, monkeypatch,
            tasks=[_fake_task("working", self.OUTLINE),
                   _fake_task("failed", "boom")])
        store.update_settings({"poll_interval_seconds": 1})
        await router.dispatch(channel["id"], agent, "go", "you")
        texts = [m["text"] for m in store.messages(channel["id"])]
        assert any("Small LLMs" in t for t in texts)
        assert any("failed: boom" in t for t in texts)

    @pytest.mark.asyncio
    async def test_a_silent_turn_still_says_so(self, store, monkeypatch):
        router, agent, channel = _polling_board(
            store, monkeypatch, tasks=[_fake_task("completed", "")])
        await router.dispatch(channel["id"], agent, "go", "you")
        assert "without saying anything" in store.messages(channel["id"])[-1]["text"]


# ---------------------------------------------------------------------------
# helpers for the submit-and-poll dispatch
# ---------------------------------------------------------------------------

_STATES = {
    "submitted": TaskState.TASK_STATE_SUBMITTED,
    "working": TaskState.TASK_STATE_WORKING,
    "completed": TaskState.TASK_STATE_COMPLETED,
    "failed": TaskState.TASK_STATE_FAILED,
    "rejected": TaskState.TASK_STATE_REJECTED,
    "canceled": TaskState.TASK_STATE_CANCELED,
    "input": TaskState.TASK_STATE_INPUT_REQUIRED,
    "auth": TaskState.TASK_STATE_AUTH_REQUIRED,
}


class _FakePart:
    def __init__(self, text=""):
        self.text = text


class _FakeTask:
    """Enough of a2a's Task for the router: status.state, status.message.parts."""

    def __init__(self, state, text="", approval=None, artifact=None, task_id="task-1"):
        self.id = task_id
        self.context_id = "ctx-1"
        self.approval = approval
        parts = [_FakePart(text)] if text else []
        self.status = type("Status", (), {
            "state": _STATES[state],
            "message": type("Msg", (), {"parts": parts})(),
        })()
        if artifact:
            self.artifacts = [type("Artifact", (), {"parts": [_FakePart(artifact)]})()]
        else:
            self.artifacts = []


def _fake_task(state, text="", approval=None, artifact=None):
    return _FakeTask(state, text, approval=approval, artifact=artifact)


def _polling_board(store, monkeypatch, tasks, after_confirmation=None, prefix=""):
    """
    A router whose AIAgentClient returns a scripted sequence of tasks.

    `tasks` is walked by submit() then get_task(); `after_confirmation` takes
    over once a tool approval has been answered.
    """
    import declarative_agent_sdk.agents_control.router as mod

    queue = list(tasks)
    resumed = list(after_confirmation or [])
    calls = []

    class FakeClient:
        def __init__(self, agent_url, timeout=0, poll_timeout=0):
            calls.append(("client", agent_url))

        async def submit(self, prompt, context_id):
            calls.append(("submit", context_id))
            return queue.pop(0)

        async def get_task(self, task_id, history_length=0):
            calls.append(("poll", task_id))
            return queue.pop(0) if queue else _fake_task("completed", "")

        async def submit_tool_confirmation(self, task_id, context_id, function_id,
                                           approved=False):
            calls.append(("confirm", approved))
            queue.clear()
            queue.extend(resumed or [_fake_task("completed", "")])
            return queue.pop(0)

    monkeypatch.setattr(mod, "AIAgentClient", FakeClient)
    monkeypatch.setattr(
        mod, "_part_text",
        lambda parts: " ".join(p.text for p in parts if getattr(p, "text", "")))

    # The approval payload rides on the scripted task rather than on protobuf
    # parts, so the parser is replaced with one that reads it.
    current_approval = {"value": (None, "", {})}
    monkeypatch.setattr(mod, "_approval_request",
                        lambda parts: current_approval["value"])

    router = mod.ChannelRouter(store, mod.EventBus())
    router._calls = calls
    router._approval_slot = current_approval

    machine = store.add_machine("here", "localhost", is_local=True)
    agent = store.upsert_agent(machine["id"], {
        "name": "researcher", "url": "http://127.0.0.1:8000/", "ready": 1,
        "prefix": prefix,
    })
    channel = store.add_channel("dev")
    store.join(channel["id"], agent["id"])

    # whichever scripted task carries an approval decides what the parser sees
    for task in tasks:
        if getattr(task, "approval", None):
            current_approval["value"] = task.approval
            break

    return router, store.agent(agent["id"]), channel


async def _resolved(value):
    """A stand-in for _await_decision that answers immediately."""
    return value


# ---------------------------------------------------------------------------
# the board tools arrive on their own
# ---------------------------------------------------------------------------

class TestBoardToolsAreCore:
    """
    An agent that can be addressed as @name should be able to answer in kind
    without anyone listing a tool for it. Asking people to opt in was a trap:
    the symptom was an agent that could not bring in a peer and said nothing
    about why.
    """

    @staticmethod
    def _registry():
        from declarative_agent_sdk.tools.tool_registry import ToolRegistry
        return ToolRegistry

    def test_they_are_core_on_a_board(self, monkeypatch):
        monkeypatch.setenv("AGENTS_CONTROL_URL", "http://127.0.0.1:8900")
        names = self._registry().core_tool_names()
        assert "send_to_agent" in names and "list_agents" in names

    def test_list_agents_is_absent_off_a_board(self, monkeypatch):
        """Off a board list_agents could only report that there is no board."""
        monkeypatch.delenv("AGENTS_CONTROL_URL", raising=False)
        names = self._registry().core_tool_names()
        assert "list_agents" not in names
        assert names == ["read_file", "write_file", "web_request", "exec_command", "send_to_agent", "share_file", "read_shared"]

    def test_core_tools_returns_the_callables(self, monkeypatch):
        monkeypatch.setenv("AGENTS_CONTROL_URL", "http://127.0.0.1:8900")
        got = [fn.__name__ for fn in self._registry().core_tools()]
        assert got == ["read_file", "write_file", "web_request", "exec_command", "send_to_agent", "share_file", "read_shared", "list_agents"]

    def test_the_lean_runtime_registers_them(self, tmp_path, monkeypatch):
        from declarative_agent_sdk.agents.lean.runtime.skills import SkillRegistry
        from declarative_agent_sdk.agents.lean.runtime.tools import LeanToolRegistry

        monkeypatch.setenv("AGENTS_CONTROL_URL", "http://127.0.0.1:8900")
        registry = LeanToolRegistry(
            SkillRegistry(root=str(tmp_path / "skills")), workspace=str(tmp_path / "ws"))
        assert "send_to_agent" in registry.names()
        assert "list_agents" in registry.names()

    def test_the_lean_runtime_leaves_them_out_off_a_board(self, tmp_path, monkeypatch):
        from declarative_agent_sdk.agents.lean.runtime.skills import SkillRegistry
        from declarative_agent_sdk.agents.lean.runtime.tools import LeanToolRegistry

        monkeypatch.delenv("AGENTS_CONTROL_URL", raising=False)
        registry = LeanToolRegistry(
            SkillRegistry(root=str(tmp_path / "skills")), workspace=str(tmp_path / "ws"))
        assert "send_to_agent" in registry.names()
        assert "list_agents" not in registry.names()

    def test_listing_them_anyway_does_not_duplicate(self, tmp_path, monkeypatch):
        """An existing agent.yaml still naming them must keep working."""
        from declarative_agent_sdk.agents.lean.runtime.skills import SkillRegistry
        from declarative_agent_sdk.agents.lean.runtime.tools import LeanToolRegistry
        from declarative_agent_sdk.tools.tool_registry import ToolRegistry

        monkeypatch.setenv("AGENTS_CONTROL_URL", "http://127.0.0.1:8900")
        ToolRegistry.register_built_in_tools()
        registry = LeanToolRegistry(
            SkillRegistry(root=str(tmp_path / "skills")), workspace=str(tmp_path / "ws"))
        registry.add_callable(ToolRegistry.get("send_to_agent"))

        schemas = [s["function"]["name"] for s in registry.schemas()]
        assert schemas.count("send_to_agent") == 1

    def test_neither_needs_approval(self):
        """
        send_to_agent posts what the agent could write as @name anyway, so a
        click in front of it buys nothing; list_agents only reads a roster.
        """
        from declarative_agent_sdk.agents.lean.runtime.tools import AUTO_APPROVE_TOOLS

        assert {"send_to_agent", "list_agents"} <= AUTO_APPROVE_TOOLS

    def test_the_prompt_mentions_them(self, store):
        router = ChannelRouter(store, EventBus())
        machine = store.add_machine("here", "localhost", is_local=True)
        agent = store.upsert_agent(machine["id"], {"name": "a", "url": "http://x"})
        channel = store.add_channel("dev")
        store.join(channel["id"], agent["id"])
        prompt = router._prompt_for(channel["id"], "a.here", "hi", "you")
        assert "send_to_agent" in prompt and "list_agents" in prompt


# ---------------------------------------------------------------------------
# status or the agent talking
# ---------------------------------------------------------------------------

class TestAgentTalkingDecision:
    """
    Two independent signals promote a WORKING update to a board message.
    Length alone missed a short hand-off — "@reviewer.omarchy over to you" is
    thirty characters and is the most important thing an agent can say. A
    mention alone would miss a finished outline, which mentions nobody.
    """

    @pytest.fixture
    def judge(self, store):
        router = ChannelRouter(store, EventBus())
        machine = store.add_machine("omarchy", "10.0.0.132")
        reviewer = store.upsert_agent(machine["id"], {
            "name": "reviewer", "url": "http://x", "ready": 1})
        channel = store.add_channel("dev")
        store.join(channel["id"], reviewer["id"])
        members = store.members(channel["id"])
        return lambda text: router._is_agent_talking(text, members)

    def test_tool_chatter_stays_status(self, judge):
        assert judge("Calling tools: web_request") is False
        assert judge("Running tool: write_file") is False

    def test_a_short_hand_off_is_a_message(self, judge):
        """The gap length alone left open."""
        assert judge("@reviewer.omarchy over to you") is True

    def test_a_bare_member_name_counts(self, judge):
        assert judge("@reviewer please take a look") is True

    def test_long_content_without_a_mention_is_a_message(self, judge):
        assert judge("Working title: Small LLMs on Small GPUs. " * 8) is True

    def test_prose_with_a_stray_at_is_not_an_address(self, judge):
        assert judge("email me @ work about it") is False

    def test_a_mention_of_a_non_member_does_not_promote(self, judge):
        """Otherwise any @word in tool chatter would become a message."""
        assert judge("@nobody are you there?") is False

    def test_empty_text_is_not_promoted(self, judge):
        assert judge("") is False and judge("   ") is False

    @pytest.mark.asyncio
    async def test_a_short_hand_off_actually_relays(self, store, monkeypatch):
        """End to end: the hand-off that used to vanish into the ticker."""
        router, agent, channel = _polling_board(
            store, monkeypatch,
            tasks=[_fake_task("working", "@reviewer.here over to you"),
                   _fake_task("completed", "")])
        machine = store.machines()[0]
        reviewer = store.upsert_agent(machine["id"], {
            "name": "reviewer", "url": "http://reviewer", "ready": 1})
        store.join(channel["id"], reviewer["id"])
        store.update_settings({"poll_interval_seconds": 1})

        relayed = []
        original = router.dispatch

        async def watched(channel_id, member, text, sender, depth=0):
            if member["name"] == "reviewer":
                relayed.append((sender, text))
                return
            await original(channel_id, member, text, sender, depth)

        router.dispatch = watched
        await watched(channel["id"], agent, "hand it over", "you")
        await asyncio.sleep(0)

        assert relayed and relayed[0][0] == "researcher.here"
        assert "@reviewer.here over to you" in relayed[0][1]
        # and it is on the board, not just in a ticker
        assert any("over to you" in m["text"] for m in store.messages(channel["id"]))


# ---------------------------------------------------------------------------
# one dispatch per peer per turn
# ---------------------------------------------------------------------------

class TestRelayOncePerTurn:
    """
    A turn commonly names the same peer twice — once in its working text, once
    in its final answer. Each extra dispatch is a whole model call. Observed:
    two dispatches 17 seconds apart, both spending the peer's full 300s
    timeout, for one hand-off.
    """

    @pytest.fixture
    def board(self, store, monkeypatch):
        hand_off = "@reviewer.here Full draft is ready — here it is. " + "Detail. " * 40
        restated = "Yes — the paper is now with @reviewer.here for review. " + "Status. " * 40
        router, agent, channel = _polling_board(
            store, monkeypatch,
            tasks=[_fake_task("working", hand_off),
                   _fake_task("completed", restated)])
        machine = store.machines()[0]
        reviewer = store.upsert_agent(machine["id"], {
            "name": "reviewer", "url": "http://reviewer", "ready": 1})
        store.join(channel["id"], reviewer["id"])
        store.update_settings({"poll_interval_seconds": 1})

        dispatched = []
        original = router.dispatch

        async def watched(channel_id, member, text, sender, depth=0):
            if member["name"] == "reviewer":
                dispatched.append(text[:40])
                return
            await original(channel_id, member, text, sender, depth)

        router.dispatch = watched
        return router, agent, channel, dispatched, watched

    @pytest.mark.asyncio
    async def test_the_peer_is_dispatched_once(self, board):
        router, agent, channel, dispatched, watched = board
        await watched(channel["id"], agent, "write it", "you")
        await asyncio.sleep(0)
        assert len(dispatched) == 1, f"dispatched {len(dispatched)} times"

    @pytest.mark.asyncio
    async def test_both_messages_still_reach_the_board(self, board):
        """Suppressing a dispatch must not hide what the agent said."""
        router, agent, channel, _, watched = board
        await watched(channel["id"], agent, "write it", "you")
        texts = [m["text"] for m in router.store.messages(channel["id"])]
        assert any("Full draft is ready" in t for t in texts)
        assert any("now with @reviewer.here" in t for t in texts)

    @pytest.mark.asyncio
    async def test_a_later_turn_may_hand_off_again(self, store, monkeypatch):
        """Outline now, draft later: a new turn gets a fresh set."""
        # one scripted task per turn: submit() consumes one each time
        router, agent, channel = _polling_board(
            store, monkeypatch,
            tasks=[_fake_task("completed", "@reviewer.here outline ready. " + "x " * 150),
                   _fake_task("completed", "@reviewer.here draft ready. " + "x " * 150)])
        machine = store.machines()[0]
        reviewer = store.upsert_agent(machine["id"], {
            "name": "reviewer", "url": "http://reviewer", "ready": 1})
        store.join(channel["id"], reviewer["id"])

        dispatched = []
        original = router.dispatch

        async def watched(channel_id, member, text, sender, depth=0):
            if member["name"] == "reviewer":
                dispatched.append(1)
                return
            await original(channel_id, member, text, sender, depth)

        router.dispatch = watched
        await watched(channel["id"], agent, "first", "you")
        await watched(channel["id"], agent, "second", "you")
        await asyncio.sleep(0)
        assert len(dispatched) == 2, "a new turn must be able to hand off again"

    @pytest.mark.asyncio
    async def test_different_peers_are_all_reached(self, store, monkeypatch):
        text = "@reviewer.here and @editor.here please both look. " + "y " * 150
        router, agent, channel = _polling_board(
            store, monkeypatch, tasks=[_fake_task("completed", text)])
        machine = store.machines()[0]
        for name in ("reviewer", "editor"):
            peer = store.upsert_agent(machine["id"], {
                "name": name, "url": f"http://{name}", "ready": 1})
            store.join(channel["id"], peer["id"])

        reached = []
        original = router.dispatch

        async def watched(channel_id, member, text, sender, depth=0):
            if member["name"] in ("reviewer", "editor"):
                reached.append(member["name"])
                return
            await original(channel_id, member, text, sender, depth)

        router.dispatch = watched
        await watched(channel["id"], agent, "go", "you")
        await asyncio.sleep(0)
        assert sorted(reached) == ["editor", "reviewer"]


# ---------------------------------------------------------------------------
# which settings reach which runtime
# ---------------------------------------------------------------------------

class TestPerRuntimeProperties:
    """
    The loop budgets are lean's own and truncation is adk/deepagent's. Offering
    all of them to every agent meant the YAML accepted a knob the runtime never
    read — the agent started, nothing changed, and nothing said why.
    """

    @staticmethod
    def _keys(framework):
        from declarative_agent_sdk.agents_control.config import properties_for
        return {prop["key"] for prop in properties_for(framework)}

    def test_lean_only_knobs_are_hidden_from_the_others(self):
        lean, adk = self._keys("lean"), self._keys("adk")
        for key in ("history_limit", "max_tool_iterations", "request_timeout",
                    "tool_payload_keep", "tool_payload_max_chars"):
            assert key in lean
            assert key not in adk, f"{key} does not reach adk"

    def test_truncation_belongs_to_adk_and_deepagent(self):
        lean, adk, deep = self._keys("lean"), self._keys("adk"), self._keys("deepagent")
        for key in ("enable_truncation", "context_window", "truncate_strategy",
                    "safety_margin"):
            assert key in adk and key in deep
            assert key not in lean

    def test_the_common_settings_reach_everything(self):
        shared = self._keys("lean") & self._keys("adk") & self._keys("deepagent")
        for key in ("description", "agent_framework", "provider", "model",
                    "endpoint.url", "temperature", "max_tokens", "tools",
                    "tools_approval_required", "instruction_file",
                    "skills_directory", "workspace_directory"):
            assert key in shared, f"{key} should apply to every runtime"

    def test_an_unknown_framework_falls_back_to_lean(self):
        assert self._keys("nonsense") == self._keys("lean")

    def test_the_api_reports_the_framework(self, client, plane, tmp_path):
        prefix = tmp_path / "a"
        prefix.mkdir()
        (prefix / "agent.yaml").write_text("name: a\nagent_framework: deepagent\n")
        (prefix / "instructions.md").write_text("hi\n")
        machine = plane.store.add_machine("here", "localhost", is_local=True)
        agent = plane.store.upsert_agent(machine["id"], {
            "name": "a", "prefix": str(prefix), "url": "http://x"})

        data = client.get(f"/api/agents/{agent['id']}/config", headers=AUTH).json()
        assert data["framework"] == "deepagent"
        # the whole schema still ships, so the form can re-decide on change
        assert any(p["key"] == "history_limit" for p in data["schema"])

    def test_every_runtime_only_knob_declares_its_runtimes(self):
        """A new lean-only knob added without the marker would silently mislead."""
        from declarative_agent_sdk.agents_control.config import EDITABLE_PROPERTIES

        lean_only = {"history_limit", "max_tool_iterations", "request_timeout",
                     "tool_payload_keep", "tool_payload_max_chars"}
        for prop in EDITABLE_PROPERTIES:
            if prop["key"] in lean_only:
                assert prop.get("runtimes") == ["lean"], prop["key"]

    def test_the_board_hides_the_rows_it_should(self):
        app_js = (
            Path(__file__).resolve().parents[1]
            / "agents_control" / "static" / "app.js"
        ).read_text()
        assert "appliesTo" in app_js
        assert "prop.runtimes" in app_js
        # and re-decides when the dropdown changes, before anything is saved
        assert "agent_framework" in app_js and "showFor" in app_js


# ---------------------------------------------------------------------------
# auto-approval, per channel
# ---------------------------------------------------------------------------

class TestAutoApprove:
    """
    Clicking Approve for every web_request in a long research run is the whole
    of the friction. The setting lives on the channel, not the agent:
    `tools_approval_required: false` is all-or-nothing, needs a restart, and
    follows the agent into every channel it joins.
    """

    @pytest.fixture
    def board(self, store):
        router = ChannelRouter(store, EventBus())
        channel = store.add_channel("dev")
        return router, store, channel["id"]

    @staticmethod
    def _approval(channel_id, tool):
        from declarative_agent_sdk.agents_control.router import PendingApproval
        return PendingApproval(channel_id, "researcher.here", tool, {},
                               task_id="t", context_id="c", function_id="f")

    def test_asking_is_the_default(self, board):
        router, store, channel_id = board
        assert store.auto_approve(channel_id) == ""
        assert router._auto_decision(channel_id, self._approval(channel_id, "web_request")) is None

    def test_safe_approves_a_fetch(self, board):
        router, store, channel_id = board
        store.set_auto_approve(channel_id, "safe")
        for tool in ("web_request", "write_file", "tavily_search"):
            assert router._auto_decision(channel_id, self._approval(channel_id, tool)) is True

    def test_safe_still_refuses_shell(self, board):
        """Running commands is a different decision from fetching a page."""
        router, store, channel_id = board
        store.set_auto_approve(channel_id, "safe")
        for tool in ("exec_command", "exec_async", "run_script"):
            assert router._auto_decision(channel_id, self._approval(channel_id, tool)) is False

    def test_all_approves_shell_too(self, board):
        router, store, channel_id = board
        store.set_auto_approve(channel_id, "all")
        assert router._auto_decision(channel_id, self._approval(channel_id, "exec_command")) is True

    @pytest.mark.asyncio
    async def test_an_auto_decision_skips_the_wait(self, board):
        router, store, channel_id = board
        store.update_settings({"approval_timeout_seconds": 600})
        store.set_auto_approve(channel_id, "safe")
        approval = self._approval(channel_id, "web_request")
        router.pending[approval.id] = approval

        # nobody resolves it; without auto-approval this would block 600s
        assert await asyncio.wait_for(
            router._await_decision(channel_id, "researcher.here", approval),
            timeout=2) is True
        assert router.pending == {}

    @pytest.mark.asyncio
    async def test_it_is_recorded_in_the_transcript(self, board):
        """An approval nobody saw still belongs in the record."""
        router, store, channel_id = board
        store.set_auto_approve(channel_id, "safe")
        approval = self._approval(channel_id, "web_request")
        router.pending[approval.id] = approval
        await router._await_decision(channel_id, "researcher.here", approval)
        assert "auto-approved" in store.messages(channel_id)[-1]["text"]

    @pytest.mark.asyncio
    async def test_a_safe_refusal_is_visible(self, board):
        """Otherwise it looks like the agent stalled."""
        router, store, channel_id = board
        store.set_auto_approve(channel_id, "safe")
        approval = self._approval(channel_id, "exec_command")
        router.pending[approval.id] = approval
        assert await router._await_decision(channel_id, "x", approval) is False
        assert "shell commands still need a person" in store.messages(channel_id)[-1]["text"]

    def test_the_level_is_validated(self, board):
        router, store, channel_id = board
        with pytest.raises(ValueError, match="unknown auto-approve level"):
            store.set_auto_approve(channel_id, "yolo")

    def test_it_survives_a_restart(self, tmp_path):
        path = str(tmp_path / "s.db")
        store = Store(path)
        channel = store.add_channel("dev")
        store.set_auto_approve(channel["id"], "all")
        store.close()
        assert Store(path).auto_approve(channel["id"]) == "all"

    def test_the_api_sets_and_reports_it(self, client, plane):
        channel = client.post("/api/channels", json={"name": "dev"},
                              headers=AUTH).json()["channel"]
        response = client.post(f"/api/channels/{channel['id']}/auto-approve",
                               json={"level": "safe"}, headers=AUTH)
        assert response.status_code == 200
        read = client.get(f"/api/channels/{channel['id']}/messages", headers=AUTH).json()
        assert read["auto_approve"] == "safe"
        # and the change is announced, because who approves is part of the record
        assert any("except shell" in m["text"] for m in read["messages"])

    def test_a_bad_level_is_rejected(self, client):
        channel = client.post("/api/channels", json={"name": "dev"},
                              headers=AUTH).json()["channel"]
        assert client.post(f"/api/channels/{channel['id']}/auto-approve",
                           json={"level": "yolo"}, headers=AUTH).status_code == 400

    def test_the_board_shows_it_next_to_stop(self):
        html = (Path(__file__).resolve().parents[1]
                / "agents_control" / "static" / "index.html").read_text()
        app_js = (Path(__file__).resolve().parents[1]
                  / "agents_control" / "static" / "app.js").read_text()
        assert 'id="auto-approve"' in html
        assert "auto-approve`" in app_js
        assert "armed" in app_js          # visibly on, not a quiet default


class TestTranscriptSlots:
    """
    What the board replays to an agent that has been restarted.

    Measured on a real board: one dispatch spent seven of its eight slots on
    tool-approval bookkeeping and replayed a single conversation message —
    which was the human's own question, already appended below as
    "you says:". The agent was handed its own question twice and no history.
    """

    @pytest.fixture
    def channel(self, store):
        return store.add_channel("dev")

    @staticmethod
    def _tool_call(store, channel_id, tool="exec_command"):
        """The two lines the board writes per tool call."""
        store.add_message(channel_id, "system", "system",
                          f"@a.here wants to run {tool}(command='ls')")
        store.add_message(channel_id, "system", "system",
                          f"{tool} auto-approved for @a.here.")

    def test_bookkeeping_no_longer_spends_the_budget(self, store, channel):
        cid = int(channel["id"])
        for i in range(4):
            store.add_message(cid, "human", "you", f"question {i}")
            self._tool_call(store, cid)
            store.add_message(cid, "agent", "a.here", f"answer {i}")

        text = store.recent_transcript(cid, 8)
        # all eight conversation messages survive
        for i in range(4):
            assert f"question {i}" in text
            assert f"answer {i}" in text

    def test_an_approval_run_collapses_to_one_line(self, store, channel):
        cid = int(channel["id"])
        store.add_message(cid, "human", "you", "go")
        for _ in range(17):
            self._tool_call(store, cid)
        store.add_message(cid, "agent", "a.here", "done")

        text = store.recent_transcript(cid, 8)
        assert "[tool calls: exec_command x17 — approved]" in text
        assert "wants to run" not in text
        assert "auto-approved" not in text

    def test_each_tool_is_counted_separately(self, store, channel):
        cid = int(channel["id"])
        store.add_message(cid, "human", "you", "go")
        self._tool_call(store, cid, "exec_command")
        self._tool_call(store, cid, "web_request")
        self._tool_call(store, cid, "web_request")
        assert "exec_command, web_request x2" in store.recent_transcript(cid, 8)

    def test_a_denial_is_not_reported_as_approved(self, store, channel):
        """The outcome is the part an agent has to get right."""
        cid = int(channel["id"])
        store.add_message(cid, "human", "you", "go")
        store.add_message(cid, "system", "system",
                          "@a.here wants to run exec_command(command='rm x')")
        store.add_message(cid, "system", "system",
                          "exec_command auto-denied (shell commands still need a person)")
        assert "some denied" in store.recent_transcript(cid, 8)

    def test_a_timeout_is_reported_as_denied(self, store, channel):
        cid = int(channel["id"])
        store.add_message(cid, "human", "you", "go")
        store.add_message(cid, "system", "system",
                          "@a.here wants to run exec_command(command='ls')")
        store.add_message(cid, "system", "system",
                          "Approval for exec_command timed out after 300s — denied.")
        assert "timed out" in store.recent_transcript(cid, 8)

    def test_other_system_lines_are_kept_verbatim(self, store, channel):
        """
        A dispatch failure is not bookkeeping — it is the explanation for why
        an agent went quiet, and summarising it away would lose the only
        record the agent sees.
        """
        cid = int(channel["id"])
        store.add_message(cid, "human", "you", "go")
        store.add_message(cid, "system", "system",
                          "@a.here could not be reached: A2AClientError")
        text = store.recent_transcript(cid, 8)
        assert "could not be reached: A2AClientError" in text

    def test_the_dispatched_message_is_not_replayed(self, store, channel):
        """It is appended by _prompt_for as '<sender> says:' already."""
        cid = int(channel["id"])
        store.add_message(cid, "agent", "a.here", "earlier answer")
        asked = store.add_message(cid, "human", "you", "the new question")
        text = store.recent_transcript(cid, 8, exclude_id=int(asked["id"]))
        assert "the new question" not in text
        assert "earlier answer" in text

    def test_without_an_exclusion_nothing_is_dropped(self, store, channel):
        cid = int(channel["id"])
        store.add_message(cid, "human", "you", "the new question")
        assert "the new question" in store.recent_transcript(cid, 8)

    def test_a_zero_limit_sends_no_history(self, store, channel):
        cid = int(channel["id"])
        store.add_message(cid, "human", "you", "hi")
        assert store.recent_transcript(cid, 0) == ""

    def test_order_is_oldest_first(self, store, channel):
        cid = int(channel["id"])
        for i in range(3):
            store.add_message(cid, "human", "you", f"m{i}")
        text = store.recent_transcript(cid, 8)
        assert text.index("m0") < text.index("m1") < text.index("m2")

    def test_the_prompt_carries_the_question_exactly_once(self, store):
        """End to end, through the prompt the agent actually receives."""
        router = ChannelRouter(store, EventBus())
        channel = store.add_channel("dev")
        cid = int(channel["id"])
        store.add_message(cid, "agent", "a.here", "an earlier reply")
        asked = store.add_message(cid, "human", "you", "please convert the paper")

        prompt = router._prompt_for(cid, "a.here", "please convert the paper", "you",
                                    message_id=int(asked["id"]))
        assert prompt.count("please convert the paper") == 1
        assert "an earlier reply" in prompt

    def test_a_restarted_agent_still_gets_the_thread(self, store):
        """The whole point of the block: continuity across a bounce."""
        router = ChannelRouter(store, EventBus())
        channel = store.add_channel("dev")
        cid = int(channel["id"])
        store.add_message(cid, "human", "you", "write the paper")
        self._tool_call(store, cid)
        store.add_message(cid, "agent", "a.here", "the paper is at workspace/p.md")

        prompt = router._prompt_for(cid, "a.here", "now convert it", "you")
        assert "workspace/p.md" in prompt
# ---------------------------------------------------------------------------
# model-server metrics
# ---------------------------------------------------------------------------

LLAMACPP_METRICS = """\
# HELP llamacpp:prompt_tokens_total Number of prompt tokens processed
# TYPE llamacpp:prompt_tokens_total counter
llamacpp:prompt_tokens_total 1.13378e+06
# HELP llamacpp:prompt_tokens_cached_total Number of prompt tokens reused
# TYPE llamacpp:prompt_tokens_cached_total counter
llamacpp:prompt_tokens_cached_total 7.80234e+06
llamacpp:prompt_seconds_total 857.147
llamacpp:tokens_predicted_total 1.26773e+06
llamacpp:tokens_predicted_seconds_total 27871.3
llamacpp:predicted_tokens_seconds 46.9731
llamacpp:prompt_tokens_seconds 1323.7
llamacpp:requests_processing 0
llamacpp:requests_deferred 0
llamacpp:n_busy_slots_per_decode 1
llamacpp:spec_decode_num_drafts_total 0
"""

# The shape /slots really returns, taken from a running server mid-request.
BUSY_SLOT = [{
    "id": 0, "n_ctx": 131072, "is_processing": True, "id_task": 1269278,
    "n_prompt_tokens": 18144, "n_prompt_tokens_processed": 346,
    "n_prompt_tokens_cache": 17487,
    "params": {"n_predict": 32768, "temperature": 1.0},
    "next_token": [{"has_next_token": True, "n_remain": 32457, "n_decoded": 311}],
}]

IDLE_SLOT = [dict(BUSY_SLOT[0], is_processing=False, n_prompt_tokens=21559,
                  n_prompt_tokens_cache=0, n_prompt_tokens_processed=0,
                  next_token=[{"n_remain": -1, "n_decoded": 0}])]


class TestMetricsParsing:
    def test_counters_and_gauges_are_read(self):
        raw = metrics.parse_prometheus(LLAMACPP_METRICS)
        assert raw["llamacpp:prompt_tokens_total"] == pytest.approx(1133780)
        assert raw["llamacpp:requests_deferred"] == 0

    def test_comments_are_not_metrics(self):
        raw = metrics.parse_prometheus(LLAMACPP_METRICS)
        assert not any(k.startswith("#") or k == "HELP" for k in raw)

    def test_labelled_series_still_parse(self):
        raw = metrics.parse_prometheus('vllm:prompt_tokens_total{model="x"} 42\n')
        assert raw["vllm:prompt_tokens_total"] == 42

    def test_nan_and_junk_are_skipped(self):
        raw = metrics.parse_prometheus("a:b NaN\nnonsense\nc:d 3\n")
        assert "c:d" in raw and "a:b" not in raw

    def test_names_are_normalised(self):
        values = metrics.normalise(metrics.parse_prometheus(LLAMACPP_METRICS))
        assert values["predicted_tokens"] == pytest.approx(1267730)
        assert values["decode_rate"] == pytest.approx(46.9731)

    def test_vllm_maps_onto_the_same_shape(self):
        values = metrics.normalise(metrics.parse_prometheus(
            "vllm:generation_tokens_total 10\nvllm:num_requests_waiting 2\n"))
        assert values == {"predicted_tokens": 10.0, "deferred": 2.0}

    @pytest.mark.parametrize("endpoint,expected", [
        ("http://h:30081/v1", "http://h:30081"),
        ("http://h:30081/v1/", "http://h:30081"),
        ("http://h:30081/v1/chat/completions", "http://h:30081"),
        ("http://h:11434/api", "http://h:11434"),
        ("http://h:30081", "http://h:30081"),
        ("", ""),
    ])
    def test_the_api_suffix_comes_off(self, endpoint, expected):
        """Configs point at /v1; /metrics and /slots live at the root."""
        assert metrics.base_url(endpoint) == expected


class TestSlotContext:
    def test_a_live_request_reports_its_prompt_size(self):
        """The question 'what context is it passing?' is answered here."""
        ctx = metrics._slot_context(BUSY_SLOT)
        assert ctx["live"] is True
        assert ctx["tokens"] == 18144
        assert ctx["cached"] == 17487
        assert ctx["processed"] == 346
        assert ctx["fill"] == pytest.approx(18144 / 131072, abs=1e-4)
        assert ctx["decoded"] == 311
        assert ctx["max_tokens"] == 32768

    def test_an_idle_slot_keeps_the_last_prompt_but_not_the_zeros(self):
        """
        llama.cpp zeroes the cache split when the request ends. Reporting that
        as 'nothing was cached' would be a false claim about the model.
        """
        ctx = metrics._slot_context(IDLE_SLOT)
        assert ctx["live"] is False
        assert ctx["tokens"] == 21559
        assert "cached" not in ctx and "decoded" not in ctx

    def test_the_busy_slot_wins_over_an_idle_one(self):
        ctx = metrics._slot_context(IDLE_SLOT + BUSY_SLOT)
        assert ctx["tokens"] == 18144

    def test_slots_are_counted(self):
        ctx = metrics._slot_context(BUSY_SLOT + IDLE_SLOT)
        assert (ctx["slots_busy"], ctx["slots_total"]) == (1, 2)

    def test_no_slots_is_not_an_error(self):
        assert metrics._slot_context([]) == {}
        assert metrics._slot_context({"error": "disabled"}) == {}


class TestWindowedRates:
    """
    Lifetime throughput hides the thing being diagnosed: 46 tok/s averaged over
    every high-context turn ever served says nothing about this one.
    """

    def test_a_window_gives_tokens_per_second_of_model_time(self):
        before = {"at": time.time() - 30,
                  "metrics": {"predicted_tokens": 1000.0, "predicted_seconds": 20.0}}
        rates = metrics._rates({"predicted_tokens": 1600.0, "predicted_seconds": 30.0},
                               before)
        assert rates["decode_rate"] == pytest.approx(60.0)   # 600 tokens / 10s
        assert rates["decode_tokens"] == 600

    def test_a_window_with_no_completion_reports_nothing(self):
        """Counters move only when a request finishes — absent, not 0 tok/s."""
        before = {"at": time.time() - 30, "metrics": {"predicted_tokens": 1000.0,
                                                      "predicted_seconds": 20.0}}
        rates = metrics._rates({"predicted_tokens": 1000.0, "predicted_seconds": 20.0},
                               before)
        assert "decode_rate" not in rates

    def test_a_restarted_server_does_not_invent_a_rate(self):
        before = {"at": time.time() - 30, "metrics": {"predicted_tokens": 9000.0,
                                                      "predicted_seconds": 90.0}}
        assert "decode_rate" not in metrics._rates(
            {"predicted_tokens": 10.0, "predicted_seconds": 1.0}, before)

    def test_a_stale_sample_is_not_a_window(self):
        before = {"at": time.time() - (metrics.SAMPLE_MAX_AGE + 60),
                  "metrics": {"predicted_tokens": 1.0, "predicted_seconds": 1.0}}
        assert metrics._rates({"predicted_tokens": 500.0, "predicted_seconds": 10.0},
                              before) == {}

    def test_the_first_reading_has_no_window(self):
        assert metrics._rates({"predicted_tokens": 5.0}, None) == {}

    def test_the_sampler_hands_back_the_previous_reading(self):
        sampler = metrics.Sampler()
        assert sampler.take("http://h", {"predicted_tokens": 1.0}) is None
        before = sampler.take("http://h", {"predicted_tokens": 2.0})
        assert before["metrics"] == {"predicted_tokens": 1.0}

    def test_endpoints_are_sampled_apart(self):
        sampler = metrics.Sampler()
        sampler.take("http://a", {"predicted_tokens": 1.0})
        assert sampler.take("http://b", {"predicted_tokens": 9.0}) is None


class TestProbe:
    """Against a stub server, so the suite does not need a GPU box."""

    @staticmethod
    def _handler(routes):
        def handle(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path not in routes:
                return httpx.Response(404)
            status, payload = routes[path]
            if isinstance(payload, str):
                return httpx.Response(status, text=payload)
            return httpx.Response(status, json=payload)
        return handle

    async def _probe(self, routes, sampler=None):
        transport = httpx.MockTransport(self._handler(routes))
        async with httpx.AsyncClient(transport=transport) as client:
            return await metrics.probe(client, "http://box:30081/v1", sampler)

    @pytest.mark.asyncio
    async def test_a_healthy_server_reports_everything(self):
        report = await self._probe({
            "/props": (200, {"model_alias": "qwen:IQ3_S", "total_slots": 1,
                             "default_generation_settings": {"n_ctx": 131072}}),
            "/metrics": (200, LLAMACPP_METRICS),
            "/slots": (200, BUSY_SLOT),
        })
        assert report["reachable"] is True
        assert report["model"] == "qwen:IQ3_S"
        assert report["n_ctx"] == 131072
        assert report["context"]["tokens"] == 18144
        assert report["metrics"]["decode_rate"] == pytest.approx(46.9731)
        # 7.80M reused against 1.13M computed
        assert report["cache_hit_ratio"] == pytest.approx(0.873, abs=0.01)

    @pytest.mark.asyncio
    async def test_an_unreachable_endpoint_is_a_result_not_an_exception(self):
        def refuse(request):
            raise httpx.ConnectError("connection refused")
        transport = httpx.MockTransport(refuse)
        async with httpx.AsyncClient(transport=transport) as client:
            report = await metrics.probe(client, "http://box:30081/v1")
        assert report["reachable"] is False
        assert "ConnectError" in report["error"]

    @pytest.mark.asyncio
    async def test_metrics_disabled_costs_only_the_metrics(self):
        """llama.cpp needs --metrics; without it the rest still reads."""
        report = await self._probe({
            "/props": (200, {"model_alias": "m", "total_slots": 1}),
            "/metrics": (404, "not found"),
            "/slots": (200, BUSY_SLOT),
        })
        assert "--metrics" in report["metrics_note"]
        assert report["context"]["tokens"] == 18144
        assert "metrics" not in report

    @pytest.mark.asyncio
    async def test_no_slots_costs_only_the_context(self):
        report = await self._probe({
            "/props": (200, {"model_alias": "m"}),
            "/metrics": (200, LLAMACPP_METRICS),
            "/slots": (501, "slots disabled"),
        })
        assert "--no-slots" in report["slots_note"]
        assert "context" not in report
        assert report["metrics"]["decode_rate"] == pytest.approx(46.9731)

    @pytest.mark.asyncio
    async def test_a_server_with_no_known_metrics_says_so(self):
        report = await self._probe({
            "/props": (200, {}),
            "/metrics": (200, "some_other_project_tokens 5\n"),
            "/slots": (404, ""),
        })
        assert report["metrics_note"] == "no recognised metrics at /metrics"

    @pytest.mark.asyncio
    async def test_a_blank_endpoint_is_reported_not_fetched(self):
        async with httpx.AsyncClient() as client:
            report = await metrics.probe(client, "")
        assert report["reachable"] is False
        assert report["error"] == "no endpoint configured"

    @pytest.mark.asyncio
    async def test_probe_all_dedupes_shared_endpoints(self):
        """Two agents on one server is one server, read once."""
        seen = []

        async def fake(client, endpoint, sampler=None, timeout=4.0):
            seen.append(endpoint)
            return {"endpoint": endpoint}

        original = metrics.probe
        metrics.probe = fake
        try:
            out = await metrics.probe_all(["http://a/v1", "http://a/v1", "http://b/v1", ""])
        finally:
            metrics.probe = original
        assert sorted(seen) == ["http://a/v1", "http://b/v1"]
        assert len(out) == 2


class TestMetricsRoute:
    @pytest.fixture
    def two_agents(self, plane):
        """Two agents sharing one endpoint, which is the interesting case."""
        store = plane.store
        machine = store.add_machine("box", "box", ssh_user="u")
        for name in ("researcher", "reviewer"):
            store.upsert_agent(int(machine["id"]), {
                "name": name, "model": "qwen", "provider": "vllm", "ready": True,
                "endpoint": "http://10.0.0.147:30081/v1",
            })
        return plane

    def test_the_endpoint_is_remembered_from_the_agent_record(self, two_agents):
        agents = two_agents.store.agents()
        assert {a["endpoint"] for a in agents} == {"http://10.0.0.147:30081/v1"}

    def test_a_blank_endpoint_does_not_erase_a_known_one(self, two_agents):
        """install.sh --list reports no endpoint; a refresh must not lose it."""
        store = two_agents.store
        machine = store.machines()[0]
        store.upsert_agent(int(machine["id"]), {"name": "researcher", "ready": True})
        assert store.agent_by_name(int(machine["id"]), "researcher")["endpoint"] == \
            "http://10.0.0.147:30081/v1"

    def test_agents_are_grouped_by_server_and_marked_shared(self, two_agents, monkeypatch):
        async def fake_probe_all(endpoints, sampler=None, timeout=4.0):
            return [{"endpoint": endpoints[0], "reachable": True, "model": "qwen",
                     "context": {"tokens": 18144, "n_ctx": 131072, "live": True}}]
        monkeypatch.setattr(metrics, "probe_all", fake_probe_all)

        client = TestClient(two_agents.app)
        servers = client.get("/api/agents/metrics", headers=AUTH).json()["servers"]
        assert len(servers) == 1
        assert servers[0]["shared"] is True
        assert servers[0]["context"]["tokens"] == 18144
        assert {a["qualified"] for a in servers[0]["agents"]} == \
            {"researcher.box", "reviewer.box"}

    def test_an_agent_with_no_endpoint_is_listed_as_unknown(self, plane, monkeypatch):
        """Dropping it would hide the agent; it is the endpoint that is missing."""
        async def nothing(endpoints, sampler=None, timeout=4.0):
            return []
        monkeypatch.setattr(metrics, "probe_all", nothing)
        machine = plane.store.add_machine("box", "b", ssh_user="u")
        plane.store.upsert_agent(int(machine["id"]), {"name": "solo", "ready": True})

        servers = TestClient(plane.app).get(
            "/api/agents/metrics", headers=AUTH).json()["servers"]
        assert len(servers) == 1
        assert servers[0]["reachable"] is False
        assert "no endpoint recorded" in servers[0]["error"]
        assert servers[0]["agents"][0]["qualified"] == "solo.box"

    def test_it_needs_the_token(self, client):
        assert client.get("/api/agents/metrics").status_code == 401

    def test_the_route_is_not_swallowed_by_the_agent_id_route(self, client):
        """'/api/agents/metrics' must not be read as an agent called 'metrics'."""
        assert client.get("/api/agents/metrics", headers=AUTH).status_code == 200

    def test_the_agents_tab_renders_the_cards(self):
        root = Path(__file__).resolve().parents[1] / "agents_control" / "static"
        html = (root / "index.html").read_text()
        app_js = (root / "app.js").read_text()
        assert 'id="server-cards"' in html
        assert "/api/agents/metrics" in app_js
        # polled only while the tab is open, and stopped when it is not
        assert "startServerPolling" in app_js and "stopServerPolling" in app_js


class TestAgentContext:
    """
    The per-agent figure: how much history it prefixes to every request.

    Distinct from a server card, which reports whatever request that server
    last handled — on a shared endpoint that could be either agent's.
    """

    @staticmethod
    def _reply(payload, status=200):
        def handle(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/context"
            if isinstance(payload, str):
                return httpx.Response(status, text=payload)
            return httpx.Response(status, json=payload)
        return httpx.MockTransport(handle)

    async def _read(self, transport):
        async with httpx.AsyncClient(transport=transport) as client:
            return await metrics.agent_context(client, "http://box:8001/")

    @pytest.mark.asyncio
    async def test_the_largest_session_is_surfaced(self):
        report = await self._read(self._reply({
            "supported": True, "history_limit": 48,
            "sessions": [{"session": "a", "messages": 34, "chars": 112000,
                          "prompt_tokens": 28100},
                         {"session": "b", "messages": 2, "chars": 90}],
        }))
        assert report["top"]["prompt_tokens"] == 28100
        assert report["session_count"] == 2
        assert report["reachable"] is True

    @pytest.mark.asyncio
    async def test_an_agent_with_no_history_is_not_an_error(self):
        report = await self._read(self._reply({"supported": True, "sessions": []}))
        assert report["supported"] is True and report["top"] is None

    @pytest.mark.asyncio
    async def test_an_older_agent_without_the_route_is_told_apart(self):
        """404 means 'reinstall me', not 'I am broken'."""
        report = await self._read(self._reply("nope", status=404))
        assert report["supported"] is False
        assert "reinstall" in report["error"]
        # It answered, so it is up — only the route is missing.
        assert report["reachable"] is True

    @pytest.mark.asyncio
    async def test_a_stopped_agent_reports_the_connection(self):
        def refuse(request):
            raise httpx.ConnectError("refused")
        report = await self._read(httpx.MockTransport(refuse))
        assert report["supported"] is False and "ConnectError" in report["error"]
        # The board's status column overrides install-time `ready` with this.
        assert report["reachable"] is False

    @pytest.mark.asyncio
    async def test_an_agent_with_no_address_is_not_fetched(self):
        async with httpx.AsyncClient() as client:
            report = await metrics.agent_context(client, "")
        assert report["error"] == "no address for this agent"
        assert report["reachable"] is False

    @pytest.mark.asyncio
    async def test_contexts_are_keyed_by_agent_id(self):
        async def fake(client, url, timeout=4.0):
            return {"supported": True, "url": url}
        original = metrics.agent_context
        metrics.agent_context = fake
        try:
            out = await metrics.contexts_for([{"id": 7, "url": "http://a:1"},
                                              {"id": 9, "url": "http://b:1"}])
        finally:
            metrics.agent_context = original
        assert out[7]["url"] == "http://a:1" and out[9]["url"] == "http://b:1"

    def test_the_route_returns_it_per_agent(self, plane, monkeypatch):
        async def fake_contexts(agents, timeout=4.0):
            return {int(a["id"]): {"supported": True, "top": {"prompt_tokens": 28100}}
                    for a in agents}
        async def no_servers(endpoints, sampler=None, timeout=4.0):
            return []
        monkeypatch.setattr(metrics, "contexts_for", fake_contexts)
        monkeypatch.setattr(metrics, "probe_all", no_servers)

        machine = plane.store.add_machine("box", "b", ssh_user="u")
        agent = plane.store.upsert_agent(int(machine["id"]),
                                         {"name": "researcher", "url": "http://b:8001"})
        data = TestClient(plane.app).get("/api/agents/metrics", headers=AUTH).json()
        assert data["contexts"][str(agent["id"])]["top"]["prompt_tokens"] == 28100

    def test_the_agents_table_has_a_context_column(self):
        app_js = (Path(__file__).resolve().parents[1]
                  / "agents_control" / "static" / "app.js").read_text()
        assert "contextCell" in app_js
        assert "'Context'" in app_js


class TestAgentContextRoute:
    """The agent's own side of it."""

    @pytest.fixture
    def served(self, tmp_path):
        from declarative_agent_sdk.agents.lean.agent import LeanAIAgent
        from declarative_agent_sdk.transports.a2a.server import AIAgentServer
        agent = LeanAIAgent(
            name="researcher", description="d", instruction_file=None, tools=[],
            model="qwen", provider="vllm", endpoint_url="http://box:30081/v1",
            workspace_directory=str(tmp_path / "ws"),
            skills_directory=str(tmp_path / "skills"),
        )
        return agent, TestClient(AIAgentServer(agent, port=8111).app)

    def test_it_reports_sizes(self, served):
        agent, client = served
        agent._loop._history["s"] = [{"role": "user", "content": "x" * 8000}]
        data = client.get("/context").json()
        assert data["supported"] is True
        assert data["agent"] == "researcher"
        assert data["sessions"][0]["chars"] == 8000
        assert data["sessions"][0]["prompt_tokens"] > 2000

    def test_delete_forgets_one_session(self, served):
        agent, client = served
        agent._loop._history["a"] = [{"role": "user", "content": "x"}]
        agent._loop._history["b"] = [{"role": "user", "content": "y"}]
        reply = client.delete("/context", params={"session": "a"})
        assert reply.status_code == 200 and reply.json()["cleared"] == 1
        assert list(agent._loop._history) == ["b"]
        assert client.get("/context").json()["sessions"][0]["session"] == "b"

    def test_delete_without_a_session_forgets_all(self, served):
        agent, client = served
        agent._loop._history["a"] = [{"role": "user", "content": "x"}]
        agent._loop._history["b"] = [{"role": "user", "content": "y"}]
        assert client.delete("/context").json()["cleared"] == 2
        assert agent._loop._history == {}

    def test_it_does_not_leak_the_conversation(self, served):
        agent, client = served
        agent._loop._history["s"] = [{"role": "user", "content": "swordfish"}]
        assert "swordfish" not in client.get("/context").text

    def test_a_runtime_that_cannot_answer_says_so(self, tmp_path):
        """adk and deepagent keep history elsewhere; that is reported, not faked."""
        from declarative_agent_sdk.transports.a2a.server import AIAgentServer

        class Mute:
            name = "mute"
            agent_card = None

        from declarative_agent_sdk.agents.lean.agent import LeanAIAgent
        agent = LeanAIAgent(name="m", description="d", instruction_file=None,
                            tools=[], model="q", provider="vllm",
                            endpoint_url="http://b:1/v1",
                            workspace_directory=str(tmp_path / "ws"),
                            skills_directory=str(tmp_path / "sk"))
        server = AIAgentServer(agent, port=8112)
        # stand in for a runtime without the method
        object.__setattr__(server, "_agent", Mute())
        data = TestClient(server.app).get("/context").json()
        assert data["supported"] is False
        assert "lean runtime does" in data["note"]

    def test_the_agent_card_still_serves(self, served):
        """The new route must not displace the A2A surface."""
        _, client = served
        assert client.get("/.well-known/agent-card.json").status_code == 200


class TestModelCatalog:
    """The create form's endpoint autofill and model picker."""

    @staticmethod
    def _serving(payload, status=200, seen=None):
        def handle(request: httpx.Request) -> httpx.Response:
            if seen is not None:
                seen.append(request)
            return httpx.Response(status, json=payload)
        return httpx.MockTransport(handle)

    @pytest.mark.asyncio
    async def test_openai_defaults_its_endpoint_and_drops_non_chat_models(self):
        from declarative_agent_sdk.agents_control import catalog
        seen = []
        result = await catalog.list_models(
            "openai", "", "sk-test",
            transport=self._serving({"data": [
                {"id": "gpt-x"}, {"id": "text-embedding-3-small"},
                {"id": "whisper-1"}, {"id": "o-mini"},
            ]}, seen=seen),
        )
        assert result["ok"] and result["models"] == ["gpt-x", "o-mini"]
        assert str(seen[0].url) == "https://api.openai.com/v1/models"
        assert seen[0].headers["Authorization"] == "Bearer sk-test"

    @pytest.mark.asyncio
    async def test_openai_without_a_key_says_so_instead_of_calling(self, monkeypatch):
        from declarative_agent_sdk.agents_control import catalog
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        result = await catalog.list_models("openai", "", "")
        assert not result["ok"] and "API key" in result["error"]

    @pytest.mark.asyncio
    async def test_vllm_lists_everything_it_serves_without_a_key(self, monkeypatch):
        from declarative_agent_sdk.agents_control import catalog
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        seen = []
        result = await catalog.list_models(
            "vllm", "http://box:30081/v1/", "",
            transport=self._serving({"data": [{"id": "Qwen/Qwen3-embedding"}]}, seen=seen),
        )
        # vLLM serves what it was started with; nothing is filtered.
        assert result["models"] == ["Qwen/Qwen3-embedding"]
        assert "Authorization" not in seen[0].headers
        assert str(seen[0].url) == "http://box:30081/v1/models"

    @pytest.mark.asyncio
    async def test_a_rejected_key_is_named(self):
        from declarative_agent_sdk.agents_control import catalog
        result = await catalog.list_models(
            "openai", "", "bad", transport=self._serving({}, status=401))
        assert not result["ok"] and "rejected" in result["error"]

    @pytest.mark.asyncio
    async def test_an_unreachable_endpoint_is_a_result(self):
        from declarative_agent_sdk.agents_control import catalog
        def refuse(request):
            raise httpx.ConnectError("refused")
        result = await catalog.list_models(
            "vllm", "http://box:1/v1", "", transport=httpx.MockTransport(refuse))
        assert not result["ok"] and "could not reach" in result["error"]

    @pytest.mark.asyncio
    async def test_providers_without_a_listing_are_declined(self):
        from declarative_agent_sdk.agents_control import catalog
        result = await catalog.list_models("anthropic", "", "key")
        assert not result["ok"] and result["models"] == []

    def test_state_carries_provider_endpoints(self, client):
        providers = client.get("/api/state", headers=AUTH).json()["providers"]
        assert providers["endpoints"]["openai"] == "https://api.openai.com/v1"
        assert "vllm" in providers["listable"]

    def test_the_route_answers_with_a_reason(self, client, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        reply = client.post("/api/models", headers=AUTH, json={"provider": "openai"})
        assert reply.status_code == 200
        assert reply.json()["ok"] is False


class TestAgentApiKey:
    """Updating a provider key after install, through the agent's .env."""

    LOCAL = {"host": "localhost", "is_local": 1, "name": "here"}

    def test_set_replaces_the_line_keeps_others_and_stays_private(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("OPENAI_API_KEY=old\nexport OPENAI_API_KEY=older\n"
                       "AGENTS_CONTROL_URL=http://board:8900\n")
        env.chmod(0o600)
        driver = MachineDriver()
        driver.set_env_var(self.LOCAL, str(tmp_path), "OPENAI_API_KEY", "sk-new")
        lines = env.read_text().splitlines()
        assert lines.count("OPENAI_API_KEY=sk-new") == 1
        assert not any("old" in line for line in lines)
        assert "AGENTS_CONTROL_URL=http://board:8900" in lines
        assert (env.stat().st_mode & 0o777) == 0o600
        assert not (tmp_path / ".env.tmp").exists()

    def test_set_creates_a_missing_env(self, tmp_path):
        MachineDriver().set_env_var(self.LOCAL, str(tmp_path), "GOOGLE_API_KEY", "g-1")
        assert (tmp_path / ".env").read_text() == "GOOGLE_API_KEY=g-1\n"

    def test_names_are_listed_without_values(self, tmp_path):
        (tmp_path / ".env").write_text(
            "OPENAI_API_KEY=sk-secret\nexport B_TOKEN=x\nEMPTY=\n# C=1\n")
        names = MachineDriver().env_names(self.LOCAL, str(tmp_path))
        assert names == ["B_TOKEN", "OPENAI_API_KEY"]
        assert "sk-secret" not in "".join(names)

    def test_no_env_means_no_names(self, tmp_path):
        assert MachineDriver().env_names(self.LOCAL, str(tmp_path)) == []

    @pytest.mark.parametrize("bad", ["two words", "a'b", 'a"b', "a$b", "a`b", "a\\b"])
    def test_values_that_would_break_the_file_are_refused(self, tmp_path, bad):
        with pytest.raises(MachineError):
            MachineDriver().set_env_var(self.LOCAL, str(tmp_path), "OPENAI_API_KEY", bad)

    def test_vllm_keys_go_to_openai_api_key(self):
        from declarative_agent_sdk.agents_control.machines import key_name_for
        assert key_name_for("vllm") == "OPENAI_API_KEY"
        assert key_name_for("anthropic") == "ANTHROPIC_API_KEY"


class TestBoardIsNeverStale:
    """The board is edited in place; a browser must revalidate, not guess."""

    def test_static_files_say_no_cache(self, client):
        response = client.get("/static/app.js")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-cache"
        assert response.headers.get("etag")

    def test_index_says_no_cache(self, client):
        assert client.get("/").headers["cache-control"] == "no-cache"


class TestAgentNetwork:
    """Changing an installed agent's bind address and published URL."""

    LOCAL = {"host": "localhost", "is_local": 1, "name": "here"}

    @pytest.fixture
    def layout(self, tmp_path):
        import sys
        root = tmp_path / "root"
        prefix = root / "coder"
        (prefix / ".venv" / "bin").mkdir(parents=True)
        (prefix / ".venv" / "bin" / "python").symlink_to(sys.executable)
        conf = root / "supervisor" / "conf.d" / "coder.conf"
        conf.parent.mkdir(parents=True)
        conf.write_text('[program:coder]\ncommand=x\n'
                        'environment=PYTHONUNBUFFERED="1",HOST="0.0.0.0",PORT="8000",PATH="/bin"\n')
        (prefix / "run_server.py").write_text(
            'x = 1\n        AgentInterface(\n                url="http://10.0.0.132:8000/",\n')
        (prefix / ".agent-meta").write_text(json.dumps({
            "name": "coder", "bind_host": "0.0.0.0", "advertise_host": "10.0.0.132",
            "url": "http://10.0.0.132:8000/", "port": 8000}))
        return prefix, conf

    def test_read_reports_the_files_not_the_board(self, layout):
        prefix, _ = layout
        net = MachineDriver().agent_network(self.LOCAL, str(prefix), "coder")
        assert net["ok"] and net["bind_host"] == "0.0.0.0"
        assert net["advertise_host"] == "10.0.0.132" and net["port"] == 8000

    def test_write_updates_all_three_places(self, layout):
        prefix, conf = layout
        net = MachineDriver().agent_network(
            self.LOCAL, str(prefix), "coder", "0.0.0.0", "100.83.163.18", write=True)
        assert net["url"] == "http://100.83.163.18:8000/"
        assert 'url="http://100.83.163.18:8000/"' in (prefix / "run_server.py").read_text()
        meta = json.loads((prefix / ".agent-meta").read_text())
        assert meta["url"] == net["url"] and meta["advertise_host"] == "100.83.163.18"
        assert meta["agent_card"].endswith(":8000/.well-known/agent-card.json")
        assert net["conf_changed"] is False                 # HOST was not asked to change
        assert 'HOST="0.0.0.0"' in conf.read_text()

    def test_a_specific_bind_is_also_what_gets_published(self, layout):
        prefix, conf = layout
        net = MachineDriver().agent_network(
            self.LOCAL, str(prefix), "coder", "100.83.163.18", "", write=True)
        assert 'HOST="100.83.163.18",PORT="8000"' in conf.read_text()
        assert net["url"] == "http://100.83.163.18:8000/" and net["conf_changed"]

    def test_all_interfaces_without_an_address_keeps_the_published_one(self, layout):
        prefix, _ = layout
        net = MachineDriver().agent_network(
            self.LOCAL, str(prefix), "coder", "0.0.0.0", "", write=True)
        assert net["url"] == "http://10.0.0.132:8000/"

    def test_ipv6_is_bracketed(self, layout):
        prefix, _ = layout
        net = MachineDriver().agent_network(
            self.LOCAL, str(prefix), "coder", "", "fd7a::1", write=True)
        assert net["url"] == "http://[fd7a::1]:8000/"
        again = MachineDriver().agent_network(self.LOCAL, str(prefix), "coder")
        assert again["advertise_host"] == "fd7a::1"

    def test_a_pre_supervisor_install_says_why(self, layout):
        prefix, conf = layout
        conf.unlink()
        net = MachineDriver().agent_network(self.LOCAL, str(prefix), "coder")
        assert not net["ok"] and "missing" in net["error"]

    def test_bad_addresses_are_refused(self, layout):
        prefix, _ = layout
        with pytest.raises(MachineError):
            MachineDriver().agent_network(
                self.LOCAL, str(prefix), "coder", "x; reboot", "", write=True)

    def test_route_updates_the_board_record(self, client, plane, layout):
        prefix, _ = layout
        (prefix / "agent.yaml").write_text(SAMPLE_YAML)
        (prefix / "instructions.md").write_text("# coder\n")
        machine = plane.store.add_machine("here", "localhost", is_local=True)
        agent = plane.store.upsert_agent(machine["id"], {
            "name": "coder", "prefix": str(prefix), "url": "http://10.0.0.132:8000/"})
        got = client.get(f"/api/agents/{agent['id']}/config", headers=AUTH).json()
        assert got["network"]["advertise_host"] == "10.0.0.132"
        reply = client.put(f"/api/agents/{agent['id']}/config", json={
            "properties": {}, "restart": False,
            "network": {"bind_host": "0.0.0.0", "advertise_host": "100.83.163.18"},
        }, headers=AUTH).json()
        assert reply.get("network", {}).get("url") == "http://100.83.163.18:8000/", reply
        record = plane.store.agent(agent["id"])
        assert record["url"] == "http://100.83.163.18:8000/"
        assert record["agent_card"] == "http://100.83.163.18:8000/.well-known/agent-card.json"

    def test_route_rejects_a_bad_address_before_writing(self, client, plane, layout):
        prefix, _ = layout
        (prefix / "agent.yaml").write_text(SAMPLE_YAML)
        (prefix / "instructions.md").write_text("# coder\n")
        machine = plane.store.add_machine("here", "localhost", is_local=True)
        agent = plane.store.upsert_agent(machine["id"], {"name": "coder", "prefix": str(prefix)})
        before = (prefix / "agent.yaml").read_text()
        response = client.put(f"/api/agents/{agent['id']}/config", json={
            "properties": {"model": "changed"}, "network": {"bind_host": "a b"},
        }, headers=AUTH)
        assert response.status_code == 400
        assert (prefix / "agent.yaml").read_text() == before


class TestAgentModels:
    """The settings form's model picker, listed from the agent's machine."""

    LOCAL = {"host": "localhost", "is_local": 1, "name": "here"}

    @pytest.fixture
    def endpoint(self):
        import http.server
        import threading
        seen = {}

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                seen["auth"] = self.headers.get("Authorization")
                seen["path"] = self.path
                if seen["auth"] != "Bearer sk-fake-for-test":
                    self.send_response(401)
                    self.end_headers()
                    return
                body = json.dumps({"data": [{"id": "gpt-x"}, {"id": "whisper-1"}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        yield f"http://127.0.0.1:{server.server_port}/v1", seen
        server.shutdown()

    @pytest.fixture
    def prefix(self, tmp_path):
        import sys
        (tmp_path / ".venv" / "bin").mkdir(parents=True)
        (tmp_path / ".venv" / "bin" / "python").symlink_to(sys.executable)
        return tmp_path

    def test_the_key_comes_from_the_agents_env(self, endpoint, prefix, monkeypatch):
        url, seen = endpoint
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        (prefix / ".env").write_text("export OPENAI_API_KEY='sk-fake-for-test'\n")
        reply = MachineDriver().list_models_on_machine(
            self.LOCAL, str(prefix), url, "OPENAI_API_KEY")
        assert reply["ok"] and reply["ids"] == ["gpt-x", "whisper-1"]
        assert seen["path"] == "/v1/models"
        assert "sk-fake" not in json.dumps(reply)           # only ids come back

    def test_a_missing_key_is_reported_as_missing(self, endpoint, prefix, monkeypatch):
        url, _ = endpoint
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        reply = MachineDriver().list_models_on_machine(
            self.LOCAL, str(prefix), url, "OPENAI_API_KEY")
        assert reply == {"ok": False, "status": 401, "has_key": False}

    def test_a_bad_endpoint_is_refused_before_running(self, prefix):
        with pytest.raises(MachineError):
            MachineDriver().list_models_on_machine(
                self.LOCAL, str(prefix), "file:///etc/passwd", "OPENAI_API_KEY")

    def _agent(self, plane, prefix, provider="openai"):
        machine = plane.store.add_machine("here", "localhost", is_local=True)
        return plane.store.upsert_agent(machine["id"], {
            "name": "coder", "prefix": str(prefix), "provider": provider})

    def test_route_filters_and_follows_unsaved_edits(self, client, plane, prefix, monkeypatch):
        calls = []

        def fake(machine, prefix_, endpoint, key_name, timeout=30):
            calls.append((endpoint, key_name))
            return {"ok": True, "ids": ["gpt-x", "text-embedding-3-small"], "has_key": True}

        monkeypatch.setattr(plane.driver, "list_models_on_machine", fake)
        agent = self._agent(plane, prefix, provider="vllm")
        data = client.get(f"/api/agents/{agent['id']}/models?provider=openai",
                          headers=AUTH).json()
        assert data["models"] == ["gpt-x"]
        assert calls == [("https://api.openai.com/v1", "OPENAI_API_KEY")]

    def test_route_explains_a_missing_key(self, client, plane, prefix, monkeypatch):
        monkeypatch.setattr(plane.driver, "list_models_on_machine",
                            lambda *a, **k: {"ok": False, "status": 401, "has_key": False})
        agent = self._agent(plane, prefix)
        data = client.get(f"/api/agents/{agent['id']}/models", headers=AUTH).json()
        assert not data["ok"] and "OPENAI_API_KEY is not set" in data["error"]

    def test_route_declines_unlisted_providers(self, client, plane, prefix):
        agent = self._agent(plane, prefix, provider="anthropic")
        data = client.get(f"/api/agents/{agent['id']}/models", headers=AUTH).json()
        assert not data["ok"] and data["models"] == []


class TestSupervisordDown:
    """A dead supervisord is named, not reported as a bare exit code."""

    LOCAL = {"host": "localhost", "is_local": 1, "name": "gpu"}

    def test_stdout_is_kept_when_stderr_is_empty(self):
        with pytest.raises(MachineError) as excinfo:
            MachineDriver().run(self.LOCAL, "echo 'unix:///x/s.sock no such file'; exit 7")
        assert "no such file" in excinfo.value.stderr

    @staticmethod
    def _fake_supervisor(tmp_path):
        """supervisorctl that is 'down' until fake supervisord drops a marker."""
        prefix = tmp_path / "root" / "coder"
        bin_dir = tmp_path / "root" / "supervisor" / ".venv" / "bin"
        bin_dir.mkdir(parents=True)
        prefix.mkdir()
        marker = tmp_path / "up"
        ctl = bin_dir / "supervisorctl"
        ctl.write_text(f"#!/bin/sh\n[ -f {marker} ] && exit 0\n"
                       "echo 'unix:///x/s.sock no such file'\nexit 7\n")
        daemon = bin_dir / "supervisord"
        daemon.write_text(f"#!/bin/sh\ntouch {marker}\n")
        for tool in (ctl, daemon):
            tool.chmod(0o755)
        return prefix, marker

    def test_restart_with_supervisord_down_starts_it(self, tmp_path):
        prefix, marker = self._fake_supervisor(tmp_path)
        result = MachineDriver().supervisor_action(self.LOCAL, str(prefix), "coder", "restart")
        assert result["ok"] is True and result["state"] == "started"
        assert marker.exists()
        assert "started its agents" in result["output"]

    def test_apply_with_supervisord_down_starts_it(self, tmp_path):
        prefix, _ = self._fake_supervisor(tmp_path)
        applied = MachineDriver().apply_service_change(self.LOCAL, str(prefix), "coder")
        assert applied["ok"] and applied["state"] == "started"

    def test_status_with_supervisord_down_only_reports(self, tmp_path):
        prefix, marker = self._fake_supervisor(tmp_path)
        result = MachineDriver().supervisor_action(self.LOCAL, str(prefix), "coder", "status")
        assert result["ok"] is False and not marker.exists()
        assert "supervisord is not running on 'gpu'" in result["output"]

    def test_a_reaping_logind_without_linger_is_reported_not_attempted(
            self, tmp_path, monkeypatch):
        prefix, marker = self._fake_supervisor(tmp_path)
        tools = tmp_path / "tools"
        tools.mkdir()
        (tools / "busctl").write_text("#!/bin/sh\necho 'b true'\n")
        (tools / "loginctl").write_text("#!/bin/sh\necho no\n")
        for tool in tools.iterdir():
            tool.chmod(0o755)
        monkeypatch.setenv("PATH", f"{tools}:{os.environ['PATH']}")
        result = MachineDriver().supervisor_action(self.LOCAL, str(prefix), "coder", "restart")
        assert result["ok"] is False and result["state"] == "needs-linger"
        assert "sudo loginctl enable-linger" in result["output"]
        assert not marker.exists()                  # nothing started that would die

    def test_other_failures_pass_through(self):
        from declarative_agent_sdk.agents_control.machines import supervisor_down_hint
        assert supervisor_down_hint("coder: ERROR (no such process)", self.LOCAL, "/r/a") == ""


class TestAgentsKnowTheirOwnTools:
    """An agent on the board is told its tools act on its own machine."""

    def test_user_at_host_is_not_a_mention(self):
        assert mentions('check GPU on "mahadev@10.0.0.147"') == []
        assert mentions("ssh mahadev@mahadev-ms7d28.tailbe5bd0.ts.net") == []
        assert mentions("mail me at a.b@example.com") == []

    def test_real_mentions_still_parse(self):
        assert mentions("@coder.box go, then (@scout) and\n@reviewer") == [
            "coder.box", "scout", "reviewer"]
        assert mentions("ask @coder: now") == ["coder"]

    def test_the_prompt_says_where_the_agent_runs_and_what_it_holds(self, store):
        router = ChannelRouter(store, EventBus())
        machine = store.add_machine("MacBook-pro", "localhost", is_local=True)
        agent = store.upsert_agent(machine["id"], {
            "name": "master", "url": "http://x",
            "tools": "tavily_search"})
        channel = store.add_channel("Master")
        store.join(channel["id"], agent["id"])
        prompt = router._prompt_for(channel["id"], "master.MacBook-pro", "hi", "you")
        assert "You run on the machine 'MacBook-pro'" in prompt
        assert "(read_file, write_file, web_request, exec_command, send_to_agent, " in prompt
        assert "read_shared, tavily_search)" in prompt
        assert "user@host is not an agent" in prompt


class TestClearing:
    """Clear conversation (per channel) and clear context (per agent)."""

    @pytest.fixture
    def setup(self, store):
        router = ChannelRouter(store, EventBus())
        machine = store.add_machine("here", "localhost", is_local=True)
        agent = store.upsert_agent(machine["id"], {"name": "coder", "url": "http://x"})
        channel = store.add_channel("dev")
        store.join(channel["id"], agent["id"])
        return router, channel, agent

    def test_an_untouched_board_keeps_its_original_ids(self):
        from declarative_agent_sdk.agents_control.router import context_id_for
        assert context_id_for({"name": "dev"}, {"qualified": "coder.here"}) == "ac-dev-coder.here"

    @pytest.mark.asyncio
    async def test_clear_conversation_empties_and_moves_every_member_on(
            self, setup, store, monkeypatch):
        from declarative_agent_sdk.agents_control.router import context_id_for
        router, channel, agent = setup
        asked = []

        async def forget(member, session=""):
            asked.append((member["qualified"], session))
            return "freed"

        monkeypatch.setattr(router, "_forget", forget)
        store.add_message(channel["id"], "human", "you", "old question")
        before = context_id_for(store.channel(channel["id"]), store.agent(agent["id"]))
        result = await router.clear_channel(channel["id"])
        after = context_id_for(store.channel(channel["id"]), store.agent(agent["id"]))
        assert result["messages_removed"] == 1
        assert before != after
        assert asked == [("coder.here", before)]          # the old history is freed
        texts = [m["text"] for m in store.messages(channel["id"])]
        assert texts == ["Conversation cleared. Members start from a blank history."]

    @pytest.mark.asyncio
    async def test_clear_context_moves_one_agent_on_everywhere(self, setup, store, monkeypatch):
        from declarative_agent_sdk.agents_control.router import context_id_for
        router, channel, agent = setup
        other = store.add_channel("ops")
        store.join(other["id"], agent["id"])

        async def forget(member, session=""):
            return "freed"

        monkeypatch.setattr(router, "_forget", forget)
        ids = lambda: [context_id_for(store.channel(c["id"]), store.agent(agent["id"]))
                       for c in (channel, other)]
        before = ids()
        store.add_message(channel["id"], "human", "you", "kept")
        await router.clear_agent(agent["id"])
        assert all(b != a for b, a in zip(before, ids()))
        assert [m["text"] for m in store.messages(channel["id"])] == ["kept"]

    @pytest.mark.asyncio
    async def test_an_older_agent_is_reported_not_failed(self, setup, monkeypatch):
        router, _, agent = setup
        original = httpx.AsyncClient
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(
            transport=httpx.MockTransport(lambda request: httpx.Response(405))))
        assert "older agent" in await router._forget(dict(agent, url="http://x"))

    @pytest.mark.asyncio
    async def test_a_stopped_agent_is_reported_not_failed(self, setup, monkeypatch):
        router, _, agent = setup
        original = httpx.AsyncClient

        def refuse(request):
            raise httpx.ConnectError("refused")

        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(
            transport=httpx.MockTransport(refuse)))
        assert "unreachable" in await router._forget(dict(agent, url="http://x"))

    def test_routes(self, client, plane):
        channel = plane.store.add_channel("dev")
        reply = client.post(f"/api/channels/{channel['id']}/clear", headers=AUTH)
        assert reply.status_code == 200 and reply.json()["messages_removed"] == 0
        assert client.post("/api/channels/999/clear", headers=AUTH).status_code == 404
        assert client.post("/api/agents/999/clear-context", headers=AUTH).status_code == 404
        assert client.post(f"/api/channels/{channel['id']}/clear").status_code == 401


class TestWorkingState:
    """The sidebar's idle/working icon is seeded from /api/state."""

    def test_turns_are_counted_per_agent_and_channel(self, store):
        router = ChannelRouter(store, EventBus())
        router._turns[("coder.here", 1)] = 2
        router._turns[("coder.here", 3)] = 1
        assert router.working() == {"coder.here": [1, 3]}

    @pytest.mark.asyncio
    async def test_a_finished_turn_leaves_no_trace(self, store):
        router = ChannelRouter(store, EventBus())
        machine = store.add_machine("here", "localhost", is_local=True)
        agent = store.upsert_agent(machine["id"], {"name": "coder", "url": "http://127.0.0.1:9"})
        channel = store.add_channel("dev")
        store.join(channel["id"], agent["id"])
        await router.dispatch(channel["id"], store.members(channel["id"])[0], "hi", "you")
        assert router.working() == {}              # unreachable agent, turn still ended

    def test_state_carries_it(self, client):
        assert client.get("/api/state", headers=AUTH).json()["working"] == {}


class TestHeldThroughput:
    """tok/s keeps the last measured value between turns instead of reading 0."""

    def test_a_measured_window_passes_through_and_is_remembered(self):
        from declarative_agent_sdk.agents_control.metrics import Sampler
        sampler = Sampler()
        rates = {"window_seconds": 5.0, "decode_rate": 41.2, "decode_tokens": 300}
        assert sampler.held("http://box", rates) == rates

    def test_an_empty_window_shows_the_last_figure_with_its_age(self):
        from declarative_agent_sdk.agents_control.metrics import Sampler
        sampler = Sampler()
        sampler.held("http://box", {"window_seconds": 5.0, "decode_rate": 41.2,
                                    "prefill_rate": 900.0})
        held = sampler.held("http://box", {"window_seconds": 5.0})
        assert held["decode_rate"] == 41.2 and held["prefill_rate"] == 900.0
        assert held["held"] is True and held["age_seconds"] >= 0
        assert "measured_at" not in held

    def test_a_new_measurement_replaces_the_held_one(self):
        from declarative_agent_sdk.agents_control.metrics import Sampler
        sampler = Sampler()
        sampler.held("http://box", {"decode_rate": 41.2})
        assert sampler.held("http://box", {"decode_rate": 12.0}) == {"decode_rate": 12.0}
        assert sampler.held("http://box", {})["decode_rate"] == 12.0

    def test_nothing_measured_yet_stays_empty(self):
        from declarative_agent_sdk.agents_control.metrics import Sampler
        assert Sampler().held("http://box", {}) == {}

    def test_endpoints_are_held_separately(self):
        from declarative_agent_sdk.agents_control.metrics import Sampler
        sampler = Sampler()
        sampler.held("http://a", {"decode_rate": 41.2})
        assert sampler.held("http://b", {}) == {}


class TestFileRelay:
    """/api/files/relay passes a shared file through from its owner, and only that."""

    LINK = "http://100.83.163.18:8000/files/AbCdEfGh12345678?exp=9999999999&sig=" + "a" * 32

    @pytest.fixture
    def owner(self, plane):
        machine = plane.store.add_machine("omarchy", "100.83.163.18")
        return plane.store.upsert_agent(machine["id"], {
            "name": "reviewer", "url": "http://100.83.163.18:8000/"})

    def _upstream(self, monkeypatch, handler):
        import declarative_agent_sdk.agents_control.server as server_module
        original = server_module.httpx.AsyncClient
        monkeypatch.setattr(server_module.httpx, "AsyncClient", lambda **kw: original(
            transport=httpx.MockTransport(handler)))

    def test_streams_from_the_owner_with_its_headers(self, client, owner, monkeypatch):
        seen = []

        def handler(request):
            seen.append(str(request.url))
            return httpx.Response(200, text="# findings\n", headers={
                "content-type": "text/markdown", "content-disposition": 'inline; filename="r.md"'})

        self._upstream(monkeypatch, handler)
        reply = client.get("/api/files/relay", params={"url": self.LINK}, headers=AUTH)
        assert reply.status_code == 200 and reply.text == "# findings\n"
        assert reply.headers["x-shared-by"] == "reviewer.omarchy"
        assert reply.headers["cache-control"] == "no-store"
        assert seen[0].startswith("http://100.83.163.18:8000/files/AbCdEfGh12345678?")
        assert "sig=" + "a" * 32 in seen[0]

    def test_the_owners_refusal_passes_through(self, client, owner, monkeypatch):
        self._upstream(monkeypatch, lambda r: httpx.Response(410, json={"error": "expired"}))
        reply = client.get("/api/files/relay", params={"url": self.LINK}, headers=AUTH)
        assert reply.status_code == 410

    def test_a_host_that_is_not_a_board_agent_is_refused(self, client, owner):
        other = self.LINK.replace("100.83.163.18", "169.254.169.254")
        reply = client.get("/api/files/relay", params={"url": other}, headers=AUTH)
        assert reply.status_code == 404

    def test_only_files_links_are_relayed(self, client, owner):
        for url in ("http://100.83.163.18:8000/", "http://100.83.163.18:8000/context",
                    "http://100.83.163.18:8000/files/../etc?exp=1&sig=a", "file:///etc/passwd"):
            assert client.get("/api/files/relay", params={"url": url},
                              headers=AUTH).status_code == 400, url

    def test_needs_the_board_token(self, client, owner):
        assert client.get("/api/files/relay", params={"url": self.LINK}).status_code == 401

    def test_an_unreachable_owner_is_a_502(self, client, owner, monkeypatch):
        def refuse(request):
            raise httpx.ConnectError("refused")
        self._upstream(monkeypatch, refuse)
        reply = client.get("/api/files/relay", params={"url": self.LINK}, headers=AUTH)
        assert reply.status_code == 502 and "reviewer.omarchy" in reply.json()["error"]

    def test_the_prompt_mentions_sharing_to_every_agent(self, store):
        """share_file / read_shared are core, so no tools: list is needed."""
        router = ChannelRouter(store, EventBus())
        machine = store.add_machine("here", "localhost", is_local=True)
        store.upsert_agent(machine["id"], {"name": "b", "tools": ""})
        block = router._self_block("b.here")
        assert "call share_file" in block and "with read_shared" in block


class TestReinstall:
    """Reinstall re-runs install.sh without resetting what the agent has."""

    @pytest.fixture
    def agent(self, plane, tmp_path):
        prefix = tmp_path / "coder"
        prefix.mkdir()
        (prefix / "agent.yaml").write_text(SAMPLE_YAML)
        (prefix / "instructions.md").write_text("# coder\n")
        machine = plane.store.add_machine("here", "localhost", is_local=True)
        return plane.store.upsert_agent(machine["id"], {
            "name": "coder", "prefix": str(prefix), "url": "http://100.1.1.1:8000/",
            "endpoint": "http://box:30081/v1"})

    def _fake(self, plane, monkeypatch, network=None):
        calls = {"env": [], "install": None}
        monkeypatch.setattr(plane.driver, "agent_network", lambda *a, **k: network or {
            "ok": True, "bind_host": "0.0.0.0", "advertise_host": "100.83.163.18"})
        monkeypatch.setattr(plane.driver, "set_env_var",
                            lambda m, p, k, v, **kw: calls["env"].append((k, v)))

        def install(machine, **kwargs):
            calls["install"] = kwargs
            return {"name": kwargs["name"], "prefix": kwargs["prefix"], "ready": True,
                    "url": "http://100.83.163.18:8000/", "port": 8000}

        monkeypatch.setattr(plane.driver, "create_agent", install)
        return calls

    def test_it_keeps_config_key_and_address(self, client, plane, agent, monkeypatch):
        calls = self._fake(plane, monkeypatch)
        reply = client.post(f"/api/agents/{agent['id']}/reinstall",
                            json={"sdk_source": "local"}, headers=AUTH)
        assert reply.status_code == 200, reply.text
        kw = calls["install"]
        assert kw["seed_env"] is False                 # .env is not replaced
        assert kw["prefix"] == agent["prefix"]         # same directory
        assert kw["bind_host"] == "0.0.0.0" and kw["advertise_host"] == "100.83.163.18"
        assert kw["provider"] == "vllm" and kw["model"] == "qwen2.5-coder:latest"
        assert kw["sdk_source"] == "local"
        assert "api_key" not in kw or not kw.get("api_key")
        record = plane.store.agent(agent["id"])
        assert record["url"] == "http://100.83.163.18:8000/"
        assert record["endpoint"]                      # not wiped by the result

    def test_board_env_is_refreshed_one_variable_at_a_time(self, client, plane, agent, monkeypatch):
        calls = self._fake(plane, monkeypatch)
        client.post(f"/api/agents/{agent['id']}/reinstall", json={}, headers=AUTH)
        keys = [k for k, _ in calls["env"]]
        assert "AGENTS_CONTROL_URL" in keys and "AGENT_QUALIFIED_NAME" in keys
        assert ("AGENT_QUALIFIED_NAME", "coder.here") in calls["env"]
        assert not any(k.endswith("API_KEY") for k in keys)   # the key is left alone

    def test_refresh_can_be_skipped(self, client, plane, agent, monkeypatch):
        calls = self._fake(plane, monkeypatch)
        client.post(f"/api/agents/{agent['id']}/reinstall",
                    json={"refresh_board_env": False}, headers=AUTH)
        assert calls["env"] == []

    def test_an_unreadable_address_lets_install_sh_decide(self, client, plane, agent, monkeypatch):
        calls = self._fake(plane, monkeypatch, network={"ok": False, "error": "x"})
        reply = client.post(f"/api/agents/{agent['id']}/reinstall", json={}, headers=AUTH).json()
        assert calls["install"]["bind_host"] == "" and reply["kept_address"] is False

    def test_driver_passes_prefix_and_no_env_file(self, monkeypatch):
        driver = MachineDriver()
        commands = []
        monkeypatch.setattr(driver, "push_scripts", lambda m: None)
        monkeypatch.setattr(driver, "_script_path", lambda m, k: "install.sh")
        monkeypatch.setattr(driver, "run", lambda m, c, **kw: commands.append(c) or '{"name": "coder"}')
        driver.create_agent({"name": "here", "host": "box"}, name="coder", provider="vllm",
                            control_env={"AGENTS_CONTROL_URL": "http://b"}, seed_env=False,
                            prefix="/home/u/.declarative-agent/coder", sdk_source="git")
        assert len(commands) == 1                       # no env file staged or removed
        assert "--prefix /home/u/.declarative-agent/coder" in commands[0]
        assert "--env-file" not in commands[0] and "--force" not in commands[0]
