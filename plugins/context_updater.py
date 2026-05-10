from declarative_agent_sdk.agent_logging import get_logger
from declarative_agent_sdk.agent_context import AgentContext
import os

logger = get_logger(__name__)


# Standalone function for backward compatibility and convenience
def get_updated_context(agent_name: str) -> str:
    """
    Get updated context for an agent (convenience function).
    
    This is a wrapper around AgentContext.get_updated_context() for convenience.
    
    Args:
        agent_name: The name of the agent for which to fetch context.
        
    Returns:
        A string representing the current context for the agent.
        
    Example:
        context = get_updated_context('my_agent')
    """
    updater = AgentContext(agent_name)
    return updater.get_updated_context()