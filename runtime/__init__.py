"""
Lean agent runtime — ESP32-style ReAct loop with progressive skills.

Ported from esp32s3-ai-agent for CPython host use (Discord, A2A, CLI).
No ADK / LangGraph dependency.
"""

from declarative_agent_sdk.runtime.loop import LeanLoop
from declarative_agent_sdk.runtime.skills import SkillRegistry, parse_frontmatter
from declarative_agent_sdk.runtime.tools import LeanToolRegistry
from declarative_agent_sdk.runtime.llm import LeanLLMClient, LLMError

__all__ = [
    "LeanLoop",
    "SkillRegistry",
    "parse_frontmatter",
    "LeanToolRegistry",
    "LeanLLMClient",
    "LLMError",
]
