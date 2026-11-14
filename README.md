# Declarative Agent SDK

YAML-first agents with **three peer runtimes**, shared transports (Discord, A2A), progressive skills, and tool approval — without nesting frameworks.

> **TL;DR:** Define an agent in YAML, pick `lean` (default), `adk`, or `deepagent` as the **end-to-end** loop, then serve it over Discord or A2A. Same `BaseAgent` / `AgentEvent` contract for every runtime.

See [ARCHITECTURE.md](ARCHITECTURE.md) for detailed flows and package layout.

---

## Features

| Feature | Description |
|---------|-------------|
| **Peer runtimes** | `lean` · `adk` · `deepagent` — each owns its loop end-to-end (no mixing) |
| **Declarative YAML** | Name, model, tools, skills, approval, workspace |
| **Progressive skills** | Lean: L1 catalog → L2 body → L3 scripts (ESP-style) |
| **Built-in tools** | `web_request`, `tavily_search`, `exec_command`, files, lean FS tools |
| **Tool approval** | Human-in-the-loop (Discord ✅/❌ or A2A) |
| **Transports** | Discord bot, A2A / JSON-RPC server |
| **Background agents** | `install.sh` provisions A2A agents under supervisord, no root needed |
| **Multi-agent workflows** | Optional LangGraph workflows via YAML |

---

## Install

