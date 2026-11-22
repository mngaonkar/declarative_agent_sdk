"""
The three runtimes over one MCP hub (D1): the same tools, called the same way,
with the same approval rule (MCP-RT-1..3, MCP-APPR-1).
"""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("mcp", reason="the mcp extra is not installed")

from declarative_agent_sdk.core.agent_factory import AgentFactory  # noqa: E402

FIXTURE = str(Path(__file__).resolve().parent / "mcp_server_fixture.py")
FIXTURE_TOOLS = {"fx__echo", "fx__add", "fx__picture", "fx__wipe", "fx__slow",
                 "fx__broken", "fx__die"}
RUNTIMES = ("lean", "adk", "deepagent")


def build(tmp_path, framework, approval="false", agent_approval=True):
    (tmp_path / "agent.yaml").write_text(f"""
name: mcp_{framework}
agent_framework: {framework}
provider: openai
model: gpt-4o-mini
endpoint: {{url: "http://127.0.0.1:9/v1"}}
workspace_directory: ws
skills_directory: skills
tools_approval_required: {str(agent_approval).lower()}
mcp_servers:
  fx:
    transport: stdio
    command: {sys.executable}
    args: ["{FIXTURE}"]
    approval: {approval}
""")
    return AgentFactory.from_yaml_file(str(tmp_path / "agent.yaml"))


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")    # building, never calling


@pytest.fixture
def made(tmp_path):
    agents = []

    def make(*args, **kwargs):
        folder = tmp_path / f"agent{len(agents)}"
        folder.mkdir()
        agents.append(build(folder, *args, **kwargs))
        return agents[-1]

    yield make
    for agent in agents:
        if agent.mcp is not None:
            agent.mcp.close()


class _Ctx:
    """The part of ADK's ToolContext an MCP tool uses."""

    def __init__(self, confirmed=None):
        self.tool_confirmation = None if confirmed is None else SimpleNamespace(confirmed=confirmed)
        self.actions = SimpleNamespace(skip_summarization=False)
        self.asked = False

    def request_confirmation(self, hint):
        self.asked = True


def mcp_tools(agent, framework):
    """name -> runtime tool object, for the MCP tools only."""
    if framework == "lean":
        return {n: n for n in agent._tools.names() if n.startswith("fx__")}
    if framework == "adk":
        return {t.name: t for t in agent._adk_agent.tools
                if getattr(t, "name", "").startswith("fx__")}
    from declarative_agent_sdk.tools.mcp import langchain_tools
    tools, _ = langchain_tools(agent.mcp, set())
    return {t.name: t for t in tools}


def call(agent, framework, name, args):
    if framework == "lean":
        return agent._tools.invoke(name, args)
    tool = mcp_tools(agent, framework)[name]
    if framework == "adk":
        return asyncio.run(tool.run_async(args=args, tool_context=_Ctx()))["result"]
    return asyncio.run(tool.ainvoke(args))


def asks(agent, framework):
    """The MCP tools this agent would stop to ask about."""
    if framework == "lean":
        return {n for n in mcp_tools(agent, framework) if agent._loop._needs_approval(n)}
    if framework == "adk":
        # Asks before running: an unconfirmed call never reaches the server.
        found = set()
        for name, tool in mcp_tools(agent, framework).items():
            context = _Ctx()
            if tool._confirm:
                asyncio.run(tool.run_async(args={}, tool_context=context))
                assert context.asked, name
                found.add(name)
        return found
    return {n for n in agent.interrupt_on if n.startswith("fx__")}


@pytest.mark.parametrize("framework", RUNTIMES)
def test_every_runtime_gets_the_same_tools(made, framework):
    assert set(mcp_tools(made(framework), framework)) == FIXTURE_TOOLS


@pytest.mark.parametrize("framework", RUNTIMES)
def test_every_runtime_calls_them(made, framework):
    agent = made(framework)
    assert call(agent, framework, "fx__add", {"a": 40, "b": 2}) == "42"
    assert call(agent, framework, "fx__echo", {"text": "hi"}) == "echo: hi"


@pytest.mark.parametrize("framework", RUNTIMES)
@pytest.mark.parametrize("approval, expected", [
    ("false", set()),
    ("destructive", {"fx__wipe"}),
    ("true", FIXTURE_TOOLS),
])
def test_approval_is_the_same_everywhere(made, framework, approval, expected):
    assert asks(made(framework, approval=approval), framework) == expected


@pytest.mark.parametrize("framework", RUNTIMES)
def test_agent_wide_no_approval_wins(made, framework):
    assert asks(made(framework, approval="true", agent_approval=False), framework) == set()


def test_adk_runs_a_confirmed_call_and_refuses_a_rejected_one(made):
    agent = made("adk", approval="true")
    wipe = mcp_tools(agent, "adk")["fx__wipe"]
    done = asyncio.run(wipe.run_async(args={"target": "x"}, tool_context=_Ctx(confirmed=True)))
    refused = asyncio.run(wipe.run_async(args={"target": "x"}, tool_context=_Ctx(confirmed=False)))
    assert done == {"result": "wiped x"} and refused == {"error": "This tool call is rejected."}


def test_adk_declares_the_servers_schema(made):
    from google.adk.models.lite_llm import _function_declaration_to_tool_param
    add = mcp_tools(made("adk"), "adk")["fx__add"]
    tool = _function_declaration_to_tool_param(add._get_declaration())
    assert tool["function"]["name"] == "fx__add"
    assert tool["function"]["parameters"]["required"] == ["a", "b"]
