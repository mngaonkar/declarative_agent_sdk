"""
Agent Factory - Create AIAgent instances from YAML configuration files.

This module provides a factory pattern to instantiate AIAgent objects
from YAML definitions, making agent configuration more maintainable
and declarative.
"""

import yaml
from typing import Any, Dict, List, Optional
from pathlib import Path
from declarative_agent_sdk import AIAgent
from declarative_agent_sdk.constants import DEFAULT_MODEL, SKILLS_DIRECTORY, WORKSPACE_DIRECTORY
from declarative_agent_sdk.tool_registry import ToolRegistry
from declarative_agent_sdk.model_factory import ModelFactory
from declarative_agent_sdk.agent_logging import get_logger

logger = get_logger(__name__)


class AgentFactory:
    """
    Factory class to create AIAgent instances from YAML configuration.
    
    The factory follows the Factory Pattern - it creates agents but doesn't
    track them. If you need to manage created agents globally, use AgentRegistry:
    
    Example with AgentRegistry:
        from declarative_agent_sdk import AgentFactory, AgentRegistry
        
        # Create agent
        agent = AgentFactory.from_yaml_file('config.yaml')
        
        # Register for global tracking
        AgentRegistry.register(agent, category='workflow')
        
        # Retrieve later
        agent = AgentRegistry.get('my_agent')
    """
    
    @staticmethod
    def from_yaml_file(yaml_file_path: str) -> AIAgent:
        """
        Create an AIAgent from a YAML configuration file.
        
        Args:
            yaml_file_path: Path to the YAML configuration file
            
        Returns:
            AIAgent instance configured according to the YAML file
            
        Example YAML format (Google Gemini):
            name: toc_agent
            description: An agent to create a table of contents for a book
            instruction_file: toc_agent/SKILL.md
            skills:
              - skills/toc
              - skills/chapter
            tools:
              - google_search
              - toc_validate_yaml  # Auto-discovered from skills/toc/scripts
            output_key: toc_agent_response
            model: gemini-2.0-flash-exp  # optional, defaults to DEFAULT_MODEL
            
        Example YAML format (vLLM):
            name: vllm_agent
            description: Agent using local vLLM server
            instruction_file: agent/instructions.md
            model: Qwen/Qwen3-4B-Thinking-2507-FP8
            provider: vllm
            endpoint:
              url: http://10.0.0.147:8000/v1
              max_tokens: 3000
              temperature: 0.8
            # Or specify at root level:
            # max_tokens: 3000
            # temperature: 0.8
            
        Example YAML format (with Token Management):
            name: vllm_agent_with_truncation
            description: Agent with automatic input truncation
            instruction_file: agent/instructions.md
            model: Qwen/Qwen3-4B-Thinking-2507-FP8
            provider: vllm
            endpoint:
              url: http://10.0.0.147:8000/v1
              max_tokens: 3000
              temperature: 0.8
            # Token management configuration (optional)
            enable_truncation: true         # Enable automatic input truncation
            context_window: 20384           # Total context window size
            truncate_strategy: end          # "end", "start", or "middle"
            safety_margin: 100              # Extra tokens for overhead
        """
        yaml_path = Path(yaml_file_path)
        if not yaml_path.exists():
            raise FileNotFoundError(f"YAML file not found: {yaml_file_path}")
        
        with open(yaml_path, 'r') as f:
            config = yaml.safe_load(f)
        
        return AgentFactory.from_dict(config)
    
    @staticmethod
    def from_dict(config: Dict[str, Any]) -> AIAgent:
        """
        Create an AIAgent from a configuration dictionary.
        
        Args:
            config: Dictionary containing agent configuration
                   Required: name, instruction_file
                   Optional: description, skills, tools, output_key, model, 
                            provider, endpoint, max_tokens, temperature,
                            context_window, enable_truncation, truncate_strategy,
                            safety_margin
            
        Returns:
            AIAgent instance configured according to the dictionary
        """
        # Extract required fields
        name = config.get('name')
        if not name:
            raise ValueError("Agent 'name' is required in configuration")
        
        instruction_file = config.get('instruction_file')
        if not instruction_file:
            logger.warning(f"Agent '{name}' has no instruction_file specified, will use instruction from skills if available")
        
        # Extract optional fields with defaults
        description = config.get('description', '')
        output_key = config.get('output_key', None)
        model_name = config.get('model', DEFAULT_MODEL)
        skills = config.get('skills', None)
        skills_directory = config.get('skills_directory', SKILLS_DIRECTORY)
        workspace_directory = config.get('workspace_directory', WORKSPACE_DIRECTORY)
        provider = config.get('provider', None)
        endpoint_config = config.get('endpoint', None)
        tools_approval_required = config.get('tools_approval_required', True)
        
        # Extract endpoint URL if endpoint configuration exists
        endpoint_url = None
        max_tokens = None
        temperature = None
        
        if endpoint_config and isinstance(endpoint_config, dict):
            endpoint_url = endpoint_config.get('url')
            max_tokens = endpoint_config.get('max_tokens')
            temperature = endpoint_config.get('temperature')
        
        # Also check for max_tokens and temperature at root level (if not in endpoint)
        if max_tokens is None:
            max_tokens = config.get('max_tokens')
        if temperature is None:
            temperature = config.get('temperature')
        
        # Extract context window configuration for token management
        context_window = config.get('context_window')
        enable_truncation = config.get('enable_truncation', False)
        truncate_strategy = config.get('truncate_strategy', 'end')
        safety_margin = config.get('safety_margin', 100)
        publish_url = config.get('publish_url', None)
        
        # Create model object using ModelFactory
        # Build kwargs dynamically to avoid passing None values
        model_kwargs = {}
        if max_tokens is not None:
            model_kwargs['max_tokens'] = max_tokens
        if temperature is not None:
            model_kwargs['temperature'] = temperature
        
        model = ModelFactory.create_model(
            model_name=model_name,
            provider=provider,
            endpoint_url=endpoint_url,
            **model_kwargs
        )
        logger.debug(f"Created model for agent '{name}': {model}")
        
        # Process tools - tool names can be resolved from global registry or instance registry
        # Instance registry automatically discovers from skills directories
        tool_names = config.get('tools', [])
        tools = []
        if tool_names:
            try:
                tools = ToolRegistry.get_multiple(tool_names)
            except ValueError as e:
                # It's okay if tools aren't in global registry - they may be auto-discovered from skills
                logger.info(f"Some tools for agent '{name}' will be resolved from skills: {e}")
                # Pass tool names as-is and let AIAgent resolve from its instance registry
                tools = tool_names
        
        # Create and return the agent
        logger.info(f"Creating agent '{name}' from configuration")
        agent = AIAgent(
            name=name,
            description=description,
            instruction_file=instruction_file if instruction_file is not None else '',
            tools=tools,
            tools_approval_required=tools_approval_required,
            output_key=output_key,
            model=model,
            skills_directory=skills_directory,
            skills=skills,
            context_window=context_window,
            max_output_tokens=max_tokens,
            enable_truncation=enable_truncation,
            truncate_strategy=truncate_strategy,
            safety_margin=safety_margin,
            workspace_directory=workspace_directory,
            publish_url=publish_url
        )
        
        return agent
    
    @staticmethod
    def from_yaml_string(yaml_string: str) -> AIAgent:
        """
        Create an AIAgent from a YAML string.
        
        Args:
            yaml_string: YAML configuration as a string
            
        Returns:
            AIAgent instance configured according to the YAML string
        """
        config = yaml.safe_load(yaml_string)
        return AgentFactory.from_dict(config)

