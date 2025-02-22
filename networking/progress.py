import shutil
import sys
from datetime import datetime, timedelta

class ProgressBar:
    def __init__(self, total: int, prefix: str = ''):
        self.total = total
        self.prefix = prefix
        self.current = 0
        self.start_time = datetime.now()
        
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
        if self.total == 0:
            return
            
        percentage = (self.current / self.total) * 100
        term_width = shutil.get_terminal_size().columns
        max_bar_length = max(term_width - 60, 20)  # Adjust for text elements
        filled_length = int(max_bar_length * self.current // self.total)
        bar = '█' * filled_length + '░' * (max_bar_length - filled_length)
        
        elapsed_time = (datetime.now() - self.start_time).total_seconds()
        speed = self.current / elapsed_time if elapsed_time > 0 else 0
        speed_str = self._format_speed(speed)
        eta = (self.total - self.current) / speed if speed > 0 else 0
        eta_str = self._format_time(int(eta)) if speed > 0 else "Unknown"
        
        progress_text = f'\r{self.prefix} |{bar}| {percentage:6.2f}% | {speed_str} | ETA: {eta_str}'
        
        # Color coding
        if percentage < 33:
            color = '\033[91m'  # Red
        elif percentage < 66:
            color = '\033[93m'  # Yellow
        else:
            color = '\033[92m'  # Green
            
        print(f'{color}{progress_text}\033[0m', end='', flush=True)
        
        if self.current >= self.total:
            print()