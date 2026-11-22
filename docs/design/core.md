# Core — design spec

The framework-neutral centre of the SDK: building an agent from YAML, the one
event type every runtime emits, the base agent interface, logging, and the
workspace file links agents hand to each other.

Code: `__init__.py`, `__version__.py`, `core/`, `models/`.

## `__init__.py`

**Does:** the public import surface: `from declarative_agent_sdk import …`.

**Rules:**

- CORE-PKG-1: Re-export the classes callers build agents with: `BaseAgent`,
  the three runtimes (`LeanAIAgent`, `ADKAIAgent`, `LangChainAIAgent`),
  `AgentEvent`, `AgentFactory`, `AgentRegistry`, `ToolRegistry`,
  `SkillRegistry`, `ModelFactory`, the A2A and Discord servers and the
  workflow classes. The list lives in `__all__`.
- CORE-PKG-2: Importing the package must not need optional extras that are not
  installed (Discord, deepagents); those imports are guarded.

## `__version__.py`

**Does:** holds `__version__`, the one place the SDK version is set.

**Rules:**

- CORE-VER-1: `declarative-agent version` and `install.sh` read the version
  from here. Bump it for every release.

## `core/__init__.py`

**Does:** marks `core` as a package. No behaviour.

## `core/agent_config.py`

**Does:** reads the fields every runtime shares out of a raw config dict into
`CommonAgentConfig`.

**Rules:**

- CORE-CFG-1: `parse_common_config(config, framework, …)` returns the shared
  fields with defaults filled in (see [agent-yaml.md](agent-yaml.md) for the
  list). Runtime-specific keys are left for the runtime to read.
- CORE-CFG-2: An unset optional field stays `None`, so a runtime can tell "not
  set" from a value and apply its own default.

**Tests:** `tests/test_agent_factory.py`.

## `core/agent_context.py`

**Does:** `AgentContext`: a placeholder for extra context an agent can pull in
before a model call. Returns a default context today.

**Rules:**

- CORE-CTX-1: Must never raise; callers treat it as optional.

## `core/agent_event.py`

**Does:** `AgentEvent`, the single stream event type every runtime yields,
and `from_adk_event` to convert Google ADK events into it.

**Rules:**

- CORE-EVT-1: An event's `kind` is one of `text`, `status`, `tool_approval`,
  `error`, and it carries content parts. Build events with the class methods
  `final_text()`, `status()`, `tool_approval()` and `error()`;
  `tool_confirmation()` is an older name for `tool_approval()`.
- CORE-EVT-2: `final_text` and `error` events are final; `status` and
  `tool_approval` are not. `is_final_response()` is true for a final event
  that is not waiting on a tool approval.
- CORE-EVT-3: A tool-approval event carries the call id, tool name and
  arguments in the same shape ADK uses (`requested_tool_confirmations`), so
  transports handle all runtimes the same way.

**Tests:** `tests/test_agent_event.py`.

## `core/agent_factory.py`

**Does:** `AgentFactory` builds a `BaseAgent` from a YAML file, a YAML string
or a dict, choosing the runtime from `agent_framework`.

**Rules:**

- CORE-FAC-1: `agent_framework` picks the runtime; the legacy key `backend` is
  read when it is missing. Aliases: lean = `lean`, `simple`, `esp`, `esp32`,
  `native`; adk = `adk`, `google_adk`, `google-adk`; deepagent = `deepagent`,
  `deepagents`, `langchain`, `langgraph`. Default `lean`.
- CORE-FAC-2: The chosen runtime owns the whole loop. The factory never mixes
  frameworks.
- CORE-FAC-3: Relative paths in a YAML file (`instruction_file`,
  `skills_directory`, `workspace_directory`) are resolved against the YAML
  file's directory, not the current directory.
- CORE-FAC-4: An unknown framework name is an error that lists the valid ones.

**Interface:** `AgentFactory.from_yaml_file(path)`, `from_yaml_string(text)`,
`from_dict(config)`, `load_config(path)`; `resolve_agent_framework(config)`.

**Tests:** `tests/test_agent_factory.py`.

## `core/agent_logging.py`

**Does:** one logging setup for the whole SDK.

**Rules:**

- CORE-LOG-1: `get_logger(name)` configures logging once on first use. Level
  from `SDK_LOG_LEVEL` (default INFO), optional file from `SDK_LOG_FILE`.
