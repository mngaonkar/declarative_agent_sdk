# Architecture — Declarative Agent SDK

This document describes package layout, how an agent is created from YAML, how
the three **peer** runtimes relate, and how requests flow through A2A and Discord.

**Design rule:** pick one `agent_framework` per agent. That runtime owns the
execution loop end-to-end. Do not nest ADK or deepagent inside lean (or vice
versa). Transports only depend on the shared `BaseAgent` + `AgentEvent` contract.

---

## 1. System Overview

```
┌──────────────────────────────────────────────────────────────────────────┐
│                       Declarative Agent SDK                              │
│                                                                          │
│   YAML / Python ──► AgentFactory ──► LeanAIAgent      (default)          │
│                                   or AIAgent          (ADK)              │
│                                   or LangChainAIAgent (deepagent)        │
│                                              │                           │
│                         each owns its own loop / sessions                │
│                                              │                           │
│                                              ▼                           │
│                                    AgentEvent stream                     │
│                                              │                           │
│              ┌───────────────────────────────┼────────────────────┐      │
│              ▼                               ▼                    ▼      │
│   AIAgentServer                    DiscordAgentServer      (CLI / tests) │
│   (A2A / JSON-RPC)                 (Discord gateway)                     │
└──────────────────────────────────────────────────────────────────────────┘
```

| Layer | Responsibility |
|-------|----------------|
| **Core** | Factory, events, registries, config — framework-agnostic |
| **Agents** | Three peer runtimes; each owns loop + tools + sessions |
| **Tools** | Shared builtin tools + registries |
| **Transports** | Discord and A2A only see `BaseAgent` / `AgentEvent` |
| **Workflows** | Optional multi-agent LangGraph workflows (separate from agent runtimes) |

---

## 2. Package Layout

Source maps to package name `declarative_agent_sdk` via
`[tool.setuptools.package-dir] declarative_agent_sdk = "."`.

```
declarative_agent_sdk/                 # repo root == package root
├── __init__.py                        # public re-exports
├── __version__.py
│
├── core/                              # shared, framework-agnostic
│   ├── base_agent.py                  # BaseAgent ABC
│   ├── agent_event.py                 # AgentEvent (+ ADK adapter)
│   ├── agent_config.py                # CommonAgentConfig / parse_common_config
│   ├── agent_factory.py               # YAML → agent (routes by agent_framework)
│   ├── agent_registry.py
│   ├── agent_context.py / agent_state.py
│   ├── agent_logging.py
│   ├── constants.py / utils.py / token_utils.py
│
├── agents/                            # one subpackage per end-to-end loop
│   ├── lean/
│   │   ├── agent.py                   # LeanAIAgent
│   │   └── runtime/                   # loop, skills, tools, llm, chat_backend
│   ├── adk/
│   │   ├── agent.py                   # AIAgent (Google ADK Runner)
│   │   └── plugins/                   # context_updater, etc.
│   └── deepagent/
│       └── agent.py                   # LangChainAIAgent (create_deep_agent)
│
├── tools/
│   ├── tool_registry.py
│   ├── skill_registry.py              # ADK/deepagent skill→tool discovery
│   └── builtin/                       # web_request, exec_command, files, tavily
│
├── models/
│   └── model_factory.py               # ADK LiteLLM / provider models
│
├── transports/
│   ├── discord/
│   │   └── server.py                  # DiscordAgentServer (+ markdown, images)
│   └── a2a/
│       ├── server.py / client.py / executor.py
│       ├── converter.py / utils.py / base_executor.py
│       └── formatters/
│
├── workflows/                         # multi-agent graphs (optional)
│   ├── workflow.py / factory.py / registry.py
│   ├── graph_executor.py / server.py
│
├── examples/discord_bot/
├── tests/
└── a2ui/                              # frontend (separate Node app)
```

### Public imports (stable)

