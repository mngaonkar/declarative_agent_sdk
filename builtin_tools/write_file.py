"""
Write File Tool - Write content to files on the filesystem

This tool allows agents to create and modify files.
Use with caution as it provides filesystem write access.
"""

import os
from declarative_agent_sdk.agent_logging import get_logger

logger = get_logger(__name__)


def write_file(
    file_path: str,
    content: str,
    mode: str = 'w',
    encoding: str = 'utf-8',
    create_dirs: bool = True
) -> str:
    """
    Write content to a file and return success message.
    
    Args:
        file_path: Path to the file to write
        content: Content to write to the file
        mode: File open mode ('w' = write, 'a' = append, 'x' = exclusive create)
        encoding: Character encoding (default: 'utf-8')
        create_dirs: If True, create parent directories if they don't exist
        
    Returns:
        str: Success message with file path and characters written
        
    Raises:
        ValueError: If mode is invalid
        FileExistsError: If mode='x' and file already exists
        FileNotFoundError: If create_dirs=False and directory doesn't exist
        PermissionError: If insufficient permissions
        
    Example:
        result = write_file("/tmp/test.txt", "Hello, World!")
        print(result)  # "Successfully wrote 13 characters to /tmp/test.txt"
    """
    file_path = os.path.abspath(file_path)
    
    # Validate mode
    if mode not in ['w', 'a', 'x']:
        raise ValueError(f"Invalid mode '{mode}'. Must be 'w', 'a', or 'x'")
    
    # Create parent directories if needed
    parent_dir = os.path.dirname(file_path)
    if parent_dir and not os.path.exists(parent_dir):
        if create_dirs:
            os.makedirs(parent_dir, exist_ok=True)
            logger.info(f"Created directory: {parent_dir}")
        else:
            raise FileNotFoundError(f"Directory does not exist: {parent_dir}")
    
    # Write the file
    logger.debug(f"Writing to file: {file_path} (mode={mode})")
    
    with open(file_path, mode, encoding=encoding) as f:
        chars_written = f.write(content)
    
    success_msg = f"Successfully wrote {chars_written} characters to {file_path}"
    logger.info(success_msg)
    return success_msg
