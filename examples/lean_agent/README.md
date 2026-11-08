# Lean Agent Example (`agent_framework: lean`)

Demonstrates an agent using the **Lean** runtime — a native ReAct loop featuring deliberative planning, tool execution, and progressive skill discovery (ESP-style).

---

## Overview

- **Runtime**: `agent_framework: lean` (default in Declarative Agent SDK)
- **Class**: [`LeanAIAgent`](file:///Users/mahadevgaonkar/code/declarative_agent_sdk/agents/lean/agent.py#L66)
- **Key Features**:
  - Deliberative multi-step ReAct loop with retries
  - Progressive skills (`skills/` directory) loaded on demand
  - Built-in tool approval support (`tools_approval_required: true`)
  - Works with OpenAI, local vLLM, DeepSeek, Anthropic, or Gemini

---

## 1. Setup

Set your API key:

```bash
export OPENAI_API_KEY="sk-..."
```

Or copy the environment template:

```bash
cp ../../.env.example .env
```

---

## 2. Configuration (`agent.yaml`)

```yaml
name: lean_assistant
description: Demonstrates the Lean runtime with progressive skills and tools
agent_framework: lean

instruction_file: instructions.md
skills_directory: skills
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

Run a quick query and receive the final answer directly:

```bash
# Default greeting
python run_sync.py

# Custom computation query
python run_sync.py "Calculate 125 * 48 and verify the result."
```

### Streaming Mode (`run_stream.py`)

Run with real-time event streaming, status indicators, and interactive tool approvals:

```bash
# One-shot command query
python run_stream.py "List the files in the current workspace directory"

# Interactive REPL session
python run_stream.py
```
