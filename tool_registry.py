import os
import importlib.util
import inspect
from typing import Any, Dict, List, Callable
from pathlib import Path
from declarative_agent_sdk.agent_logging import get_logger
from dotenv import load_dotenv

load_dotenv()
logger = get_logger(__name__)

class ToolRegistry:
    """Registry to map tool names to actual tool objects."""
    
    _tools: Dict[str, Any] = {}
    
    @classmethod
    def register(cls, name: str, tool: Any) -> None:
        """Register a tool with a given name."""
        cls._tools[name] = tool
        logger.debug(f"Registered tool: {name}")
    
    @classmethod
    def get(cls, name: str) -> Any:
        """Get a tool by name."""
        if name not in cls._tools:
            raise ValueError(f"Tool '{name}' not found in registry. Available tools: {list(cls._tools.keys())}")
        return cls._tools[name]
    
    @classmethod
    def get_multiple(cls, names: List[str]) -> List[Any]:
        """Get multiple tools by their names."""
        return [cls.get(name) for name in names]
    
    @classmethod
    def get_all(cls) -> List[Any]:
        """Get the entire tool registry."""
        return list(cls._tools.values())
    
    @classmethod
    def list_available(cls) -> List[str]:
        """List all available tool names."""
        return list(cls._tools.keys())
    
    @classmethod
    def register_built_in_tools(cls):
        """Register built-in tools from builtin directory."""
        builtin_dir = Path(__file__).parent / 'builtin_tools'
        if not builtin_dir.exists():
            logger.warning(f"Built-in tools directory does not exist: {builtin_dir}")
            return
        
        registered_count = 0
        for file in builtin_dir.glob('*.py'):
            if file.name.startswith('__'):
                continue
            registered_count += cls._register_functions_from_file(str(file), prefix='')
        
        logger.info(f"Registered {registered_count} built-in tools from {builtin_dir}")

    @classmethod
    def register_from_scripts_folder(cls, scripts_dir: str, prefix: str = '') -> int:
        """
        Auto-discover and register all functions from Python files in a scripts directory.
        
        Args:
            scripts_dir: Path to the scripts directory
            prefix: Optional prefix to add to function names when registering
            
        Returns:
            Number of functions registered
            
        Example:
            # Register all functions from skills/toc/scripts/
            count = ToolRegistry.register_from_scripts_folder('skills/toc/scripts', prefix='toc_')
        """
        if not os.path.exists(scripts_dir):
            logger.warning(f"Scripts directory does not exist: {scripts_dir}")
            return 0
        
        registered_count = 0
        
        # Walk through all Python files in the directory
        for root, dirs, files in os.walk(scripts_dir):
            for file in files:
                if file.endswith('.py') and not file.startswith('__'):
                    file_path = os.path.join(root, file)
                    registered_count += cls._register_functions_from_file(file_path, prefix)
        
        return registered_count
    
    @classmethod
    def _register_functions_from_file(cls, file_path: str, prefix: str = '') -> int:
        """
        Register all functions from a Python file.
        
        Args:
            file_path: Path to the Python file
            prefix: Optional prefix to add to function names
            
        Returns:
            Number of functions registered
        """
        try:
            # Load the module from file path
            module_name = Path(file_path).stem
            spec = importlib.util.spec_from_file_location(module_name, file_path)
            if spec is None or spec.loader is None:
                logger.warning(f"Could not load module from {file_path}")
                return 0
            
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            
            # Find all functions in the module
            registered_count = 0
            for name, obj in inspect.getmembers(module):
                # Register only user-defined functions (not imported ones)
                if (inspect.isfunction(obj) and 
                    obj.__module__ == module.__name__ and 
                    not name.startswith('_')) and name not in ['main']:
                    
                    tool_name = f"{prefix}{name}" if prefix else name
                    cls.register(tool_name, obj)
                    registered_count += 1
                    logger.info(f"Registered function '{tool_name}' from {file_path}")
            
            return registered_count
            
        except Exception as e:
            logger.error(f"Error loading functions from {file_path}: {e}")
            return 0
    
    @classmethod
    def clear(cls) -> None:
        """Clear all registered tools. Useful for testing."""
        cls._tools.clear()
        logger.debug("Cleared all tools from registry")


def register_common_tools():
    """Register commonly used tools in the ToolRegistry."""
    try:
        from google.adk.tools import google_search
        #ToolRegistry.register('google_search', google_search)
        logger.info("Registered google_search tool")
    except ImportError as e:
        logger.warning(f"Could not import google_search: {e}")
    
    # Try to register Tavily search from Langchain if available
    try:
        from langchain_community.tools.tavily_search import TavilySearchResults
        tavily_api_key = os.getenv("TAVILY_API_KEY")
        if tavily_api_key:
            # Create Langchain tool instance
            _tavily_tool = TavilySearchResults(api_key=tavily_api_key)
            
            # Wrap in a callable function for ADK compatibility
            def tavily_search(query: str) -> str:
                """Search the web using Tavily search engine.
                
                Args:
                    query: The search query string
                    
                Returns:
                    Search results as a formatted string
                """
                try:
                    results = _tavily_tool.invoke({"query": query})
                    return str(results)
                except Exception as e:
                    logger.error(f"Tavily search error: {e}")
                    return f"Error performing search: {str(e)}"
            
            ToolRegistry.register('tavily_search', tavily_search)
            logger.info("Registered tavily_search tool from Langchain")
        else:
            logger.info("TAVILY_API_KEY not found in environment, skipping Tavily registration")
    except ImportError:
        logger.debug("Tavily search tool not available (langchain_community not installed)")


# Auto-register common tools when module is imported
register_common_tools()
