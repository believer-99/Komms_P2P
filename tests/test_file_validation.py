import os
import pytest
import tempfile
from pathlib import Path
from unittest import mock

# Import the module to test
from utils.file_validation import check_file_size, check_disk_space, safe_close_file

class TestFileValidation:
    
    def test_check_file_size(self):
        # Create a temporary file for testing
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            # Write 1MB of data
            tmp.write(b'0' * (1024 * 1024))
            tmp_path = tmp.name
        
        try:
            # Test with a file under the max size
            is_valid, message = check_file_size(tmp_path, max_size_mb=2)
            assert is_valid is True
            assert "valid" in message.lower()
            
            # Test with a file over the max size
            is_valid, message = check_file_size(tmp_path, max_size_mb=0.5)
            assert is_valid is False
            assert "exceeds" in message.lower()
            
            # Test with non-existent file
            is_valid, message = check_file_size("non_existent_file.txt")
            assert is_valid is False
            assert "error" in message.lower() or "not found" in message.lower()
            
            # Test with direct file size parameter (1MB under 2MB limit)
            is_valid, message = check_file_size(None, max_size_mb=2, file_size_bytes=1024*1024)
            assert is_valid is True
            assert "valid" in message.lower()
            
            # Test with direct file size parameter (3MB over 2MB limit)
            is_valid, message = check_file_size(None, max_size_mb=2, file_size_bytes=3*1024*1024)
            assert is_valid is False
            assert "exceeds" in message.lower()
            
            # Test with None file path and no size
            is_valid, message = check_file_size(None)
            assert is_valid is False
            assert "no file path or size provided" in message.lower()
        finally:
            # Clean up
            os.unlink(tmp_path)
    
    def test_check_disk_space(self):
        # Create a temp directory
        with tempfile.TemporaryDirectory() as tmp_dir:
            # Mock psutil.disk_usage to return controlled values
            with mock.patch('psutil.disk_usage') as mock_usage:
                # Test with sufficient space
                mock_usage.return_value = mock.Mock(free=5 * 1024 * 1024 * 1024)  # 5GB
                has_space, message = check_disk_space(tmp_dir, required_mb=1000)
                assert has_space is True
                assert "sufficient" in message.lower()
                
                # Test with insufficient space
                mock_usage.return_value = mock.Mock(free=500 * 1024 * 1024)  # 500MB
                has_space, message = check_disk_space(tmp_dir, required_mb=1000)
                assert has_space is False
                assert "insufficient" in message.lower()
    
    def test_safe_close_file(self):
        # Test with a real file
        with tempfile.NamedTemporaryFile() as tmp:
            safe_close_file(tmp)
            # File should be closed
            with pytest.raises(ValueError):
                tmp.write(b'test')
        
        # Test with None
        safe_close_file(None)  # Should not raise an exception
        
        # Test with a file that raises an exception on close
        mock_file = mock.Mock()
        mock_file.close.side_effect = Exception("Test exception")
        
        # Mock the logger to verify it's called
        with mock.patch('utils.file_validation.logger') as mock_logger:
            safe_close_file(mock_file)  # Should not raise an exception
            mock_file.close.assert_called_once()
            
            # Verify that a warning was logged
            mock_logger.warning.assert_called_once()
