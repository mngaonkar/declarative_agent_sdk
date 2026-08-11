"""Google ADK-backed agent — ADK Runner owns the loop end-to-end.

Peer of LeanAIAgent and LangChainAIAgent. Select with::

    agent_framework: adk

Do not nest ADK under lean or deepagent; pick one outer loop.
"""

import asyncio
import json
import os
import uuid
from typing import Any, AsyncIterator, Dict, List, Optional, Union

from google.adk.agents.callback_context import CallbackContext
from google.adk.agents.llm_agent import Agent as _ADKAgent
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.tools import BaseTool, FunctionTool, ToolContext
from google.genai import types
from google.protobuf.json_format import MessageToDict

from a2a.server.agent_execution import RequestContext
from a2a.types import Message

from declarative_agent_sdk.transports.a2a.utils import create_agent_card
from declarative_agent_sdk.core.agent_event import AgentEvent, from_adk_event
from declarative_agent_sdk.core.agent_logging import get_logger
from declarative_agent_sdk.core.base_agent import BaseAgent
from declarative_agent_sdk.core.constants import (
    DEFAULT_MODEL,
    MAX_REMOTE_CALLS,
    SKILLS_DIRECTORY,
    WORKSPACE_DIRECTORY,
)
from declarative_agent_sdk.tools.tool_registry import ToolRegistry
from declarative_agent_sdk.core.utils import read_from_file
import declarative_agent_sdk.agents.adk.plugins.context_updater as context_updater

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# ADK callbacks (unchanged from before)
# ---------------------------------------------------------------------------

async def dynamic_tool_callback(
    tool: BaseTool, args: Dict[str, Any], tool_context: ToolContext
) -> Optional[Dict]:
    logger.debug(f"Running dynamic_tool_callback for tool '{tool.name}' with args: {args}")
    if "agent_name" not in args:
        args["agent_name"] = tool_context.agent_name
    user_input = input(
        f"\033[93mTool '{tool.name}' is about to be called with args: {args}. "
        f"Do you want to proceed? (y/n): \033[0m"
    )
    if user_input.lower() != "y":
        logger.info(f"Tool '{tool.name}' call aborted by user.")
        return None


async def dynamic_context_callback(
    callback_context: CallbackContext, llm_request: LlmRequest
) -> Optional[LlmResponse]:
    logger.debug("Running dynamic_context_callback")
    agent_name = callback_context.agent_name
    modified_context = context_updater.get_updated_context(agent_name) or "No additional context"
    llm_request.config.system_instruction = modified_context
    logger.debug(f"Updated system instruction for '{agent_name}': {llm_request.config.system_instruction}")


# ---------------------------------------------------------------------------
# AIAgent
# ---------------------------------------------------------------------------

