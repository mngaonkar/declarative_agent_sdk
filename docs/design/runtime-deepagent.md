# Deepagents runtime — design spec

`agent_framework: deepagent`. The `deepagents` library (on LangGraph) runs the
whole loop through `create_deep_agent`. The SDK supplies the model, tools and
instructions, and converts the LangGraph stream into `AgentEvent`s.

Code: `agents/deepagent/`.

## `agents/deepagent/__init__.py`

**Does:** exports `LangChainAIAgent` and `LangChainEvent` (an older name for
`AgentEvent`).

## `agents/deepagent/agent.py`

**Does:** `LangChainAIAgent`, a `BaseAgent` around a compiled deep-agent
graph.

**Rules:**

- DEEP-1: Tools are chosen as in ADK-1 (core tools, the `tools:` list, skill
  tools), then converted to LangChain `StructuredTool`s. A tool that cannot be
  converted is skipped with a warning.
- DEEP-2: The model comes from `model` + `provider` (default provider
  `anthropic`). `anthropic`, `openai` and `google` become
  `anthropic:<model>`, `openai:<model>`, `google_genai:<model>`; any other
  provider goes through LiteLLM (`<provider>/<model>`), which must be
  installed. A model object passed in code is used as is.
- DEEP-3: With `tools_approval_required`, every tool — including deepagents'
  own (`write_todos`, `ls`, `read_file`, `write_file`, …) — is put in
  `interrupt_on` with approve/reject. An explicit `interrupt_on` in the YAML
  replaces this.
- DEEP-4: The graph is streamed in `updates` mode. Model text becomes `status`
  or `final_text`; model thinking blocks become `status`; a LangGraph
  interrupt becomes a `tool_approval` event (`events_from_stream_chunk`).
- DEEP-5: `tool_confirmation(…, yes)` resumes with a LangGraph
  `Command(resume=…)` carrying approve or reject.
- DEEP-6: Sessions are LangGraph threads keyed by the session id.
- DEEP-7: `middleware` and `subagents` from the YAML are passed straight to
  `create_deep_agent`.

**Tests:** `tests/test_langchain_ai_agent.py`.
