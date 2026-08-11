# Building Production-Ready AI Agents: A Declarative SDK for Google ADK

> **TL;DR:** Learn how to build, configure, and deploy production-grade AI agents using a declarative YAML-based SDK that supports multiple LLM providers, automatic token management, skills-based architecture, and zero-boilerplate agent creation.

---

## The Challenge: AI Agent Complexity at Scale

Building a single AI agent is straightforward. Building a dozen agents—each with different models, tools, skills, and context window constraints—quickly becomes a maintenance nightmare. You end up with:

- **Boilerplate everywhere**: The same initialization code copied across multiple agent files
- **Hardcoded configurations**: Model names, API keys, and parameters scattered throughout your codebase
- **Tool namespace conflicts**: Multiple agents trying to use functions with the same name
- **Context window errors**: Runtime failures when inputs exceed model token limits
- **Provider lock-in**: No easy way to switch between Google Gemini, OpenAI, or local vLLM servers

What if you could define agents declaratively in YAML, automatically manage token budgets, and compose reusable skills like building blocks?

**Enter the Declarative Agent SDK.**

---

## What Is This SDK?

The **Declarative Agent SDK** is a production-ready framework built on top of Google's Agent Development Kit (ADK). It provides:

🎯 **Zero-boilerplate agent creation** via YAML configuration  
🔧 **Skills-based architecture** with auto-discovery of tools  
🌐 **Multi-provider support** (Google Gemini, vLLM, OpenAI)  
⚡ **Automatic token management** to prevent context window errors  
🔒 **Instance-level isolation** preventing tool namespace conflicts  
📊 **Centralized logging** with configurable outputs  
🔄 **Hot-swappable configurations** without code changes  
🔀 **Multi-agent workflows** using LangGraph StateGraph in YAML

---
## Quick Start
1. Clone https://github.com/mngaonkar/declarative_agent_sdk_examples.git
2. `cd simple_agent`
3. Run `uv sync` to install dependencies
4. Run `source .venv/bin/activate`
5. `export OPENAI_API_KEY=your_key_here`
6. `export TAVILY_API_KEY=your_key_here`
7. Run `python agent.py` to execute the example agent

Refer https://github.com/mngaonkar/declarative_agent_sdk_examples/blob/master/simple_agent/README.md

---

## Architecture Overview

### High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                     Declarative Agent SDK                        │
│                                                                   │
│  ┌─────────────┐   ┌──────────────┐   ┌──────────────────────┐ │
│  │ YAML Config │──▶│ AgentFactory │──▶│       AIAgent        │ │
│  └─────────────┘   └──────────────┘   │  (Google ADK Agent)  │ │
│                                        └──────────┬───────────┘ │
│  ┌──────────────────────────────────────┐         │             │
│  │           Registries                 │         ▼             │
│  │  AgentRegistry  ToolRegistry         │  ┌────────────────┐  │
│  │  SkillRegistry  WorkflowRegistry     │  │ Runner (async) │  │
│  └──────────────────────────────────────┘  └───────┬────────┘  │
│                                                     │           │
│  ┌──────────────────────────────────────┐           ▼           │
│  │         Model Providers              │  ┌────────────────┐  │
│  │  Google Gemini | vLLM | OpenAI       │  │ Session State  │  │
│  └──────────────────────────────────────┘  └────────────────┘  │
│                                                                   │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │                     Servers                               │   │
│  │   AIAgentServer (A2A/JSON-RPC)  AIWorkflowServer         │   │
│  │   DiscordAgentServer (Discord bot)                       │   │
│  └──────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
```

---

## Agent Declaration

Agents are defined in YAML files and instantiated via `AgentFactory.from_yaml_file()`. Below is the complete reference for all supported parameters.

### Agent YAML Parameters

#### Required

| Parameter | Type | Description |
|-----------|------|-------------|
| `name` | string | Unique agent name used for identification and logging |

#### Core (Optional)

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `description` | string | `''` | Brief description of the agent's purpose |
| `instruction_file` | string | `None` | Path to a Markdown file containing the agent's system instructions |
| `output_key` | string | `None` | Session state key where the agent stores its structured output |

#### Model & Provider (Optional)

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `model` | string | `gemini-2.5-flash-lite` | Model name (e.g. `gemini-2.0-flash-exp`, `gpt-4o`, `Qwen/Qwen3-4B`) |
| `provider` | string | `None` | LLM provider: `google`, `vllm`, or `openai` |
| `max_tokens` | integer | `None` | Maximum output tokens. Can be set here or under `endpoint` |
| `temperature` | float | `None` | Sampling temperature. Can be set here or under `endpoint` |

#### Endpoint (Optional — required for `vllm` and `openai` providers)

| Parameter | Type | Description |
|-----------|------|-------------|
| `endpoint.url` | string | Base URL for the API server (e.g. `http://localhost:8000/v1`) |
| `endpoint.max_tokens` | integer | Maximum output tokens (overrides root-level `max_tokens`) |
| `endpoint.temperature` | float | Sampling temperature (overrides root-level `temperature`) |

