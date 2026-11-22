# The agent YAML file — design spec

`agent.yaml` describes one agent. `AgentFactory` reads it
(`core/agent_factory.py`), `parse_common_config` pulls the shared keys out of
it (`core/agent_config.py`), and the chosen runtime reads the rest. This page
lists the keys. It has no code sections of its own: the rules live with the
files that read them.

## Keys every runtime reads

| Key | Default | Meaning |
|---|---|---|
| `name` | required | Agent name. Also the service name when installed. |
| `description` | `""` | One line, shown in the agent card. |
| `agent_framework` | `lean` | `lean`, `adk` or `deepagent` (aliases in CORE-FAC-1). Legacy name: `backend`. |
| `instruction_file` | `""` | Markdown file with the system instructions, relative to the YAML file. |
| `model` | runtime default | Model name. |
| `provider` | lean `openai`, adk `google`, deepagent `anthropic` | `openai`, `google`, `anthropic`, `vllm`, `litellm`. |
| `endpoint.url` | provider default | Base URL of an OpenAI-compatible server (needed for `vllm`). |
| `endpoint.max_tokens` / `max_tokens` | none | Output token limit. `endpoint.` wins. |
| `endpoint.temperature` / `temperature` | none | Sampling temperature. `endpoint.` wins. |
| `endpoint.timeout` / `request_timeout` | lean 300 s | Seconds one model call may take. `endpoint.` wins. |
| `tools` | `[]` | Extra tools by name. Core tools are added anyway (TOOLS-REG-3). |
| `tools_approval_required` | `true` | Ask a human before running a tool. |
| `mcp_servers` | none | MCP servers whose tools the agent gets ([mcp.md](mcp.md)). |
| `board_mcp_servers` | none | Written by agent-board's MCP catalog; read like `mcp_servers`, which wins on a shared name (MCP-CAT-7). |
| `skills` | all found | Skill names to load from `skills_directory`. |
| `skills_directory` | `skills` | Folder of `<skill>/SKILL.md`, relative to the YAML file. |
| `workspace_directory` | `workspace` | The agent's working folder. Its files are served by link (CORE-SHARE-1). |
| `output_key` | none | Workflow state key the answer is written to. |
| `publish_url` | none | URL the agent card advertises, when set. |

## Lean runtime keys

| Key | Default | Meaning |
|---|---|---|
| `max_tool_iterations` | 32 | Model rounds allowed in one turn. |
| `max_step_retries` | 3 | Consecutive tool failures before the agent must ask for help. |
| `max_no_tool_continues` | 4 | Rounds without a tool call before the loop pushes for an answer. |
| `history_limit` | 48 | Messages kept per session. |
| `tool_payload_keep`, `tool_payload_max_chars` | 3, 2000 | How old large tool results are shortened (LEAN-LOOP-7). |
| `retry_malformed_tool_calls` | 0 | Extra attempts when the server cannot parse a tool call (LEAN-LLM-3). |

## ADK and deepagent keys

| Key | Runtime | Meaning |
|---|---|---|
| `context_window`, `enable_truncation`, `truncate_strategy`, `safety_margin` | adk, deepagent | Trim the input to fit the model's context (CORE-TOK-1). |
| `input_key_map` | adk, deepagent | Map workflow state keys into the prompt. |
| `middleware`, `subagents`, `interrupt_on` | deepagent | Passed to `create_deep_agent`. |

## Credentials

API keys never go in the YAML. They come from the environment, usually the
agent's `.env`:

| Provider | Variable |
|---|---|
| `openai`, `litellm` | `OPENAI_API_KEY` |
| `google` | `GOOGLE_API_KEY` or `GEMINI_API_KEY` |
| `anthropic` | `ANTHROPIC_API_KEY` |
| `vllm` | none needed (lean sends a placeholder) |

Agents installed by the board also get `AGENT_BOARD_URL`,
`AGENT_BOARD_TOKEN` and `AGENT_QUALIFIED_NAME` in `.env`, which turn on the
board tools.

## Editing from the board

The board's agent Settings dialog edits a fixed subset of these keys and
rewrites the file (AC-CONFIG-1). Raw YAML editing replaces the whole file.
