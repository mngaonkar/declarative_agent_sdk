# MCP tools — design spec

**Status:** phases 1–3 built — the hub, config, results, all three runtimes
and the board (see Phases). What is left is marked *(later)*. A file not written yet stays under a `###`
heading; once written, its heading becomes `` ## `path` `` so
`tests/test_design_spec.py` checks it.

## Goal

Let an agent use the tools of any MCP (Model Context Protocol) server —
GitHub, Postgres, a browser, a filesystem, an internal service — from a few
lines of its `agent.yaml`, with no Python. Today a new tool means a function
in `tools/builtin/` or a skill script.

## Decisions

- **D1 — one shared layer for all three runtimes.** A single MCP client in
  the SDK connects to the servers and calls their tools. Each runtime gets
  thin adapters over it; none uses its framework's own MCP support. So the
  three behave the same: same names, same allowlist, same result handling,
  same errors.
- **D2 — an optional extra.** The MCP client library is installed with
  `declarative-agent-sdk[mcp]`. An agent with no `mcp_servers` never imports
  it. An agent that declares servers without the extra fails to build with a
  message saying to install it.
- **D3 — no approval prompt by default.** MCP tools run without asking
  (`approval: false` is the default for a server). A server marked
  `approval: true` asks for every tool, and `approval: destructive` asks only
  for tools the server marks destructive. A stdio server is a program running
  on the agent's machine, as powerful as `exec_command`; add only servers you
  trust.

## The YAML

```yaml
mcp_servers:
  github:
    transport: http                  # stdio | http | sse
    url: https://api.githubcopilot.com/mcp/
    headers:
      Authorization: "Bearer ${GITHUB_TOKEN}"
    tools: [search_code, get_file_contents]
  files:
    transport: stdio
    command: npx
    args: ["-y", "@modelcontextprotocol/server-filesystem", "${WORKSPACE}"]
    approval: destructive
```

**Rules — config:**

- MCP-CFG-1: `mcp_servers` maps a server name to its settings. A name is
  `[a-z0-9_-]+`; it becomes the tool-name prefix (MCP-TOOL-1).
- MCP-CFG-2: `transport` is `stdio`, `http` (streamable HTTP) or `sse`.
  `stdio` needs `command`, optionally `args`, `env` and `cwd`; `http` and
  `sse` need `url`, optionally `headers`.
- MCP-CFG-3: `${NAME}` in any string value is replaced from the agent's
  environment (its `.env`), so tokens never sit in `agent.yaml`. `${WORKSPACE}`
  is the agent's workspace directory. An unset variable is a config error
  naming it, not an empty string.
- MCP-CFG-4: `tools` is an allowlist of the server's tool names; omitted means
  all of them. A listed name the server does not have is a warning naming the
  ones it does have.
- MCP-CFG-5: `approval` is `false` (default), `true` or `destructive`
  (MCP-APPR-1).
- MCP-CFG-6: `timeout` (seconds, default 60) bounds one tool call;
  `startup_timeout` (default 30) bounds connecting and listing tools.
- MCP-CFG-7: `enabled: false` keeps a server in the file without starting it.
- MCP-CFG-8: The top-level `tools:` list keeps its meaning (non-core tools
  from the registry and skills). MCP tools are not named in it; their
  allowlist is the server's `tools`.

## How it fits together

```
agent.yaml ─► CommonAgentConfig.mcp_servers ─► McpHub (one per agent process)
                                                   │  background event loop
                                                   │  one session per server
                                                   ▼
                                    McpTool(name, description, schema, hints)
                       ┌───────────────────────────┼───────────────────────────┐
                       ▼                           ▼                           ▼
             lean: LeanToolRegistry.add   adk: BaseTool subclass   deepagent: StructuredTool
```

The hub is async (the MCP client library is), and the lean loop is
synchronous, so the hub runs its own event loop on a background thread and
offers both `call(...)` (blocking, for lean) and `acall(...)` (for ADK and
deepagent).

