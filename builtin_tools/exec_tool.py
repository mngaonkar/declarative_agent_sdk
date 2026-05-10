"""
Execute Tool - Execute shell commands and processes

This tool allows agents to execute system commands and processes.
Use with caution as it provides shell access.
"""

import subprocess
import shlex
from typing import Dict, Optional, List, Union
from declarative_agent_sdk.agent_logging import get_logger

logger = get_logger(__name__)


def exec_command(
    command: str,
    shell: bool = False,
    timeout: Optional[int] = 30,
    cwd: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
    capture_output: bool = True,
    input_data: Optional[str] = None,
    encoding: str = 'utf-8'
) -> Dict[str, Union[str, int, bool]]:
    """
    Execute a shell command or process.
    
    Args:
        command: Command to execute. If shell=False, can be a string or list of arguments.
                If shell=True, should be a complete shell command string.
        shell: If True, execute command through the shell (enables pipes, redirection, etc.)
               If False (default), execute directly (safer, no shell injection risk)
        timeout: Maximum time in seconds to wait for command to complete (default: 30)
                Set to None for no timeout
        cwd: Working directory for the command (default: current directory)
        env: Environment variables dict to set for the command (default: inherit parent env)
        capture_output: If True, capture stdout and stderr (default: True)
        input_data: Optional string to send to command's stdin
        encoding: Character encoding for input/output (default: utf-8)
        
    Returns:
        Dictionary containing:
            - success: bool - Whether command completed successfully (exit code 0)
            - exit_code: int - Process exit code
            - stdout: str - Standard output (if capture_output=True)
            - stderr: str - Standard error (if capture_output=True)
            - timed_out: bool - Whether command exceeded timeout
            - error: str - Error message if execution failed
            
    Example:
        # Simple command (safe, no shell)
        result = exec_command("ls -la /tmp")
        print(result['stdout'])
        
        # With shell features (pipes, redirection)
        result = exec_command("ps aux | grep python", shell=True)
        
        # With timeout and working directory
        result = exec_command(
            "npm install",
            timeout=300,
            cwd="/path/to/project"
        )
        
        # With input data
        result = exec_command(
            "python script.py",
            input_data="hello\\nworld\\n"
        )
        
    Security Note:
        When shell=False (default), command arguments are safely parsed and shell
        injection is prevented. When shell=True, be very careful with untrusted input
        as it can lead to command injection vulnerabilities.
    """
    result = {
        'success': False,
        'exit_code': None,
        'stdout': '',
        'stderr': '',
        'timed_out': False,
        'error': None
    }
    
    try:
        # Parse command if not using shell
        if not shell:
            if isinstance(command, str):
                # Safely parse command string into arguments
                cmd_args = shlex.split(command)
            elif isinstance(command, list):
                cmd_args = command
            else:
                raise ValueError(f"command must be str or list, got {type(command)}")
        else:
            # Shell mode: command is a string
            cmd_args = command
        
        logger.debug(f"Executing command: {command} (shell={shell}, timeout={timeout}, cwd={cwd})")
        
        # Prepare subprocess arguments
        subprocess_kwargs = {
            'shell': shell,
            'timeout': timeout,
            'cwd': cwd,
            'env': env,
        }
        
        # Add capture settings
        if capture_output:
            subprocess_kwargs['stdout'] = subprocess.PIPE
            subprocess_kwargs['stderr'] = subprocess.PIPE
            subprocess_kwargs['text'] = True
            subprocess_kwargs['encoding'] = encoding
        
        # Add input if provided
        if input_data is not None:
            subprocess_kwargs['input'] = input_data
        
        # Execute the command
        proc = subprocess.run(cmd_args, **subprocess_kwargs)
        
        # Populate result
        result['exit_code'] = proc.returncode
        result['success'] = (proc.returncode == 0)
        
        if capture_output:
            result['stdout'] = proc.stdout or ''
            result['stderr'] = proc.stderr or ''
        
        if result['success']:
            logger.info(f"Command executed successfully: {command}")
        else:
            logger.warning(f"Command exited with code {proc.returncode}: {command}")
            
    except subprocess.TimeoutExpired as e:
        result['timed_out'] = True
        result['error'] = f"Command timed out after {timeout} seconds"
        
        # Capture any partial output
        if capture_output:
            result['stdout'] = e.stdout.decode(encoding) if e.stdout else ''
            result['stderr'] = e.stderr.decode(encoding) if e.stderr else ''
        
        logger.error(f"Command timed out: {command}")
        
    except FileNotFoundError as e:
        result['error'] = f"Command not found: {e.filename}"
        logger.error(f"Command not found: {command} - {e}")
        
    except PermissionError as e:
        result['error'] = f"Permission denied: {e}"
        logger.error(f"Permission denied executing: {command} - {e}")
        
    except Exception as e:
        result['error'] = f"Execution error: {str(e)}"
        logger.error(f"Error executing command: {command} - {e}", exc_info=True)
    
    return result


