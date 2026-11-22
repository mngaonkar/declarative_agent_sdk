"""
The board's MCP catalog (MCP-CAT-1..12): define a server once, attach it to
agents, and the board writes it into their agent.yaml under board_mcp_servers.
"""

import pytest
import yaml
from starlette.testclient import TestClient

from declarative_agent_sdk.agent_board import mcp_catalog
from declarative_agent_sdk.agent_board.server import ControlPlane
from declarative_agent_sdk.core.agent_config import parse_common_config

AUTH = {"Authorization": "Bearer t0ken"}
YAML = """# coder, set up by hand
name: {name}
agent_framework: lean
provider: vllm
model: m
mcp_servers:
  mine:
    transport: stdio
    command: my-server
"""
FETCH = {"transport": "stdio", "command": "uvx", "args": ["mcp-server-fetch"]}
GITHUB = {"transport": "http", "url": "https://api.example/mcp",
          "headers": {"Authorization": "Bearer ${GITHUB_TOKEN}"}}


class FakeDriver:
    """Agent files in memory; restarts and .env writes recorded (names only)."""

    def __init__(self):
        self.files = {}
        self.env = {}
        self.restarts = []
        self.env_writes = []

    def read_agent_files(self, machine, prefix, timeout=60):
        return {"yaml": self.files[prefix], "instructions": ""}

    def write_agent_files(self, machine, prefix, yaml_text=None, instructions=None, timeout=120):
        if yaml_text is not None:
            self.files[prefix] = yaml_text
        return {"written": ["agent.yaml"]}

    def supervisor_action(self, machine, prefix, name, action):
        self.restarts.append(name)
        return {"ok": True, "output": f"{name}: started"}

    def env_names(self, machine, prefix, timeout=60):
        return sorted(self.env.get(prefix, {}))

    def set_env_var(self, machine, prefix, name, value, timeout=60):
        self.env.setdefault(prefix, {})[name] = value
        self.env_writes.append((prefix, name))


@pytest.fixture
def board(tmp_path):
    plane = ControlPlane(db_path=str(tmp_path / "b.db"), token="t0ken")
    fake = FakeDriver()
    plane.driver = fake
    machine = plane.store.add_machine("box", "localhost", is_local=True)
    plane.store.set_machine_status(machine["id"], "ready", "", "Linux", [], ["uvx"])
    agents = []
    for name in ("coder", "scout"):
        prefix = f"/agents/{name}"
        fake.files[prefix] = YAML.format(name=name)
        agents.append(plane.store.upsert_agent(machine["id"], {"name": name, "prefix": prefix}))
    # create_app captured the driver; rebuild so the routes use the fake.
    from declarative_agent_sdk.agent_board.server import create_app
    plane.app = create_app(plane)
    return plane, fake, TestClient(plane.app), agents


def add(client, name, spec):
    reply = client.post("/api/mcp", json={"name": name, "spec": spec}, headers=AUTH)
    assert reply.status_code == 200, reply.text
    return reply.json()["server"]


def attach(client, agent, server, overrides=None):
    reply = client.post(f"/api/agents/{agent['id']}/mcp",
                        json={"server_id": server["id"], "overrides": overrides or {}},
                        headers=AUTH)
    assert reply.status_code == 200, reply.text
    return reply.json()


def board_key(fake, agent):
    return yaml.safe_load(fake.files[agent["prefix"]]).get("board_mcp_servers")