```python
from declarative_agent_sdk import (
    AgentFactory, LeanAIAgent, AIAgent, LangChainAIAgent,
    DiscordAgentServer, AgentEvent,
)

# Explicit paths also work:
from declarative_agent_sdk.agents.lean import LeanAIAgent
from declarative_agent_sdk.transports.discord import DiscordAgentServer
from declarative_agent_sdk.core.agent_factory import AgentFactory
```

---

## 3. Choosing a Runtime (`agent_framework`)

| Value | Class | Loop owner | Default models / notes |
|-------|--------|------------|-------------------------|
| **`lean`** (default) | `LeanAIAgent` | Native `LeanLoop` (ESP-style ReAct) | OpenAI-compatible; progressive skills |
| `adk` | `AIAgent` | Google ADK `Runner` | Gemini / LiteLLM; FunctionTool confirmation |
| `deepagent` | `LangChainAIAgent` | `deepagents.create_deep_agent` (LangGraph) | Anthropic/OpenAI/…; `interrupt_on` HITL |

YAML:

```yaml
agent_framework: lean      # or adk | deepagent
# aliases: simple|esp → lean; google_adk → adk; langchain|langgraph → deepagent
# legacy key `backend` still accepted
```

**Do not mix frameworks** in one agent. Shared surface only: YAML shape,
`BaseAgent`, `AgentEvent`, transports.

---

## 4. Initialization Flow

```
agent.yaml
    │
    ▼
AgentFactory.from_yaml_file()
    │  resolve relative instruction_file / workspace / skills_directory
    │  vs YAML parent directory
    │
    ├─► resolve_agent_framework()  → lean | adk | deepagent
    │
    ├─► parse_common_config()      → CommonAgentConfig
    │
    ├─► resolve tool names via ToolRegistry (strings may resolve later)
    │
    └─► _create_lean_agent | _create_adk_agent | _create_langchain_agent
```

### Lean path (`agents/lean/`)

```
LeanAIAgent.__init__
    │
    ├─ SkillRegistry(skills_directory)     progressive L1 catalog from SKILL.md
    ├─ LeanToolRegistry                    Skill, list_skills, FS tools, run_script
    ├─ register builtin + YAML tools       add_callable (type-coerce LLM args)
    ├─ LeanLLMClient                       OpenAI-compatible /chat/completions
    └─ LeanLoop                            history, trim, approval, tool rounds
```

**Progressive skills (lean):**

| Level | Content | When |
|-------|---------|------|
| L1 | name + description | Always in system prompt |
| L2 | full `SKILL.md` body | Model calls `Skill(name)` |
| L3 | scripts / references | `read_file` / `run_script` |

### ADK path (`agents/adk/`)

```
AIAgent.__init__
    │
    ├─ ModelFactory.create_model(provider, endpoint, …)
    ├─ SkillRegistry (instance-isolated) + ToolRegistry built-ins
    ├─ FunctionTool(..., require_confirmation=tools_approval_required)
    ├─ InMemorySessionService + Runner
    └─ AgentCard for A2A
```

### Deepagent path (`agents/deepagent/`)

```
LangChainAIAgent.__init__
    │
    ├─ _resolve_model → provider:model or ChatLiteLLM
    ├─ tools → LangChain StructuredTool
    ├─ interrupt_on (if tools_approval_required) for HITL
    └─ create_deep_agent(..., checkpointer=MemorySaver)
```

---

## 5. Shared Contract: `BaseAgent` + `AgentEvent`

### `BaseAgent` (`core/base_agent.py`)

| Method | Role |
|--------|------|
| `run_query(query, session_id)` | Stream events for plain text |
| `invoke(context)` | Stream events for A2A `RequestContext` |
| `tool_confirmation(id, session_id, yes)` | Resume after human approve/deny |

### `AgentEvent` (`core/agent_event.py`)

Canonical stream item. Transports duck-type an ADK-like surface:

```
event.is_final_response()
event.content.parts[*].text | .function_call
event.long_running_tool_ids
event.actions.requested_tool_confirmations
```

Constructors: `AgentEvent.final_text()`, `.status()`, `.tool_approval()`, `.error()`.