#### Skills & Tools (Optional)

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `skills` | list[string] | `None` | Skill directory paths to auto-discover tools from (e.g. `["skills/search", "skills/write"]`). Each directory must contain a `SKILL.md` and a `scripts/` folder |
| `skills_directory` | string | `skills` | Base directory prepended to each path in `skills` |
| `tools` | list[string] | `[]` | Explicit tool names to include (resolved from the global `ToolRegistry`). If not specified, all discovered tools are added. |
| `tools_approval_required` | boolean | `true` | When `true`, prompts the user to approve each tool call before execution |

#### Token Management (Optional)

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `enable_truncation` | boolean | `false` | Automatically truncate input when it exceeds the context window |
| `context_window` | integer | `None` | Total context window size in tokens (required when `enable_truncation` is `true`) |
| `truncate_strategy` | string | `end` | Where to cut the input: `start`, `end`, or `middle` |
| `safety_margin` | integer | `100` | Extra tokens reserved as overhead during truncation |

#### Workspace & Publishing (Optional)

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `workspace_directory` | string | `workspace` | Directory for agent output files and intermediate artefacts |
| `publish_url` | string | `None` | URL published in the A2A agent card for remote discovery |
| `input_key_map` | dict | `{}` | Mapping that renames input keys before they are passed to the agent |

---

### Agent YAML Examples

**Minimal agent (Google Gemini)**
```yaml
name: search_agent
description: Searches the web and summarises results
instruction_file: agents/search/instructions.md
model: gemini-2.0-flash-exp
```

**Agent with skills and tools**
```yaml
name: toc_agent
description: Creates a table of contents for a book
instruction_file: agents/toc/instructions.md
model: gemini-2.0-flash-exp
skills:
  - skills/toc
  - skills/chapter
tools:
  - google_search
  - toc_validate_yaml
output_key: toc_agent_response
```

**Agent using a local vLLM server**
```yaml
name: local_agent
description: Agent backed by a locally hosted vLLM model
instruction_file: agents/local/instructions.md
model: Qwen/Qwen3-4B-Thinking-FP8
provider: vllm
endpoint:
  url: http://localhost:8000/v1
  max_tokens: 3000
  temperature: 0.7
```

**Agent with automatic token truncation**
```yaml
name: long_context_agent
description: Handles long inputs with automatic truncation
instruction_file: agents/long/instructions.md
model: Qwen/Qwen3-4B-Thinking-FP8
provider: vllm
endpoint:
  url: http://localhost:8000/v1
  max_tokens: 3000
enable_truncation: true
context_window: 20384
truncate_strategy: end
safety_margin: 100
```

---

### Workflow YAML Parameters

Workflows are defined in YAML and instantiated via `WorkflowFactory.from_yaml_file()`. All node and router functions must be registered with `WorkflowRegistry` before the workflow is created.

#### Required

| Parameter | Type | Description |
|-----------|------|-------------|
| `nodes` | list | Ordered list of workflow nodes |
| `nodes[].name` | string | Unique node identifier used when defining edges |
| `nodes[].function` | string | Name of a function registered in `WorkflowRegistry` |

#### Optional

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `name` | string | `unnamed_workflow` | Workflow name |
| `description` | string | `no description provided` | Human-readable description |
| `edges` | list | `[]` | Unconditional edges between nodes |
| `edges[].from` | string | — | Source node name. Use `START` for the workflow entry point |
| `edges[].to` | string | — | Target node name. Use `END` to terminate the workflow |
| `conditional_edges` | list | `[]` | Edges whose target is determined at runtime by a router function |
| `conditional_edges[].from` | string | — | Source node name |
| `conditional_edges[].router_function` | string | — | Name of a router function registered in `WorkflowRegistry` |

---

### Workflow YAML Example

