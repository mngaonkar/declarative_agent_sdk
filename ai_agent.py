from google.adk.agents.llm_agent import Agent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.tools import BaseTool, ToolContext
from google.genai import types
from google.adk.agents.callback_context import CallbackContext
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from a2a.types import AgentCard, AgentSkill
from vertexai.preview.reasoning_engines.templates.a2a import create_agent_card

from declarative_agent_sdk.utils import read_from_file
from declarative_agent_sdk.constants import DEFAULT_MODEL, MAX_REMOTE_CALLS, SKILLS_DIRECTORY, WORKSPACE_DIRECTORY
from declarative_agent_sdk.agent_logging import get_logger
from declarative_agent_sdk.token_utils import fit_to_context_window
from declarative_agent_sdk.tool_registry import ToolRegistry
import asyncio
import uuid
import os
from typing import Optional, Any, Union, List, Dict
from pydantic import Field
import declarative_agent_sdk.plugins.context_updater as context_updater

logger = get_logger(__name__)

async def dynamic_tool_callback(
    tool: BaseTool, args: Dict[str, Any], tool_context: ToolContext
) -> Optional[Dict]:
    """
    Example callback function that runs before each tool call.
    This can be used to modify tool arguments, inject additional information, or log tool usage.
    """
    logger.debug(f"Running dynamic_tool_callback for tool '{tool.name}' with args: {args}")
    
    # Example: Inject agent name into tool arguments if not already present
    if 'agent_name' not in args:
        args['agent_name'] = tool_context.agent_name
        logger.debug(f"Injected agent_name into tool args: {args['agent_name']}")
    
    # Wait for user confirmation before executing potentially dangerous tool
    user_input = input(f"\033[93mTool '{tool.name}' is about to be called with args: {args}. Do you want to proceed? (y/n): \033[0m")
    if user_input.lower() != 'y':
        logger.info(f"Tool '{tool.name}' call aborted by user.")
        return None  # Returning None can signal to skip the tool call


async def dynamic_context_callback(callback_context: CallbackContext, llm_request: LlmRequest) -> Optional[LlmResponse]:
    """
    Example callback function that dynamically updates system context before each model call.
    This can be used to inject relevant information, filter out unnecessary context, or manage token limits.
    """
    logger.debug("Running dynamic_context_callback")
    agent_name = callback_context.agent_name
    logger.debug(f"Agent '{agent_name}' is making a model call.")
    
    modified_context = context_updater.get_updated_context(agent_name) or "No additional context"
    llm_request.config.system_instruction = modified_context
    logger.debug(f"Updated system instruction for agent '{agent_name}': {llm_request.config.system_instruction}")