## Files

## `tools/mcp/__init__.py`

**Does:** the package's public names: `McpServerConfig`, `McpTool`, `McpHub`,
`parse_mcp_servers`, `register_lean`, `tool_name`, `McpError`,
`McpConfigError`. Importing it does not import the MCP client library; only
`McpHub.start` does (D2).

## `tools/mcp/config.py`

**Does:** reads and checks `mcp_servers` (MCP-CFG-1..8) into
`McpServerConfig` objects, filling in `${…}` from the environment.

- MCP-CFG-9: A server named `on`, `off`, `yes` or `no` without quotes is read
  by YAML as a boolean; that is refused with a message to quote the name.

**Interface:** `parse_mcp_servers(raw, env, workspace, base_dir=None) -> List[McpServerConfig]`.

**Tests:** `tests/test_mcp_config.py`.

## `tools/mcp/hub.py`

**Does:** starts every enabled server, lists its tools, calls them, and shuts
them down. Owns the background event loop.

**Rules — tools:**

- MCP-TOOL-1: A tool is exposed as `<server>__<tool>` (two underscores),
  cut to 64 characters, so two servers can never collide. Its description is
  the server's, prefixed with `[<server>]`.
- MCP-TOOL-2: A core tool's name always wins over an MCP tool's (they cannot
  meet, given MCP-TOOL-1, but the rule stands for renamed tools).
- MCP-TOOL-3: The tool's input schema is passed through as the JSON schema
  the model sees; its `readOnlyHint` and `destructiveHint` annotations are
  kept on `McpTool` (MCP-APPR-1, MCP-BOARD-3).

**Rules — lifecycle:**

- MCP-LIFE-1: Servers start when the agent is built, in parallel, each within
  `startup_timeout`. A server that fails to start is logged with its error
  (and, for stdio, the last lines of its stderr) and left out; the agent
  still starts with the rest. Its tools are simply not offered.
- MCP-LIFE-2: A call to a server whose connection dropped reconnects once and
  retries; a second failure is the tool's error result, not an exception out
  of the loop.
- MCP-LIFE-3: A call that exceeds `timeout` is cancelled and answered as an
  error naming the timeout.
- MCP-LIFE-4: The hub closes every session and stops every stdio process when
  the agent shuts down, and on interpreter exit.
- MCP-LIFE-5: `status()` reports each server: connected or not, its tools,
  last error, when it connected, its approval setting, and `tool_hints`
  (per exposed name: `read_only`, `destructive`, `needs_approval`). The A2A
  server serves it as `GET /mcp` (MCP-BOARD-1).
