from enum import Enum
from typing import Dict, Optional
from a2a.types import Part
from google.protobuf.json_format import ParseDict
from google.protobuf import struct_pb2
from a2a.types import AgentCard, AgentCapabilities, AgentInterface, AgentSkill
from a2a.utils.constants import TransportProtocol, PROTOCOL_VERSION_CURRENT

def _data_part(data: dict) -> Part:
    return Part(data=ParseDict(data, struct_pb2.Value()))

class ResponseType(Enum):
    TASK_COMPLETED = "TASK_COMPLETED"
    TASK_FAILED = "TASK_FAILED"
    TASK_INPUT_REQUIRED = "TASK_INPUT_REQUIRED"
    TASK_WORKING = "TASK_WORKING"

def create_agent_card(name: str, description: str, skills: Dict[str, str] | None, url: Optional[str] = None):
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

    interfaces = []
    if url:
        interfaces.append(AgentInterface(
            url=url,
            protocol_binding=TransportProtocol.JSONRPC.value,
            protocol_version=PROTOCOL_VERSION_CURRENT,
        ))

    return AgentCard(
        name=name,
        description=description,
        skills=agent_skills,
        supported_interfaces=interfaces,
        capabilities=AgentCapabilities(streaming=True),
    )