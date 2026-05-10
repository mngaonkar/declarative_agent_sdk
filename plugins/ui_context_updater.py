from curses import version

from declarative_agent_sdk.agent_logging import get_logger
from declarative_agent_sdk.agent_context import AgentContext
from a2ui.core.schema.manager import A2uiSchemaManager
from a2ui.core.schema.constants import VERSION_0_8, VERSION_0_9
from a2ui.basic_catalog.provider import BasicCatalog
from typing import Dict
import os

logger = get_logger(__name__)

class AgentContextUI(AgentContext):
    ROLE_DESCRIPTION = "You are a helpful assistant that generates UI schemas in response to user requests. You receive user requests in natural language and generate UI schemas in the A2UI format based on those requests. The UI schemas you generate should be designed to best fulfill the user's request while adhering to the A2UI schema specifications."
   
    def __init__(self, agent_name: str, args: dict) -> None:
        super().__init__(agent_name)
        self._schema_manager: A2uiSchemaManager
        examples_path = args.get("examples_path", "")
        assert examples_path is not "", "examples_path is required in args to initialize AgentContextUI"

        self._version = args.get("version", VERSION_0_9)
        assert self._version in [VERSION_0_8, VERSION_0_9], f"Unsupported version '{self._version}' specified in args. Supported versions are: {VERSION_0_8}, {VERSION_0_9}"

        self._schema_manager = A2uiSchemaManager(version=self._version,
                                                            catalogs=[
                                                                BasicCatalog.get_config(
                                                                    version=self._version,
                                                                    examples_path=examples_path
                                                                ),
                                                            ])
        logger.info(f"Initialized A2UI schema manager for version {self._version}")

    def get_updated_context(self, input_text: str = "") -> str:
        prompt = self._schema_manager.generate_system_prompt(
                role_description=AgentContextUI.ROLE_DESCRIPTION,
                ui_description=self.get_default_context(),
                include_schema=True,
                include_examples=True,
                validate_examples=False,  # Use invalid examples to test retry logic
            )

        return prompt
    

def get_updated_context(agent_name: str, args: dict) -> str:
    """
    Get updated context for an agent (convenience function).
    
    This is a wrapper around AgentContext.get_updated_context() for convenience.
    
    Args:
        agent_name: The name of the agent for which to fetch context.

    Returns:
        The updated context for the specified agent.
    """
    context = AgentContextUI(agent_name, args)
    return context.get_updated_context()