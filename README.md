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
| **Multi-agent workflows** | Optional LangGraph workflows via YAML |

---

## Install

```bash
# From this repo
uv sync
# or
pip install -e ".[discord]"

# Optional extras
pip install "declarative-agent-sdk[discord]"   # Discord bot
pip install "declarative-agent-sdk[tools]"     # Tavily, etc.
```

Python **3.14+** (see `pyproject.toml`).

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
export DISCORD_BOT_TOKEN=...   # for live mode
export TAVILY_API_KEY=...      # optional

python run_discord_bot.py                    # selftest (no keys)
python run_discord_bot.py --mode local       # real agent, fake Discord
python run_discord_bot.py --mode connect    # token + permissions check
python run_discord_bot.py --mode live       # real Discord
```

Full setup notes: [examples/discord_bot/README.md](examples/discord_bot/README.md).

Also see external samples: [declarative_agent_sdk_examples](https://github.com/mngaonkar/declarative_agent_sdk_examples).

---

## Peer Runtimes (`agent_framework`)

Pick **one** loop per agent. Do not nest frameworks.

| Value | Class | Loop owner | Typical use |
|-------|--------|------------|-------------|
| **`lean`** (default) | `LeanAIAgent` | Native ReAct + progressive skills | Product bots, Discord, simple tools |
| `adk` | `AIAgent` | Google ADK `Runner` | Gemini / ADK-native sessions |
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
YAML ──► AgentFactory ──► LeanAIAgent | AIAgent | LangChainAIAgent
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
core/                 # factory, events, BaseAgent, registries
agents/
  lean/               # LeanAIAgent + runtime/ (loop, skills, tools, llm)
  adk/                # AIAgent + ADK plugins
  deepagent/          # LangChainAIAgent
tools/                # ToolRegistry, SkillRegistry, builtin/
models/               # ModelFactory (ADK providers)
transports/
  discord/            # DiscordAgentServer
  a2a/                # A2A server, client, executor, formatters
workflows/            # multi-agent YAML graphs
examples/discord_bot/
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
| `enable_truncation` / `context_window` / … | | | Optional token truncation |

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
| `tools_approval` timeout | `120` | Seconds |
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

---

## Workflows

Multi-agent LangGraph workflows are configured in YAML and built with `WorkflowFactory`. Register node/router functions on `WorkflowRegistry` first. See [ARCHITECTURE.md](ARCHITECTURE.md) and the workflow parameter section in older docs if you maintain multi-agent pipelines.

---

## Environment Variables

| Variable | Used by |
|----------|---------|
| `OPENAI_API_KEY` | lean / deepagent (OpenAI) |
| `ANTHROPIC_API_KEY` | deepagent default |
| `GOOGLE_API_KEY` / `GEMINI_API_KEY` | ADK / Google |
| `TAVILY_API_KEY` | `tavily_search` tool |
| `DISCORD_BOT_TOKEN` | Discord live mode |

`.env` next to the example or repo root is loaded when using `run_discord_bot.py`.

---

## Development

```bash
uv sync
source .venv/bin/activate
pytest tests/ -q
python examples/discord_bot/run_discord_bot.py --mode selftest
```

---

## Documentation

| Doc | Contents |
|-----|----------|
| [ARCHITECTURE.md](ARCHITECTURE.md) | Runtimes, events, Discord images, package map |
| [examples/discord_bot/README.md](examples/discord_bot/README.md) | Discord modes, intents, troubleshooting |
| [DOCKER.md](DOCKER.md) | Container image and mounts |

---

## License

MIT — see project metadata in `pyproject.toml`.
