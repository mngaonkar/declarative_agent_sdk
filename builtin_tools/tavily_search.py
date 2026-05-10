from declarative_agent_sdk.agent_logging import get_logger
logger = get_logger(__name__)
import os

# Wrap in a callable function for ADK compatibility
def tavily_search(query: str) -> str:
    """Search the web using Tavily search engine.
    
    Args:
        query: The search query string
        
    Returns:
        Search results as a formatted string
    """
    try:
        from langchain_community.tools.tavily_search import TavilySearchResults
    except ImportError:
        logger.error("Langchain community module not found. Please install langchain_community to use Tavily search.")
        raise ImportError("Error: Langchain community module not found.")
    
    tavily_api_key = os.getenv("TAVILY_API_KEY")
    if not tavily_api_key:
        raise ValueError("TAVILY_API_KEY not found in environment variables. Please set it to use Tavily search.")
    
    try:
        _tavily_tool = TavilySearchResults()
        results = _tavily_tool.invoke({"query": query})
        return str(results)
    except Exception as e:
        logger.error(f"Tavily search error: {e}")
        raise RuntimeError(f"Error performing search: {str(e)}")