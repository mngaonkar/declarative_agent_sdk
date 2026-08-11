"""
Logging Configuration for SDK

Provides centralized logging configuration for all SDK modules.
Prevents duplicate logging setup and ensures consistent formatting across the SDK.
"""

import logging
import os
from typing import Optional, Union

# Default logging configuration
SDK_LOG_LEVEL = os.getenv('SDK_LOG_LEVEL', 'INFO').upper()
SDK_LOG_FORMAT = os.getenv('SDK_LOG_FORMAT', '%(asctime)s - %(name)s - %(levelname)s - %(message)s')
SDK_DATE_FORMAT = os.getenv('SDK_DATE_FORMAT', '%Y-%m-%d %H:%M:%S')
SDK_LOG_FILE = os.getenv('SDK_LOG_FILE')  # Optional log file path

# Track if logging has been configured to avoid duplicate setup
_logging_configured = False
_log_level_mapping = {
        'DEBUG': logging.DEBUG,
        'INFO': logging.INFO,
        'WARNING': logging.WARNING,
        'ERROR': logging.ERROR,
        'CRITICAL': logging.CRITICAL
    }

def setup_logging(
    level: str = SDK_LOG_LEVEL,
    format_string: Optional[str] = SDK_LOG_FORMAT,
    date_format: Optional[str] = SDK_DATE_FORMAT,
    log_file: Optional[str] = SDK_LOG_FILE,
    force: bool = False
) -> None:
    """
    Configure logging for the SDK.
    
    This function should be called once at application startup to configure
    logging for all SDK modules. Subsequent calls are ignored unless force=True.
    
    Args:
        level: Logging level (default: INFO). Can also be set via SDK_LOG_LEVEL env var.
               Accepts string levels (e.g., "DEBUG", "INFO").
        format_string: Log message format (default: timestamp - name - level - message)
        date_format: Date format for timestamps (default: YYYY-MM-DD HH:MM:SS)
        log_file: Optional file path to also write logs to a file
        force: If True, reconfigure logging even if already configured
        
    Environment Variables:
        SDK_LOG_LEVEL: Set log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        SDK_LOG_FILE: Set log file path
        
    Example:
        # Basic setup with defaults
        setup_logging()
        
        # Custom log level
        setup_logging(level=logging.DEBUG)
        
        # With file output
        setup_logging(log_file='sdk.log')
        
        # From environment
        os.environ['SDK_LOG_LEVEL'] = 'DEBUG'
        setup_logging()
    """
    global _logging_configured
    
    # Skip if already configured (unless forced)
    if _logging_configured and not force:
        return
        
    # Use defaults if not specified
    format_string = format_string or SDK_LOG_FORMAT
    date_format = date_format or SDK_DATE_FORMAT
        
    # Configure handlers
    handlers = []
    
    mapped_level = _log_level_mapping.get(level, None)
    if mapped_level is None:
        raise ValueError(f"Invalid log level: {level}, valid options are {list(_log_level_mapping.keys())}")

    # Console handler (always present)
    console_handler = logging.StreamHandler()
    console_handler.setLevel(mapped_level)
    console_handler.setFormatter(logging.Formatter(format_string, date_format))
    handlers.append(console_handler)
    
    # File handler (optional)
    if log_file:
        try:
            file_handler = logging.FileHandler(log_file)
            file_handler.setLevel(mapped_level)
            file_handler.setFormatter(logging.Formatter(format_string, date_format))
            handlers.append(file_handler)
        except Exception as e:
            raise ValueError(f"Failed to set up file handler for log file '{log_file}': {e}")
    
    # Configure root logger for SDK
    logging.basicConfig(
        level=mapped_level,
        format=format_string,
        datefmt=date_format,
        handlers=handlers,
        force=force
    )
    
    _logging_configured = True
    
    # Log the configuration
    logger = logging.getLogger('sdk.agent_logging')
    level_name = logging.getLevelName(mapped_level) if isinstance(mapped_level, int) else str(mapped_level)
    logger.debug(f"Logging configured: level={level_name}, file={log_file}")


def get_logger(name: str) -> logging.Logger:
    """
    Get a logger with the specified name.
    
    This is a convenience wrapper around logging.getLogger() that ensures
    logging is configured before returning the logger.
    
    Args:
        name: Logger name (typically __name__ from the calling module)
        
    Returns:
        Configured logger instance
        
    Example:
        logger = get_logger(__name__)
        logger.info("This is a log message")
    """
    # Ensure logging is configured
    if not _logging_configured:
        setup_logging()
    
    return logging.getLogger(name)


def set_log_level(level: str) -> None:
    """
    Change the log level for all SDK loggers.
    
    Args:
        level: New log level (e.g., "DEBUG", "INFO")
        
    Example:
        set_log_level("DEBUG")
    """
    mapped_level = _log_level_mapping.get(level, None)
    if mapped_level is None:
        raise ValueError(f"Invalid log level: {level}, valid options are {list(_log_level_mapping.keys())}")
    
    logging.getLogger().setLevel(mapped_level)
    for handler in logging.getLogger().handlers:
        handler.setLevel(mapped_level)


def reset_logging() -> None:
    """
    Reset logging configuration.
    
    This is primarily useful for testing to reset the logging state.
    """
    global _logging_configured
    _logging_configured = False

    # Close all handlers before clearing
    for handler in logging.getLogger().handlers[:]:
        handler.close()
    
    logging.getLogger().handlers.clear()


# Auto-configure with defaults when module is imported
# This ensures logging works even if setup_logging() is never called
if not _logging_configured:
    setup_logging()