- **Lean** emits `AgentEvent` directly from `LeanLoop` / `LoopEvent`.
- **ADK** maps runner events via `from_adk_event()`.
- **Deepagent** maps LangGraph `updates` / `__interrupt__` chunks to `AgentEvent`.

---

## 6. Request Handling — A2A (any agent)

```
A2A Client (HTTP POST / JSON-RPC)
    │
    ▼
AIAgentServer  (Starlette + A2A routes)
    │
    ▼
AIAgentExecutor.execute(context, event_queue)
    │
    └─► agent.invoke(context)  → AgentEvent stream
            │
            ├─ tool_approval   → TASK_STATE_INPUT_REQUIRED
            ├─ status/text     → TASK_WORKING
            └─ final_text      → TASK_STATE_COMPLETED  (ResponseFormatter)
```

---

## 7. Tool Approval

Same Discord/A2A UX; **implementation differs per runtime**.

```
                    lean                          adk                         deepagent
                    ────                          ───                         ─────────
Pause               LeanLoop before invoke        FunctionTool                interrupt_on +
                    (except auto-approve          require_confirmation        HumanInTheLoop
                     Skill/list_skills/…)
Resume              tool_confirmation →           FunctionResponse            Command(resume=
                    continue loop                 adk_request_confirmation    {decisions})
Session             in-process history dict       InMemorySessionService      MemorySaver thread_id
```

Discord:

1. Detect `tool_approval` event  
2. Post prompt + ✅ / ❌ (or typed yes/no)  
3. Call `agent.tool_confirmation(id, session_id, approved)`  
4. Continue streaming events  

---

## 8. Lean Runtime Loop (`agents/lean/runtime/loop.py`)

```
run(query, session_id)
    │
    └─ drive(messages, rounds_left)
            │
            ▼
        ChatBackend.chat(messages, tools)     ← LeanLLMClient
            │
            ├─ no tool_calls ──► final text ──► AgentEvent.final_text
            │
            └─ tool_calls
                    │
                    ├─ needs approval? ──► AgentEvent.tool_approval  (pause)
                    │                         resume(approved) continues
                    │
                    └─ else invoke LeanToolRegistry
                            └─ ToolMessage into history ──► next model round
```

Auto-approve tools (no Discord prompt): `Skill`, `list_skills`, `list_dir`, `read_file`.

---

## 9. Deepagent Loop (`agents/deepagent/agent.py`)

```
run_query
    │
    └─ graph.astream(..., stream_mode="updates")
            │
            ├─ {node: {messages: [AIMessage(tool_calls)]}}  → status event
            ├─ {node: {messages: [AIMessage(text)]}}        → final (via _message_text)
            └─ {"__interrupt__": (Interrupt(HITLRequest),)} → tool_approval
                    │
                    tool_confirmation → Command(resume={"decisions": [...]})
```

Content blocks from OpenAI/Anthropic are normalized with `_message_text()` so
Discord never sees raw `[{type: text, ...}]` dumps.

---

## 10. ADK Loop (`agents/adk/agent.py`)

```
run_query / invoke
    │
    └─ Runner.run_async(...)
            │  events mapped with from_adk_event()
            │
            ├─ long_running / confirmations → tool_approval
            ├─ intermediate text            → status
            └─ is_final_response()          → final_text
```

---

## 11. Discord Transport (`transports/discord/server.py`)

```
Discord gateway (discord.py Client)
    │  on_message
    ▼
DiscordAgentServer.handle_message
    │
    ├─ extract_query  (DM / @mention / command_prefix)
    ├─ session_id     (channel | user | global) + per-session lock
    │
    └─► agent.run_query(query, session_id)
            │
            ├─ status            → "⏳ …" status message (edit in place)
            ├─ tool_approval     → ✅/❌ or yes/no → tool_confirmation
            │
            └─ final
                    ├─ remove_think_content
                    ├─ to_discord_markdown   (tables → code blocks, strip HTML)
                    ├─ image pipeline (below)
                    └─ channel.send(content=…, files=…)
```

### Image attachments

