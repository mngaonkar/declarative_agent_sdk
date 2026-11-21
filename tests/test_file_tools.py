"""
Writing files without retyping them: edit_file, write_file append, write_link,
and the loop's recovery when a tool call is too large to survive.

The incident behind all of it: a quantized model revising a 14 KB article
rewrote the whole of it in one write_file call and broke the JSON at character
13,722, ending the turn. A revision is a few small edits, a shared file can be
copied rather than retyped, and a long new file can be written in parts.
"""

from pathlib import Path
from unittest.mock import MagicMock
from urllib.parse import urlparse

import httpx
import pytest
from starlette.testclient import TestClient

from declarative_agent_sdk.agents.lean.runtime.llm import MalformedToolCall
from declarative_agent_sdk.agents.lean.runtime.loop import LeanLoop
from declarative_agent_sdk.agents.lean.runtime.skills import SkillRegistry
from declarative_agent_sdk.agents.lean.runtime.tools import LeanToolRegistry
from declarative_agent_sdk.tools.builtin import _links


@pytest.fixture
def lean(tmp_path) -> LeanToolRegistry:
    (tmp_path / "skills").mkdir()
    return LeanToolRegistry(SkillRegistry(root=str(tmp_path / "skills")),
                            workspace=str(tmp_path / "ws"))


@pytest.fixture
def ws(lean) -> Path:
    return Path(lean.workspace)


# ---------------------------------------------------------------------------
# write_file append
# ---------------------------------------------------------------------------

class TestAppend:
    def test_parts_build_one_file(self, lean, ws):
        target = str(ws / "article.md")
        lean.invoke("write_file", {"path": target, "content": "# Title\n"})
        out = lean.invoke("write_file", {"path": target, "content": "part two\n", "append": True})
        assert out.startswith("Appended")
        assert (ws / "article.md").read_text() == "# Title\npart two\n"

    def test_append_as_a_string_from_the_model(self, lean, ws):
        target = str(ws / "a.md")
        lean.invoke("write_file", {"path": target, "content": "a"})
        lean.invoke("write_file", {"path": target, "content": "b", "append": "true"})
        assert (ws / "a.md").read_text() == "ab"

    def test_without_append_it_still_replaces(self, lean, ws):
        target = str(ws / "a.md")
        lean.invoke("write_file", {"path": target, "content": "old"})
        lean.invoke("write_file", {"path": target, "content": "new"})
        assert (ws / "a.md").read_text() == "new"

    def test_append_to_a_manifest_is_checked_as_the_whole_file(self, lean, tmp_path):
        manifest = tmp_path / "skills" / "demo" / "SKILL.md"
        manifest.parent.mkdir()
        lean.invoke("write_file", {"path": str(manifest),
                                   "content": "---\nname: demo\ndescription: d\n---\nStep 1.\n"})
        # A fragment with no frontmatter of its own is fine as an append...
        out = lean.invoke("write_file", {"path": str(manifest), "content": "Step 2.\n",
                                         "append": True})
        assert "Error" not in out and "Step 2." in manifest.read_text()

    def test_append_cannot_start_a_manifest_without_frontmatter(self, lean, tmp_path):
        manifest = tmp_path / "skills" / "demo" / "SKILL.md"
        manifest.parent.mkdir()
        out = lean.invoke("write_file", {"path": str(manifest), "content": "Step 2.\n",
                                         "append": True})
        assert "invalid SKILL.md" in out and not manifest.exists()

    def test_outside_the_sandbox_is_refused(self, lean, tmp_path):
        out = lean.invoke("write_file", {"path": str(tmp_path / "elsewhere.txt"),
                                         "content": "x", "append": True})
        assert out.startswith("Error") and not (tmp_path / "elsewhere.txt").exists()


# ---------------------------------------------------------------------------
# edit_file
# ---------------------------------------------------------------------------

