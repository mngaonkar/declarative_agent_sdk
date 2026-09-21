# Simple Agent Example (Python CLI & Script)

Demonstrates how to run an agent built with the **Declarative Agent SDK** directly in Python using both **synchronous** and **asynchronous streaming** interfaces.

---

## 1. Setup

Set your LLM provider key:

```bash
export OPENAI_API_KEY=sk-...
```

Or copy `.env.example` to `.env`:
```bash
cp ../../.env.example .env
```

---

## 2. Running the Agent

### Method 1: Synchronous Run (`run_sync`)

Use `run_sync.py` when you want a simple input-output function call that waits for the final answer:

```bash
# Default prompt
python run_sync.py

# Custom query
python run_sync.py "Calculate the square root of 1764 and explain the steps."
```

Code overview:
```python
from declarative_agent_sdk import AgentFactory

agent = AgentFactory.from_yaml_file("agent.yaml")
answer = agent.run_sync("What is 15 * 37?")
print(answer)
```

---

### Method 2: Async Streaming with Tool Approval (`run_stream`)

Use `run_stream.py` when you want real-time progress events, thinking updates, and interactive terminal approval when tools are invoked:

```bash
# One-shot query
python run_stream.py "Run a shell command to list the contents of the workspace directory"

# Interactive REPL mode
python run_stream.py
```

When a tool requires confirmation (`tools_approval_required: true`), the script intercepts the `tool_approval` event, prompts you for `[y/N]`, and resumes execution via `agent.tool_confirmation(...)`.

---

## 3. Switching Runtimes

Edit `agent.yaml` to switch between peer runtimes:

```yaml
# Lean (default, OpenAI-compatible or local vLLM)
agent_framework: lean
provider: openai
model: gpt-4o-mini

# Google ADK (Gemini)
# agent_framework: adk
# model: gemini-2.5-flash-lite

# Deepagent (Anthropic / LangGraph)
# agent_framework: deepagent
# provider: anthropic
# model: claude-sonnet-4-6
```
