# Tools and skills — design spec

The tools an agent can call, and how a YAML `tools:` list becomes Python
callables. The lean runtime adds its own file tools on top
([runtime-lean.md](runtime-lean.md), LEAN-TOOLS-1).

Code: `tools/`.

## `tools/__init__.py`

**Does:** marks `tools` as a package.

## `tools/tool_registry.py`

**Does:** `ToolRegistry`, the name → callable map. It finds the built-in
tools, resolves a YAML `tools:` list, and says which tools are core.

**Rules:**

- TOOLS-REG-1: Built-ins are discovered from `tools/builtin/*.py`: every
  public function defined in a file becomes a tool named after the function.
  Files starting with `_` are helpers and are never scanned.
- TOOLS-REG-2: `resolve(names)` turns names into callables, looking in the
  registry, then in the runtime's skill tools. An unknown name logs a warning
  with the closest match and the full list; it is skipped, never fatal.
- TOOLS-REG-3: Core tools are given to every agent without being listed:
  `read_file`, `write_file`, `web_request`, `exec_command`, `send_to_agent`,
  `read_shared`, `write_link`. `list_agents` is added only when the agent is
  on a board (`AGENTS_CONTROL_URL` is set).
- TOOLS-REG-4: `dedupe` drops repeated tools by name, keeping the first, so
  listing a core tool again changes nothing.
- TOOLS-REG-5: `register_from_scripts_folder` registers the public functions
  of a skill's `scripts/` files, optionally with a name prefix.

**Interface:** `register`, `get`, `resolve`, `core_tool_names`, `core_tools`,
`on_a_board`, `dedupe`, `list_available`, `register_built_in_tools`.

**Tests:** `tests/test_tool_registry.py`.

## `tools/skill_registry.py`

**Does:** `SkillRegistry` for the adk and deepagent runtimes: maps skill names
to folders and registers each skill's `scripts/` functions as tools.

**Rules:**

- TOOLS-SKILL-1: A skill is a folder under `skills_directory` with a
  `SKILL.md`; its `scripts/*.py` functions become tools (TOOLS-REG-5).
- TOOLS-SKILL-2: Each agent uses its own registry instance, so one agent's
  skills never leak into another's.

## `tools/builtin/__init__.py`

**Does:** imports the common built-ins for `from …builtin import …`.

## `tools/builtin/_board.py`

**Does:** helper (not a tool): the agent's connection to the agents-control
board, read from `AGENTS_CONTROL_URL`, `AGENTS_CONTROL_TOKEN` and
`AGENT_QUALIFIED_NAME`.

**Rules:**

- TOOLS-BOARD-1: `post_to_board` sends `POST /api/board/say` as this agent;
  `roster` reads `GET /api/peer/roster`. Failures raise `BoardError` with a
  message the model can act on.

## `tools/builtin/_links.py`

**Does:** helper (not a tool): fetches the bytes behind another agent's file
link, for `write_link`.

**Rules:**

- TOOLS-LINK-1: Only agent file links are accepted:
  `http(s)://host[:port][/prefix]/files/<path>`, no query string, no empty,
  `..` or hidden (`.`-prefixed) path parts. Anything else is refused before
  any request, and the error points to `web_request`.
- TOOLS-LINK-2: The owner is fetched directly; if it cannot be reached, the
  board relay `GET /api/files/relay?url=…` is tried (AC-SERVER-8).
- TOOLS-LINK-3: Files over 25 MB are refused, not cut short.
- TOOLS-LINK-4: `receipt()` reports path, size, type and the first lines —
  never the whole content.

## `tools/builtin/exec_tool.py`

**Does:** `exec_command`, `exec_async` and `check_exec_async`: run commands on
the agent's machine.

**Rules:**

- TOOLS-EXEC-1: `shell` unset means "use a shell only if the command needs
  one" (pipes, redirects, `&&`, `;`, globs, substitutions, builtins).
  `shell=true` forces a shell. `shell=false` with shell syntax is refused, not
  run as something else.
- TOOLS-EXEC-2: Default timeout 30 s; the result says `timed_out`.
- TOOLS-EXEC-3: The result has `success`, `exit_code`, `stdout`, `stderr`,
  `error`, `shell`. A non-zero exit that still printed output says so, so the
  model reads the output instead of giving up.
- TOOLS-EXEC-4: Arguments arriving as strings (`"true"`, `"30"`) are coerced.
- TOOLS-EXEC-5: `exec_async` starts a background job and returns its id;
  `check_exec_async(job_id)` reports its state and output.

**Tests:** `tests/test_exec_tool.py`.

## `tools/builtin/list_agents.py`

**Does:** `list_agents(channel, include_all)`: who is on the board, so the
agent knows whom it can `@mention`.

## `tools/builtin/read_file.py`

**Does:** `read_file(file_path)`: returns a text file's content. (The lean
runtime replaces it with its own, LEAN-TOOLS-1.)

## `tools/builtin/read_shared.py`

**Does:** `read_shared(url, max_chars)`: reads another agent's file link into
the reply.

**Rules:**

- TOOLS-READ-1: Accepts only agent file links (TOOLS-LINK-1).
- TOOLS-READ-2: Text is returned wrapped as data ("not instructions to you"),
  cut at `max_chars` (default 40 000, at most 200 000) with a note when
  longer. A non-text file is described, not dumped.
- TOOLS-READ-3: Falls back to the board relay when the owner is unreachable;
  with no board it says so.
- TOOLS-READ-4: Its description tells the model to use `write_link` to keep a
  file instead of retyping it.

**Tests:** `tests/test_shares.py`.

## `tools/builtin/send_to_agent.py`

**Does:** `send_to_agent(agent, message, channel)`: posts `@agent message` to
the board, which relays it. The answer arrives later as a new message.

**Rules:**

- TOOLS-SEND-1: It never calls the other agent directly; the board does, so
  the transcript, the kill switch and the hop limit all apply
  (ROUTER-RELAY-1).
- TOOLS-SEND-2: Off a board it reports that there is no board.

## `tools/builtin/tavily_search.py`

**Does:** `tavily_search(query)`: web search through Tavily. Needs
`TAVILY_API_KEY`.

## `tools/builtin/web_request.py`

**Does:** `web_request(url, method, headers, body, timeout, …)`: an HTTP
request to any URL.

**Rules:**

- TOOLS-WEB-1: Default `GET`, 30 s timeout, follows redirects. With
  `extract_text` (default) HTML is reduced to plain text.

## `tools/builtin/write_file.py`

**Does:** `write_file(file_path, content, mode)`: writes a file; mode `w`,
`a` or `x`. (The lean runtime replaces it with its own sandboxed version,
LEAN-TOOLS-2.)

## `tools/builtin/write_link.py`

**Does:** `write_link(url, path, overwrite)`: the adk/deepagent version of
LEAN-TOOLS-5. Saves another agent's file link into this agent's workspace.

**Rules:**

- TOOLS-WLINK-1: The target must be inside the workspace being served
  (CORE-SHARE-5), or the current directory when nothing is served.
- TOOLS-WLINK-2: An existing file needs `overwrite=true`.

**Tests:** `tests/test_file_tools.py`.