class TestCatalog:
    def test_an_entry_is_checked_by_the_mcp_parser(self, board):
        _, _, client, _ = board
        reply = client.post("/api/mcp", json={"name": "bad", "spec": {"transport": "stdio"}},
                            headers=AUTH)
        assert reply.status_code == 400 and "needs a command" in reply.json()["error"]
        reply = client.post("/api/mcp", json={"name": "Bad Name", "spec": FETCH}, headers=AUTH)
        assert reply.status_code == 400

    def test_names_are_unique(self, board):
        _, _, client, _ = board
        add(client, "fetch", FETCH)
        reply = client.post("/api/mcp", json={"name": "fetch", "spec": FETCH}, headers=AUTH)
        assert reply.status_code == 400 and "already" in reply.json()["error"]

    def test_the_list_says_what_each_entry_needs(self, board):
        _, _, client, _ = board
        add(client, "github", GITHUB)
        add(client, "fetch", FETCH)
        body = client.get("/api/mcp", headers=AUTH).json()
        by_name = {s["name"]: s for s in body["servers"]}
        assert by_name["github"]["vars"] == ["GITHUB_TOKEN"]
        assert by_name["fetch"]["runner"] == "uvx"
        assert {p["name"] for p in body["presets"]} >= {"fetch", "github", "filesystem"}

    def test_every_preset_is_a_valid_entry(self):
        for preset in mcp_catalog.PRESETS:
            mcp_catalog.check(preset["name"], mcp_catalog.clean_spec(preset["spec"]))


class TestAttaching:
    def test_attach_writes_board_mcp_servers_and_restarts(self, board):
        _, fake, client, (coder, _) = board
        server = add(client, "fetch", FETCH)
        result = attach(client, coder, server, {"approval": "true"})
        assert result["synced"]["changed"] and fake.restarts == ["coder"]
        assert board_key(fake, coder) == {"fetch": {**FETCH, "approval": "true"}}

    def test_hand_written_servers_and_comments_are_kept(self, board):
        _, fake, client, (coder, _) = board
        attach(client, coder, add(client, "fetch", FETCH))
        text = fake.files[coder["prefix"]]
        assert text.startswith("# coder, set up by hand")
        assert yaml.safe_load(text)["mcp_servers"] == {
            "mine": {"transport": "stdio", "command": "my-server"}}

    def test_the_agent_reads_both_and_its_own_wins(self, board):
        _, fake, client, (coder, _) = board
        attach(client, coder, add(client, "fetch", FETCH))
        attach(client, coder, add(client, "mine", GITHUB))   # clashes with hand-written
        common = parse_common_config(yaml.safe_load(fake.files[coder["prefix"]]), framework="lean")
        assert set(common.mcp_servers) == {"fetch", "mine"}
        assert common.mcp_servers["mine"]["command"] == "my-server"

    def test_attach_warns_about_what_will_not_work(self, board):
        plane, fake, client, (coder, _) = board
        warnings = attach(client, coder, add(client, "github", GITHUB))["warnings"]
        assert any("GITHUB_TOKEN not set" in w for w in warnings)
        warnings = attach(client, coder, add(client, "fs", {
            "transport": "stdio", "command": "npx", "args": ["fs"]}))["warnings"]
        assert any("npx was not found on box" in w for w in warnings)
        warnings = attach(client, coder, add(client, "mine", FETCH))["warnings"]
        assert any("hand-written 'mine'" in w for w in warnings)

    def test_an_unchanged_agent_is_not_restarted(self, board):
        _, fake, client, (coder, _) = board
        server = add(client, "fetch", FETCH)
        attach(client, coder, server)
        fake.restarts.clear()
        assert attach(client, coder, server)["synced"]["changed"] is False
        assert fake.restarts == []

    def test_detach_removes_the_key_when_nothing_is_left(self, board):
        _, fake, client, (coder, _) = board
        server = add(client, "fetch", FETCH)
        attach(client, coder, server)
        client.delete(f"/api/agents/{coder['id']}/mcp/{server['id']}", headers=AUTH)
        assert board_key(fake, coder) is None
        assert "mine" in yaml.safe_load(fake.files[coder["prefix"]])["mcp_servers"]

    def test_bad_overrides_are_refused(self, board):
        _, _, client, (coder, _) = board
        server = add(client, "fetch", FETCH)
        reply = client.post(f"/api/agents/{coder['id']}/mcp",
                            json={"server_id": server["id"], "overrides": {"approval": "maybe"}},
                            headers=AUTH)
        assert reply.status_code == 400

    def test_the_agents_view_lists_attached_and_catalog(self, board):
        _, _, client, (coder, _) = board
        server = add(client, "fetch", FETCH)
        attach(client, coder, server, {"tools": "fetch"})
        body = client.get(f"/api/agents/{coder['id']}/mcp", headers=AUTH).json()
        assert body["attached"] == [{"server_id": server["id"], "name": "fetch",
                                     "overrides": {"tools": ["fetch"]}}]
        assert body["catalog"][0]["name"] == "fetch"


