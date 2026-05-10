from typing import TypedDict, Any
from typing_extensions import Required

class AgentState(TypedDict, total=False):
    """Defines the state structure for agents in the AI workflow.
    
    Attributes:
        user_query (str): The user's query or input to the agent.

        Add additional keys (agent name) for storing agent output
    """
    user_query: Required[str]
    final_answer: str
    agents_output: Required[dict[str, Any]]