class AIAgent(BaseAgent):
    """
    Google ADK-backed agent.

    Composes (rather than extends) google.adk.agents.Agent so that AIAgent
    is a plain Python class that satisfies the BaseAgent interface.  The
    internal ADK agent is stored as self._adk_agent and passed to the Runner.
    """

    def __init__(
        self,
        name: str,
        instruction_file: str,
        description: str = "",
        tools: Optional[list] = None,
        tools_approval_required: bool = True,
        skills_directory: str = SKILLS_DIRECTORY,
        workspace_directory: str = WORKSPACE_DIRECTORY,
        skills: Optional[List[str]] = None,
        input_key_map: Optional[Dict[str, str]] = None,
        output_key: Optional[str] = None,
        model: Union[str, Any] = DEFAULT_MODEL,
        context_window: Optional[int] = None,
        max_output_tokens: Optional[int] = None,
        enable_truncation: bool = False,
        truncate_strategy: str = "end",
        safety_margin: int = 100,
        publish_url: Optional[str] = None,
    ) -> None:
        """
        Initialize the Google ADK agent.

        Args:
            name: Agent name (used for identification and logging).
            instruction_file: Path to the system instruction file (markdown).
            description: Brief description of the agent's purpose.
            tools: Tool names (strings) or callable tool objects.
            tools_approval_required: Wrap tools in FunctionTool(require_confirmation=True).
            skills_directory: Base directory containing skill sub-directories.
            workspace_directory: Directory for agent outputs / scratch files.
            skills: Skill sub-directory names to auto-discover tools from.
            input_key_map: Optional mapping of input keys.
            output_key: Session-state key for structured output.
            model: Model name string or ADK-compatible model object.
            context_window: Total context window size (for truncation).
            max_output_tokens: Reserved for truncation / config.
            enable_truncation: Automatically truncate inputs exceeding context_window.
            truncate_strategy: "start", "end", or "middle".
            safety_margin: Extra token buffer subtracted from context_window.
            publish_url: URL written into the A2A AgentCard for discovery.
        """
        # Plain instance attributes — no Pydantic fields needed
        self.name = name
        self.description = description
        self.input_key_map = input_key_map or {}
        self.output_key = output_key
        self.skills = skills or []
        self.publish_url = publish_url
        self.context_window = context_window
        self.max_output_tokens = max_output_tokens
        self.enable_truncation = enable_truncation
        self.truncate_strategy = truncate_strategy
        self.safety_margin = safety_margin
        self.skill_directory = skills_directory
        self.workspace_directory = workspace_directory
        self.instruction_file = instruction_file

        # Read system instruction
        instruction_text = read_from_file(instruction_file) if instruction_file else ""

        # Instance-isolated SkillRegistry
        from declarative_agent_sdk.tools.skill_registry import SkillRegistry

        skills_registry = type(
            "InstanceSkillRegistry",
            (SkillRegistry,),
            {"_skills": {}, "_metadata": {}, "_tool_registry_class": None},
        )
        if skills:
            skills_registry.register_multiple_from_directory(
                skill_directory=skills_directory, skills_list=skills
            )

        # Collect tool callables
        ToolRegistry.register_built_in_tools()
        resolved_tools: List[Any] = list(skills_registry._get_tool_registry().get_all())

        if tools:
            for tool_item in tools:
                if isinstance(tool_item, str):
                    try:
                        resolved_tools.append(ToolRegistry.get(tool_item))
                    except ValueError:
                        logger.warning(f"Tool '{tool_item}' not found in registry, skipping")
                else:
                    resolved_tools.append(tool_item)
        else:
            logger.info("No tools specified; using all tools from built-in registry")
            resolved_tools.extend(ToolRegistry.get_all())

        logger.info(f"Resolved tools: {resolved_tools}")

        # Optional tool-confirmation wrapping
        if tools_approval_required:
            resolved_tools = [
                FunctionTool(t, require_confirmation=True) for t in resolved_tools
            ]

        # AFC config
        afc_config = types.AutomaticFunctionCallingConfig(
            maximum_remote_calls=MAX_REMOTE_CALLS,
        )

        # Compose the internal ADK agent
        self._adk_agent = _ADKAgent(
            model=model,
            name=name,
            description=description,
            instruction=instruction_text,
            tools=resolved_tools,
            generate_content_config=types.GenerateContentConfig(
                automatic_function_calling=afc_config,
            ),
            output_key=output_key,
            before_model_callback=dynamic_context_callback,
        )

        # Session management
        self.session_service = InMemorySessionService()
        self.user_id = "user_id"
        self.runner = Runner(
            agent=self._adk_agent,
            app_name=name,
            session_service=self.session_service,
        )

        # Workspace
        if workspace_directory and not os.path.exists(workspace_directory):
            try:
                os.makedirs(workspace_directory)
            except Exception as exc:
                logger.error(f"Failed to create workspace '{workspace_directory}': {exc}")
                raise

        # A2A AgentCard
        skill_descriptions = skills_registry.get_all_skills_description()
        self.agent_card = create_agent_card(
            name=name,
            description=description,
            skills=skill_descriptions,
            url=publish_url,
        )

        self.skills_registry = skills_registry

    # ------------------------------------------------------------------
    # Session helpers
    # ------------------------------------------------------------------

    async def _get_or_create_session(self, session_id: str):
        session = await self.session_service.get_session(
            app_name=self.name,
            user_id=self.user_id,
            session_id=session_id,
        )
        if session:
            logger.info(f"Session already exists: {session_id}")
            return session
        logger.info(f"Creating new session: {session_id}")
        return await self.session_service.create_session(
            app_name=self.name,
            user_id=self.user_id,
            session_id=session_id,
        )

    # ------------------------------------------------------------------
    # ADK-specific helpers
    # ------------------------------------------------------------------

    def adk_content_from_message(self, message: Message) -> types.Content:
        parts: list[types.Part] = []
        for part in message.parts:
            which = part.WhichOneof("content")
            if which == "text" and part.text:
                parts.append(types.Part(text=part.text))
            elif which == "data":
                data = MessageToDict(part.data)
                fn_resp = data.get("function_response") or data.get("functionResponse")
                if fn_resp:
                    parts.append(
                        types.Part(
                            function_response=types.FunctionResponse(
                                id=fn_resp.get("id", ""),
                                name=fn_resp.get("name", ""),
                                response=fn_resp.get("response", {}),
                            )
                        )
                    )
                else:
                    parts.append(types.Part(text=json.dumps(data)))
        if not parts:
            raise ValueError("Could not extract any content from the A2A message")
        return types.Content(parts=parts, role="user")

    async def tool_confirmation(
        self,
        context_id: str,
        session_id: str,
        yes: bool,
    ) -> AsyncIterator[AgentEvent]:
        """Resume after Discord/A2A tool approval (ADK FunctionResponse handshake)."""
        await self._get_or_create_session(session_id)
        content = types.Content(
            role="user",
            parts=[
                types.Part(
                    function_response=types.FunctionResponse(
                        id=context_id,
                        name="adk_request_confirmation",
                        response={"confirmed": yes, "payload": {}},
                    )
                )
            ],
        )
        async for event in self.runner.run_async(
            user_id=self.user_id,
            session_id=session_id,
            new_message=content,
        ):
            mapped = from_adk_event(event)
            if mapped is not None:
                yield mapped

    # ------------------------------------------------------------------
    # BaseAgent interface
    # ------------------------------------------------------------------

    async def run_query(
        self, query: str, session_id: Optional[str] = None
    ) -> AsyncIterator[AgentEvent]:
        """Yield AgentEvents for a plain-text query."""
        sid = session_id or str(uuid.uuid4())
        await self._get_or_create_session(sid)
        new_message = types.Content(role="user", parts=[types.Part(text=query)])
        async for event in self.runner.run_async(
            user_id=self.user_id,
            session_id=sid,
            new_message=new_message,
        ):
            mapped = from_adk_event(event)
            if mapped is not None:
                yield mapped

    async def invoke(self, context: RequestContext) -> AsyncIterator[AgentEvent]:
        """Yield AgentEvents for an A2A RequestContext."""
        assert context is not None, "Context is required"
        assert context.message is not None, "Context message is required"
        assert context.context_id is not None, "Context ID is required"

        await self._get_or_create_session(context.context_id)
        new_message = self.adk_content_from_message(context.message)

        async for event in self.runner.run_async(
            user_id=self.user_id,
            session_id=context.context_id,
            new_message=new_message,
        ):
            mapped = from_adk_event(event)
            if mapped is not None:
                yield mapped
