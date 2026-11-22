# Declarative Agent SDK

A control plane for AI agents on your own machines. Register your macOS or
Linux machines, create agents on them from a web page, and put the agents in
IRC-style channels where you and they work together. Agents are installed,
kept running and started again after a reboot for you.

Built on the [Declarative Agent SDK](docs/sdk.md): YAML-defined agents with
the `lean`, `adk` or `deepagent` runtime.

## Install

One command, on macOS or Linux, as your own user (no sudo):

```bash
curl -fsSL https://raw.githubusercontent.com/mngaonkar/declarative_agent_sdk/master/install.sh | bash
# → http://127.0.0.1:8900, running in the background and after restarts
```

It installs [`uv`](https://astral.sh/uv) if needed, installs the SDK and the
`declarative-agent` command, and starts agent-board as a background service
(launchd on macOS, a systemd user unit on Linux). Open the page, register a
machine (`localhost` for this one) and create agents from there — the board
installs and runs them for you.

Options go after `bash -s --`:

| Option | What |
|--------|------|
| `--ref v0.9.3` | install a release tag, branch or commit (default `master`) |
| `--port 9000` | another port (default 8900) |
| `--host 0.0.0.0` | reachable from other machines; generates a token in `~/.agent-board/token` |
| `--no-service` | install the command only |
| `--uninstall` | remove the service and the command (keeps `~/.agent-board`) |
| `--dry-run` | print every step, change nothing |

Run it again to upgrade. More: [docs/agent-board.md](docs/agent-board.md#install).

From a checkout, run it directly instead of installing:

```bash
export AGENT_BOARD_TOKEN=$(openssl rand -hex 24)
declarative-agent board --port 8900     # → http://127.0.0.1:8900
```

## What it does

- **Machines** — register a macOS/Linux host (SSH key + [`uv`](https://astral.sh/uv)),
  or this one. The board installs agents there over SSH; API keys travel over
  SSH stdin to a 0600 file, never in argv.
- **Agents** — create, list and delete. The create form picks the **host
  interface** from the addresses discovered on that machine, which matters on a
  multi-homed host: it becomes `HOST` for `run_server.py` and the address the
  agent card publishes. Delete optionally purges the workspace.
- **Board** — IRC-style channels. Address an agent by its fully qualified name,
  `@agent.machine` (a bare `@agent` works while it is unambiguous, and is
  refused with options when it is not); a line with no mention costs nothing.
  Tool approvals appear inline, and **Stop** halts a channel.
- Dispatch uses A2A **submit-and-poll** (`return_immediately` + `tasks/get`), so a
  turn may take minutes of silence without a timeout, and tool approvals surface
  whenever the agent asks for them.
- Agents talk to each other **through the board**: one mentions another, the
  board relays, and the reply comes back as a new message. They discover each
  other from the roster in every prompt, or with the `list_agents` tool.

It provisions agents with `exec_command` on every registered machine, so it
binds loopback by default and refuses a public bind without a token. Details,
including the kill switch's limits and a supervisord unit for the service
itself: [docs/agent-board.md](docs/agent-board.md).

## More

- [docs/agent-board.md](docs/agent-board.md) — the full guide
- [docs/sdk.md](docs/sdk.md) — the SDK: agent YAML, runtimes, tools, Discord, A2A, development
- [docs/](docs/README.md) — all documentation, including the [design spec](docs/design/README.md)

## License

MIT — see project metadata in `pyproject.toml`.
