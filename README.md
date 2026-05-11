# Building Production-Ready AI Agents: A Declarative SDK for Google ADK

> **TL;DR:** Learn how to build, configure, and deploy production-grade AI agents using a declarative YAML-based SDK that supports multiple LLM providers, automatic token management, skills-based architecture, and zero-boilerplate agent creation.

---

## The Challenge: AI Agent Complexity at Scale

Building a single AI agent is straightforward. Building a dozen agents—each with different models, tools, skills, and context window constraints—quickly becomes a maintenance nightmare. You end up with:

- **Boilerplate everywhere**: The same initialization code copied across multiple agent files
- **Hardcoded configurations**: Model names, API keys, and parameters scattered throughout your codebase
- **Tool namespace conflicts**: Multiple agents trying to use functions with the same name
- **Context window errors**: Runtime failures when inputs exceed model token limits
- **Provider lock-in**: No easy way to switch between Google Gemini, OpenAI, or local vLLM servers

What if you could define agents declaratively in YAML, automatically manage token budgets, and compose reusable skills like building blocks?

**Enter the Declarative Agent SDK.**

---

## What Is This SDK?

The **Declarative Agent SDK** is a production-ready framework built on top of Google's Agent Development Kit (ADK). It provides:

🎯 **Zero-boilerplate agent creation** via YAML configuration  
🔧 **Skills-based architecture** with auto-discovery of tools  
🌐 **Multi-provider support** (Google Gemini, vLLM, OpenAI)  
⚡ **Automatic token management** to prevent context window errors  
🔒 **Instance-level isolation** preventing tool namespace conflicts  
📊 **Centralized logging** with configurable outputs  
🔄 **Hot-swappable configurations** without code changes  
🔀 **Multi-agent workflows** using LangGraph StateGraph in YAML

---
## Quick Start
1. Clone https://github.com/mngaonkar/declarative_agent_sdk_examples.git
2. `cd simple_agent`
3. Run `uv sync` to install dependencies
4. Run `source .venv/bin/activate`
5. `export OPENAI_API_KEY=your_key_here`
6. `export TAVILY_API_KEY=your_key_here`
7. Run `python agent.py` to execute the example agent

Refer https://github.com/mngaonkar/declarative_agent_sdk_examples/blob/master/simple_agent/README.md

---

## Architecture Overview

### High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                     Declarative Agent SDK                        │
│                                                                   │
│  ┌─────────────┐   ┌──────────────┐   ┌──────────────────────┐ │
│  │ YAML Config │──▶│ AgentFactory │──▶│       AIAgent        │ │
│  └─────────────┘   └──────────────┘   │  (Google ADK Agent)  │ │
│                                        └──────────┬───────────┘ │
│  ┌──────────────────────────────────────┐         │             │
│  │           Registries                 │         ▼             │
│  │  AgentRegistry  ToolRegistry         │  ┌────────────────┐  │
│  │  SkillRegistry  WorkflowRegistry     │  │ Runner (async) │  │
│  └──────────────────────────────────────┘  └───────┬────────┘  │
│                                                     │           │
│  ┌──────────────────────────────────────┐           ▼           │
│  │         Model Providers              │  ┌────────────────┐  │
│  │  Google Gemini | vLLM | OpenAI       │  │ Session State  │  │
│  └──────────────────────────────────────┘  └────────────────┘  │
│                                                                   │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │                     Servers                               │   │
│  │   AIAgentServer (A2A/JSON-RPC)  AIWorkflowServer         │   │
│  └──────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
```

---

## Agent Declaration

Agents are defined in YAML files and instantiated via `AgentFactory.from_yaml_file()`. Below is the complete reference for all supported parameters.

### Agent YAML Parameters

#### Required

| Parameter | Type | Description |
|-----------|------|-------------|
| `name` | string | Unique agent name used for identification and logging |

#### Core (Optional)

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `description` | string | `''` | Brief description of the agent's purpose |
| `instruction_file` | string | `None` | Path to a Markdown file containing the agent's system instructions |
| `output_key` | string | `None` | Session state key where the agent stores its structured output |

#### Model & Provider (Optional)

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `model` | string | `gemini-2.5-flash-lite` | Model name (e.g. `gemini-2.0-flash-exp`, `gpt-4o`, `Qwen/Qwen3-4B`) |
| `provider` | string | `None` | LLM provider: `google`, `vllm`, or `openai` |
| `max_tokens` | integer | `None` | Maximum output tokens. Can be set here or under `endpoint` |
| `temperature` | float | `None` | Sampling temperature. Can be set here or under `endpoint` |

#### Endpoint (Optional — required for `vllm` and `openai` providers)

| Parameter | Type | Description |
|-----------|------|-------------|
| `endpoint.url` | string | Base URL for the API server (e.g. `http://localhost:8000/v1`) |
| `endpoint.max_tokens` | integer | Maximum output tokens (overrides root-level `max_tokens`) |
| `endpoint.temperature` | float | Sampling temperature (overrides root-level `temperature`) |

