from google.adk.agents.llm_agent import Agent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.tools import BaseTool, ToolContext, FunctionTool
from google.genai import types
from google.adk.agents.callback_context import CallbackContext
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from a2a.types import AgentCard, AgentCapabilities, AgentInterface, AgentSkill, Message, Role
import json
from google.protobuf.json_format import MessageToDict
from a2a.utils.constants import TransportProtocol, PROTOCOL_VERSION_CURRENT
from a2a.server.agent_execution import RequestContext

from declarative_agent_sdk.utils import read_from_file
from declarative_agent_sdk.constants import DEFAULT_MODEL, MAX_REMOTE_CALLS, SKILLS_DIRECTORY, WORKSPACE_DIRECTORY
from declarative_agent_sdk.agent_logging import get_logger
from declarative_agent_sdk.token_utils import fit_to_context_window
from declarative_agent_sdk.tool_registry import ToolRegistry
from declarative_agent_sdk.a2a_utils import create_agent_card
import asyncio
import uuid
import os
from typing import Optional, Any, Union, List, Dict, AsyncIterator
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
        instruction_text = read_from_file(instruction_file) if instruction_file else ''
                
        # Create instance-level skill registry (isolated from global registry)
        # This allows for instance-specific skill management if needed in the future
        from declarative_agent_sdk.skill_registry import SkillRegistry
        skills_registry = type('InstanceSkillRegistry', (SkillRegistry,), {
            '_skills': {},  # Instance-specific skills dict
        })

        # Register only specified skills from skill directory
        if skills:
            skills_registry.register_multiple_from_directory(skill_directory=skills_directory, skills_list=skills)

        # Resolve tool names from YAML to actual tool objects
        # Tools can be specified as strings (tool names) or tool objects
        resolved_tools = skills_registry._get_tool_registry().get_all()  # Start with all tools from skills

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

        # Set up tool approval if required
        logger.debug(f"Tools approval required: {tools_approval_required}")
        if tools_approval_required:
            resolved_tools = [FunctionTool(tool, require_confirmation=True) for tool in resolved_tools]
        
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
            # before_tool_callback=dynamic_tool_callback if tools_approval_required else None
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
        self.agent_card = create_agent_card(name=name, description=description, skills=skill_descriptions, url=publish_url)

        # Create workspace directory
        try:
            if not os.path.exists(workspace_directory):
                os.makedirs(workspace_directory)
        except Exception as e:
            logger.error(f"Failed to create output directory {workspace_directory}: {e}")
            raise

        object.__setattr__(self, 'session_service', None)
        object.__setattr__(self, 'user_id', None)
        object.__setattr__(self, 'runner', None)
        object.__setattr__(self, "event_loop_running", False)

        self.session_service = InMemorySessionService()
        self.user_id = "user_id"
                
        # Create runner
        self.runner = Runner(
            agent=self,
            app_name=self.name,
            session_service=self.session_service,
            # plugins=[SmartContextFilterPlugin(get_updated_context_func=get_updated_context)]
        )

    async def _get_or_create_session(self, session_id: str):
        assert self.session_service is not None, "Session service not initialized"
        assert self.user_id is not None, "User ID not set"
        assert session_id is not None, "Session ID not set"

        session = await self.session_service.get_session(
                app_name=self.name,
                user_id=self.user_id,
                session_id=session_id,
            )
        if session:
            logger.info(f"Session already exists: {session_id}")
            return session
        else:
            logger.info(f"Creating new session: {session_id}")
            session = await self.session_service.create_session(
                app_name=self.name,
                user_id=self.user_id,
                session_id=session_id,
            )
            return session

    async def tool_confirmation(
        self,
        context_id: str,
        session_id: str,
        yes: bool
    ) -> AsyncIterator[Any]:
        assert self.runner is not None, "Runner not initialized"
        assert self.session_service is not None, "Session service not initialized"

        await self._get_or_create_session(session_id)

        content = types.Content(
            role="user",
            parts=[
                types.Part(
                    function_response=types.FunctionResponse(
                        id=context_id,
                        name="adk_request_confirmation",
                        response={
                            "confirmed": yes,
                            "payload": {
                            },
                        },
                    )
                )
            ],
        )

        async for event in self.runner.run_async(
            user_id=self.user_id,
            session_id=session_id,
            new_message=content,
        ):
            if event.content and event.content.parts:
                logger.info(f"EVENT: {event.content.parts}")
            yield event


    def adk_content_from_message(self, message: Message) -> types.Content:
        parts: list[types.Part] = []
        for part in message.parts:
            which = part.WhichOneof('content')
            if which == 'text' and part.text:
                parts.append(types.Part(text=part.text))
            elif which == 'data':
                data = MessageToDict(part.data)
                fn_resp = data.get('function_response') or data.get('functionResponse')
                if fn_resp:
                    parts.append(types.Part(
                        function_response=types.FunctionResponse(
                            id=fn_resp.get('id', ''),
                            name=fn_resp.get('name', ''),
                            response=fn_resp.get('response', {}),
                        )
                    ))
                else:
                    parts.append(types.Part(text=json.dumps(data)))
        if not parts:
            raise ValueError("Could not extract any content from the A2A message")
        return types.Content(parts=parts, role="user")


    async def invoke(self, context: RequestContext) -> AsyncIterator[Any]:
        assert self.runner is not None, "Runner not initialized"
        assert self.session_service is not None, "Session service not initialized"
        assert context is not None, "Context is required for running the agent"
        assert context.message is not None, "Context message is required for running the agent"
        assert context.context_id is not None, "Context ID is required for running the agent"

        # TODO: apply token truncation if configured

        _ = await self._get_or_create_session(context.context_id)
        logger.info(f"runner = {self.runner} session_id = {context.context_id} user_id = {self.user_id}")
        
        # Experimental API to convert incoming A2A message to ADK event format
        # adk_event = convert_a2a_message_to_event(context.message, author="user")
        new_message = self.adk_content_from_message(context.message)

        async for event in self.runner.run_async(
            user_id=self.user_id,
            session_id=context.context_id,
            new_message=new_message,
        ):
            if event.content and event.content.parts:
                logger.info(f"EVENT: {event.content.parts}")
            yield event
    

    def run_sync(self, input_text: str, session_id: str) -> str:
        async def _collect() -> str:
            final_response = ""
            async for event in self.invoke(input_text, session_id):
                if event.is_final_response() and event.content and event.content.parts:
                    final_response = event.content.parts[0].text
            return final_response
        return asyncio.run(_collect())