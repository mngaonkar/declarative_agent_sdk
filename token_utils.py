"""
Token Management Utilities

Utilities for managing token counts and truncating inputs
to fit within model context windows.
"""

import tiktoken
from typing import Optional
from declarative_agent_sdk.agent_logging import get_logger

logger = get_logger(__name__)


def _count_tokens(text: str, model: str = "gpt-4") -> int:
    """
    Count the number of tokens in a text string.
    
    Args:
        text: The text to count tokens for
        model: Model name to use for tokenization (default: gpt-4)
        
    Returns:
        Number of tokens in the text
    """
    try:
        encoding = tiktoken.encoding_for_model(model)
    except KeyError:
        # Fallback to cl100k_base for unknown models
        encoding = tiktoken.get_encoding("cl100k_base")
        logger.warning(f"Model {model} not found, using cl100k_base encoding")
    
    return len(encoding.encode(text))


def _truncate_text(
    text: str,
    max_tokens: int,
    model: str = "gpt-4",
    truncate_from: str = "end"
) -> str:
    """
    Truncate text to fit within a maximum token count.
    
    Args:
        text: The text to truncate
        max_tokens: Maximum number of tokens to keep
        model: Model name to use for tokenization
        truncate_from: Where to truncate from ("start", "end", or "middle")
        
    Returns:
        Truncated text
    """
    try:
        encoding = tiktoken.encoding_for_model(model)
    except KeyError:
        encoding = tiktoken.get_encoding("cl100k_base")
    
    tokens = encoding.encode(text)
    current_tokens = len(tokens)
    
    if current_tokens <= max_tokens:
        return text
    
    logger.info(f"Truncating text from {current_tokens} to {max_tokens} tokens")
    
    if truncate_from == "start":
        # Keep last max_tokens
        truncated_tokens = tokens[-max_tokens:]
        
    elif truncate_from == "middle":
        # Keep equal amounts from start and end
        half = max_tokens // 2
        truncated_tokens = tokens[:half] + tokens[-(max_tokens - half):]
        
    else:  # truncate_from == "end" (default)
        # Keep first max_tokens
        truncated_tokens = tokens[:max_tokens]
    
    return encoding.decode(truncated_tokens)


def fit_to_context_window(
    input_text: str,
    max_context_tokens: Optional[int] = None,
    max_output_tokens: Optional[int] = None,
    model: str = "gpt-4",
    safety_margin: int = 100,
    truncate_strategy: str = "end"
) -> str:
    """
    Truncate input text to fit within model's context window.
    
    Args:
        input_text: The input text/prompt
        max_context_tokens: Total context window size (e.g., 20384 for Qwen3-4B).
                           If None, returns input_text unchanged.
        max_output_tokens: Tokens reserved for output (e.g., 3000).
                          If None, returns input_text unchanged.
        model: Model name for tokenization
        safety_margin: Extra tokens to reserve for safety (default: 100)
        truncate_strategy: How to truncate ("start", "end", or "middle")
        
    Returns:
        Truncated input text that fits within available token budget.
        Returns original input if max_context_tokens or max_output_tokens is None.
        
    Example:
        # Fit input for a model with 20384 context, reserving 3000 for output
        safe_input = fit_to_context_window(
            long_prompt,
            max_context_tokens=20384,
            max_output_tokens=3000,
            truncate_strategy="end"
        )
        
        # If parameters not provided, returns original text
        result = fit_to_context_window(text)  # Returns text unchanged
    """
    # If context window parameters not provided, return input unchanged
    if max_context_tokens is None or max_output_tokens is None:
        if max_context_tokens is None and max_output_tokens is None:
            logger.debug("No context window constraints provided, returning input unchanged")
        else:
            logger.warning(
                f"Incomplete context window configuration: "
                f"max_context_tokens={max_context_tokens}, max_output_tokens={max_output_tokens}. "
                f"Both must be provided for truncation. Returning input unchanged."
            )
        return input_text
    
    # Calculate available tokens for input
    max_input_tokens = max_context_tokens - max_output_tokens - safety_margin
    
    if max_input_tokens <= 0:
        raise ValueError(
            f"Invalid context window configuration: "
            f"max_context_tokens ({max_context_tokens}) - max_output_tokens ({max_output_tokens}) "
            f"- safety_margin ({safety_margin}) = {max_input_tokens} <= 0. "
            f"Reduce max_output_tokens or safety_margin, or increase max_context_tokens."
        )
    
    logger.info(f"Context window: {max_context_tokens} tokens")
    logger.info(f"Reserved for output: {max_output_tokens} tokens")
    logger.info(f"Safety margin: {safety_margin} tokens")
    logger.info(f"Available for input: {max_input_tokens} tokens")
    
    current_tokens = _count_tokens(input_text, model)
    
    if current_tokens <= max_input_tokens:
        logger.info(f"Input size ({current_tokens} tokens) fits within budget")
        return input_text
    
    logger.warning(
        f"Input size ({current_tokens} tokens) exceeds budget "
        f"({max_input_tokens} tokens), truncating..."
    )
    
    return _truncate_text(
        input_text,
        max_tokens=max_input_tokens,
        model=model,
        truncate_from=truncate_strategy
    )

