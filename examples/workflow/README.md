# Declarative Multi-Agent Workflow Example

Demonstrates building and running multi-agent workflows defined declaratively in **YAML** using LangGraph's `StateGraph` through `WorkflowFactory`.

---

## 1. Workflow Architecture

Workflows are declared in `workflow.yaml` with explicit nodes and edge definitions:

```yaml
name: research_and_summary_workflow
description: Plans research points and synthesizes a summary

nodes:
  - name: planner
    function: plan_topics
  - name: researcher
    function: research_topics
  - name: synthesizer
    function: synthesize_summary

edges:
  - from: START
    to: planner
  - from: planner
    to: researcher
  - from: researcher
    to: synthesizer
  - from: synthesizer
    to: END
```

---

## 2. Running the Workflow

### Local CLI Execution

Run the workflow locally in your terminal:

```bash
cd examples/workflow
python run_workflow.py "Progressive Disclosure in Agent Systems"
```

### Serve via A2A Server

Serve the compiled workflow over Google's Agent-to-Agent (A2A) JSON-RPC protocol:

```bash
python run_workflow.py --serve --port 8000
```

---

## 3. How It Works

1. **Register functions in `WorkflowRegistry`:**
   ```python
   from declarative_agent_sdk.workflows.factory import register_workflow_functions

   register_workflow_functions({
       "plan_topics": plan_topics,
       "research_topics": research_topics,
       "synthesize_summary": synthesize_summary,
   })
   ```

2. **Load and compile from YAML:**
   ```python
   from declarative_agent_sdk.workflows.factory import WorkflowFactory
   from declarative_agent_sdk.core.agent_state import AgentState

   workflow = WorkflowFactory.from_yaml_file("workflow.yaml", AgentState)
   compiled_graph = workflow.compile()
   ```

3. **Execute:**
   ```python
   result = compiled_graph.invoke({"user_query": "Explain AI agents"})
   print(result["final_answer"])
   ```
