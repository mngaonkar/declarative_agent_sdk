"""The mcp_servers block (MCP-CFG-1..8). Pure parsing: no MCP library needed."""

import pytest

from declarative_agent_sdk.tools.mcp.config import McpConfigError, parse_mcp_servers


def parse(raw, env=None, workspace="/ws", base_dir=None):
    return parse_mcp_servers(raw, env or {}, workspace, base_dir=base_dir)


class TestShape:
    def test_nothing_is_no_servers(self):
        assert parse(None) == [] and parse({}) == []

    def test_a_stdio_server(self):
        (server,) = parse({"files": {"transport": "stdio", "command": "npx",
                                     "args": ["-y", "server-fs", "${WORKSPACE}"]}})
        assert server.name == "files" and server.command == "npx"
        assert server.args == ["-y", "server-fs", "/ws"]
        assert server.approval == "false" and server.tools is None
        assert server.timeout == 60 and server.startup_timeout == 30

    def test_an_http_server_with_a_token_from_env(self):
        (server,) = parse({"github": {"transport": "http", "url": "https://api.example/mcp",
                                      "headers": {"Authorization": "Bearer ${GH_TOKEN}"},
                                      "tools": ["search_code"]}},
                          env={"GH_TOKEN": "s3cret"})
        assert server.headers == {"Authorization": "Bearer s3cret"}
        assert server.tools == ["search_code"]

    def test_describe_never_shows_tokens(self):
        (server,) = parse({"github": {"transport": "http", "url": "https://api.example/mcp",
                                      "headers": {"Authorization": "Bearer ${T}"}}},
                          env={"T": "s3cret"})
        assert "s3cret" not in server.describe()

    def test_disabled_servers_are_left_out(self):
        assert parse({"x": {"transport": "stdio", "command": "a", "enabled": False}}) == []

    def test_relative_cwd_is_taken_from_the_yaml_folder(self):
        (server,) = parse({"x": {"transport": "stdio", "command": "a", "cwd": "tools"}},
                          base_dir="/agents/coder")
        assert server.cwd == "/agents/coder/tools"

    @pytest.mark.parametrize("value, expected", [
        (False, "false"), (True, "true"), ("destructive", "destructive")])
    def test_approval(self, value, expected):
        (server,) = parse({"x": {"transport": "stdio", "command": "a", "approval": value}})
        assert server.approval == expected


class TestRefused:
    @pytest.mark.parametrize("raw, words", [
        ({"Bad Name": {"transport": "stdio", "command": "a"}}, "server name"),
        ({"x": {"transport": "carrier-pigeon"}}, "transport"),
        ({"x": {"transport": "stdio"}}, "needs a command"),
        ({"x": {"transport": "http"}}, "needs an http(s) url"),
        ({"x": {"transport": "http", "url": "ftp://a"}}, "needs an http(s) url"),
        ({"x": {"transport": "stdio", "command": "a", "approval": "maybe"}}, "approval"),
        ({"x": {"transport": "stdio", "command": "a", "timeout": 0}}, "more than 0"),
        ({"x": {"transport": "stdio", "command": "a", "args": "one"}}, "list of strings"),
        ({"x": {"transport": "stdio", "command": "a", "headers": ["a"]}}, "mapping"),
        (["not", "a", "map"], "map server names"),
    ])
    def test_with_a_reason(self, raw, words):
        with pytest.raises(McpConfigError, match=words.replace("(", r"\(").replace(")", r"\)")):
            parse(raw)

    def test_a_name_yaml_turned_into_a_boolean(self):
        import yaml
        raw = yaml.safe_load("off:\n  transport: stdio\n  command: a\n")
        with pytest.raises(McpConfigError, match="put the name in quotes"):
            parse(raw)

    def test_an_unset_variable_names_itself(self):
        with pytest.raises(McpConfigError, match=r"\$\{GH_TOKEN\}.*add GH_TOKEN"):
            parse({"github": {"transport": "http", "url": "https://a",
                              "headers": {"Authorization": "Bearer ${GH_TOKEN}"}}})


def test_without_the_extra_the_message_says_what_to_install(monkeypatch):
    """D2: declaring servers without the mcp extra fails with the fix."""
    import sys
    from declarative_agent_sdk.tools.mcp import McpError, McpHub
    monkeypatch.setitem(sys.modules, "mcp", None)        # as if not installed
    (server,) = parse({"x": {"transport": "stdio", "command": "a"}})
    with pytest.raises(McpError, match=r"declarative-agent-sdk\[mcp\]"):
        McpHub([server], "/ws").start()