class AIAgent(Agent):
    """Extended Agent with convenient initialization and run methods."""
    instruction_file: str = Field(default="", exclude=True)
    input_key_map: dict[str, str] = Field(default_factory=dict, exclude=True)
    skills_registry: Any = Field(default=None, exclude=True)
    context_window: Optional[int] = Field(default=None, exclude=True)
    max_output_tokens: Optional[int] = Field(default=None, exclude=True)
    enable_truncation: bool = Field(default=False, exclude=True)
    truncate_strategy: str = Field(default="end", exclude=True)
    safety_margin: int = Field(default=100, exclude=True)
    skill_directory: str = Field(default=SKILLS_DIRECTORY, exclude=True)
    workspace_directory: str = Field(default=WORKSPACE_DIRECTORY, exclude=True)
    skills : List[str] = Field(default_factory=list, exclude=True)
    agent_card: Optional[AgentCard] = Field(default=None, exclude=True)
    runner: Optional[Runner] = Field(default=None, exclude=True)
    session_service: Optional[InMemorySessionService] = Field(default=None, exclude=True)
    session_id: Optional[str] = Field(default=None, exclude=True)
    user_id: str = Field(default="user_id", exclude=True)
    event_loop_running: bool = Field(default=False, exclude=True)
    publish_url: Optional[str] = Field(default=None, exclude=True)
   
    def __init__(self, 
                 name: str, 
                 instruction_file: str,
                 description: str = '',
                 tools: list | None = None,
                 tools_approval_required: bool = True,
                 skills_directory: str = SKILLS_DIRECTORY, 
                 workspace_directory: str = WORKSPACE_DIRECTORY,
                 skills: List[str] | None = None,
                 input_key_map: dict[str, str] | None = None,
                 output_key: str | None = None,
                 model: Union[str, Any] = DEFAULT_MODEL,
                 context_window: Optional[int] = None,
                 max_output_tokens: Optional[int] = None,
                 enable_truncation: bool = False,
                 truncate_strategy: str = "end",
                 safety_margin: int = 100,
                 publish_url: Optional[str] = None):
        """
        Initialize the AI Agent with tools, skills, and instructions.
        
        Args:
            name: Agent name (used for identification and logging)
            instruction_file: Path to the main instruction file (markdown format)
            description: Brief description of the agent's purpose
            tools: List of tool names (strings) or tool objects to provide to the agent
            skills_directory: Base directory for skills (defaults to SKILLS_DIRECTORY constant)
            skills: List of skill directory names to auto-discover tools from.
                   Each skill directory should contain:
                   - SKILL.md: Instructions to append to agent's instruction text
                   - scripts/: Folder with Python scripts to register as tools
            input_key_map: Optional mapping of input keys for data transformation
            output_key: Optional key where agent stores structured output in session state
            model: Model name (string) or model object (defaults to DEFAULT_MODEL from constants)
            context_window: Total context window size in tokens (e.g., 20384 for Qwen3-4B)
            max_output_tokens: Tokens reserved for output generation
            enable_truncation: If True, automatically truncate inputs exceeding context window
            truncate_strategy: How to truncate ("start", "end", or "middle")
            tools_approval_required: If True, requires user approval before executing certain tools
            safety_margin: Extra tokens to reserve for safety (default: 100)
            publish_url: Optional URL to set in the agent card for discovery (e.g., when running on a server)
        
        Workflow:
            1. Reads instruction text from instruction_file
            2. Creates instance-specific tool registry
            3. For each skill directory:
               - Appends SKILL.md content to instructions
               - Auto-discovers and registers tools from scripts/ folder
            4. Resolves tool names to tool objects
            5. Configures automatic function calling with MAX_REMOTE_CALLS limit
            6. Initializes parent Agent class with all configuration
        """
        # Read main instruction file
        # Read main instruction file
        instruction_text = read_from_file(instruction_file) if instruction_file else ''
                
        # Create instance-level skill registry (isolated from global registry)
        # This allows for instance-specific skill management if needed in the future
        from declarative_agent_sdk.skill_registry import SkillRegistry
        skills_registry = type('InstanceSkillRegistry', (SkillRegistry,), {
            '_skills': {},  # Instance-specific skills dict
        })

        # Register skill from specified directories
        if skills:
            skills_registry.register_multiple_from_directory(skill_directory=skills_directory, skills_list=skills)

        # Resolve tool names from YAML to actual tool objects
        # Tools can be specified as strings (tool names) or tool objects
        resolved_tools = skills_registry._get_tool_registry().get_all()  # Start with all tools from skills
        # register_common_tools()
        # resolved_tools.extend(ToolRegistry.get_all()) # Append global tools

        # Register built-in tools from declarative_agent_sdk's builtin directory
        ToolRegistry.register_built_in_tools()

        if tools:
            for tool_item in tools:
                if isinstance(tool_item, str):
                    # Tool name - resolve from instance registry
                    try:
                        resolved_tools.append(ToolRegistry.get(tool_item))
                    except ValueError:
                        logger.warning(f"Tool '{tool_item}' not found in instance registry, skipping")
                else:
                    # Already a tool object
                    resolved_tools.append(tool_item)
        else:
            logger.info("No tools specified in configuration, using all tools from built-in registry")
            resolved_tools.extend(ToolRegistry.get_all())
        logger.info(f"resolved tools : {resolved_tools}")
        
        # Define automatic function calling config
        # Controls how many rounds of tool calls the agent can make
        afc_config = types.AutomaticFunctionCallingConfig(
            maximum_remote_calls=MAX_REMOTE_CALLS,  # e.g., limit to 5 rounds of tool calls (default is 10)
            # disable=True,          # Optional: fully disable auto-loop if needed
        )

        # Initialize parent Agent class with all configuration
        logger.debug(f"Tools approval required: {tools_approval_required}")
        super().__init__(
            model=model,
            name=name,
            description=description,
            instruction=instruction_text,
            tools=resolved_tools,
            generate_content_config=types.GenerateContentConfig(
                automatic_function_calling=afc_config,
                # Add other Gemini configs if needed: temperature=0.7, max_output_tokens=2048, etc.
            ),
            output_key=output_key,
            before_model_callback=dynamic_context_callback,
            before_tool_callback=dynamic_tool_callback if tools_approval_required else None
        )
        
        # Set custom fields AFTER parent initialization
        # These are excluded from Pydantic model but stored on the instance
        object.__setattr__(self, 'instruction_file', instruction_file)
        object.__setattr__(self, 'input_key_map', input_key_map or {})
        object.__setattr__(self, 'skills_registry', skills_registry)
        object.__setattr__(self, 'context_window', context_window)
        object.__setattr__(self, 'max_output_tokens', max_output_tokens)
        object.__setattr__(self, 'enable_truncation', enable_truncation)
        object.__setattr__(self, 'truncate_strategy', truncate_strategy)
        object.__setattr__(self, 'safety_margin', safety_margin)
        object.__setattr__(self, 'skill_directory', skills_directory)
        object.__setattr__(self, 'workspace_directory', workspace_directory)
        object.__setattr__(self, 'skills', skills or [])
        object.__setattr__(self, 'agent_card', None)
        object.__setattr__(self, 'publish_url', publish_url)  

        # Create agent card
        skill_descriptions = skills_registry.get_all_skills_description()
        # Don't pass URL during initialization - it will be set by AIAgentServer
        self.agent_card = self._create_agent_card(name, description, skill_descriptions, url=publish_url)

        # Create workspace directory
        try:
            if not os.path.exists(workspace_directory):
                os.makedirs(workspace_directory)
        except Exception as e:
            logger.error(f"Failed to create output directory {workspace_directory}: {e}")
            raise

        object.__setattr__(self, 'session_service', None)
        object.__setattr__(self, 'session_id', None)
        object.__setattr__(self, 'user_id', None)
        object.__setattr__(self, 'runner', None)
        object.__setattr__(self, "event_loop_running", False)

        self.session_service = InMemorySessionService()
        self.session_id = uuid.uuid4().hex
        self.user_id = "user_id"
        
        # Create session synchronously (since __init__ cannot be async)
        try:
            asyncio.get_running_loop()
            object.__setattr__(self, "event_loop_running", True)
        except RuntimeError:
            # Event loop is not running safe to create session.
            asyncio.run(self.session_service.create_session(
                app_name=self.name,
                user_id=self.user_id,
                session_id=self.session_id,
            
            ))
            object.__setattr__(self, "event_loop_running", False)
        
        # Create runner
        self.runner = Runner(
            agent=self,
            app_name=self.name,
            session_service=self.session_service,
            # plugins=[SmartContextFilterPlugin(get_updated_context_func=get_updated_context)]
        )

    async def run(self, 
                  input_text: str, 
                  app_name: Optional[str] = None, 
                  user_id: str = "user123") -> dict[str, Any]:
        """
        Run the agent with the given input.
        
        Args:
            input_text: The input query or prompt for the agent
            app_name: Optional app name (defaults to agent name)
            user_id: Optional user ID (defaults to "user123")
            
        Returns:
            Dictionary containing:
                - final_response: The agent's final response text
                - session_id: The session ID used
                - output_key_data: Data stored in output_key (if configured)
        """
        # Ensure required attributes are initialized
        assert self.runner is not None, "Runner not initialized"
        assert self.session_id is not None, "Session ID not initialized"
        assert self.session_service is not None, "Session service not initialized"
        
        # Delayed session creation if event loop is not running.
        if self.event_loop_running:
            await self.session_service.create_session(
                app_name=self.name,
                user_id=self.user_id,
                session_id=self.session_id,
            
            )

        # Apply token truncation if configured
        processed_input = input_text
        if self.enable_truncation and self.context_window and self.max_output_tokens:
            logger.info(f"Token truncation enabled for agent '{self.name}'")
            processed_input = fit_to_context_window(
                input_text=input_text,
                max_context_tokens=self.context_window,
                max_output_tokens=self.max_output_tokens,
                model="gpt-4",  # For tokenization
                safety_margin=self.safety_margin,
                truncate_strategy=self.truncate_strategy
            )
        
        # Create content message
        content = types.Content(
            role="user",
            parts=[types.Part(text=processed_input)]
        )
        
        final_response = ""
        
        # Run async loop
        logger.info(f"runner = {self.runner} session_id = {self.session_id} user_id = {self.user_id}")
        async for event in self.runner.run_async(
            user_id=self.user_id,
            session_id=self.session_id,
            new_message=content
        ):
            if event.content and event.content.parts:
                logger.info(f"EVENT: {event.content.parts}")

            
            if event.is_final_response():
                if event.content and event.content.parts:
                    final_response = event.content.parts[0].text
                break
        
        # Get output_key data if configured
        output_key_data = None
        if self.output_key:
            session = await self.session_service.get_session(
                app_name=self.name,
                user_id=self.user_id,
                session_id=self.session_id
            )
            if session:
                output_key_data = session.state.get(self.output_key)
        
        return {
            "final_response": final_response,
            "session_id": self.session_id,
            "output_key_data": output_key_data
        }
    
    def run_sync(self, 
                 input_text: str, 
                 app_name: Optional[str] = None, 
                 user_id: str = "user123") -> dict[str, Any]:
        """
        Synchronous wrapper for the run method.
        
        Args:
            input_text: The input query or prompt for the agent
            app_name: Optional app name (defaults to agent name)
            user_id: Optional user ID (defaults to "user123")
            
        Returns:
            Dictionary containing final_response, session_id, and output_key_data
        """
        return asyncio.run(self.run(input_text, app_name, user_id))

    def _create_agent_card(self, name: str, description: str, skills: Dict[str, str] | None, url: Optional[str] = None):
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

        agent_card = create_agent_card(
            agent_name=name,
            description=description,
            skills=agent_skills
        )
        
        # Override URL if provided (to match the actual server port)
        if url:
            agent_card.url = url
        
        return agent_card