class TestEditFile:
    DRAFT = "# Agent orchestration\n\nIntro paragraph.\n\n## Patterns\nFan-out.\n"

    @pytest.fixture
    def draft(self, ws):
        path = ws / "draft.md"
        path.write_text(self.DRAFT)
        return path

    def test_replaces_one_passage(self, lean, draft):
        out = lean.invoke("edit_file", {"path": str(draft), "old_text": "Fan-out.",
                                        "new_text": "Fan-out and fan-in."})
        assert out.startswith("Edited") and "1 occurrence" in out
        assert draft.read_text() == self.DRAFT.replace("Fan-out.", "Fan-out and fan-in.")

    def test_not_found_says_how_to_fix_it(self, lean, draft):
        out = lean.invoke("edit_file", {"path": str(draft), "old_text": "Fan out.",
                                        "new_text": "x"})
        assert out.startswith("Error") and "read_file" in out
        assert draft.read_text() == self.DRAFT

    def test_ambiguous_needs_more_context_or_replace_all(self, lean, ws):
        path = ws / "twice.md"
        path.write_text("agent, agent")
        out = lean.invoke("edit_file", {"path": str(path), "old_text": "agent",
                                        "new_text": "bot"})
        assert "occurs 2 times" in out and path.read_text() == "agent, agent"
        out = lean.invoke("edit_file", {"path": str(path), "old_text": "agent",
                                        "new_text": "bot", "replace_all": True})
        assert "2 occurrences" in out and path.read_text() == "bot, bot"

    def test_missing_file(self, lean, ws):
        out = lean.invoke("edit_file", {"path": str(ws / "nope.md"), "old_text": "a",
                                        "new_text": "b"})
        assert "does not exist" in out

    def test_empty_old_text_points_at_append(self, lean, draft):
        out = lean.invoke("edit_file", {"path": str(draft), "old_text": "", "new_text": "x"})
        assert "append=true" in out

    def test_outside_the_sandbox_is_refused(self, lean, tmp_path):
        outside = tmp_path / "outside.md"
        outside.write_text("keep me")
        out = lean.invoke("edit_file", {"path": str(outside), "old_text": "keep",
                                        "new_text": "lose"})
        assert out.startswith("Error") and outside.read_text() == "keep me"

    def test_an_edit_that_breaks_a_manifest_is_refused(self, lean, tmp_path):
        manifest = tmp_path / "skills" / "demo" / "SKILL.md"
        manifest.parent.mkdir()
        manifest.write_text("---\nname: demo\ndescription: d\n---\nBody.\n")
        out = lean.invoke("edit_file", {"path": str(manifest), "old_text": "name: demo",
                                        "new_text": "name: other"})
        assert "invalid SKILL.md" in out and "name: demo" in manifest.read_text()


# ---------------------------------------------------------------------------
# write_link
# ---------------------------------------------------------------------------

LINK = "http://100.83.163.18:8000/files/drafts/review.md"


class FakeResponse:
    def __init__(self, status=200, body=b"", kind="text/markdown"):
        self.status_code = status
        self._body = body
        self.headers = {"content-type": kind}

    def read(self):
        return self._body

    def iter_bytes(self):
        for i in range(0, len(self._body), 4):
            yield self._body[i:i + 4]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _serve(monkeypatch, response, calls=None):
    def stream(method, url, **kwargs):
        if calls is not None:
            calls.append((url, kwargs.get("params")))
        if isinstance(response, Exception):
            raise response
        return response(url, kwargs) if callable(response) else response
    monkeypatch.setattr(_links.httpx, "stream", stream)


