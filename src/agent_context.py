from declarative_agent_sdk.constants import SKILLS_DIRECTORY
from declarative_agent_sdk.utils import read_from_file
from declarative_agent_sdk.agent_logging import get_logger
import os

logger = get_logger(__name__)

class AgentContext():
    """Utility class to fetch and update context for agents. 
    This is a placeholder implementation and should be extended with actual logic to retrieve relevant context based on the agent's state or environment."""
    def __init__(self, agent_name: str) -> None:
        from declarative_agent_sdk.agent_registry import AgentRegistry
        self.agent_name = agent_name
        self.agent = AgentRegistry.get(agent_name)

    def get_updated_context(self, input_text: str = "") -> str:
        """Fetch the current context for the agent. 
        This is a placeholder implementation and should be replaced with actual logic to retrieve relevant context based on the agent's state or environment.
        Args:
            agent_name: The name of the agent for which to fetch context.
            input_text: Optional input text to consider when updating context.
        Returns:
            A string representing the current context for the agent.
        """
        context_text = self.get_default_context()
        return context_text
    
    def get_default_context(self) -> str:
        """Fetch the current context for the agent. 
        This is a placeholder implementation and should be replaced with actual logic to retrieve relevant context based on the agent's state or environment.
        Args:
            agent_name: The name of the agent for which to fetch context.
        Returns:
            A string representing the current context for the agent.
        """

        # TODO: load instructions from all SKILL.md files of the agent's skills and append to context
        skills_directory = self.agent.skill_directory or SKILLS_DIRECTORY
        instruction_text = ""
        
        if self.agent.skills and len(self.agent.skills) > 0:
            for skill in self.agent.skills:
                skill_dir = os.path.join(skills_directory, skill)
                if not os.path.exists(skill_dir):
                    logger.warning(f"Skill directory not found for skill '{skill}': {skill_dir}")
                    continue
                
                # Append SKILL.md content to instruction text
                skill_md_path = os.path.join(skill_dir, 'SKILL.md')
                if os.path.exists(skill_md_path):
                    skill_instruction = read_from_file(skill_md_path)
                    instruction_text += f"\n\n# Skill: {skill}\n{skill_instruction}"
                    logger.info(f"Appended SKILL.md from {skill_dir}")
                else:
                    logger.warning(f"SKILL.md not found for skill '{skill}' in directory: {skill_dir}")
        else:
            logger.info(f"No skills specified for agent '{self.agent_name}', skipping skill context aggregation.")
    
        return instruction_text        
