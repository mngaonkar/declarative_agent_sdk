import os
import difflib
import importlib.util
import inspect
from typing import Any, Dict, List, Callable, Optional
from pathlib import Path
from declarative_agent_sdk.core.agent_logging import get_logger
from declarative_agent_sdk.core.board_env import board_env
from dotenv import load_dotenv

load_dotenv()
logger = get_logger(__name__)

class ToolRegistry:
    """Registry to map tool names to actual tool objects."""
    
    _tools: Dict[str, Any] = {}
    _builtins_registered: bool = False

    # Tools every agent gets whether or not the YAML declares a `tools:` list;
    # `tools:` only ever gates non-core tools. The lean runtime has its own
    # sandboxed read_file / write_file and bridges the rest of these in
    # (LeanToolRegistry._register_shared_core); adk / deepagent take them all.
    CORE_TOOL_NAMES: tuple = (
        'read_file',
        'write_file',
        'web_request',
        'exec_command',
        'send_to_agent',
        'read_shared',
        'write_link',
    )

    # Core only when on a board: off a board it can only report that there is
    # no board, and every tool offered costs tokens on every request.
    BOARD_TOOL_NAMES: tuple = ('list_agents',)
    
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
    def register_built_in_tools(cls, force: bool = False):
        """Register built-in tools from builtin directory (once per class)."""
        # __dict__ (not getattr) so a subclass with its own _tools still loads.
        if cls.__dict__.get('_builtins_registered') and not force:
            return
        cls._builtins_registered = True
        builtin_dir = Path(__file__).parent / 'builtin'
        if not builtin_dir.exists():
            logger.warning(f"Built-in tools directory does not exist: {builtin_dir}")
            return
        
        registered_count = 0
        for file in builtin_dir.glob('*.py'):
            # A leading underscore marks a module as plumbing for the tools
            # beside it, not a tool itself. Without this, a shared helper's
            # public functions become tools — and a helper called `get` would
            # be offered to every model.
            if file.name.startswith('_'):
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
                    logger.info(f"Registered tool '{tool_name}' from {file_path}")
            
            return registered_count
            
        except Exception as e:
            logger.error(f"Error loading functions from {file_path}: {e}")
            return 0
    
    @classmethod
    def as_dict(cls) -> Dict[str, Any]:
        """Snapshot of the registry as a name -> callable mapping."""
        return dict(cls._tools)

    @classmethod
    def on_a_board(cls) -> bool:
        """True when agent-board seeded this agent's .env with a board URL."""
        return bool(board_env("URL"))

    @classmethod
    def core_tool_names(cls) -> List[str]:
        """Core names for this agent, board tools included when applicable."""
        names = list(cls.CORE_TOOL_NAMES)
        if cls.on_a_board():
            names += list(cls.BOARD_TOOL_NAMES)
        return names

    @classmethod
    def core_tools(cls) -> List[Any]:
        """
        Always-available tools.

        Registered regardless of the YAML ``tools:`` list, which only ever
        gates *non-core* tools.
        """
        cls.register_built_in_tools()
        return [cls._tools[name] for name in cls.core_tool_names() if name in cls._tools]

    @classmethod
    def dedupe(cls, tools: List[Any]) -> List[Any]:
        """Drop repeats (by tool name, else identity), keeping first occurrence."""
        seen = set()
        unique = []
        for tool in tools:
            key = getattr(tool, '__name__', None) or id(tool)
            if key in seen:
                continue
            seen.add(key)
            unique.append(tool)
        return unique

    @classmethod
    def unknown_tool_message(
        cls,
        name: str,
        agent_name: Optional[str] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Warning text for a tool name that is not in the registry."""
        cls.register_built_in_tools()
        available = sorted(set(cls._tools) | set(extra or {}))
        close = difflib.get_close_matches(name, available, n=1, cutoff=0.6)
        owner = f" for agent '{agent_name}'" if agent_name else ""
        hint = f" Did you mean '{close[0]}'?" if close else ""
        return (
            f"Unknown tool '{name}' in the tools list{owner}; it will be skipped "
            f"and the agent will not have that capability.{hint} "
            f"Available tools: {', '.join(available) or '(none)'}"
        )

    @classmethod
    def resolve(
        cls,
        items: Optional[List[Any]],
        agent_name: Optional[str] = None,
        extra: Optional[Dict[str, Any]] = None,
        keep_unresolved: bool = False,
    ) -> List[Any]:
        """
        Resolve a YAML ``tools:`` list to callables.

        Strings are looked up in the registry, then in ``extra`` (a runtime's
        skill-derived tools); anything else (a callable passed programmatically)
        is kept as-is. Every name that resolves in neither logs a warning naming
        the available tools, so a typo is never silent.
        """
        cls.register_built_in_tools()
        resolved: List[Any] = []
        for item in items or []:
            if not isinstance(item, str):
                resolved.append(item)
                continue
            tool = cls._tools.get(item)
            if tool is None and extra:
                tool = extra.get(item)
            if tool is not None:
                resolved.append(tool)
                continue
            logger.warning(cls.unknown_tool_message(item, agent_name, extra))
            if keep_unresolved:
                resolved.append(item)
        return resolved

    @classmethod
    def clear(cls) -> None:
        """Clear all registered tools. Useful for testing."""
        cls._tools.clear()
        cls._builtins_registered = False
        logger.debug("Cleared all tools from registry")
