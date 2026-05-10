"""
Agent Registry - Registry pattern for managing AIAgent instances.

This module provides a centralized registry to track and manage agent instances
created by AgentFactory or manually. This is separate from the factory pattern
to maintain separation of concerns.
"""

from typing import Dict, List, Any, Optional
from declarative_agent_sdk.agent_logging import get_logger
from declarative_agent_sdk import AIAgent

logger = get_logger(__name__)


class AgentRegistry:
    """
    Registry to track and manage AIAgent instances.
    
    This registry allows you to:
    - Register agents for global lookup by name
    - Retrieve agents without recreating them
    - List all registered agents
    - Track agent metadata and statistics
    
    Example:
        # Create and register agent
        agent = AgentFactory.from_yaml_file('config.yaml')
        AgentRegistry.register(agent)
        
        # Retrieve later
        agent = AgentRegistry.get('my_agent')
        
        # List all agents
        agents = AgentRegistry.list_available()
    
    Note:
        This is a class-level registry (singleton pattern). All registered
        agents are shared across the application.
    """
    
    _agents: Dict[str, Any] = {}  # Maps agent name to agent instance
    _metadata: Dict[str, Dict[str, Any]] = {}  # Maps agent name to metadata
    
    @classmethod
    def register(
        cls,
        agent: Any,
        category: Optional[str] = None,
        **metadata
    ) -> None:
        """
        Register an agent instance in the registry.
        
        Args:
            agent: AIAgent instance to register (must have 'name' attribute)
            category: Optional category (e.g., 'workflow', 'tool', 'assistant')
            **metadata: Additional metadata to store with the agent
            
        Raises:
            ValueError: If agent doesn't have a name attribute
            Warning: If agent name is already registered (will overwrite)
            
        Example:
            agent = AgentFactory.from_yaml_file('toc_agent.yaml')
            AgentRegistry.register(
                agent,
                category='workflow',
                purpose='Generate table of contents'
            )
        """
        if not hasattr(agent, 'name'):
            raise ValueError("Agent must have a 'name' attribute to be registered")
        
        agent_name = agent.name
        
        # Warn if overwriting existing agent
        if agent_name in cls._agents:
            logger.warning(f"Agent '{agent_name}' is already registered. Overwriting.")
        
        # Store agent instance
        cls._agents[agent_name] = agent
        
        # Store metadata
        agent_metadata = {
            'category': category,
            'description': getattr(agent, 'description', None),
            'model': str(getattr(agent, 'model', None)),
            'instruction_file': getattr(agent, 'instruction_file', None),
            **metadata
        }
        cls._metadata[agent_name] = agent_metadata
        
        logger.info(f"Registered agent: {agent_name} (category: {category})")
    
    @classmethod
    def get(cls, name: str) -> AIAgent:
        """
        Get an agent by name.
        
        Args:
            name: Agent name
            
        Returns:
            AIAgent instance
            
        Raises:
            ValueError: If agent name is not found in registry
            
        Example:
            agent = AgentRegistry.get('toc_agent')
            result = agent.run_sync("Generate ToC for my book")
        """
        if name not in cls._agents:
            available = list(cls._agents.keys())
            raise ValueError(
                f"Agent '{name}' not found in registry. "
                f"Available agents: {available}"
            )
        return cls._agents[name]
    
    @classmethod
    def get_metadata(cls, name: str) -> Dict[str, Any]:
        """
        Get metadata for an agent.
        
        Args:
            name: Agent name
            
        Returns:
            Dictionary of metadata including category, description, model, etc.
            
        Example:
            metadata = AgentRegistry.get_metadata('toc_agent')
            print(f"Model: {metadata['model']}")
            print(f"Category: {metadata['category']}")
        """
        return cls._metadata.get(name, {})
    
    @classmethod
    def list_available(cls, category: Optional[str] = None) -> List[str]:
        """
        List all registered agent names, optionally filtered by category.
        
        Args:
            category: Optional category to filter by
            
        Returns:
            List of agent names
            
        Example:
            all_agents = AgentRegistry.list_available()
            workflow_agents = AgentRegistry.list_available(category='workflow')
        """
        if category is None:
            return list(cls._agents.keys())
        
        return [
            name for name, metadata in cls._metadata.items()
            if metadata.get('category') == category
        ]
    
    @classmethod
    def list_by_category(cls) -> Dict[str, List[str]]:
        """
        List all agents grouped by category.
        
        Returns:
            Dictionary mapping categories to lists of agent names
            
        Example:
            by_category = AgentRegistry.list_by_category()
            print(by_category)
            # {'workflow': ['toc_agent', 'chapter_agent'], 
            #  'tool': ['validator_agent'], ...}
        """
        categories: Dict[str, List[str]] = {}
        for name, metadata in cls._metadata.items():
            category = metadata.get('category', 'uncategorized')
            if category not in categories:
                categories[category] = []
            categories[category].append(name)
        return categories
    
    @classmethod
    def is_registered(cls, name: str) -> bool:
        """
        Check if an agent is registered.
        
        Args:
            name: Agent name to check
            
        Returns:
            True if registered, False otherwise
            
        Example:
            if AgentRegistry.is_registered('toc_agent'):
                agent = AgentRegistry.get('toc_agent')
        """
        return name in cls._agents
    
    @classmethod
    def unregister(cls, name: str) -> None:
        """
        Unregister an agent.
        
        This removes the agent from the registry but doesn't delete the
        agent instance itself. Any external references to the agent
        will still be valid.
        
        Args:
            name: Agent name to unregister
            
        Example:
            AgentRegistry.unregister('old_agent')
        """
        if name in cls._agents:
            del cls._agents[name]
        if name in cls._metadata:
            del cls._metadata[name]
        logger.debug(f"Unregistered agent: {name}")
    
    @classmethod
    def clear(cls) -> None:
        """
        Clear all registered agents.
        
        This is primarily useful for testing to reset the registry state.
        Agent instances themselves are not deleted, only the registry
        references are cleared.
        
        Example:
            AgentRegistry.clear()
        """
        cls._agents.clear()
        cls._metadata.clear()
        logger.debug("Cleared all agents from registry")
    
    @classmethod
    def info(cls) -> Dict[str, Any]:
        """
        Get information about the registry.
        
        Returns:
            Dictionary with registry statistics including total agents,
            categories, and agents by category
            
        Example:
            info = AgentRegistry.info()
            print(f"Total agents: {info['total_agents']}")
            print(f"Categories: {info['categories']}")
        """
        by_category = cls.list_by_category()
        return {
            'total_agents': len(cls._agents),
            'categories': list(by_category.keys()),
            'agents_by_category': by_category
        }
    
    @classmethod
    def get_all(cls) -> Dict[str, Any]:
        """
        Get all registered agent instances.
        
        Returns:
            Dictionary mapping agent names to agent instances
            
        Example:
            all_agents = AgentRegistry.get_all()
            for name, agent in all_agents.items():
                print(f"Agent: {name}")
        """
        return cls._agents.copy()
    
    @classmethod
    def register_multiple(
        cls,
        agents: List[Any],
        category: Optional[str] = None
    ) -> int:
        """
        Register multiple agents at once.
        
        Args:
            agents: List of AIAgent instances to register
            category: Optional category to apply to all agents
            
        Returns:
            Number of agents successfully registered
            
        Example:
            agents = [
                AgentFactory.from_yaml_file('agent1.yaml'),
                AgentFactory.from_yaml_file('agent2.yaml'),
            ]
            count = AgentRegistry.register_multiple(agents, category='workflow')
            print(f"Registered {count} agents")
        """
        registered = 0
        for agent in agents:
            try:
                cls.register(agent, category=category)
                registered += 1
            except Exception as e:
                logger.error(f"Failed to register agent: {e}")
        
        logger.info(f"Registered {registered}/{len(agents)} agents")
        return registered
    
    @classmethod
    def register_from_yaml_files(
        cls,
        yaml_files: List[str],
        category: Optional[str] = None
    ) -> int:
        """
        Create and register agents from YAML files.
        
        This is a convenience method that combines AgentFactory.from_yaml_file()
        with registration.
        
        Args:
            yaml_files: List of paths to YAML configuration files
            category: Optional category to apply to all agents
            
        Returns:
            Number of agents successfully created and registered
            
        Example:
            files = [
                'agents/toc_agent.yaml',
                'agents/chapter_agent.yaml',
                'agents/collation_agent.yaml'
            ]
            count = AgentRegistry.register_from_yaml_files(files, category='workflow')
            print(f"Registered {count} agents")
        """
        from declarative_agent_sdk.agent_factory import AgentFactory
        
        registered = 0
        for yaml_file in yaml_files:
            try:
                agent = AgentFactory.from_yaml_file(yaml_file)
                cls.register(agent, category=category)
                registered += 1
            except Exception as e:
                logger.error(f"Failed to create/register agent from {yaml_file}: {e}")
        
        logger.info(f"Registered {registered}/{len(yaml_files)} agents from YAML files")
        return registered