class TestLinks:
    @pytest.mark.parametrize("url", [
        LINK,
        "https://host/prefix/files/notes/day%201.md",
        "http://100.123.207.16:8000/files/agent-orchestration-draft-v1.md",
    ])
    def test_share_links_are_accepted(self, url):
        assert _links.is_share_link(url)

    @pytest.mark.parametrize("url", [
        "http://169.254.169.254/latest/meta-data",
        "http://host/other",
        "file:///etc/passwd",
        LINK + "?x=1",
        "http://host/files/../etc/passwd",
        "http://host/files/.env",
        "http://host/files/",
        "",
    ])
    def test_anything_else_is_refused(self, url):
        assert not _links.is_share_link(url)

    def test_other_urls_are_not_fetched(self, monkeypatch):
        calls = []
        _serve(monkeypatch, FakeResponse(body=b"x"), calls)
        with pytest.raises(_links.LinkError) as exc:
            _links.download("http://169.254.169.254/latest/meta-data")
        assert "web_request" in str(exc.value) and calls == []

    def test_too_large_is_refused_not_truncated(self, monkeypatch):
        _serve(monkeypatch, FakeResponse(body=b"x" * 100))
        with pytest.raises(_links.LinkError) as exc:
            _links.download(LINK, max_bytes=10)
        assert "larger than" in str(exc.value)

    def test_falls_back_to_the_board_relay(self, monkeypatch):
        monkeypatch.setenv("AGENTS_CONTROL_URL", "http://board:8900")
        calls = []

        def answer(url, kwargs):
            if url == LINK:
                raise httpx.ConnectError("refused")
            return FakeResponse(body=b"via board")
        _serve(monkeypatch, answer, calls)
        data, _ = _links.download(LINK)
        assert data == b"via board"
        assert calls[1] == ("http://board:8900/api/files/relay", {"url": LINK})


class TestLeanWriteLink:
    def test_saves_the_bytes_and_returns_a_receipt_not_the_file(self, lean, ws, monkeypatch):
        body = ("# Review\n" + "line\n" * 4000).encode()
        _serve(monkeypatch, FakeResponse(body=body))
        out = lean.invoke("write_link", {"url": LINK, "path": str(ws / "review.md")})
        assert (ws / "review.md").read_bytes() == body
        assert out.startswith("Saved 20,009 bytes") and "| # Review" in out
        assert len(out) < 500                      # the model never sees the file
        assert "edit_file" in out

    def test_existing_file_needs_overwrite(self, lean, ws, monkeypatch):
        (ws / "review.md").write_text("mine")
        _serve(monkeypatch, FakeResponse(body=b"theirs"))
        out = lean.invoke("write_link", {"url": LINK, "path": str(ws / "review.md")})
        assert "already exists" in out and (ws / "review.md").read_text() == "mine"
        lean.invoke("write_link", {"url": LINK, "path": str(ws / "review.md"),
                                   "overwrite": "true"})
        assert (ws / "review.md").read_text() == "theirs"

    def test_outside_the_sandbox_is_refused_before_fetching(self, lean, tmp_path, monkeypatch):
        calls = []
        _serve(monkeypatch, FakeResponse(body=b"x"), calls)
        out = lean.invoke("write_link", {"url": LINK, "path": str(tmp_path / "out.md")})
        assert out.startswith("Error") and calls == []

    def test_a_missing_file_says_so(self, lean, ws, monkeypatch):
        _serve(monkeypatch, FakeResponse(status=404, body=b"no such file"))
        out = lean.invoke("write_link", {"url": LINK, "path": str(ws / "r.md")})
        assert "404" in out and "check the path" in out and not (ws / "r.md").exists()

    def test_the_receipt_gives_the_saved_files_own_link(self, lean, ws, monkeypatch):
        from declarative_agent_sdk.core import shares
        monkeypatch.setattr(shares, "_current",
                            shares.WorkspaceFiles(str(ws), "http://100.1.1.1:8000/"))
        _serve(monkeypatch, FakeResponse(body=b"text"))
        out = lean.invoke("write_link", {"url": LINK, "path": str(ws / "copy.md")})
        assert "Link for other agents: http://100.1.1.1:8000/files/copy.md" in out

    def test_write_file_reports_the_link_too(self, lean, ws, monkeypatch):
        from declarative_agent_sdk.core import shares
        monkeypatch.setattr(shares, "_current",
                            shares.WorkspaceFiles(str(ws), "http://100.1.1.1:8000/"))
        out = lean.invoke("write_file", {"path": str(ws / "a b.md"), "content": "x"})
        assert out.endswith("Link for other agents: http://100.1.1.1:8000/files/a%20b.md")

    def test_it_is_a_lean_tool_not_the_bridged_builtin(self, lean):
        names = lean.names()
        assert names.count("write_link") == 1 and "edit_file" in names


