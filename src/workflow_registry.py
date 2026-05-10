"""
Workflow Registry - Registry pattern for workflow functions.

This module provides a centralized registry to map function names to actual
callable functions used in LangGraph workflows.
"""

from typing import Callable, Dict, List
from declarative_agent_sdk.agent_logging import get_logger

logger = get_logger(__name__)


class WorkflowRegistry:
    """
    Registry to map function names to actual callable functions.
    
    This class uses the Registry pattern to maintain a global mapping of
    function names (strings used in YAML) to their actual Python function objects.
    
    Example:
        # Register functions
        WorkflowRegistry.register('my_agent', my_agent_function)
        WorkflowRegistry.register('my_router', my_router_function)
        
        # Retrieve functions
        func = WorkflowRegistry.get('my_agent')
        
        # List all registered
        all_funcs = WorkflowRegistry.list_available()
    """
    
    _functions: Dict[str, Callable] = {}
    
    @classmethod
    def register(cls, name: str, function: Callable) -> None:
        """
        Register a function with a given name.
        
        Args:
            name: String identifier for the function (used in YAML)
            function: Callable function object
            
        Example:
            WorkflowRegistry.register('toc_agent', toc_agent)
        """
        cls._functions[name] = function
        logger.debug(f"Registered workflow function: {name}")
    
    @classmethod
    def get(cls, name: str) -> Callable:
        """
        Get a function by name.
        
        Args:
            name: The function name to retrieve
            
        Returns:
            The callable function object
            
        Raises:
            ValueError: If function name is not found in registry
            
        Example:
            func = WorkflowRegistry.get('toc_agent')
        """
        if name not in cls._functions:
            available = list(cls._functions.keys())
            raise ValueError(
                f"Function '{name}' not found in registry. "
                f"Available functions: {available}"
            )
        return cls._functions[name]
    
    @classmethod
    def list_available(cls) -> List[str]:
        """
        List all available function names.
        
        Returns:
            List of registered function names
            
        Example:
            functions = WorkflowRegistry.list_available()
            print(functions)  # ['toc_agent', 'chapter_agent', 'collation_agent']
        """
        return list(cls._functions.keys())
    
    @classmethod
    def clear(cls) -> None:
        """
        Clear all registered functions.
        
        This is primarily useful for testing to reset the registry state.
        
        Example:
            WorkflowRegistry.clear()
        """
        cls._functions.clear()
        logger.debug("Cleared all workflow functions from registry")
    
    @classmethod
    def is_registered(cls, name: str) -> bool:
        """
        Check if a function name is registered.
        
        Args:
            name: The function name to check
            
        Returns:
            True if registered, False otherwise
            
        Example:
            if WorkflowRegistry.is_registered('my_func'):
                print("Function is registered")
        """
        return name in cls._functions
    
    @classmethod
    def register_multiple(cls, functions: Dict[str, Callable]) -> None:
        """
        Register multiple functions at once.
        
        Args:
            functions: Dictionary mapping function names to callable functions
            
        Example:
            WorkflowRegistry.register_multiple({
                'toc_agent': toc_agent,
                'chapter_agent': chapter_agent,
                'collation_agent': collation_agent
            })
        """
        for name, function in functions.items():
            cls.register(name, function)
        logger.info(f"Registered {len(functions)} workflow functions")