- MCP-LIFE-6: A closed connection (the client's `CONNECTION_CLOSED` error)
  counts as the link failing (MCP-LIFE-2); any other `MCPError` is the
  server's answer to the call and is returned as the tool's error.

**Interface:** `McpHub(servers, workspace).start()`, `.tools`, `.call(name, args)`,
`.acall(name, args)`, `.status()`, `.close()`; `McpTool`; `tool_name(server, tool)`.

**Tests:** `tests/test_mcp_hub.py`, against `tests/mcp_server_fixture.py` over
stdio and streamable HTTP.

## `tools/mcp/results.py`

**Does:** turns an MCP tool result into the text a model reads.

**Rules — results:**

- MCP-RES-1: Text content is joined in order and returned as the result.
- MCP-RES-2: Image, audio and embedded-resource content is written to the
  workspace under `mcp/<server>/` and replaced by a line naming the file and
  its `/files/` link (`core/shares.py`), so the board previews it.
- MCP-RES-3: Structured content is returned as compact JSON when there is no
  text.
- MCP-RES-4: A result marked `isError` is returned prefixed `Error:` so the
  model sees it failed.
- MCP-RES-5: Text over 20,000 characters is cut there, with a note saying how
  much was left out. Aged results are elided like any tool payload
  (`tool_payload_keep` / `tool_payload_max_chars`).

**Interface:** `render_result(result, workspace, server, tool)`.

## `tools/mcp/adapters.py`

**Does:** starts an agent's hub (`start_hub`) and presents its tools to each
runtime. The runtime frameworks are imported inside each function, so a lean
agent never imports ADK or LangChain for MCP.

**Rules — runtimes:**

- MCP-RT-0: Every runtime starts its servers the same way: `start_hub(
  mcp_servers, workspace, config_dir)` parses the block (MCP-CFG), starts the
  hub and keeps it as `agent.mcp` (None without servers). A tool whose name
  is already taken by the runtime's own tools is skipped (MCP-TOOL-2).
- MCP-RT-1 (lean): each `McpTool` is registered with
  `LeanToolRegistry.add(name, description, properties, required, handler)`;
  the handler calls `hub.call` and returns MCP-RES text.
- MCP-RT-2 (adk): each `McpTool` is a `google.adk.tools.BaseTool` subclass
  declaring the JSON schema as `parameters_json_schema` (Gemini reads it
  directly; ADK's LiteLLM path turns it into an OpenAI tool), whose
  `run_async` calls `hub.acall` and returns `{"result": text}`. When the tool
  needs approval it asks with `tool_context.request_confirmation`, exactly as
  ADK's `FunctionTool(require_confirmation=True)` does (ADK-2, ADK-3), and a
  rejected call returns `{"error": "This tool call is rejected."}`.
- MCP-RT-3 (deepagent): each `McpTool` is a LangChain `StructuredTool` with
  the JSON schema as `args_schema` (no schema inference), a function calling
  `hub.call` and a coroutine calling `hub.acall`. Tools that need no approval
  are taken out of `interrupt_on` (DEEP-3), unless the YAML gives its own
  `interrupt_on`; the agent keeps the result as `agent.interrupt_on`.

**Interface:** `start_hub`, `register_lean(hub, registry) -> auto-approve
names`, `adk_tools(hub, approval_required, taken)`, `langchain_tools(hub,
taken) -> (tools, auto-approve names)`.

**Tests:** `tests/test_mcp_runtimes.py` (all three runtimes), `tests/test_mcp_hub.py`
(`TestLean`).

**Rules — approval:**

- MCP-APPR-1: `approval: false` puts the server's tools on the runtime's
  auto-approve list; `true` leaves them all to the approval gate;
  `destructive` leaves to the gate only tools with `destructiveHint: true`.
  `tools_approval_required: false` on the agent still turns every prompt off.

## Changes to existing files

- `core/agent_config.py`: `CommonAgentConfig.mcp_servers` (raw mapping,
  default empty), parsed by `tools/mcp/config.py`.
- `core/agent_factory.py`: builds the hub when `mcp_servers` is non-empty and
  passes it to the runtime; resolves relative `cwd` against the YAML's folder.
- `agents/lean/agent.py`, `agents/lean/runtime/tools.py`: register hub tools
  (MCP-RT-1); add the auto-approve names (MCP-APPR-1).
- `agents/adk/agent.py`, `agents/deepagent/agent.py`: MCP-RT-2, MCP-RT-3.
- `transports/a2a/server.py`: `GET /mcp` returns `hub.status()`.
- `pyproject.toml`: the `mcp` extra (the official `mcp` Python SDK), and in
  `all`.
- `packaging/deploy/install.sh`: installs the SDK with the `mcp` extra, so a
  board-made agent can use servers without a reinstall.

## agent-board

- MCP-BOARD-1: The board's agent poll (`/api/agents/metrics`, while the page
  is open) also reads each agent's `/mcp` (AC-METRICS-5) and returns it as
  `mcp` keyed by agent id. The Agents tab's MCP column shows each server:
  connected with its tool count, or not connected, with the address, the
  tools or the error, and the approval setting on hover.
- MCP-BOARD-2: The router keeps the last `/mcp` reading per agent (an agent
  that could not be read keeps its last one). An agent's own prompt names its
  connected MCP servers and their tools, at most 12 per server then "+N
  more" (ROUTER-PROMPT-2); the roster shows `· MCP: <servers>` after each
  peer that has any (ROUTER-PROMPT-1). An agent the poll has not read yet has
  no MCP lines.
- MCP-BOARD-3: The channel's `safe` level (ROUTER-APPR-2), for an MCP tool of
  that agent's: one marked destructive and not read-only goes to a person
  (never auto-approved, never auto-denied); a read-only one, or one with no
  hints, is approved like any other non-shell tool. `all` approves
  everything. The hints come from `/mcp` (`tool_hints`, MCP-LIFE-5).
- MCP-BOARD-4: Probing a machine also looks for `npx` and `uvx`, which run
  most stdio servers, and stores what it found (`runners`, AC-STORE-1). The
  Machines tab shows each with ✓ or ✗.
- MCP-BOARD-5: MCP servers are defined once, in the board's MCP catalog, and
  attached to agents (the next section). Servers can still be written into an
  agent's `agent.yaml` by hand.

## The MCP catalog (agent-board)

Instead of editing every agent's YAML, the board keeps a catalog of MCP
server definitions on its **MCP** tab; an agent's Settings attaches entries
to it. Each agent still runs its own copy of each server (a stdio server has
to run on the agent's machine, to reach that machine), so the catalog holds
definitions, not running servers.

**Rules — catalog:**

- MCP-CAT-1: A catalog entry is a name (MCP-CFG-1) and the same settings as an
  `mcp_servers` entry (MCP-CFG-2..7), checked by the same parser before it is
  saved. Names are unique on the board.
- MCP-CAT-2: Secrets are never in the catalog. An entry refers to them as
  `${NAME}`; their values live only in each agent's `.env` (AC-5).
- MCP-CAT-3: Setting a value ("Set…" on the MCP tab) writes it into the `.env`
  of every agent the entry is attached to, then restarts them. The value is
  sent once, never stored on the board and never returned. Only names the
  entry refers to can be set.
- MCP-CAT-4: Presets (fetch, time, git, filesystem, GitHub, browser) fill in a
  new entry; they are a starting point, not separate kinds.

**Rules — attaching:**

- MCP-CAT-5: Attaching an entry to an agent stores the link, with optional
  per-agent overrides of `tools` (allowlist) and `approval`. Detaching
  removes it. Deleting an agent removes its links.
- MCP-CAT-6: The board writes an agent's attached entries into its
  `agent.yaml` under its own key, `board_mcp_servers`, and restarts the
  agent. The board owns that key entirely and never touches `mcp_servers`,
  so servers written by hand are left alone. An agent with nothing attached
  has no `board_mcp_servers` key. The rest of the file is kept as the
  Settings form keeps it (AC-CONFIG: leading comments kept, keys re-emitted).
- MCP-CAT-7: The agent reads `board_mcp_servers` and `mcp_servers` together.
  On a name in both, the hand-written `mcp_servers` entry wins, with a
  warning.
- MCP-CAT-8: Attaching warns (it does not refuse) when the agent's machine
  lacks the runner the command needs (`npx` / `uvx`, MCP-BOARD-4), when a
  `${NAME}` the entry uses is not set in the agent's `.env`, and when a
  hand-written server already has the name.
- MCP-CAT-9: Editing an entry with agents attached asks whether to update
  them now; yes rewrites each one's `board_mcp_servers` and restarts it.
  Deleting an entry detaches it from every agent first, the same way.
- MCP-CAT-10: An agent whose `board_mcp_servers` already matches is not
  rewritten or restarted.

**Rules — MCP tab:**

- MCP-CAT-11: One row per entry: name, what it runs (command or URL),
  approval, the `${NAME}`s it uses with Set…, and each attached agent with
  its connection state from the agent poll (MCP-BOARD-1). Edit and Delete.
- MCP-CAT-12: An agent's Settings has an MCP tab listing the catalog: attach
  or detach each entry, with the per-agent overrides.

## `agent_board/mcp_catalog.py`

**Does:** the catalog's logic, apart from storage and routes: checking an
entry, presets, the `${NAME}`s an entry uses, and writing an agent's
`board_mcp_servers`.

**Rules:**

- MCP-CATF-1: `check(name, spec)` runs the entry through the agent's own MCP
  parser (`parse_mcp_servers`), with each `${NAME}` filled by a placeholder
  for the check only (MCP-CAT-1, MCP-CAT-2).
- MCP-CATF-2: `clean_spec` keeps only the known settings and drops empty
  ones; `render(spec, overrides)` applies the per-agent `tools` and
  `approval`, leaving the catalog entry unchanged.
- MCP-CATF-3: `apply(yaml, servers)` sets or removes `board_mcp_servers` and
  re-emits the file as the Settings form does (leading comments kept). It
  never reads or changes `mcp_servers` (MCP-CAT-6).
- MCP-CATF-4: `runner_for` is `npx` / `uvx` when the stdio command is one of
  them; `hand_written` lists the agent's own `mcp_servers` names (MCP-CAT-8).

**Interface:** `PRESETS`, `BOARD_KEY`, `check`, `clean_spec`, `referenced_vars`,
`runner_for`, `render`, `board_servers`, `apply`, `current`, `hand_written`.

**Tests:** `tests/test_mcp_catalog.py`.

## Security

- A stdio server runs a program with the agent's user on the agent's machine;
  with D3 its tools run without asking. Treat adding one like granting
  `exec_command`.
- A remote server's results are text from elsewhere — a prompt-injection
  channel, like a fetched web page. They are tool results, never
  instructions.
- Tokens come from `.env` through `${…}` and never appear in logs: the hub
  logs a server's URL and command, never its headers or `env` values.

## Tests

- `tests/test_mcp_catalog.py`: the catalog — checking entries, attaching and
  detaching (agent.yaml written, hand-written servers and comments kept, the
  agent's own entry winning), warnings, edit with and without sync, delete,
  secrets reaching only the agents' `.env`, overrides.
- `tests/test_board_mcp.py`: reading `/mcp`, the metrics poll, prompts and
  roster, the `safe` level with hints, and `npx` / `uvx` probing and storage.
- `tests/mcp_server_fixture.py`: a tiny MCP server (echo, add, an image, a
  destructive tool, a slow one, one that fails, one that kills the process)
  run over stdio, or streamable HTTP behind a token, so tests need no
  network.
- `tests/test_mcp_config.py`: MCP-CFG rules.
- `tests/test_mcp_hub.py`: names, schemas, results (text, image → file link,
  error, cut), timeout, a server that fails to start, reconnect, shutdown.
- `tests/test_mcp_runtimes.py`: each runtime sees the same tools and calls
  them; approval for `false` / `true` / `destructive`, and
  `tools_approval_required: false` turning it all off, the same in all three.

## Phases

1. **Hub, config, results, lean adapter**, the extra, the A2A `/mcp` route
   and the tests above. The default runtime and the board are then usable.
   *Built.*
2. **ADK and deepagent adapters** (MCP-RT-2, MCP-RT-3). *Built.*
3. **Board:** MCP-BOARD-1..4. *Built.*
4. **MCP catalog** on the board (MCP-CAT-1..12). *Built.*
5. **Later:** MCP resources (read like `read_shared`), MCP prompts (offered
   like skills), OAuth for remote servers, tool search for servers with very
   many tools.

## Open questions

- Context cost: every tool description goes into every model call. Should an
  agent warn when MCP tools push its tool list past a size (say 40 tools or
  8,000 tokens of schemas)?
- Should a server be shareable across agents on one machine (one process
  serving several agents), or always one per agent? One per agent is simpler
  and is the plan.
