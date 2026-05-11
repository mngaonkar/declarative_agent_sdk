from langgraph.graph.state import CompiledStateGraph
from langgraph.graph import StateGraph
from typing import Dict
from vertexai.preview.reasoning_engines.templates.a2a import create_agent_card
from a2a.types import AgentSkill
from declarative_agent_sdk.agent_logging import get_logger
logger = get_logger(__name__)

class AIWorkflow():
    def __init__(self, name: str, description: str, graph: StateGraph, state: type):
        self._name = name
        self._description = description
        self._graph = graph
        self._state = state
        self._agent_card = self._create_agent_card(name, description, None)

    def compile(self) -> CompiledStateGraph:
        """Compile the StateGraph into a CompiledStateGraph that can be executed by the AIWorkflowExecutor."""
        compiled_graph = self._graph.compile()
        
        return compiled_graph
    
    def _create_agent_card(self, name: str, description: str, skills: Dict[str, str] | None):
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

        if not skills:
            skill_card = AgentSkill(
                id="default_skill",
                name="Default Skill",
                description="A default skill for the AI workflow.",
                tags = ["default"]
            )
            agent_skills.append(skill_card)

        agent_card = create_agent_card(
            agent_name=name,
            description=description,
            skills=agent_skills
        )
        return agent_card
    
    @property
    def agent_card(self):
        return self._agent_card
    
    @property
    def graph(self):
        return self._graph
    
    @property
    def state(self):
        return self._state