class TestEditing:
    def test_edit_with_sync_updates_every_attached_agent(self, board):
        _, fake, client, (coder, scout) = board
        server = add(client, "fetch", FETCH)
        attach(client, coder, server)
        attach(client, scout, server, {"approval": "true"})
        fake.restarts.clear()
        reply = client.put(f"/api/mcp/{server['id']}", json={
            "name": "fetch", "spec": {**FETCH, "timeout": 30}, "sync": True}, headers=AUTH)
        assert sorted(fake.restarts) == ["coder", "scout"]
        assert board_key(fake, coder)["fetch"]["timeout"] == 30
        assert board_key(fake, scout)["fetch"]["approval"] == "true"     # override kept
        assert len(reply.json()["synced"]) == 2

    def test_edit_without_sync_changes_only_the_catalog(self, board):
        _, fake, client, (coder, _) = board
        server = add(client, "fetch", FETCH)
        attach(client, coder, server)
        fake.restarts.clear()
        client.put(f"/api/mcp/{server['id']}", json={
            "name": "fetch", "spec": {**FETCH, "timeout": 30}}, headers=AUTH)
        assert fake.restarts == [] and "timeout" not in board_key(fake, coder)["fetch"]

    def test_delete_detaches_from_every_agent(self, board):
        plane, fake, client, (coder, scout) = board
        server = add(client, "fetch", FETCH)
        attach(client, coder, server)
        attach(client, scout, server)
        client.delete(f"/api/mcp/{server['id']}", headers=AUTH)
        assert board_key(fake, coder) is None and board_key(fake, scout) is None
        assert plane.store.mcp_servers() == []


class TestSecrets:
    def test_a_value_goes_to_every_attached_agents_env_and_nowhere_else(self, board):
        plane, fake, client, (coder, scout) = board
        server = add(client, "github", GITHUB)
        attach(client, coder, server)
        attach(client, scout, server)
        fake.restarts.clear()
        reply = client.post(f"/api/mcp/{server['id']}/secret",
                            json={"name": "GITHUB_TOKEN", "value": "ghp_s3cret"}, headers=AUTH)
        assert reply.status_code == 200 and "ghp_s3cret" not in reply.text
        assert fake.env == {"/agents/coder": {"GITHUB_TOKEN": "ghp_s3cret"},
                            "/agents/scout": {"GITHUB_TOKEN": "ghp_s3cret"}}
        assert sorted(fake.restarts) == ["coder", "scout"]
        # Not in the catalog, the agent records or the agents' YAML.
        assert "ghp_s3cret" not in str(plane.store.mcp_servers())
        assert all("ghp_s3cret" not in text for text in fake.files.values())

    def test_only_names_the_entry_uses(self, board):
        _, fake, client, (coder, _) = board
        server = add(client, "github", GITHUB)
        attach(client, coder, server)
        reply = client.post(f"/api/mcp/{server['id']}/secret",
                            json={"name": "OPENAI_API_KEY", "value": "x"}, headers=AUTH)
        assert reply.status_code == 400 and fake.env == {}


class TestRender:
    def test_overrides_replace_tools_and_approval_only(self):
        entry = mcp_catalog.render({**FETCH, "tools": ["a", "b"], "approval": "false"},
                                   {"tools": ["a"], "approval": "destructive"})
        assert entry == {**FETCH, "tools": ["a"], "approval": "destructive"}

    def test_empty_overrides_keep_the_entry(self):
        assert mcp_catalog.render(FETCH, {"tools": [], "approval": ""}) == FETCH

    def test_rendering_does_not_change_the_catalog_entry(self):
        spec = {**FETCH, "tools": ["a"]}
        mcp_catalog.render(spec, {"tools": ["b"]})
        assert spec["tools"] == ["a"]
