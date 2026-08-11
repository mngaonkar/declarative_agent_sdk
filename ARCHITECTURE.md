# Architecture — Declarative Agent SDK

This document describes how an agent is initialized, how a request flows through the system, and how the two agent backends (Google ADK and LangChain/LangGraph) relate to each other.

---

## 1. System Overview

```
┌───────────────────────────────────────────────────────────────┐
│                    Declarative Agent SDK                       │
│                                                               │
│   YAML / Python  ──►  AgentFactory  ──►  AIAgent             │
│                                      or  LangChainAIAgent     │
│                                               │               │
│   SkillRegistry  ─┐                           │               │
│   ToolRegistry   ─┼──► resolved tools ───────►│               │
│   ModelFactory   ─┘                           │               │
│                                               ▼               │
│                                         LLM (model)           │
│                                               │               │
│                              ┌────────────────┴──────────┐    │
│                              │  ReAct Loop               │    │
│                              │  think → act → observe    │    │
│                              └───────────────────────────┘    │
│                                               │               │
│   AIAgentServer  ──► AIAgentExecutor ─────────►  response     │
│   (A2A / JSON-RPC)                                            │
│                                                               │
│   DiscordAgentServer ─────────────────────────►  response     │
│   (Discord gateway)                                           │
└───────────────────────────────────────────────────────────────┘
```

---

## 2. Initialization Flow

From YAML config to a ready-to-run agent.

```
agent.yaml
    │
    ▼
AgentFactory.from_yaml_file()
    │
    ├─► ModelFactory.create_model()
    │       │
    │       ├─ provider == "google"   ──► model name string (Gemini)
    │       ├─ provider == "vllm"     ──► LiteLlm(openai/model, api_base=url)
    │       └─ provider == "openai"   ──► LiteLlm(openai/model)
    │
    ├─► InstanceSkillRegistry   (isolated per agent)
    │       │
    │       └─► register_multiple_from_directory(skills_directory, skills)
    │               │
    │               └─ for each skill/
    │                       ├─ parse SKILL.md frontmatter (name, description)
    │                       └─ scan scripts/*.py
    │                               └─► ToolRegistry.register(fn_name, callable)
    │
    ├─► ToolRegistry.register_built_in_tools()   (builtin_tools/*.py)
    │
    ├─► resolve tools list
    │       ├─ from InstanceSkillRegistry (skill scripts)
    │       ├─ from explicit tool names   (ToolRegistry.get)
    │       └─ from built-in registry     (if no tools specified)
    │
    └─► AIAgent.__init__(name, model, instruction, tools, ...)
            │
            ├─ reads instruction_file → instruction text
            ├─ wraps tools in FunctionTool (+ require_confirmation if needed)
            ├─ configures AutomaticFunctionCallingConfig(max_remote_calls=40)
            ├─ creates InMemorySessionService
            ├─ creates Runner(agent=self, app_name=name, session_service=...)
            └─ creates AgentCard (for A2A discovery)
```

---

## 3. Request Handling Flow (Google ADK path)

A single user query from API call to final response.

```
A2A Client (HTTP POST / JSON-RPC)
    │
    ▼
AIAgentServer  (Starlette + A2A routes)
    │
    ▼
DefaultRequestHandler  (a2a-sdk)
    │  creates / resumes Task
    ▼
AIAgentExecutor.execute(context, event_queue)
    │
    ├─► TaskUpdater.start_work()           ← publishes TASK_WORKING event
    │
    └─► AIAgent.invoke(context)
            │
            ├─ _get_or_create_session(context_id)
            │
            ├─ adk_content_from_message(context.message)
            │       converts A2A Message → ADK Content (text / function_response parts)
            │
            └─► Runner.run_async(user_id, session_id, new_message)
                    │
                    └─[ ReAct Loop ]─────────────────────────────────────┐
                            │                                             │
                            ▼                                             │
                        LLM call  (Gemini / vLLM / OpenAI via LiteLLM)  │
                            │                                             │
                            ├─ text response  ──────────────────► DONE   │
                            │                                             │
                            └─ tool_call(s)                              │
                                    │                                     │
                                    ▼                                     │
                                before_model_callback                     │
                                (dynamic_context_callback)                │
                                    │                                     │
                                    ▼                                     │
                                tool execution                            │
                                    │                                     │
                                    └────── result ───────────────────────┘
                    │
                    │  events yielded per iteration:
                    │
                    ├─ tool confirmation requested?
                    │       └─► update_status(TASK_STATE_INPUT_REQUIRED)
                    │               sends function_id + args back to client
                    │
                    ├─ intermediate text?
                    │       └─► start_work(TASK_WORKING parts)
                    │
                    └─ final response (is_final_response() == True)?
                            └─► update_status(TASK_STATE_COMPLETED)
                                    formatted by ResponseFormatter
```

