# Design specification

This folder describes what the code does, file by file, in plain English. It
is written to be kept up to date and to be pasted into a prompt when asking
for a code change.

## How it maps to the code

- Every source file has **exactly one** section in one spec file. The section
  heading is the file's path in backticks, for example
  ``## `core/agent_factory.py` ``.
- A spec file groups the files of one area (one package or one feature).
- `tests/test_design_spec.py` checks the mapping: it fails when a source file
  has no section, has two, or a section names a file that no longer exists.

| Area | Spec | Code |
|---|---|---|
| Package root, factory, events, registry, logging, file links | [core.md](core.md) | `__init__.py`, `__version__.py`, `core/`, `models/` |
| The agent YAML file | [agent-yaml.md](agent-yaml.md) | read by `core/agent_config.py`, `core/agent_factory.py` |
| Lean runtime | [runtime-lean.md](runtime-lean.md) | `agents/lean/` |
| Google ADK runtime | [runtime-adk.md](runtime-adk.md) | `agents/adk/` |
| Deepagents runtime | [runtime-deepagent.md](runtime-deepagent.md) | `agents/deepagent/` |
| Tools and skills | [tools.md](tools.md) | `tools/` |
| MCP tools (phase 1: lean) | [mcp.md](mcp.md) | `tools/mcp/` |
| A2A transport | [transport-a2a.md](transport-a2a.md) | `transports/a2a/` |
| Discord transport | [transport-discord.md](transport-discord.md) | `transports/discord/` |
| Workflows | [workflows.md](workflows.md) | `workflows/` |
| Command line | [cli.md](cli.md) | `cli/` |
| Installing agents on a machine | [deploy.md](deploy.md) | `packaging/deploy/`, Docker files, run scripts |
| agent-board (the board) | [agent-board/](agent-board/README.md) | `agent_board/` |
| A2UI web client | [a2ui.md](a2ui.md) | `a2ui/` |

## How to read a section

Each file section has the same parts. Leave out a part that has nothing in it.

- **Does:** what the file is responsible for, in one or two sentences.
- **Rules:** numbered behaviour the code must keep. Each rule has an ID such
  as `LEAN-LOOP-4`, so a prompt or a review can point at it.
- **Interface:** the functions, classes, routes or flags other code uses.
- **Tests:** where the rules are checked.

Rules say *what* and *why*, not *how*. Write them so a reader can tell whether
the code obeys them.

## Using it for a code change

1. Find the spec section for the file you want to change.
2. Write the change as an edit to the spec first: add, change or remove rules.
3. Paste the edited section into the prompt with: "Change the code so it
   matches this spec. Keep every rule not mentioned. Update the tests."
4. Commit the spec edit together with the code.

Example prompt:

> In `docs/design/agent-board/router.md`, rule ROUTER-RELAY-6 is new:
> "When an agent was asked by another agent and its reply mentions nobody,
> the board sends the reply back to the agent that asked." Change
> `agent_board/router.py` to follow it, keep the other rules, and add a
> test.

## Keeping it in sync

- New source file → add a section for it in the spec of its area.
- Deleted or renamed file → remove or rename its section.
- Behaviour change → change the rule; do not leave a rule the code no longer
  follows.
- Rule IDs are never reused. When a rule goes away, delete it; do not
  renumber the others.

## Terms

- **Agent** — one configured AI agent: an `agent.yaml`, instructions, skills
  and a workspace, run by one of three runtimes.
- **Runtime** (`agent_framework`) — the engine that runs the agent loop:
  `lean`, `adk` or `deepagent`.
- **Transport** — how an agent is reached: A2A (HTTP JSON-RPC) or Discord.
- **Board** — agent-board: the service that installs agents on machines
  and lets them talk in channels.
- **Machine** — a computer the board reaches over SSH (or the local one) and
  installs agents on.
- **Core tools** — tools every agent gets without listing them.
