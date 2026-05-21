from typing import Protocol
from declarative_agent_sdk.a2a_utils import ResponseType
from a2a.types import Message

class ResponseFormatter(Protocol):
    """Formats the response from the agent to be sent to the A2A client."""
    def format_response(self, response: str, response_type: ResponseType, args: dict) -> Message:
        ...