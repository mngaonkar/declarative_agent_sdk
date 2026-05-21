from google.genai import types
from a2a.helpers import new_text_message, new_message
from a2a.types import Message, Part, Role
from declarative_agent_sdk.a2a_utils import ResponseType

class TextResponseFormatter:
    """Formats the response from the agent to be sent to the A2A client."""
    def format_response(self, response: str, response_type: ResponseType, args: dict) -> Message | None:
        message = None

        if response_type == ResponseType.TASK_COMPLETED:
            message = new_text_message(text=response, role=Role.ROLE_USER)
        return message

