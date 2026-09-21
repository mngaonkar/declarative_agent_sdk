#!/usr/bin/env python3
"""Run a declarative LangGraph workflow defined in YAML.

Usage:
    cd examples/workflow
    python run_workflow.py
    python run_workflow.py "Explain the benefits of progressive skills in AI agents"
    python run_workflow.py --serve  # Serve over A2A JSON-RPC
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Dict

# Add project root to sys.path if running directly
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from declarative_agent_sdk.core.agent_state import AgentState
from declarative_agent_sdk.workflows.factory import WorkflowFactory, register_workflow_functions
from declarative_agent_sdk.workflows.server import AIWorkflowServer


# ---------------------------------------------------------------------------
# Workflow Node Implementations
# ---------------------------------------------------------------------------

def plan_topics(state: AgentState) -> Dict[str, Any]:
    query = state.get("user_query", "")
    print(f"  [1. Planner] Analyzing query: '{query}'")
    topics = [
        f"Core concept and definition of '{query}'",
        f"Key architectural trade-offs and advantages",
        f"Real-world application patterns",
    ]
    agents_output = dict(state.get("agents_output") or {})
    agents_output["planner"] = topics
    return {"agents_output": agents_output}


def research_topics(state: AgentState) -> Dict[str, Any]:
    topics = (state.get("agents_output") or {}).get("planner", [])
    print(f"  [2. Researcher] Investigating {len(topics)} planned topics...")
    findings = {}
    for i, topic in enumerate(topics, 1):
        findings[f"topic_{i}"] = f"Detailed findings and verification for: {topic}"
    agents_output = dict(state.get("agents_output") or {})
    agents_output["researcher"] = findings
    return {"agents_output": agents_output}


def synthesize_summary(state: AgentState) -> Dict[str, Any]:
    print("  [3. Synthesizer] Compiling final structured briefing...")
    query = state.get("user_query", "")
    research = (state.get("agents_output") or {}).get("researcher", {})

    lines = [f"# Briefing: {query}\n"]
    for key, finding in research.items():
        lines.append(f"- **{key.replace('_', ' ').title()}**: {finding}")
    final_text = "\n".join(lines)

    agents_output = dict(state.get("agents_output") or {})
    agents_output["final_answer"] = final_text
    return {"final_answer": final_text, "agents_output": agents_output}


# ---------------------------------------------------------------------------
# Main Execution
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Run declarative LangGraph workflow")
    parser.add_argument("query", nargs="?", default="Declarative YAML Agents", help="Query to run")
    parser.add_argument("--serve", action="store_true", help="Serve workflow via A2A server")
    parser.add_argument("--port", type=int, default=8000, help="Port for A2A server")
    args = parser.parse_args()

    # 1. Register node functions before loading YAML
    register_workflow_functions({
        "plan_topics": plan_topics,
        "research_topics": research_topics,
        "synthesize_summary": synthesize_summary,
    })

    # 2. Build workflow from YAML
    yaml_path = Path(__file__).parent / "workflow.yaml"
    print(f"Loading workflow from: {yaml_path.name}")
    workflow = WorkflowFactory.from_yaml_file(str(yaml_path), AgentState)
    compiled_graph = workflow.compile()
    print("Compiled StateGraph successfully.\n")

    # 3. Serve via A2A or run locally
    if args.serve:
        print(f"Starting A2A server on http://0.0.0.0:{args.port}/ ...")
        AIWorkflowServer(workflow, compiled_graph, host="0.0.0.0", port=args.port).run()
    else:
        print(f"Executing workflow for: '{args.query}'\n" + "-" * 50)
        initial_state = {
            "user_query": args.query,
            "agents_output": {},
        }
        result = compiled_graph.invoke(initial_state)

        print("\nWorkflow Execution Result:\n" + "=" * 50)
        print(result.get("final_answer", ""))
        print("=" * 50)


if __name__ == "__main__":
    main()
