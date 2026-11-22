# Workflows — design spec

Multi-step LangGraph workflows described in YAML: named nodes (Python
functions, usually calling agents), edges between them, and router functions
that pick the next node. A workflow can be served over A2A like an agent.

Code: `workflows/`.

## `workflows/__init__.py`

**Does:** marks `workflows` as a package.

## `workflows/registry.py`

**Does:** `WorkflowRegistry`, the name → function map that YAML node and
router names refer to.

**Rules:**

- WF-REG-1: Functions must be registered before a workflow that names them is
  built. Looking up an unknown name is an error.

## `workflows/factory.py`

**Does:** `WorkflowFactory` builds a LangGraph `StateGraph` from YAML.

**Rules:**

- WF-FAC-1: YAML keys: `name`, `description`, `nodes` (each `name` and
  `function`), `edges` (each `from`, `to`), `conditional_edges` (each `from`,
  `router_function`, and the routes it may return).
- WF-FAC-2: `START` and `END` in an edge mean LangGraph's start and end.
- WF-FAC-3: The state type defaults to `AgentState` (`core/agent_state.py`).
- WF-FAC-4: `compile_from_yaml` builds and compiles in one step.

## `workflows/workflow.py`

**Does:** `AIWorkflow`: a named graph with its state type and agent card;
`compile()` returns the runnable graph.

## `workflows/graph_executor.py`

**Does:** `AIWorkflowExecutor`, the A2A executor for a workflow: runs the
compiled graph once per request.

**Rules:**

- WF-EXEC-1: The initial state is `user_query` (the request text), `context`
  (the A2A request) and an empty `agents_output`. The result's final answer
  is returned as the task's artifact. Errors fail the task (A2A-EXEC-1).

## `workflows/server.py`

**Does:** `AIWorkflowServer`, serves a workflow over A2A, with the same card
address rules as A2A-SERVER-2.
