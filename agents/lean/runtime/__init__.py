"""
Lean agent runtime — ESP-style ReAct loop with progressive skills.

One of three peer agent_framework choices (each owns its loop end-to-end):

  - lean      → this package (LeanLoop + skills)
  - adk       → Google ADK Runner (ai_agent.AIAgent)
  - deepagent → deepagents / LangGraph (langchain_ai_agent.LangChainAIAgent)

No framework mixing: pick one outer loop via agent_framework in YAML.
"""

from declarative_agent_sdk.agents.lean.runtime.chat_backend import ChatBackend
from declarative_agent_sdk.agents.lean.runtime.loop import LeanLoop
from declarative_agent_sdk.agents.lean.runtime.llm import LeanLLMClient, LLMError
from declarative_agent_sdk.agents.lean.runtime.skills import SkillRegistry, parse_frontmatter
from declarative_agent_sdk.agents.lean.runtime.tools import LeanToolRegistry

__all__ = [
    "ChatBackend",
    "LeanLoop",
    "SkillRegistry",
    "parse_frontmatter",
    "LeanToolRegistry",
    "LeanLLMClient",
    "LLMError",
]
