from enum import Enum
from typing import Dict, List, Optional
from a2a.types import Part
from google.protobuf.json_format import ParseDict
from google.protobuf import struct_pb2
from a2a.types import AgentCard, AgentCapabilities, AgentInterface, AgentSkill
from a2a.utils.constants import TransportProtocol, PROTOCOL_VERSION_CURRENT

# The A2A spec marks these REQUIRED on an AgentCard, and a2a-sdk >= 1.3 logs
# "AgentCard is not spec-compliant - REQUIRED fields missing or empty" for every
# card fetch without them, warning that it "could be rejected in a future major
# release". Both transports exchange plain text and JSON data parts: text for
# questions and answers, JSON for the tool-approval round trip, where the server
# sends a function_response part and the client replies with one.
DEFAULT_INPUT_MODES = ["text/plain", "application/json"]
DEFAULT_OUTPUT_MODES = ["text/plain", "application/json"]

def _data_part(data: dict) -> Part:
    return Part(data=ParseDict(data, struct_pb2.Value()))

class ResponseType(Enum):
    TASK_COMPLETED = "TASK_COMPLETED"
    TASK_FAILED = "TASK_FAILED"
    TASK_INPUT_REQUIRED = "TASK_INPUT_REQUIRED"
    TASK_WORKING = "TASK_WORKING"

def create_agent_card(
    name: str,
    description: str,
    skills: Dict[str, str] | None,
    url: Optional[str] = None,
    version: Optional[str] = None,
    default_input_modes: Optional[List[str]] = None,
    default_output_modes: Optional[List[str]] = None,
):
    """Build the AgentCard served at /.well-known/agent-card.json.

    ``version`` is the agent's own version, which the spec leaves to the
    provider; it defaults to the SDK version so a deployed card says which
    build is answering. The mode lists describe the media types this transport
    exchanges — override them for an agent that handles something narrower or
    wider.
    """
    agent_skills = []

    if skills:
        for skill, skill_description in skills.items():
            skill_card = AgentSkill(
                id=skill,
                name=skill,
                description=skill_description,
                tags = [skill]
            )
            agent_skills.append(skill_card)

    if not agent_skills:
        # skills is REQUIRED too, and an empty list fails the same validator.
        # Workflows always hit this (workflows/workflow.py passes skills=None),
        # as does any agent with no progressive skills on disk. Such an agent
        # still does something, so describe it as one general skill rather than
        # serving a card clients may reject.
        agent_skills.append(AgentSkill(
            id="general",
            name=name,
            description=description,
            tags=["general"],
        ))

    interfaces = []
    if url:
        interfaces.append(AgentInterface(
            url=url,
            protocol_binding=TransportProtocol.JSONRPC.value,
            protocol_version=PROTOCOL_VERSION_CURRENT,
        ))

    if version is None:
        # Imported here, not at module scope: declarative_agent_sdk/__init__.py
        # imports the agents, which import this module, so a top-level import
        # of a sibling would close that cycle.
        from declarative_agent_sdk.__version__ import __version__

        version = __version__

    return AgentCard(
        name=name,
        description=description,
        skills=agent_skills,
        supported_interfaces=interfaces,
        capabilities=AgentCapabilities(streaming=True),
        version=version,
        default_input_modes=default_input_modes or DEFAULT_INPUT_MODES,
        default_output_modes=default_output_modes or DEFAULT_OUTPUT_MODES,
    )