# A2A transport — design spec

Serves any `BaseAgent` over A2A (Agent-to-Agent: JSON-RPC over HTTP), and
calls other A2A agents. This is how the board and other agents reach an agent.

Code: `transports/__init__.py`, `transports/a2a/`.

## `transports/__init__.py`

**Does:** marks `transports` as a package.

## `transports/a2a/__init__.py`

**Does:** marks `transports.a2a` as a package.

## `transports/a2a/server.py`

**Does:** `AIAgentServer`, a Starlette app that serves one agent.

**Rules:**

- A2A-SERVER-1: Routes: the agent card at `/.well-known/agent-card.json`, the
  JSON-RPC endpoint, `GET /context` (how big the agent's history is),
  `DELETE /context?session=…` (forget one or all histories), and
  `GET /files/{path}` (workspace files, CORE-SHARE-1), and `GET /mcp` (the
  agent's MCP servers, MCP-LIFE-5; an empty list when it has none).
- A2A-SERVER-2: The card's URL is the agent's published address. When bound
  to `0.0.0.0` and no URL is set, it uses the address the default route goes
  out of — never `0.0.0.0`.
- A2A-SERVER-3: At start it calls `shares.configure(workspace, card URL)`, so
  file links use the address peers already dial.
- A2A-SERVER-4: `/files/` answers with the file inline, `Cache-Control:
  no-store` and `nosniff`. Errors are JSON with the reason and never echo the
  requested path.
- A2A-SERVER-5: `/context` answers 501 for a runtime that keeps no history.
- A2A-SERVER-6: There is no authentication. CORS allows any origin. Whoever
  can reach the port can send tasks and read workspace files; bind to a
  private address to limit that.

**Tests:** `tests/test_shares.py` (`TestRoute`), `tests/test_lean_a2a.py`.

## `transports/a2a/base_executor.py`

**Does:** `BaseExecutor`, shared A2A task handling: reads the query from the
request, creates the task, and fails it cleanly on error.

**Rules:**

- A2A-EXEC-1: Any exception moves the task to FAILED with a message;
  the caller never gets an empty stream with no reason.

## `transports/a2a/executor.py`

**Does:** `AIAgentExecutor`, runs the agent for one A2A request and turns its
events into task updates.

**Rules:**

- A2A-EXEC-2: `status` events → WORKING updates with the text.
- A2A-EXEC-3: A `tool_approval` event → INPUT_REQUIRED, carrying the function
  call id, name and arguments, so the caller can approve.
- A2A-EXEC-4: The final answer → an artifact and COMPLETED. An `error` event
  → FAILED.
- A2A-EXEC-5: An incoming tool-approval answer resumes the paused turn
  (LEAN-AGENT-6, ADK-3, DEEP-5).

## `transports/a2a/client.py`

**Does:** `AIAgentClient`, calls another A2A agent.

**Rules:**

- A2A-CLIENT-1: Two ways to call: stream a turn (`run`, `send_message`), or
  submit and poll (`submit`, `get_task`, `submit_tool_confirmation`). Long
  turns use submit-and-poll, because a stream's timeout limits silence, and a
  single slow model call is silent.
- A2A-CLIENT-2: Poll requests are short (`poll_timeout`, default 60 s); the
  turn itself can take as long as it needs.

**Tests:** `tests/test_ai_agent_client.py`.

## `transports/a2a/converter.py`

**Does:** `A2AConverter`, converts message parts between A2A and ADK form.

## `transports/a2a/utils.py`

**Does:** `create_agent_card(name, description, skills, url, …)` builds the
`AgentCard`; default input/output modes are text.

## `transports/a2a/formatters/__init__.py`

**Does:** marks the formatters as a package.

## `transports/a2a/formatters/response_formatter.py`

**Does:** `ResponseFormatter`, the interface: turn an agent's answer into A2A
parts.

## `transports/a2a/formatters/text_response_formatter.py`

**Does:** `TextResponseFormatter`, the default: the answer as one text part.

## `transports/a2a/formatters/a2ui_response_formatter.py`

**Does:** `A2UIResponseFormatter`, the answer as A2UI data parts for the
[A2UI client](a2ui.md).
