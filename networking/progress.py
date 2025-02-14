import shutil
import sys
from datetime import datetime, timedelta

class ProgressBar:
    def __init__(self, total: int, prefix: str = '', length: int = 50):
        self.total = total
        self.prefix = prefix
        self.length = length
        self.current = 0
        self.start_time = datetime.now()
        self.term_width = shutil.get_terminal_size().columns
        
    def update(self, current: int) -> None:
        self.current = current
        self._draw()
        
    def _format_speed(self, bytes_per_sec: float) -> str:
        if bytes_per_sec >= 1024 * 1024:
            return f"{bytes_per_sec / (1024 * 1024):.2f} MB/s"
        elif bytes_per_sec >= 1024:
            return f"{bytes_per_sec / 1024:.2f} KB/s"
        return f"{bytes_per_sec:.0f} B/s"
            
    def _format_time(self, seconds: int) -> str:
        return str(timedelta(seconds=seconds))
            
    def _draw(self) -> None:
        percentage = (self.current / self.total) * 100
        filled_length = int(self.length * self.current // self.total)
        bar = '█' * filled_length + '░' * (self.length - filled_length)
        
        elapsed_time = (datetime.now() - self.start_time).total_seconds()
        if elapsed_time > 0:
            speed = self.current / elapsed_time
            speed_str = self._format_speed(speed)
            
            if speed > 0:
                remaining_bytes = self.total - self.current
                eta_seconds = int(remaining_bytes / speed)
                eta = self._format_time(eta_seconds)
            else:
                eta = "Unknown"
        else:
            speed_str = "0 B/s"
            eta = "Calculating..."
            
        progress_text = f'\r{self.prefix} |{bar}| {percentage:6.2f}% | {speed_str} | ETA: {eta}'
        
        if percentage < 33:
            color = '\033[91m'  # Red
        elif percentage < 66:
            color = '\033[93m'  # Yellow
        else:
            color = '\033[92m'  # Green
            
        print(f'{color}{progress_text}\033[0m', end='', flush=True)
        
        if self.current >= self.total:
            print()