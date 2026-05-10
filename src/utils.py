import os

def read_from_file(file_path: str) -> str:
    """Read file content from the given file path and return it as a string."""
    if os.path.exists(file_path):
        instruction_text = open(file_path).read()
    else:
        raise FileNotFoundError(f"Instructions file not found at {file_path}")
    return instruction_text

def save_to_file(content: str, file_path: str) -> str:
    """
    Save the Table of Contents to a markdown file.
    
    Args:
        content: The table of contents content to save
        file_path: The path to the file where the content will be saved
        
    Returns:
        A confirmation message with the file path
    """    
    with open(file_path, 'w', encoding='utf-8') as f:
        f.write(content)
    
    return file_path

def remove_think_content(content: str) -> str:
    """
    Remove all content from the start until </think> tag.

    Args:
        content: The input string that may contain "<think> </think>" content.

    Returns:
        A string with all content removed from start up to and including "</think>".
    """
    import re
    # Use regex to remove all content from start till </think>, including the tags
    pattern = re.compile(r'.*?</think>', re.DOTALL)
    cleaned_content = re.sub(pattern, '', content)
    return cleaned_content.strip()