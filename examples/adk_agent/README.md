# Google ADK Agent Example (`agent_framework: adk`)

Demonstrates an agent using the **Google ADK** (Agent Development Kit) runtime — delegating loop execution directly to the ADK `Runner` with native Gemini models and ADK session management.

---

## Overview

- **Runtime**: `agent_framework: adk`
- **Class**: [`ADKAIAgent`](file:///Users/mahadevgaonkar/code/declarative_agent_sdk/agents/adk/agent.py#L79)
- **Key Features**:
  - Google ADK `Runner` end-to-end execution loop
  - Native Google Gemini models (`gemini-2.5-flash`, `gemini-2.5-pro`, `gemini-2.0-flash`)
  - ADK session and tool confirmation mapping
  - Seamless interoperability with standard `AgentEvent` stream, Discord, and A2A transports

---

## 1. Setup

Set your Google Gemini API key:

```bash
export GEMINI_API_KEY="..."
```

Or copy the environment template:

```bash
cp ../../.env.example .env
```

---

## 2. Configuration (`agent.yaml`)

```yaml
name: adk_assistant
description: Demonstrates the Google ADK runtime powered by Gemini
agent_framework: adk

instruction_file: instructions.md
workspace_directory: workspace

provider: google
model: gemini-2.5-flash

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
python run_sync.py "Explain the key advantages of declarative agent definitions."
```

### Streaming Mode (`run_stream.py`)

Run with real-time event streaming and interactive ADK tool approvals:

```bash
# One-shot command query
python run_stream.py "Run a command to inspect the workspace directory"

# Interactive REPL session
python run_stream.py
```
