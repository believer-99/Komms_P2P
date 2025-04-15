import os
from pathlib import Path
from typing import Tuple, Optional
import logging
import sys

logger = logging.getLogger(__name__)

# Try to import psutil but handle gracefully if not installed
try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False
    logger.warning("psutil module not installed. Disk space checking will be limited.")
    print("\n=====================================================")
    print("WARNING: psutil module is not installed.")
    print("Some features like disk space checking will be limited.")
    print("Please install it: pip install psutil")
    print("=====================================================\n")

def check_file_size(file_path: str, max_size_mb: int = 2000, file_size_bytes: int = None) -> Tuple[bool, str]:
    """
    Check if a file's size is within an acceptable limit.
    
    Args:
        file_path: Path to the file to check (can be None if file_size_bytes is provided)
        max_size_mb: Maximum allowed size in megabytes
        file_size_bytes: Optional pre-determined file size in bytes
        
    Returns:
        (is_valid, message): Tuple containing validity status and descriptive message
    """
    max_size_bytes = max_size_mb * 1024 * 1024
    
    # If file size is provided directly, use it
    if file_size_bytes is not None:
        if file_size_bytes > max_size_bytes:
            size_mb = file_size_bytes / (1024 * 1024)
            return False, f"File size ({size_mb:.2f} MB) exceeds maximum allowed size of {max_size_mb} MB"
        return True, f"File size ({file_size_bytes / (1024 * 1024):.2f} MB) is valid"
    
    # Otherwise check the file on disk
    if file_path is None:
        return False, "No file path or size provided for validation"
        
    try:
        size = os.path.getsize(file_path)
        if size > max_size_bytes:
            size_mb = size / (1024 * 1024)
            return False, f"File size ({size_mb:.2f} MB) exceeds maximum allowed size of {max_size_mb} MB"
        return True, f"File size ({size / (1024 * 1024):.2f} MB) is valid"
    except FileNotFoundError:
        return False, f"File not found: {file_path}"
    except Exception as e:
        return False, f"Error checking file size: {str(e)}"

def check_disk_space(destination_dir: str, required_mb: float) -> Tuple[bool, str]:
    """
    Check if enough disk space is available at the destination.
    
    Args:
        destination_dir: Directory where file will be saved
        required_mb: Required space in MB
        
    Returns:
        Tuple of (has_space, message)
    """
    try:
        dest_path = Path(destination_dir)
        if not dest_path.exists():
            dest_path.mkdir(parents=True, exist_ok=True)
            
        if not PSUTIL_AVAILABLE:
            # Fallback method if psutil is not available
            logger.warning(f"psutil not available. Cannot check disk space for {destination_dir}")
            return True, "Disk space check skipped (psutil not installed)"
            
        disk = psutil.disk_usage(dest_path)
        available_mb = disk.free / (1024 * 1024)  # Convert to MB
        
        if available_mb < required_mb:
            return False, f"Insufficient disk space: {available_mb:.2f}MB available, {required_mb:.2f}MB required"
        return True, f"Sufficient disk space: {available_mb:.2f}MB available"
    except Exception as e:
        return False, f"Error checking disk space: {str(e)}"

def safe_close_file(file_handle: Optional[object]) -> None:
    """
    Safely close a file handle, catching and logging any exceptions.
    
    Args:
        file_handle: The file handle to close, or None if no handle is provided
    """
    if file_handle is None:
        logger.debug("Attempted to close None file handle - ignoring")
        return
        
    try:
        file_handle.close()
        logger.debug(f"Successfully closed file handle: {file_handle}")
    except Exception as e:
        logger.warning(f"Error closing file handle: {e}", exc_info=True)