Not published to PyPI yet. Install from a published release, from git, or from a
checkout. Releases are at
[/releases](https://github.com/mngaonkar/declarative_agent_sdk/releases) and
carry a wheel, an sdist and `SHA256SUMS`.

**Use `uv`, not `pip`.** This is ~30 direct dependencies with loose lower
bounds, and pip's resolver does not finish: it backtracks for several minutes
and exits with `error: resolution-too-deep` (reproduced on Arch with Python
3.14.7 and pip 26.3). uv resolves the same graph in seconds, and it fetches
Python 3.14 itself if the host does not have it.

```bash
# From a published release — no checkout, nothing to download by hand
uv venv --python 3.14 .venv
uv pip install --python .venv/bin/python \
  "declarative-agent-sdk[discord] @ https://github.com/mngaonkar/declarative_agent_sdk/releases/download/v0.9.2/declarative_agent_sdk-0.9.2-py3-none-any.whl"
.venv/bin/declarative-agent version

# From a wheel already on disk — one wheel works on every platform (py3-none-any)
uv pip install --python .venv/bin/python "./declarative_agent_sdk-0.9.2-py3-none-any.whl[discord]"

# From git, pinned to a tag
uv pip install --python .venv/bin/python \
  "declarative-agent-sdk[discord] @ git+https://github.com/mngaonkar/declarative_agent_sdk.git@v0.9.2"

# From a checkout (development)
uv sync --extra discord --extra dev
```

Extras: `discord` (Discord bot), `tools` (Tavily), `dev` (pytest, ruff, mypy).

The wheel carries the SDK only — `examples/` is not packaged. To run an example
against an installed wheel, take its directory from the matching tag:

```bash
curl -sSL https://github.com/mngaonkar/declarative_agent_sdk/archive/refs/tags/v0.9.2.tar.gz \
  | tar -xz --strip-components=2 declarative_agent_sdk-0.9.2/examples/a2a_lean
```

**Python 3.14 exactly** for now. `requires-python = ">=3.14"` would also accept
3.15, but several dependencies have no 3.15 wheels yet and fall back to source
builds that fail — so [`.python-version`](.python-version) pins 3.14, which uv
honours automatically.

---

## Quick Start

### 1. Minimal lean agent (YAML)

```yaml
# configs/agent.yaml
name: helper
description: Concise assistant
agent_framework: lean          # default if omitted

instruction_file: instructions.md
skills_directory: skills
workspace_directory: workspace

provider: openai
model: gpt-4o-mini
endpoint:
  url: https://api.openai.com/v1

tools:
  - web_request
  - exec_command

tools_approval_required: true
```

### 2. Run programmatically

```python
import os
from declarative_agent_sdk import AgentFactory, AgentRegistry, DiscordAgentServer

agent = AgentFactory.from_yaml_file("configs/agent.yaml")
AgentRegistry.register(agent, category="demo")

# Discord
server = DiscordAgentServer(agent, token=os.environ["DISCORD_BOT_TOKEN"])
server.run()

# Or A2A
# from declarative_agent_sdk import AIAgentServer
# AIAgentServer(agent, host="0.0.0.0", port=8000).run()
```

### 3. Discord example (recommended)

```bash
cd examples/discord_bot
export OPENAI_API_KEY=...
export DISCORD_BOT_TOKEN=...
export TAVILY_API_KEY=...      # optional

python run_bot.py --check      # token, intent + channel permissions, then exit
python run_bot.py              # serve the agent on Discord
```

`run_bot.py` verifies credentials before connecting and names what to export if
something is missing. `--config` picks a different agent YAML, `--log-level DEBUG`
turns up SDK logging.

Once installed, the same thing is available as a command:

```bash
declarative-agent discord --config agent.yaml --check
declarative-agent discord --config agent.yaml
declarative-agent version
```

### 4. Run A2A agents as background services

```bash
./packaging/deploy/install.sh --name researcher --provider openai --model gpt-4o-mini
./packaging/deploy/install.sh --name coder --provider vllm \
    --model qwen2.5-coder:latest --endpoint http://10.0.0.147:11434/v1 --json
./packaging/deploy/install.sh --list
./packaging/deploy/install.sh --enable-at-boot     # once per machine
./packaging/deploy/uninstall.sh --name coder --purge
```

`install.sh` is non-interactive and self-contained: it installs the SDK from
GitHub into a per-agent venv, generates `agent.yaml`, `instructions.md` and
`run_server.py` from the flags, and registers the agent as a program under a
single per-user **supervisord** (no root, same on Linux and macOS). Each agent
gets its own directory and port (allocated from 8000 unless `--port` is given),
binds `0.0.0.0`, and publishes this host's primary address in its agent card;
override with `--host` / `--advertise-host` behind NAT or a proxy. `--json`
prints one result object, so an orchestrating agent can provision sub-agents.
Without `--enable-at-boot`, agents survive crashes but not reboots. Details:
[packaging/deploy/README.md](packaging/deploy/README.md).

### CLI

```
declarative-agent discord --config PATH   serve an agent as a Discord bot
                          --check         verify token, intent, permissions
                          --env-file PATH explicit .env
                          --prefix STR    command prefix (default "!ask ")
                          --activity STR  the bot's "Playing ..." status
                          --embed         answers as embed cards
                          --log-level LVL DEBUG|INFO|WARNING|ERROR|CRITICAL
declarative-agent a2a                     placeholder, not wired up yet
declarative-agent version
```

Also available as `python -m declarative_agent_sdk.cli`. To serve over A2A, use
`AIAgentServer` from Python (see [A2A Server](#a2a-server)).

Full setup notes: [examples/discord_bot/README.md](examples/discord_bot/README.md).

### Examples

| Example | Shows |
|---------|-------|
| [lean_agent](examples/lean_agent/) | Lean runtime, sync + streaming, a skill |
| [adk_agent](examples/adk_agent/) | Google ADK runtime with Gemini |
| [deepagent_agent](examples/deepagent_agent/) | `deepagents` / LangGraph runtime with HITL interrupts |
| [simple_agent](examples/simple_agent/) | Minimal Python sync/stream usage (uv workspace member) |
| [a2a_lean](examples/a2a_lean/) | Lean agent over A2A + interactive client with tool approval |
| [discord_bot](examples/discord_bot/) | Discord bot with skills and file attachments |
| [llm_wiki](examples/llm_wiki/) | Agent that maintains a markdown knowledge base |
| [workflow](examples/workflow/) | Multi-agent LangGraph workflow from YAML |
| [docker](examples/docker/) | Generic image + `docker compose` |

Also see external samples: [declarative_agent_sdk_examples](https://github.com/mngaonkar/declarative_agent_sdk_examples).

---

## Peer Runtimes (`agent_framework`)

Pick **one** loop per agent. Do not nest frameworks.

| Value | Class | Loop owner | Typical use |
|-------|--------|------------|-------------|
| **`lean`** (default) | `LeanAIAgent` | Native ReAct + progressive skills | Product bots, Discord, simple tools |
| `adk` | `ADKAIAgent` | Google ADK `Runner` | Gemini / ADK-native sessions |
| `deepagent` | `LangChainAIAgent` | `deepagents` / LangGraph | Deep multi-step graphs, HITL interrupts |

```yaml
agent_framework: lean
# agent_framework: adk
# agent_framework: deepagent

# Aliases: simple|esp → lean; google_adk → adk; langchain|langgraph → deepagent
# Legacy key `backend` is still accepted
```

Shared across all three:

- YAML config shape (`CommonAgentConfig`)
- `BaseAgent` + `AgentEvent` stream
- Discord / A2A transports

---

## Architecture (high level)

```
YAML ──► AgentFactory ──► LeanAIAgent | ADKAIAgent | LangChainAIAgent
                                      │
                                      ▼
                               AgentEvent stream
                                      │
              ┌───────────────────────┼───────────────────────┐
              ▼                       ▼                       ▼
        AIAgentServer          DiscordAgentServer          CLI / tests
         (A2A)                    (gateway)
```

### Package layout

```
cli/                  # declarative-agent console entry point
core/                 # factory, events, BaseAgent, registries
agents/
  lean/               # LeanAIAgent + runtime/ (loop, skills, tools, llm)
  adk/                # ADKAIAgent + ADK plugins
  deepagent/          # LangChainAIAgent
tools/                # ToolRegistry, SkillRegistry, builtin/
models/               # ModelFactory (ADK providers)
transports/
  discord/            # DiscordAgentServer
  a2a/                # A2A server, client, executor, formatters
workflows/            # multi-agent YAML graphs
packaging/deploy/     # install.sh / uninstall.sh: A2A agents under supervisord
examples/             # runnable samples (not in the wheel)
```

---

## Agent YAML Reference

### Required

| Parameter | Type | Description |
|-----------|------|-------------|
| `name` | string | Unique agent name |

### Core

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `agent_framework` | string | `lean` | `lean` \| `adk` \| `deepagent` |
| `description` | string | `''` | Short description |
| `instruction_file` | string | — | System prompt markdown path |
| `output_key` | string | — | Optional session output key |

### Model & provider

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `model` | string | runtime-dependent | e.g. `gpt-4o-mini`, `gemini-2.5-flash-lite` |
| `provider` | string | lean→`openai`, adk→google, deepagent→`anthropic` | `openai`, `google`, `anthropic`, `vllm`, … |
| `max_tokens` / `temperature` | | — | Root level or under `endpoint` |
| `endpoint.url` | string | — | API base (OpenAI-compatible / vLLM) |

### Skills & tools

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `skills_directory` | string | `skills` | Root for progressive skills (**lean**) |
| `skills` | list | — | Skill paths for ADK/deepagent discovery |
| `tools` | list | `[]` | Builtin/registry tool names |
| `tools_approval_required` | bool | `true` | Human approval before non-meta tools |

### Workspace & other

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `workspace_directory` | string | `workspace` | Scratch / outputs |
| `publish_url` | string | — | A2A agent card URL |
| `max_tool_iterations` / `max_step_retries` / `max_no_tool_continues` / `history_limit` | int | runtime default | Loop limits |
| `enable_truncation` / `context_window` / `truncate_strategy` / `safety_margin` | | `false` / — / `end` / `100` | Optional token truncation |

### Examples

**Lean (default) + OpenAI**

```yaml
name: helper
agent_framework: lean
instruction_file: instructions.md
skills_directory: skills
provider: openai
model: gpt-4o-mini
endpoint:
  url: https://api.openai.com/v1
tools:
  - web_request
  - exec_command
tools_approval_required: true
```

**ADK + Gemini**

```yaml
name: gemini_agent
agent_framework: adk
instruction_file: instructions.md
model: gemini-2.5-flash-lite
tools_approval_required: true
```

**Deepagent**

```yaml
name: deep_agent
agent_framework: deepagent
instruction_file: instructions.md
provider: openai
model: gpt-4o
tools_approval_required: true
```

**Local vLLM**

```yaml
name: local_agent
agent_framework: lean
instruction_file: instructions.md
provider: vllm
model: Qwen/Qwen3-4B
endpoint:
  url: http://localhost:8000/v1
  max_tokens: 3000
```

---

## Progressive Skills (lean)

Lean skills follow progressive disclosure (same idea as the ESP32 agent):

| Level | What | When |
|-------|------|------|
| L1 | `name` + `description` | Always in the system prompt |
| L2 | Full `SKILL.md` body | Model calls `Skill(name)` |
| L3 | `scripts/`, references | `read_file` / `run_script` |

```
skills/
  disk-space/
    SKILL.md
    scripts/   # optional
```

Meta tools that auto-approve on lean: `Skill`, `list_skills`, `list_dir`, `read_file`.

---

## Discord

### Behaviour

- **Triggers:** @mention, DMs, optional `command_prefix` (e.g. `!ask `)
- **Sessions:** per channel (default), user, or global
- **Tool approval:** ✅ / ❌ or typed `yes` / `no` when `tools_approval_required: true`
- **Long text:** split at 2000 characters; GFM tables adapted for Discord
- **Files:** auto-attach from final text, turn paths, and new files under `attachments/` / `workspace/`

### File attachments

In the agent’s **final** answer, mark files for upload:

```text
[[attach:attachments/report.png]]
```

Also works: `![label](path_or_url)`, bare paths, or `https://…` file URLs.

**Bot permissions:** Send Messages, Read Message History, Add Reactions, **Attach Files**.  
Enable **MESSAGE CONTENT INTENT** in the developer portal.

### Constructor (selected)

| Parameter | Default | Description |
|-----------|---------|-------------|
| `agent` | — | Any `BaseAgent` |
| `token` | `$DISCORD_BOT_TOKEN` | Bot token |
| `command_prefix` | `None` | e.g. `"!ask "` |
| `session_scope` | `channel` | `channel` \| `user` \| `global` |
| `tool_confirmation_timeout` | `120.0` | Seconds to wait for ✅/❌ |
| `allowed_channels` | `None` | Restrict to these channel IDs |
| `respond_to_all_messages` | `false` | Reply without a mention or prefix |
| `format_markdown` | `true` | Discord-friendly markdown |
| `reply_as_embed` | `false` | Post answers as embeds |

```python
from declarative_agent_sdk import AgentFactory, AgentRegistry, DiscordAgentServer

agent = AgentFactory.from_yaml_file("agent.yaml")
AgentRegistry.register(agent, category="discord")

DiscordAgentServer(
    agent,
    command_prefix="!ask ",
    activity_status=f"{agent.name} — mention me",
).run()
```

---

## A2A Server

```python
from declarative_agent_sdk import AgentFactory, AgentRegistry, AIAgentServer

agent = AgentFactory.from_yaml_file("configs/agent.yaml")
AgentRegistry.register(agent, category="api")
AIAgentServer(agent, host="0.0.0.0", port=8000).run()
```

Lean end-to-end example (server + interactive client + tool approval):

```bash
cd examples/a2a_lean
export OPENAI_API_KEY=sk-...
python run_server.py          # terminal 1
python run_client.py          # terminal 2 (AGENT_URL=... to point elsewhere)
pytest ../../tests/test_lean_a2a.py -q   # mock LLM, no API key
```

When the agent asks to run a tool, the client prompts
`Approve? [y = yes, Y = yes to all, N = no]`. `Y` auto-approves every further
tool call for the rest of the session.

---

## Workflows

Multi-agent LangGraph workflows are configured in YAML and built with `WorkflowFactory`. Register node/router functions on `WorkflowRegistry` first. See [examples/workflow](examples/workflow/) and [ARCHITECTURE.md](ARCHITECTURE.md).

---

## Environment Variables

| Variable | Used by |
|----------|---------|
| `OPENAI_API_KEY` | lean / deepagent (OpenAI) |
| `ANTHROPIC_API_KEY` | deepagent default |
| `GOOGLE_API_KEY` / `GEMINI_API_KEY` | ADK / Google |
| `TAVILY_API_KEY` | `tavily_search` tool |
| `DISCORD_BOT_TOKEN` | Discord bot |

Credentials are read from the environment, or from the first `.env` found:
`--env-file`, then beside the agent config, then `./.env`. Missing ones are
reported at startup with the exact variable to set, rather than failing on the
first model call.

---

## Development

```bash
uv sync --extra discord --extra dev     # same as CI
source .venv/bin/activate

pytest tests/ -q                                # 209 tests
pytest tests/test_discord_agent_server.py -q    # Discord transport only
pytest tests/test_cli.py -q                     # CLI / entry point
```

CI ([`.github/workflows/release.yml`](.github/workflows/release.yml)) runs on
every push and PR: the test suite, then a wheel + sdist build, a version
consistency check (`pyproject.toml` vs `__version__.py`), a smoke install of the
wheel, and a syntax check of the deploy scripts. The build is uploaded as a
`wheel-<sha>` workflow artifact. A tag push (`v*`) also publishes a GitHub
release with the wheel, sdist and `SHA256SUMS`; the tag must match the built
version.

---

## Documentation

| Doc | Contents |
|-----|----------|
| [ARCHITECTURE.md](ARCHITECTURE.md) | Runtimes, events, Discord images, package map |
| [examples/discord_bot/README.md](examples/discord_bot/README.md) | Discord modes, intents, troubleshooting |
| [packaging/deploy/README.md](packaging/deploy/README.md) | Background A2A agents under supervisord |
| [DOCKER.md](DOCKER.md) | Container image and mounts |

---

## License

MIT — see project metadata in `pyproject.toml`.