#### Skills & Tools (Optional)

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `skills` | list[string] | `None` | Skill directory paths to auto-discover tools from (e.g. `["skills/search", "skills/write"]`). Each directory must contain a `SKILL.md` and a `scripts/` folder |
| `skills_directory` | string | `skills` | Base directory prepended to each path in `skills` |
| `tools` | list[string] | `[]` | Explicit tool names to include (resolved from the global `ToolRegistry`) |
| `tools_approval_required` | boolean | `true` | When `true`, prompts the user to approve each tool call before execution |

#### Token Management (Optional)

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `enable_truncation` | boolean | `false` | Automatically truncate input when it exceeds the context window |
| `context_window` | integer | `None` | Total context window size in tokens (required when `enable_truncation` is `true`) |
| `truncate_strategy` | string | `end` | Where to cut the input: `start`, `end`, or `middle` |
| `safety_margin` | integer | `100` | Extra tokens reserved as overhead during truncation |

#### Workspace & Publishing (Optional)

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `workspace_directory` | string | `workspace` | Directory for agent output files and intermediate artefacts |
| `publish_url` | string | `None` | URL published in the A2A agent card for remote discovery |
| `input_key_map` | dict | `{}` | Mapping that renames input keys before they are passed to the agent |

---

### Agent YAML Examples

**Minimal agent (Google Gemini)**
```yaml
name: search_agent
description: Searches the web and summarises results
instruction_file: agents/search/instructions.md
model: gemini-2.0-flash-exp
```

**Agent with skills and tools**
```yaml
name: toc_agent
description: Creates a table of contents for a book
instruction_file: agents/toc/instructions.md
model: gemini-2.0-flash-exp
skills:
  - skills/toc
  - skills/chapter
tools:
  - google_search
  - toc_validate_yaml
output_key: toc_agent_response
```

**Agent using a local vLLM server**
```yaml
name: local_agent
description: Agent backed by a locally hosted vLLM model
instruction_file: agents/local/instructions.md
model: Qwen/Qwen3-4B-Thinking-FP8
provider: vllm
endpoint:
  url: http://localhost:8000/v1
  max_tokens: 3000
  temperature: 0.7
```

**Agent with automatic token truncation**
```yaml
name: long_context_agent
description: Handles long inputs with automatic truncation
instruction_file: agents/long/instructions.md
model: Qwen/Qwen3-4B-Thinking-FP8
provider: vllm
endpoint:
  url: http://localhost:8000/v1
  max_tokens: 3000
enable_truncation: true
context_window: 20384
truncate_strategy: end
safety_margin: 100
```

---

### Workflow YAML Parameters

Workflows are defined in YAML and instantiated via `WorkflowFactory.from_yaml_file()`. All node and router functions must be registered with `WorkflowRegistry` before the workflow is created.

#### Required

| Parameter | Type | Description |
|-----------|------|-------------|
| `nodes` | list | Ordered list of workflow nodes |
| `nodes[].name` | string | Unique node identifier used when defining edges |
| `nodes[].function` | string | Name of a function registered in `WorkflowRegistry` |

#### Optional

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `name` | string | `unnamed_workflow` | Workflow name |
| `description` | string | `no description provided` | Human-readable description |
| `edges` | list | `[]` | Unconditional edges between nodes |
| `edges[].from` | string | — | Source node name. Use `START` for the workflow entry point |
| `edges[].to` | string | — | Target node name. Use `END` to terminate the workflow |
| `conditional_edges` | list | `[]` | Edges whose target is determined at runtime by a router function |
| `conditional_edges[].from` | string | — | Source node name |
| `conditional_edges[].router_function` | string | — | Name of a router function registered in `WorkflowRegistry` |

---

### Workflow YAML Example

```yaml
name: book_generation_workflow
description: Multi-agent workflow that generates a complete book
nodes:
  - name: toc_agent
    function: toc_agent
  - name: chapter_agent
    function: chapter_agent_parallel
  - name: collation_agent
    function: collation_agent
edges:
  - from: START
    to: toc_agent
  - from: chapter_agent
    to: collation_agent
  - from: collation_agent
    to: END
conditional_edges:
  - from: toc_agent
    router_function: route_after_toc
```

