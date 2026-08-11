"""
Declarative Agent SDK — YAML agents for lean / ADK / deepagent runtimes.
"""

from declarative_agent_sdk.__version__ import __version__

# Core
from declarative_agent_sdk.core.base_agent import BaseAgent
from declarative_agent_sdk.core.agent_event import AgentEvent, from_adk_event
from declarative_agent_sdk.core.agent_factory import AgentFactory, resolve_agent_framework
from declarative_agent_sdk.core.agent_registry import AgentRegistry
from declarative_agent_sdk.core.agent_context import AgentContext
from declarative_agent_sdk.core.agent_state import AgentState
from declarative_agent_sdk.core.agent_logging import setup_logging, get_logger, set_log_level
from declarative_agent_sdk.core.token_utils import fit_to_context_window
from declarative_agent_sdk.core import utils, constants

# Agents (peer end-to-end loops)
from declarative_agent_sdk.agents.lean.agent import LeanAIAgent
from declarative_agent_sdk.agents.adk.agent import ADKAIAgent
from declarative_agent_sdk.agents.deepagent.agent import LangChainAIAgent, LangChainEvent
from declarative_agent_sdk.agents.adk.plugins.context_updater import get_updated_context

# Tools / models
from declarative_agent_sdk.tools.tool_registry import ToolRegistry
from declarative_agent_sdk.tools.skill_registry import SkillRegistry
from declarative_agent_sdk.tools import builtin as builtin_tools
from declarative_agent_sdk.models.model_factory import ModelFactory

# Transports
from declarative_agent_sdk.transports.discord.server import DiscordAgentServer
from declarative_agent_sdk.transports.a2a.server import AIAgentServer
from declarative_agent_sdk.transports.a2a.executor import AIAgentExecutor

# Workflows
from declarative_agent_sdk.workflows.workflow import AIWorkflow
from declarative_agent_sdk.workflows.factory import WorkflowFactory, register_workflow_functions
from declarative_agent_sdk.workflows.registry import WorkflowRegistry
from declarative_agent_sdk.workflows.graph_executor import AIWorkflowExecutor
from declarative_agent_sdk.workflows.server import AIWorkflowServer


__all__ = [
    "__version__",
    "BaseAgent",
    "ADKAIAgent",
    "LeanAIAgent",
    "LangChainAIAgent",
    "LangChainEvent",
    "AgentEvent",
    "from_adk_event",
    "AIAgentExecutor",
    "AIWorkflowExecutor",
    "AIAgentServer",
    "AIWorkflowServer",
    "DiscordAgentServer",
    "AgentFactory",
    "resolve_agent_framework",
    "AgentRegistry",
    "ModelFactory",
    "ToolRegistry",
    "SkillRegistry",
    "WorkflowFactory",
    "WorkflowRegistry",
    "register_workflow_functions",
    "setup_logging",
    "get_logger",
    "set_log_level",
    "fit_to_context_window",
    "utils",
    "constants",
    "builtin_tools",
    "get_updated_context",
    "AgentContext",
    "AgentState",
    "AIWorkflow",
]