```yaml
name: book_generation_workflow
description: Multi-agent workflow that generates a complete book
nodes:
  - name: toc_agent
    function: toc_agent
  - name: chapter_agent
    function: chapter_agent_parallel
  - name: collation_agent
    function: collation_agent
edges:
  - from: START
    to: toc_agent
  - from: chapter_agent
    to: collation_agent
  - from: collation_agent
    to: END
conditional_edges:
  - from: toc_agent
    router_function: route_after_toc
```


---

## Running an Agent

The same agent object can be exposed over different transports. Pick a server, hand it the agent, call `run()`.

### A2A / JSON-RPC

```python
from declarative_agent_sdk import AgentFactory, AgentRegistry, AIAgentServer

agent = AgentFactory.from_yaml_file('configs/agent.yaml')
AgentRegistry.register(agent, category="news")

server = AIAgentServer(agent, host="0.0.0.0", port=8000)
server.run()
```

### Discord Bot

`DiscordAgentServer` runs the same agent as a Discord bot. It listens for @-mentions, direct messages, or a command prefix, keeps one agent session per channel, and posts the agent's answer back to the channel.

```python
import os
from declarative_agent_sdk import AgentFactory, AgentRegistry, DiscordAgentServer

agent = AgentFactory.from_yaml_file('configs/agent.yaml')
AgentRegistry.register(agent, category="news")

server = DiscordAgentServer(agent, token=os.environ["DISCORD_BOT_TOKEN"])
server.run()
```

A runnable test program lives in [`examples/discord_bot/`](examples/discord_bot/) — `python run_discord_bot.py` self-tests the bot behaviour with no token, API key or network, and `--mode local` gives you a terminal REPL against your real agent before you connect to Discord.

#### Setup

1. Install the optional dependency: `pip install "declarative-agent-sdk[discord]"`
2. Create an application and bot at https://discord.com/developers/applications
3. Under **Bot → Privileged Gateway Intents**, enable **MESSAGE CONTENT INTENT** (without it, message text arrives empty and the bot never replies)
4. Invite the bot with the `bot` scope and the *Send Messages*, *Read Message History* and *Add Reactions* permissions
5. Export the token as `DISCORD_BOT_TOKEN` (or pass `token=...`)

#### Constructor Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `agent` | BaseAgent | — | Any agent — `AIAgent`, `LangChainAIAgent`, or a custom `BaseAgent` |
| `token` | string | `$DISCORD_BOT_TOKEN` | Discord bot token |
| `respond_to_mentions` | bool | `true` | Reply when the bot is @-mentioned in a server channel |
| `respond_to_dms` | bool | `true` | Reply to direct messages |
| `respond_to_all_messages` | bool | `false` | Reply to every message in allowed channels, no mention needed |
| `command_prefix` | string | `None` | Extra trigger, e.g. `"!ask "`; stripped before the query reaches the agent |
| `allowed_channels` | list | `None` | Channel IDs the bot may answer in (DMs are unaffected) |
| `session_scope` | string | `channel` | Conversation memory granularity — `channel`, `user`, or `global` |
| `show_working_updates` | bool | `true` | Post a status message while the agent works, replaced by the final answer |
| `tool_confirmation_timeout` | float | `120.0` | Seconds to wait for a tool-approval reaction before denying |
| `activity_status` | string | `None` | Text shown as the bot's "Playing …" status |

#### Behaviour Notes

- **Sessions** — each Discord channel maps to its own agent session by default, so conversations in different channels stay independent. Turns within a session are serialised, so rapid-fire messages queue instead of interleaving.
- **Tool approval** — agents created with `tools_approval_required: true` prompt in-channel with the tool name and arguments. The bot seeds ✅ and ❌ so they are one click away; the person who asked approves by clicking either **or** by replying `yes` / `no`, and the typed reply is consumed by the prompt rather than treated as a new question. Once answered the bot withdraws its own reactions and edits the outcome into the prompt, so a resolved request never looks like a pending vote. Only the asker's answer counts, and no answer within `tool_confirmation_timeout` denies the call.
- **Long answers** — responses over Discord's 2000-character limit are split across messages on line and word boundaries.
- **Embedding** — use `await server.start()` instead of `server.run()` to attach the bot to an event loop you already own, and `await server.close()` to disconnect.

```python
server = DiscordAgentServer(
    agent,
    command_prefix="!ask ",
    allowed_channels=[1234567890],
    session_scope="user",
    activity_status="answering questions",
)
server.run()
```