- CORE-LOG-2: Calling `setup_logging` again does nothing unless `force=True`.

**Interface:** `setup_logging`, `get_logger`, `set_log_level`, `reset_logging`.

## `core/board_env.py`

**Does:** reads the agent-board environment variables.

**Rules:**

- CORE-BENV-1: `board_env(name)` returns `AGENT_BOARD_<name>`, else the legacy
  `AGENTS_CONTROL_<name>` (agent-board was called agents-control), else the
  default. Every reader of the board's URL, token, timeout or deploy dir goes
  through it, so agents installed under the old name keep working.

**Interface:** `board_env(name, default="")`.

## `core/agent_registry.py`

**Does:** `AgentRegistry`: a process-wide name → agent map, with categories
and metadata.

**Rules:**

- CORE-REG-1: Registering a name that exists replaces it.
- CORE-REG-2: `register_from_yaml_files` builds each agent with
  `AgentFactory` and registers it.

## `core/agent_state.py`

**Does:** `AgentState`, the typed dict a workflow graph passes between
nodes: `user_query`, `context`, `final_answer`, plus one key per agent output.

## `core/base_agent.py`

**Does:** `BaseAgent`, the interface every runtime implements and every
transport calls.

**Rules:**

- CORE-BASE-1: `run_query(query, session_id)` and `invoke(context)` are async
  generators of `AgentEvent`.
- CORE-BASE-2: `tool_confirmation(context_id, session_id, yes)` resumes a turn
  paused for tool approval, and yields the rest of the turn's events.
- CORE-BASE-3: `run_query_and_collect` returns the final text; `run_sync` wraps
  it and must not be called from inside a running event loop.
- CORE-BASE-4: A session id keeps one conversation's history; different ids
  never share history.

## `core/constants.py`

**Does:** shared defaults: `DEFAULT_MODEL` (`gemini-2.5-flash-lite`),
`MAX_REMOTE_CALLS` (40, ADK automatic function calling), `SKILLS_DIRECTORY`
(`skills`), `WORKSPACE_DIRECTORY` (`workspace`).

## `core/shares.py`

**Does:** maps an agent's workspace files to plain links,
`http://<agent address>/files/<path in the workspace>`, and back. The A2A
server serves those links; tools build and read them.

**Rules:**

- CORE-SHARE-1: A link is the file's path inside the workspace, URL-encoded.
  There is no share step, expiry or signature: anyone who can reach the
  agent's port can read any workspace file. Keep private files out of the
  workspace.
- CORE-SHARE-2: Only files inside the workspace are served. The path is
  resolved (symlinks included) on every read, so `..` or a symlink cannot lead
  out. Refused: 403.
- CORE-SHARE-3: A path with any part starting with `.` (`.env`, `.git/…`) is
  refused (403). Directories are not listed (404). A missing file is 404.
- CORE-SHARE-4: `relative(path)` accepts a path relative to the agent's
  directory (`workspace/report.md`), relative to the workspace (`report.md`),
  or absolute inside the workspace.
- CORE-SHARE-5: `configure(workspace, base_url)` is called once by the A2A
  server at start. `link_for(path)` returns `None` when nothing is serving or
  the path is outside the workspace, so tools can add a link only when one
  works.

**Interface:** `WorkspaceFiles.relative/url_for/resolve`, `ShareError`
(carries `status`), `configure`, `current`, `link_for`.

**Tests:** `tests/test_shares.py`.

## `core/token_utils.py`

**Does:** counts tokens (tiktoken when installed) and trims text to fit a
model's context window.

**Rules:**

- CORE-TOK-1: `fit_to_context_window` keeps room for the output tokens and a
  safety margin, and trims from the start or the end as asked.

## `core/utils.py`

**Does:** small helpers: read a file, save a file, strip `<think>` content.

## `models/__init__.py`

**Does:** marks `models` as a package.

## `models/model_factory.py`

**Does:** `ModelFactory.create_model` turns `model` + `provider` into what the
ADK runtime accepts.

**Rules:**

- MODEL-1: `google` (or no provider) returns the model name unchanged
  (Gemini). `vllm` needs `endpoint_url` and returns an OpenAI-compatible
  LiteLLM model. `openai` returns an OpenAI model.
- MODEL-2: Any other provider is a `ValueError` naming the supported ones.