Tool stdout is **not** streamed to Discord, so paths often never appear in the
final event alone. The transport therefore:

1. Harvests image refs from **all** event text in the turn  
2. Snapshots `attachments/`, `workspace/`, etc. **before** the turn and attaches
   **new/updated** real image files after  
3. Parses final text for:
   - `![alt](path_or_url)`
   - bare `https://…png|jpg|…`
   - paths like `attachments/foo.jpg`, absolute paths  
4. Resolves local files (multiple search bases), downloads HTTP images  
5. Validates magic bytes (rejects HTML saved as `.jpg`)  
6. Uploads via `discord.File` (up to 10 files, ~24 MiB each)

Agents should still mention paths in the final answer when possible:

```markdown
![label](attachments/plot.png)
```

---

## 12. Session & Memory

| | lean | ADK | deepagent |
|--|------|-----|-----------|
| Store | In-process history dict | `InMemorySessionService` | LangGraph `MemorySaver` |
| Key | `session_id` | `(app, user, session_id)` | `thread_id = session_id` |
| Lifetime | Process | Process | Process |
| Multi-turn | Yes | Yes | Yes |

Discord maps sessions to `discord-channel-{id}` (default), `discord-user-{id}`,
or a global agent key.

---

## 13. Registries

```
Global ToolRegistry  ──► tools/builtin/*.py

ADK / deepagent agents may use instance-isolated SkillRegistry subclasses
so skill tools do not collide across agents in one process.

Lean uses agents/lean/runtime SkillRegistry (progressive disclosure) plus
LeanToolRegistry for OpenAI function schemas.
```

---

## 14. Key Classes

| Class | Location | Responsibility |
|-------|----------|----------------|
| `AgentFactory` | `core/agent_factory.py` | YAML → peer agent |
| `AgentEvent` | `core/agent_event.py` | Shared stream contract |
| `BaseAgent` | `core/base_agent.py` | Interface for all agents |
| `LeanAIAgent` | `agents/lean/agent.py` | Lean runtime entry |
| `LeanLoop` | `agents/lean/runtime/loop.py` | Native ReAct + approval |
| `SkillRegistry` (lean) | `agents/lean/runtime/skills.py` | Progressive skills |
| `AIAgent` | `agents/adk/agent.py` | ADK Runner end-to-end |
| `LangChainAIAgent` | `agents/deepagent/agent.py` | deepagents end-to-end |
| `DiscordAgentServer` | `transports/discord/server.py` | Discord gateway |
| `AIAgentServer` | `transports/a2a/server.py` | A2A HTTP server |
| `AIAgentExecutor` | `transports/a2a/executor.py` | A2A task ↔ events |
| `ToolRegistry` | `tools/tool_registry.py` | Name → callable |
| `ModelFactory` | `models/model_factory.py` | ADK model construction |
| `AgentRegistry` | `core/agent_registry.py` | Named agent lookup (e.g. Discord) |

---

## 15. Example: Discord bot

```
examples/discord_bot/
├── agent.yaml              # agent_framework: lean | adk | deepagent
├── instructions.md
├── skills/                 # progressive skills (lean)
│   └── disk-space/SKILL.md
├── attachments/            # agent-written images (auto-uploaded if new)
├── workspace/
└── run_discord_bot.py      # selftest | local | connect | live
```

```bash
# smoke (no network)
python run_discord_bot.py --mode selftest

# lean + OpenAI
export OPENAI_API_KEY=...
python run_discord_bot.py --mode local --config agent.yaml
```

---

## 16. Design Summary

| Principle | Practice |
|-----------|----------|
| One loop per agent | `agent_framework` selects lean **or** ADK **or** deepagent |
| Shared product shell | Factory, events, Discord/A2A, YAML |
| Lean skills | Progressive disclosure (ESP lineage) |
| Human approval | Runtime-specific pause; same Discord UX |
| Images on Discord | Path/URL harvest + dir snapshot + `discord.File` |
| Package clarity | `core` / `agents/*` / `tools` / `transports` / `workflows` |
