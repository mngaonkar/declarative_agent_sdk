# DeepAgent Example (`agent_framework: deepagent`)

Demonstrates an agent using the **DeepAgent** runtime — backed by `deepagents` and LangGraph's compiled state graph execution with built-in human-in-the-loop (HITL) tool approval interrupts.

---

## Overview

- **Runtime**: `agent_framework: deepagent`
- **Class**: [`LangChainAIAgent`](file:///Users/mahadevgaonkar/code/declarative_agent_sdk/agents/deepagent/agent.py#L315)
- **Key Features**:
  - LangGraph compiled state graph loop
  - LangChain chat model abstractions (OpenAI, Anthropic, Google GenAI)
  - Human-in-the-loop (HITL) interrupt mapping to SDK's `tool_approval` and `tool_confirmation`
  - Yields canonical `AgentEvent` streams compatible with Discord and A2A transports

---

## 1. Setup

Set your API key (depending on provider):

```bash
# For OpenAI
export OPENAI_API_KEY="sk-..."

# For Anthropic (if using provider: anthropic)
export ANTHROPIC_API_KEY="sk-ant-..."
```

Or copy the environment template:

```bash
cp ../../.env.example .env
```

---

## 2. Configuration (`agent.yaml`)

```yaml
name: deepagent_assistant
description: Demonstrates the DeepAgent runtime powered by deepagents and LangGraph
agent_framework: deepagent

instruction_file: instructions.md
workspace_directory: workspace

provider: openai
model: gpt-4o-mini
endpoint:
  url: https://api.openai.com/v1

tools:
  - web_request
  - exec_command

tools_approval_required: true
```

---

## 3. Running the Agent

### Synchronous Mode (`run_sync.py`)

Run a query and receive the final answer directly:

```bash
# Default prompt
python run_sync.py

# Custom query
python run_sync.py "Explain state graphs in autonomous agents."
```

### Streaming Mode (`run_stream.py`)

Run with real-time event streaming and interactive LangGraph tool approvals:

```bash
# One-shot command query
python run_stream.py "Run a command to inspect the workspace directory"

# Interactive REPL session
python run_stream.py
```
