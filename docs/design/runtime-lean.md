# Lean runtime — design spec

`agent_framework: lean`. A small, dependency-light agent loop: it plans,
calls tools one step at a time, reflects, and retries. It talks to any
OpenAI-compatible chat server (OpenAI, vLLM, llama.cpp, LiteLLM). Skills are
loaded progressively: the model sees one line per skill and loads the full
instructions only when it needs them.

Code: `agents/lean/`.

## `agents/__init__.py`

**Does:** marks `agents` as a package.

## `agents/lean/__init__.py`

**Does:** exports `LeanAIAgent`.

## `agents/lean/agent.py`

**Does:** `LeanAIAgent`, the `BaseAgent` adapter around the loop. It builds the
model client, the skills, the tools and the loop from the YAML values, and
turns loop events into `AgentEvent`s.

**Rules:**

- LEAN-AGENT-1: The base URL is `endpoint.url` when set, else the provider's
  default (`openai` → `https://api.openai.com/v1`).
- LEAN-AGENT-2: The API key comes from the environment by provider (see
  [agent-yaml.md](agent-yaml.md#credentials)). A `vllm` agent with no key
  sends the placeholder `not-needed`.
- LEAN-AGENT-3: Tools named in `tools:` are resolved through `ToolRegistry`
  and added to the lean tool registry. An unknown name logs a warning and is
  skipped; it never stops the agent from starting.
- LEAN-AGENT-4: `workspace_directory` and `skills_directory` are created if
  missing. `workspace_directory` is kept on the agent so the A2A server can
  serve it (CORE-SHARE-5).
- LEAN-AGENT-9: With `mcp_servers`, the agent starts an `McpHub` (kept as
  `agent.mcp`, else `None`) and registers its tools (MCP-RT-1); tools that
  need no approval (MCP-APPR-1) join the loop's auto-approve set. A bad block
  fails the build with the reason; a server that will not start is left out.
- LEAN-AGENT-5: Loop events map one to one: `tool_approval` →
  `AgentEvent.tool_approval`, `final` → `final_text`, `error` → `error`,
  anything else → `status`.
- LEAN-AGENT-6: An A2A message whose data part is a
  `function_response` with `confirmed` is a tool-approval answer and resumes
  the paused turn instead of starting a new one.
- LEAN-AGENT-7: The loop is synchronous; it is run on a worker thread so the
  event loop (A2A server, Discord) is never blocked.
- LEAN-AGENT-8: `reset_context(session_id)` forgets one session, or all of
  them when no id is given, and reports how many it cleared.
  `context_report()` reports how big the prompt prefix has grown.

**Tests:** `tests/test_lean_runtime.py`, `tests/test_lean_a2a.py`.

## `agents/lean/runtime/__init__.py`

**Does:** exports the runtime parts: `LeanLoop`, `LeanToolRegistry`,
`SkillRegistry`, `LeanLLMClient`.

## `agents/lean/runtime/chat_backend.py`

**Does:** `ChatBackend`, the protocol the loop needs from a model client: one
`chat(messages, tools)` call returning an OpenAI-style assistant message.

**Rules:**

- LEAN-BACKEND-1: The loop depends only on this protocol, so tests can drive
  it with a fake client.

## `agents/lean/runtime/llm.py`

**Does:** `LeanLLMClient`, a minimal client for `POST /chat/completions`.

**Rules:**

- LEAN-LLM-1: No streaming. One request per model round; the reply's
  assistant message is returned as a dict.
- LEAN-LLM-2: A 400 that says the model wants `max_completion_tokens`, or
  rejects a custom `temperature`, is fixed once and retried.
- LEAN-LLM-3: A server that cannot parse the model's own tool call (the JSON
  broke part-way) raises `MalformedToolCall`, which says roughly where it
  broke. It is retried only `retry_malformed_tool_calls` times (default 0),
  because each retry is another full generation.
- LEAN-LLM-4: A timeout is not retried; it raises `LLMError` with advice to
  raise `request_timeout`. Default timeout 300 s per model call.
- LEAN-LLM-5: Every failure is an `LLMError` with a readable message, never raw
  server JSON or a bare exception.

**Tests:** `tests/test_lean_runtime.py` (`TestMalformedToolCall`).

## `agents/lean/runtime/loop.py`

**Does:** `LeanLoop`, the turn engine. It keeps one history per session,
sends it with the tool schemas, runs the tool calls that come back, and
decides whether the turn is finished.

**Rules:**

- LEAN-LOOP-1: The system prompt tells the model to work in cycles:
  deliberate and plan, do one step with tools, reflect, retry on failure. When
  it answers without a tool call it must end with one control tag:
  `[[decision:continue]]`, `[[decision:done]]` or `[[decision:ask]]`. An
  optional `[[phase:plan|act|reflect|done|ask]]` labels progress. Tags are
  removed before text is shown.
- LEAN-LOOP-2: `done` ends the turn with a final answer; `ask` ends it with a
  question for the user; `continue` (or no tag on a plan) runs another round.
  After `max_no_tool_continues` rounds without tools the loop asks for an
  answer instead of continuing.
- LEAN-LOOP-3: A turn makes at most `max_tool_iterations` model rounds. Near
  the end the model is told it is running out of rounds.
- LEAN-LOOP-4: After `max_step_retries` consecutive failed tool calls the
  model must stop and ask the user for help.
- LEAN-LOOP-5: When approval is required, a tool call pauses the turn with a
  `tool_approval` event and the state is kept as `PendingApproval`.
  `resume(session_id, approved)` runs or refuses the call and carries on.
  Tools in `AUTO_APPROVE_TOOLS` (`Skill`, `list_skills`, `list_dir`,
  `read_file`, `send_to_agent`, `list_agents`) never pause.
- LEAN-LOOP-6: The history sent to the server is always a valid OpenAI tool
  chain: every assistant tool call is followed by a tool message for each id.
  Missing answers are filled with a short synthetic reply before any other
  message is added.
- LEAN-LOOP-7: Large values in older tool calls and results (beyond the last
  `tool_payload_keep`, over `tool_payload_max_chars`) are shortened once, in
  the stored history, and never recomputed, so a server's prefix cache stays
  valid. The call, its id and the path stay.
- LEAN-LOOP-8: `<think>` blocks and plan text are shown as status updates, not
  as the answer.
- LEAN-LOOP-9: When the server rejects a tool call as malformed
  (`MalformedToolCall`), nothing ran, so the loop tells the model where the
  call broke and asks it to redo the work in smaller calls (`edit_file`,
  `write_link`, `write_file` with `append`). At most twice per turn
  (`MAX_MALFORMED_RECOVERIES`); then the turn ends with the error.
- LEAN-LOOP-10: Any other `LLMError` ends the turn with an `error` event.
- LEAN-LOOP-11: History is trimmed to `history_limit` messages without
  starting in the middle of a tool block.

**Interface:** `LeanLoop.run(query, session_id)`, `resume(session_id,
approved)`, `reset(session_id)`, `reset_all()`, `context_report()`.

**Tests:** `tests/test_lean_runtime.py`, `tests/test_file_tools.py`
(`TestMalformedRecovery`).

## `agents/lean/runtime/skills.py`

**Does:** `SkillRegistry`, Anthropic-style skills: each is a folder with a
`SKILL.md` that starts with YAML frontmatter (`name`, `description`).

**Rules:**

- LEAN-SKILL-1: Level 1: the system prompt lists one line per skill (name and
  description). Level 2: the `Skill` tool returns the full `SKILL.md` body.
  Level 3: the body lists the skill's bundled files, read on demand.
- LEAN-SKILL-2: `discover()` re-reads the folder, so a skill written during a
  turn is usable straight away.

## `agents/lean/runtime/tools.py`

**Does:** `LeanToolRegistry`, the lean agent's tools as OpenAI function
schemas with handlers: its own file and skill tools, plus Python callables
bridged in from `ToolRegistry`.

**Rules:**

- LEAN-TOOLS-1: Lean's own tools: `Skill`, `list_skills`, `list_dir`,
  `read_file`, `write_file`, `edit_file`, `write_link`, `run_script`. The
  SDK-wide core tools (TOOLS-REG-3) are bridged in after them; a name already
  taken is skipped, so lean's own versions win.
- LEAN-TOOLS-2: Writes (`write_file`, `edit_file`, `write_link`) are allowed
  only under the workspace, the `attachments/` folder beside it, the skills
  tree and `/tmp/`. Anything else is refused with the reason.
- LEAN-TOOLS-3: `write_file(path, content, append=false)`. With `append` the
  content is added to the end, so a long file can be written in parts.
- LEAN-TOOLS-4: `edit_file(path, old_text, new_text, replace_all=false)`
  replaces exactly one occurrence; zero or several (without `replace_all`) is
  an error that says how to fix the call.
- LEAN-TOOLS-5: `write_link(url, path, overwrite=false)` saves another agent's
  file link straight to disk and returns a short receipt, never the content.
  An existing file needs `overwrite`. See TOOLS-LINK-1.
- LEAN-TOOLS-6: A write to a `SKILL.md` is checked as the whole file it
  produces: frontmatter with `name` equal to the folder name, a
  `description`, and a body. Loose `.py` files directly under `skills/` are
  refused.
- LEAN-TOOLS-7: After a write inside the workspace, the result ends with
  `Link for other agents: <link>` (CORE-SHARE-5).
- LEAN-TOOLS-8: `run_script` runs a skill's `.py` file with `args` and a
  `tool(name, args)` helper, and returns what it printed and its `result`.
- LEAN-TOOLS-9: Arguments from the model are coerced to the Python type hints
  (`"30"` → 30, `"true"` → True), and unknown argument names are dropped.
- LEAN-TOOLS-10: A handler that raises returns `Error: <type>: <message>`; it
  never crashes the loop.

**Tests:** `tests/test_lean_runtime.py`, `tests/test_file_tools.py`.