def exec_async(
    command: str,
    shell: bool = False,
    cwd: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
    stdout_file: Optional[str] = None,
    stderr_file: Optional[str] = None
) -> Dict[str, Union[int, str]]:
    """
    Execute a command asynchronously (non-blocking).
    
    This starts a process and returns immediately without waiting for completion.
    Useful for long-running processes like servers or daemons.
    
    Args:
        command: Command to execute
        shell: If True, execute through shell
        cwd: Working directory
        env: Environment variables
        stdout_file: Optional file path to redirect stdout
        stderr_file: Optional file path to redirect stderr
        
    Returns:
        Dictionary containing:
            - pid: int - Process ID of started process
            - success: bool - Whether process started successfully
            - error: str - Error message if failed to start
            
    Example:
        # Start a web server
        result = exec_async("python -m http.server 8000")
        print(f"Server started with PID: {result['pid']}")
        
        # Start with output redirection
        result = exec_async(
            "npm run dev",
            stdout_file="server.log",
            stderr_file="server.error.log"
        )
    """
    result = {
        'success': False,
        'pid': None,
        'error': None
    }
    
    try:
        # Parse command if not using shell
        if not shell:
            if isinstance(command, str):
                cmd_args = shlex.split(command)
            else:
                cmd_args = command
        else:
            cmd_args = command
        
        # Prepare subprocess arguments
        subprocess_kwargs = {
            'shell': shell,
            'cwd': cwd,
            'env': env,
        }
        
        # Set up file redirects if specified
        if stdout_file:
            subprocess_kwargs['stdout'] = open(stdout_file, 'w')
        else:
            subprocess_kwargs['stdout'] = subprocess.DEVNULL
            
        if stderr_file:
            subprocess_kwargs['stderr'] = open(stderr_file, 'w')
        else:
            subprocess_kwargs['stderr'] = subprocess.DEVNULL
        
        # Start process asynchronously
        proc = subprocess.Popen(cmd_args, **subprocess_kwargs)
        
        result['success'] = True
        result['pid'] = proc.pid
        
        logger.info(f"Started async process (PID {proc.pid}): {command}")
        
    except Exception as e:
        result['error'] = f"Failed to start process: {str(e)}"
        logger.error(f"Error starting async process: {command} - {e}", exc_info=True)
    
    return result


def check_exec_async(job_id: int) -> Dict[str, Union[bool, int, str]]:
    """
    Check the status of an asynchronously started process by exec_async.
    
    Args:
        job_id: Process ID (PID) returned from exec_async
        
    Returns:
        Dictionary containing:
            - running: bool - Whether process is still running
            - exit_code: int or None - Exit code if process finished (only available for child processes)
            - success: bool - Whether process completed successfully (exit code 0)
            - error: str - Error message if any
            
    Example:
        # Start a process
        result = exec_async("long_running_command")
        pid = result['pid']
        
        # Check status later
        status = check_exec_async(pid)
        if status['running']:
            print("Process still running")
        elif status['success']:
            print("Process completed successfully")
        else:
            print(f"Process finished (exit code: {status.get('exit_code', 'unknown')})")
            
    Note:
        Exit code is only available for child processes that haven't been reaped yet.
        For other processes, you can only determine if they're running or not.
    """
    import os
    import errno
    
    result = {
        'running': False,
        'exit_code': None,
        'success': False,
        'error': None
    }
    
    try:
        # First, try to check if it's a child process and get its exit status
        pid, status = os.waitpid(job_id, os.WNOHANG)
        
        if pid == 0:
            # Process is still running
            result['running'] = True
            logger.debug(f"Process {job_id} is still running")
        else:
            # Process has finished
            result['running'] = False
            
            # Extract exit code from status
            if os.WIFEXITED(status):
                exit_code = os.WEXITSTATUS(status)
                result['exit_code'] = exit_code
                result['success'] = (exit_code == 0)
                logger.info(f"Process {job_id} exited with code {exit_code}")
            elif os.WIFSIGNALED(status):
                signal_num = os.WTERMSIG(status)
                result['exit_code'] = -signal_num
                result['error'] = f"Process terminated by signal {signal_num}"
                logger.warning(f"Process {job_id} terminated by signal {signal_num}")
                
    except ChildProcessError:
        # Not a child process or already reaped, try to check if process exists
        try:
            # Send signal 0 to check if process exists (doesn't actually send a signal)
            os.kill(job_id, 0)
            # If we get here, process exists and is running
            result['running'] = True
            logger.debug(f"Process {job_id} is running (not a child process)")
        except OSError as e:
            if e.errno == errno.ESRCH:
                # Process doesn't exist (finished)
                result['running'] = False
                logger.info(f"Process {job_id} has finished (exit code unknown)")
            elif e.errno == errno.EPERM:
                # Process exists but we don't have permission to check
                result['running'] = True
                result['error'] = "Process exists but permission denied to check status"
                logger.warning(f"Permission denied checking process {job_id}")
            else:
                raise
                
    except Exception as e:
        result['error'] = f"Error checking process status: {str(e)}"
        logger.error(f"Error checking process {job_id}: {e}", exc_info=True)
    
    return result