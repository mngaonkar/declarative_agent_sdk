# Google ADK runtime — design spec

`agent_framework: adk`. Google's Agent Development Kit runs the whole loop:
the SDK builds an ADK `Agent`, hands it the tools, and translates ADK events
into `AgentEvent`s.

Code: `agents/adk/`.

## `agents/adk/__init__.py`

**Does:** exports `ADKAIAgent`.

## `agents/adk/agent.py`

**Does:** `ADKAIAgent`, a `BaseAgent` backed by an ADK `Runner` with an
in-memory session service.

**Rules:**

- ADK-1: Tools are the core tools (TOOLS-REG-3), then the `tools:` list. With
  no `tools:` list, every tool from the listed skills' `scripts/` folders is
  added too; with a list, a skill tool must be named in it to be added.
  Duplicates are dropped.
- ADK-2: With `tools_approval_required`, every tool is wrapped with
  `require_confirmation=True`, so ADK pauses for approval and the SDK emits a
  `tool_approval` event (CORE-EVT-3).
- ADK-3: `tool_confirmation(context_id, session_id, yes)` resumes the paused
  call by sending ADK a `FunctionResponse` with `{"confirmed": yes}`.
- ADK-4: Automatic function calling is capped at `MAX_REMOTE_CALLS` rounds.
- ADK-5: With `enable_truncation`, the prompt is trimmed to `context_window`
  before each model call (CORE-TOK-1).
- ADK-6: `provider` + `model` go through `ModelFactory` (MODEL-1).
- ADK-7: Each ADK event becomes one `AgentEvent` via `from_adk_event`.
- ADK-8: With `mcp_servers`, the agent starts the shared MCP hub (kept as
  `agent.mcp`) and adds its tools after the others (MCP-RT-2). They ask for
  confirmation by their server's `approval` (MCP-APPR-1), not by ADK-2.

**Tests:** `tests/test_agent_factory.py`.

## `agents/adk/plugins/__init__.py`

**Does:** exports the context plugin helpers (`get_updated_context`,
`AgentContext`).

## `agents/adk/plugins/context_updater.py`

**Does:** `get_updated_context(agent_name)`, a convenience wrapper over
`AgentContext` (CORE-CTX-1).
