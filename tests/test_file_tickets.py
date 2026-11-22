"""
Streaming a video through the board.

A <video> fetches its own URL and cannot send the Authorization header, so the
page asks for a ticket for one link and the player streams the relay with it,
by byte ranges.
"""

import httpx
import pytest
from starlette.testclient import TestClient

import declarative_agent_sdk.agent_board.server as server_module
from declarative_agent_sdk.agent_board.server import (
    TICKET_TTL_SECONDS, ControlPlane, check_ticket, make_ticket,
)

AUTH = {"Authorization": "Bearer t0ken"}
LINK = "http://100.83.163.18:8000/files/clips/helix%20nebula.mp4"
OTHER = "http://100.83.163.18:8000/files/notes/secret.md"


@pytest.fixture
def plane(tmp_path) -> ControlPlane:
    plane = ControlPlane(db_path=str(tmp_path / "t.db"), token="t0ken",
                         public_url="http://127.0.0.1:8900")
    machine = plane.store.add_machine("omarchy", "100.83.163.18")
    plane.store.upsert_agent(machine["id"], {"name": "artist", "url": "http://100.83.163.18:8000/"})
    return plane


@pytest.fixture
def client(plane) -> TestClient:
    return TestClient(plane.app)


@pytest.fixture
def upstream(monkeypatch):
    """The owning agent: a 1000-byte video that answers Range requests."""
    seen = []
    data = bytes(range(250)) * 4

    def handler(request):
        seen.append(dict(request.headers))
        wanted = request.headers.get("range", "")
        if wanted.startswith("bytes="):
            start, _, end = wanted[6:].partition("-")
            start, end = int(start), int(end or len(data) - 1)
            return httpx.Response(206, content=data[start:end + 1], headers={
                "content-type": "video/mp4", "accept-ranges": "bytes",
                "content-range": f"bytes {start}-{end}/{len(data)}"})
        return httpx.Response(200, content=data, headers={
            "content-type": "video/mp4", "accept-ranges": "bytes"})

    original = server_module.httpx.AsyncClient
    monkeypatch.setattr(server_module.httpx, "AsyncClient",
                        lambda **kw: original(transport=httpx.MockTransport(handler)))
    return seen


def _ticket(client, link=LINK):
    reply = client.post("/api/files/ticket", json={"url": link}, headers=AUTH)
    assert reply.status_code == 200, reply.text
    return reply.json()


class TestTicketValue:
    def test_good_for_its_link_until_it_expires(self):
        key = b"k" * 32
        ticket = make_ticket(key, LINK, now=1000)
        assert check_ticket(key, ticket, LINK, now=1000 + TICKET_TTL_SECONDS)
        assert not check_ticket(key, ticket, LINK, now=1001 + TICKET_TTL_SECONDS)

    def test_no_good_for_another_link_or_key(self):
        ticket = make_ticket(b"k" * 32, LINK, now=1000)
        assert not check_ticket(b"k" * 32, ticket, OTHER, now=1000)
        assert not check_ticket(b"x" * 32, ticket, LINK, now=1000)

    @pytest.mark.parametrize("bad", ["", "abc", "123", "123.", ".abc", "x.y"])
    def test_garbage_is_refused(self, bad):
        assert not check_ticket(b"k" * 32, bad, LINK, now=0)


class TestStreaming:
    def test_asking_for_a_ticket_needs_the_token(self, client):
        assert client.post("/api/files/ticket", json={"url": LINK}).status_code == 401

    def test_a_ticket_is_only_for_a_relayable_link(self, client):
        for bad in ("http://169.254.169.254/files/x.mp4", "http://100.83.163.18:8000/files/.env"):
            reply = client.post("/api/files/ticket", json={"url": bad}, headers=AUTH)
            assert reply.status_code in (400, 404), bad

    def test_the_ticket_streams_byte_ranges_without_the_token(self, client, upstream):
        src = _ticket(client)["src"]
        assert "t0ken" not in src                  # the board token never goes in a URL
        reply = client.get(src, headers={"Range": "bytes=100-199"})
        assert reply.status_code == 206
        assert reply.headers["content-range"] == "bytes 100-199/1000"
        assert reply.headers["accept-ranges"] == "bytes"
        assert reply.content == (bytes(range(250)) * 4)[100:200]
        assert upstream[-1]["range"] == "bytes=100-199"     # passed to the owner

    def test_a_whole_file_still_works(self, client, upstream):
        reply = client.get(_ticket(client)["src"])
        assert reply.status_code == 200 and len(reply.content) == 1000

    def test_a_ticket_opens_only_its_own_link(self, client, upstream):
        ticket = _ticket(client)["ticket"]
        reply = client.get("/api/files/relay", params={"url": OTHER, "ticket": ticket})
        assert reply.status_code == 401

    def test_a_ticket_opens_nothing_else_on_the_board(self, client):
        ticket = _ticket(client)["ticket"]
        assert client.get("/api/state", params={"url": LINK, "ticket": ticket}).status_code == 401

    def test_an_expired_ticket_is_refused(self, client, plane, upstream):
        old = make_ticket(plane.ticket_key, LINK, now=0)
        reply = client.get("/api/files/relay", params={"url": LINK, "ticket": old})
        assert reply.status_code == 401

    def test_a_restart_ends_every_ticket(self, client, tmp_path, upstream):
        src = _ticket(client)["src"]
        again = ControlPlane(db_path=str(tmp_path / "t.db"), token="t0ken")
        assert TestClient(again.app).get(src).status_code == 401
