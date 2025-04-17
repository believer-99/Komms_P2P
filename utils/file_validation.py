import os
from pathlib import Path
from typing import Tuple, Optional
import logging
import sys
import shutil
import asyncio

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
    if file_size_bytes is None and file_path:
        try:
            file_size_bytes = os.path.getsize(file_path)
        except OSError as e:
            return False, f"Error reading file: {e}"
    
    if file_size_bytes is None:
        return False, "Cannot determine file size"
        
    file_size_mb = file_size_bytes / (1024 * 1024)
    
    if file_size_mb > max_size_mb:
        return False, f"File size ({file_size_mb:.2f} MB) exceeds maximum allowed size ({max_size_mb} MB)"
    
    if file_size_bytes == 0:
        return False, "File is empty"
        
    return True, f"File size ({file_size_mb:.2f} MB) is acceptable"

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

def check_disk_space(target_dir, required_mb, buffer_factor=1.1):
    """
    Checks if there's enough disk space for the file.
    Returns (has_space, message)
    
    Args:
        target_dir: Directory where file will be saved
        required_mb: Required space in MB
        buffer_factor: Additional space buffer (e.g., 1.1 = 10% extra)
    """
    try:
        # Get free space in bytes
        free_bytes = shutil.disk_usage(target_dir).free
        free_mb = free_bytes / (1024 * 1024)
        
        # Calculate required space with buffer
        required_with_buffer = required_mb * buffer_factor
        
        if free_mb < required_with_buffer:
            return False, f"Insufficient disk space. Need {required_with_buffer:.2f} MB, but only {free_mb:.2f} MB available."
        
        return True, f"Sufficient disk space available ({free_mb:.2f} MB)"
    except Exception as e:
        logger.error(f"Error checking disk space: {e}")
        # Default to allowing the transfer if we can't check
        return True, f"Could not verify disk space: {e}"

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

def safe_close_file(file_handle):
    """Safely close a file handle, handling both sync and async file objects."""
    if file_handle is None:
        return
        
    try:
        # For aiofiles file handle
        if hasattr(file_handle, 'close') and asyncio.iscoroutinefunction(file_handle.close):
            # We can't await here, so we need to close synchronously
            # Use the internal file object if available
            if hasattr(file_handle, '_file') and hasattr(file_handle._file, 'close'):
                file_handle._file.close()
            else:
                logger.warning("Could not access _file to close async file handle synchronously")
        # For regular file handle
        elif hasattr(file_handle, 'close'):
            file_handle.close()
    except Exception as e:
        logger.error(f"Error closing file handle: {e}")