---

## 4. Tool Confirmation Round-Trip

When `tools_approval_required: true` the agent pauses for human approval.

```
Agent detects tool call
    │
    ▼
AIAgentExecutor sends TASK_STATE_INPUT_REQUIRED
    payload: { function_response: { id, name, args } }
    │
    ▼
Client inspects args and calls tool_confirmation endpoint
    │
    ├─ yes ──► AIAgent.tool_confirmation(context_id, session_id, yes=True)
    │               └─► Runner.run_async  (FunctionResponse confirmed=True)
    │                       └─► tool executes → loop continues
    │
    └─ no  ──► AIAgent.tool_confirmation(context_id, session_id, yes=False)
                    └─► Runner.run_async  (FunctionResponse confirmed=False)
                            └─► agent decides next step without running tool
```

---

## 5. LangChain Deep Agent Flow

`LangChainAIAgent` uses the same initialization and tool resolution steps but replaces the ADK Runtime with the `deepagents` library (a LangGraph harness with built-in planning, file system, and sub-agent tools).

```
LangChainAIAgent.__init__
    │
    ├─ (same SkillRegistry + ToolRegistry resolution as §2)
    │
    ├─► _resolve_model(model, provider)
    │       ├─ "anthropic"    ──► "anthropic:{model}"   (init_chat_model string)
    │       ├─ "openai"       ──► "openai:{model}"
    │       ├─ "google"       ──► "google_genai:{model}"
    │       └─ "vllm/litellm" ──► ChatLiteLLM instance (BaseChatModel)
    │
    ├─► _to_lc_tool(callable)  ──► StructuredTool.from_function(fn)
    │       (for every resolved callable — additive to deepagents built-ins)
    │
    └─► create_deep_agent(
                model, tools, system_prompt=instruction,
                middleware, subagents,
                checkpointer=MemorySaver, name=name
            )
            │
            └─ builds a CompiledStateGraph with nodes:
                   "model"                          ← LLM call
                   "tools"                          ← tool execution
                   "TodoListMiddleware.after_model"  ← planning / todo
                   "PatchToolCallsMiddleware.before_agent"


run_query(query, session_id)
    │
    └─► graph.astream(
                {"messages": [HumanMessage(query)]},
                config={thread_id: session_id},
                stream_mode="updates"
            )
            │
            └─[ Deep Agent Loop ]──────────────────────────────────────┐
                    │                                                   │
                    ▼                                                   │
                "model" node  ──► LLM call                             │
                    │                                                   │
                    ├─ AIMessage with tool_calls                        │
                    │       └─► yield LangChainEvent(is_final=False)   │
                    │               (working / intermediate)            │
                    │                                                   │
                    └─ AIMessage with text content                      │
                            └─► yield LangChainEvent(is_final=True)   │
                    │                                                   │
                "tools" node  ──► execute tool functions               │
                    │  built-ins: write_todos, ls, read_file,          │
                    │             write_file, edit_file, glob, grep,   │
                    │             execute, task                         │
                    │  user tools: any callables passed via tools=      │
                    └──────────────── ToolMessage(result) ──────────────┘
```

---

## 6. Discord Transport

`DiscordAgentServer` is a second transport in front of the same `BaseAgent`. Where `AIAgentServer` speaks A2A over HTTP, this one speaks the Discord gateway protocol and consumes agent events directly — no A2A task lifecycle involved.

