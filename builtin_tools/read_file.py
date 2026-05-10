"""
Read File Tool - Read content from files on the filesystem

This tool allows agents to read files and directories.
"""

import os
from pathlib import Path
from typing import Optional, List
from declarative_agent_sdk.agent_logging import get_logger

logger = get_logger(__name__)


def read_file(file_path: str, encoding: str = 'utf-8') -> str:
    """
    Read content from a file and return as plain text.
    
    Args:
        file_path: Path to the file to read
        encoding: Character encoding (default: 'utf-8')
        
    Returns:
        str: File content as plain text
        
    Raises:
        FileNotFoundError: If file doesn't exist
        PermissionError: If file cannot be read
        UnicodeDecodeError: If file encoding is incorrect
        
    Example:
        content = read_file("/tmp/test.txt")
        print(content)
    """
    file_path = os.path.abspath(file_path)
    
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"File not found: {file_path}")
    
    if not os.path.isfile(file_path):
        raise IsADirectoryError(f"Path is a directory, not a file: {file_path}")
    
    logger.debug(f"Reading file: {file_path}")
    
    with open(file_path, 'r', encoding=encoding) as f:
        content = f.read()
    
    logger.info(f"Successfully read {len(content)} characters from {file_path}")
    return content