class TestBuiltinWriteLink:
    """The adk / deepagent version, end to end against a real share link."""

    @pytest.fixture
    def served(self, tmp_path):
        from declarative_agent_sdk.agents.lean.agent import LeanAIAgent
        from declarative_agent_sdk.transports.a2a.server import AIAgentServer
        owner = tmp_path / "owner" / "workspace"
        owner.mkdir(parents=True)
        (owner / "draft.md").write_text("# Draft\nbody\n")
        agent = LeanAIAgent(
            name="owner", description="d", instruction_file=None, tools=[],
            model="m", provider="vllm", endpoint_url="http://box:1/v1",
            workspace_directory=str(owner), skills_directory=str(tmp_path / "owner" / "skills"),
        )
        server = AIAgentServer(agent, port=8124)
        return TestClient(server.app), owner

    def test_copies_a_shared_file_into_the_workspace(self, served, monkeypatch):
        from declarative_agent_sdk.core import shares
        from declarative_agent_sdk.tools.builtin.write_link import write_link
        client, owner = served
        url = shares.link_for("draft.md")
        monkeypatch.setattr(_links.httpx, "stream", lambda method, u, **kw: client.stream(
            method, urlparse(u).path))
        out = write_link(url, "copy.md")
        assert out.startswith("Saved") and (owner / "copy.md").read_text() == "# Draft\nbody\n"

    def test_paths_outside_the_workspace_are_refused(self, served):
        from declarative_agent_sdk.tools.builtin.write_link import write_link
        assert "outside your workspace" in write_link(LINK, "../escape.md")


# ---------------------------------------------------------------------------
# the loop recovers instead of ending the turn
# ---------------------------------------------------------------------------

class TestMalformedRecovery:
    def _loop(self, tmp_path, client):
        (tmp_path / "skills").mkdir(exist_ok=True)
        skills = SkillRegistry(root=str(tmp_path / "skills"))
        tools = LeanToolRegistry(skills, workspace=str(tmp_path / "ws"))
        return LeanLoop(client=client, tools=tools, skills=skills,
                        tools_approval_required=False, max_tool_iterations=8)

    def test_asks_for_smaller_calls_and_carries_on(self, tmp_path):
        client = MagicMock()
        client.chat.side_effect = [
            MalformedToolCall("broke", chars=13722),
            {"role": "assistant", "content": "Saved in parts.\n[[decision:done]]"},
        ]
        events = list(self._loop(tmp_path, client).run("write the article", "s1"))
        assert any(e.kind == "final" and "Saved in parts" in e.text for e in events)
        assert not any(e.kind == "error" for e in events)
        assert any(e.kind == "status" and "smaller pieces" in e.text for e in events)
        sent = client.chat.call_args_list[1].args[0]
        nudge = next(m["content"] for m in sent
                     if m["role"] == "user" and "[runtime]" in str(m["content"]))
        assert "13,722" in nudge and "edit_file" in nudge and "write_link" in nudge
        assert "append=true" in nudge

    def test_gives_up_after_two_recoveries(self, tmp_path):
        client = MagicMock()
        client.chat.side_effect = [MalformedToolCall("broke")] * 3
        events = list(self._loop(tmp_path, client).run("x", "s1"))
        assert events[-1].kind == "error" and "broke" in events[-1].text
        assert client.chat.call_count == 3

    def test_other_llm_errors_still_end_the_turn(self, tmp_path):
        from declarative_agent_sdk.agents.lean.runtime.llm import LLMError
        client = MagicMock()
        client.chat.side_effect = [LLMError("timed out")]
        events = list(self._loop(tmp_path, client).run("x", "s1"))
        assert events[-1].kind == "error" and client.chat.call_count == 1