```
Discord gateway (discord.py Client)
    │  on_message
    ▼
DiscordAgentServer.handle_message
    │
    ├─ skip bots and the bot's own messages
    │
    ├─ extract_query(message)
    │       ├─ DM                      ──► whole message
    │       ├─ @mention                ──► message minus the mention
    │       ├─ command_prefix          ──► message minus the prefix
    │       └─ otherwise               ──► None  (stay silent)
    │
    ├─ session_id(message)   channel / user / global scope
    │       └─ per-session asyncio.Lock  (turns never interleave)
    │
    └─► BaseAgent.run_query(query, session_id)
            │
            ├─ working event        ──► post / edit "⏳ …" status message
            │
            ├─ confirmation request ──► §4 flow; the asker answers by
            │                            raw_reaction_add (✅ / ❌) or by
            │                            typing yes / no — first one wins
            │                              └─► agent.tool_confirmation(...)
            │                                      └─ resumed event stream
            │
            └─ final event          ──► remove_think_content → split_message
                                            └─► channel.send(chunk) …
```

Both agent backends work unchanged: the server only relies on the `BaseAgent`
contract (`run_query`) plus duck-typed event attributes (`is_final_response()`,
`content.parts`, `long_running_tool_ids`, `actions.requested_tool_confirmations`),
which `AIAgent` and `LangChainAIAgent` both satisfy. Tool approval is only
offered when the agent exposes `tool_confirmation` (the ADK path); otherwise the
bot reports that the pending call cannot be resumed.

---

## 7. Session & Memory Model

```
                 Google ADK path                LangChain path
                 ───────────────                ──────────────
Session store:   InMemorySessionService         MemorySaver (LangGraph)
Session key:     (app_name, user_id, session_id) thread_id = session_id
Scope:           process lifetime               process lifetime
Multi-turn:      ✓  (Runner replays history)   ✓  (checkpoint per thread)
Cross-process:   ✗  (in-memory only)           ✗  (in-memory only)
```

---

## 8. Registries & Isolation

Each `AIAgent` / `LangChainAIAgent` instance creates its own subclass of `SkillRegistry` with an isolated `_skills`, `_metadata`, and `_tool_registry_class` dict. This prevents tool name collisions between agents running in the same process.

```
Global ToolRegistry  ──► built-in tools (builtin_tools/*.py)

Agent A ──► InstanceSkillRegistry_A ──► InstanceToolRegistry_A
                skills: [skill_x]           tools: [fn_from_skill_x]

Agent B ──► InstanceSkillRegistry_B ──► InstanceToolRegistry_B
                skills: [skill_y]           tools: [fn_from_skill_y]

    ↑ no cross-contamination between A and B
```

---

## 9. Key Classes at a Glance

| Class | File | Responsibility |
|---|---|---|
| `AgentFactory` | `agent_factory.py` | Parse YAML → create `AIAgent` |
| `AIAgent` | `ai_agent.py` | Google ADK agent (Runner, sessions, A2A integration) |
| `LangChainAIAgent` | `langchain_ai_agent.py` | deepagents-backed deep agent (same public interface as AIAgent) |
| `AIAgentServer` | `ai_agent_server.py` | Starlette HTTP server (A2A / JSON-RPC routes) |
| `DiscordAgentServer` | `discord_agent_server.py` | Discord gateway bot (mentions / DMs ↔ agent events) |
| `AIAgentExecutor` | `ai_agent_executor.py` | Bridges A2A task lifecycle ↔ agent events |
| `BaseExecutor` | `base_executor.py` | Abstract executor (task init, error handling) |
| `ToolRegistry` | `tool_registry.py` | Name → callable map; auto-discovers scripts |
| `SkillRegistry` | `skill_registry.py` | Name → directory map; reads `SKILL.md` frontmatter |
| `ModelFactory` | `model_factory.py` | Provider string → LangChain / ADK model object |
| `AgentRegistry` | `agent_registry.py` | Optional global registry for named agent lookup |
