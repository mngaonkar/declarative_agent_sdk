"""
An agent can only listen on an address its own machine has.

The incident: agent 'omarchy' ran on the Mac but was configured to bind
100.83.163.18 — the Omarchy box's address. It started, failed to bind, and
supervisord retried it into BACKOFF. install.sh reported it installed and the
board marked it ready; every Reinstall passed the same address back.
"""

import pytest
from starlette.testclient import TestClient

from declarative_agent_sdk.agent_board.server import ControlPlane, _nearest_address

AUTH = {"Authorization": "Bearer t0ken"}

MAC = [
    {"name": "en0", "address": "10.0.0.170", "family": "ipv4", "note": ""},
    {"name": "utun9", "address": "100.100.133.22", "family": "ipv4", "note": ""},
]

YAML = """name: omarchy
description: d
agent_framework: lean
provider: vllm
model: m
endpoint:
  url: http://box:30081/v1
"""


@pytest.fixture
def plane(tmp_path) -> ControlPlane:
    return ControlPlane(db_path=str(tmp_path / "api.db"), token="t0ken",
                        public_url="http://127.0.0.1:8900")


@pytest.fixture
def client(plane) -> TestClient:
    return TestClient(plane.app)


@pytest.fixture
def mac(plane, monkeypatch):
    machine = plane.store.add_machine("MacBook-pro", "localhost", is_local=True)
    probes = []

    def probe(m):
        probes.append(m["name"])
        return {"ok": True, "uv": True, "os": "Darwin", "interfaces": MAC}

    monkeypatch.setattr(plane.driver, "probe", probe)
    machine["probes"] = probes
    return machine


class TestNearestAddress:
    def test_same_network_wins(self):
        assert _nearest_address(["10.0.0.170", "100.100.133.22"], "100.83.163.18") \
            == "100.100.133.22"

    def test_no_match_is_empty(self):
        assert _nearest_address(["10.0.0.170"], "100.83.163.18") == ""
        assert _nearest_address(["10.0.0.170"], "not-an-ip") == ""


class TestCreate:
    def test_another_machines_address_is_refused(self, client, mac):
        reply = client.post("/api/agents", json={
            "machine_id": mac["id"], "name": "omarchy", "bind_host": "100.83.163.18",
        }, headers=AUTH)
        assert reply.status_code == 400
        error = reply.json()["error"]
        assert "100.83.163.18 is not an address of 'MacBook-pro'" in error
        assert "10.0.0.170" in error and "100.100.133.22" in error

    def test_the_list_is_reread_before_refusing(self, client, plane, mac, monkeypatch):
        # Recorded list is stale (an old DHCP lease); the fresh read has it.
        plane.store.set_machine_status(mac["id"], "ready", "", "Darwin",
                                       [{"name": "en0", "address": "172.20.20.20",
                                         "family": "ipv4", "note": ""}])
        calls = {}
        monkeypatch.setattr(plane.driver, "create_agent",
                            lambda m, **kw: calls.update(kw) or {
                                "name": "a", "ready": True, "url": "http://10.0.0.170:8001/"})
        reply = client.post("/api/agents", json={
            "machine_id": mac["id"], "name": "a", "bind_host": "10.0.0.170",
        }, headers=AUTH)
        assert reply.status_code == 200, reply.text
        assert mac["probes"] and calls["bind_host"] == "10.0.0.170"

    @pytest.mark.parametrize("host", ["0.0.0.0", "127.0.0.1", "my-host.local", ""])
    def test_wildcards_and_hostnames_are_left_to_install_sh(self, client, plane, mac,
                                                            monkeypatch, host):
        monkeypatch.setattr(plane.driver, "create_agent", lambda m, **kw: {
            "name": "a", "ready": True, "url": "http://10.0.0.170:8001/"})
        reply = client.post("/api/agents", json={
            "machine_id": mac["id"], "name": "a", "bind_host": host,
        }, headers=AUTH)
        assert reply.status_code == 200, reply.text


class TestReinstallRepairsTheAddress:
    @pytest.fixture
    def agent(self, plane, mac, tmp_path, monkeypatch):
        prefix = tmp_path / "omarchy"
        prefix.mkdir()
        (prefix / "agent.yaml").write_text(YAML)
        (prefix / "instructions.md").write_text("# omarchy\n")
        monkeypatch.setattr(plane.driver, "set_env_var", lambda *a, **k: None)
        return plane.store.upsert_agent(mac["id"], {
            "name": "omarchy", "prefix": str(prefix), "url": "http://100.83.163.18:8001/"})

    def _install(self, plane, monkeypatch, network):
        calls = {}
        monkeypatch.setattr(plane.driver, "agent_network", lambda *a, **k: network)

        def install(machine, **kw):
            calls.update(kw)
            return {"name": kw["name"], "prefix": kw["prefix"], "ready": True,
                    "url": f"http://{kw['advertise_host'] or '10.0.0.170'}:8001/", "port": 8001}

        monkeypatch.setattr(plane.driver, "create_agent", install)
        return calls

    def test_a_foreign_bind_address_is_replaced(self, client, plane, agent, monkeypatch):
        calls = self._install(plane, monkeypatch, {
            "ok": True, "bind_host": "100.83.163.18", "advertise_host": "100.83.163.18"})
        reply = client.post(f"/api/agents/{agent['id']}/reinstall", json={},
                            headers=AUTH).json()
        assert calls["bind_host"] == "100.100.133.22"         # same (tailnet) network
        assert calls["advertise_host"] == "100.100.133.22"
        assert "rebound to 100.100.133.22" in reply["address_fixed"]
        assert reply["kept_address"] is False
        assert plane.store.agent(agent["id"])["url"] == "http://100.100.133.22:8001/"

    def test_with_no_similar_address_it_binds_everything(self, client, plane, agent,
                                                         monkeypatch):
        calls = self._install(plane, monkeypatch, {
            "ok": True, "bind_host": "192.168.9.9", "advertise_host": "192.168.9.9"})
        reply = client.post(f"/api/agents/{agent['id']}/reinstall", json={},
                            headers=AUTH).json()
        assert calls["bind_host"] == "0.0.0.0" and calls["advertise_host"] == ""
        assert "all interfaces" in reply["address_fixed"]

    def test_a_deliberate_advertise_address_is_kept(self, client, plane, agent, monkeypatch):
        # Published address differs from the bind on purpose (NAT, a proxy).
        calls = self._install(plane, monkeypatch, {
            "ok": True, "bind_host": "100.83.163.18", "advertise_host": "agents.example.com"})
        client.post(f"/api/agents/{agent['id']}/reinstall", json={}, headers=AUTH)
        assert calls["advertise_host"] == "agents.example.com"

    def test_a_good_address_is_kept_without_rescanning(self, client, plane, agent, mac,
                                                       monkeypatch):
        plane.store.set_machine_status(mac["id"], "ready", "", "Darwin", MAC)
        mac["probes"].clear()
        calls = self._install(plane, monkeypatch, {
            "ok": True, "bind_host": "100.100.133.22", "advertise_host": "100.100.133.22"})
        reply = client.post(f"/api/agents/{agent['id']}/reinstall", json={},
                            headers=AUTH).json()
        assert calls["bind_host"] == "100.100.133.22" and reply["address_fixed"] == ""
        assert reply["kept_address"] is True and mac["probes"] == []
