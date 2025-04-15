import os
from pathlib import Path
from pathvalidate import sanitize_filepath

def sanitize_path(file_path: str, base_dir: str) -> str:
    """
    Sanitize and validate file paths to prevent path traversal attacks.
    
    Args:
        file_path: The original file path to sanitize
        base_dir: The base directory that should contain the file
        
    Returns:
        Sanitized absolute path
    
    Raises:
        ValueError: If the path would escape the base directory
    """
    sanitized = sanitize_filepath(file_path, platform="auto")
    
    abs_path = os.path.abspath(os.path.join(base_dir, sanitized))
    
    if not abs_path.startswith(os.path.abspath(base_dir)):
        raise ValueError(f"Path traversal attempt detected: {file_path}")
    
    return abs_path

def create_safe_directory(directory_path: str) -> str:
    """
    Create a directory safely with appropriate permissions.
    
    Args:
        directory_path: Path to create
        
    Returns:
        The created directory path
    """
    dir_path = Path(directory_path)
    
    if not dir_path.exists():
        dir_path.mkdir(parents=True, exist_ok=True)
        os.chmod(dir_path, 0o700)
    
    return str(dir_path)
