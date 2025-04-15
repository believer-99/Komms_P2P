from PyQt6.QtCore import Qt, pyqtSignal, QObject
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QPushButton, QProgressBar, 
    QLabel, QHBoxLayout, QFrame
)
from typing import Dict, Optional, Callable

class TransferControl(QObject):
    """
    Control class for managing file transfer state and signaling.
    """
    transfer_paused = pyqtSignal(str)
    transfer_resumed = pyqtSignal(str)
    transfer_cancelled = pyqtSignal(str)
    
    def __init__(self, transfer_id: str):
        super().__init__()
        self.transfer_id = transfer_id
        self.is_paused = False
        self._callbacks: Dict[str, Callable] = {}
    
    def toggle_pause(self) -> bool:
        """Toggle pause state and emit the appropriate signal"""
        self.is_paused = not self.is_paused
        if self.is_paused:
            self.transfer_paused.emit(self.transfer_id)
        else:
            self.transfer_resumed.emit(self.transfer_id)
        return self.is_paused
    
    def cancel(self) -> None:
        """Cancel the transfer"""
        self.transfer_cancelled.emit(self.transfer_id)


class TransferWidget(QWidget):
    """
    Widget for displaying file transfer progress with pause/resume functionality.
    """
    def __init__(self, transfer_id: str, filename: str, parent=None):
        super().__init__(parent)
        self.transfer_id = transfer_id
        self.filename = filename
        self.control = TransferControl(transfer_id)
        
        self._init_ui()
        self._connect_signals()
    
    def _init_ui(self) -> None:
        """Initialize the UI components"""
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(8, 8, 8, 8)
        
        # File info
        file_label = QLabel(self.filename)
        file_label.setWordWrap(True)
        main_layout.addWidget(file_label)
        
        # Progress bar
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        main_layout.addWidget(self.progress_bar)
        
        # Status and controls
        controls_layout = QHBoxLayout()
        
        self.status_label = QLabel("Waiting...")
        controls_layout.addWidget(self.status_label, 1)
        
        self.pause_button = QPushButton("Pause")
        self.cancel_button = QPushButton("Cancel")
        
        controls_layout.addWidget(self.pause_button)
        controls_layout.addWidget(self.cancel_button)
        
        main_layout.addLayout(controls_layout)
        
        # Add separator line
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFrameShadow(QFrame.Shadow.Sunken)
        main_layout.addWidget(line)
    
    def _connect_signals(self) -> None:
        """Connect button signals to control actions"""
        self.pause_button.clicked.connect(self._toggle_pause)
        self.cancel_button.clicked.connect(self.control.cancel)
    
    def _toggle_pause(self) -> None:
        """Handle pause button click"""
        is_paused = self.control.toggle_pause()
        if is_paused:
            self.pause_button.setText("Resume")
            self.status_label.setText("Paused")
        else:
            self.pause_button.setText("Pause")
            self.status_label.setText("Transferring...")
    
    def update_progress(self, percentage: int, status: str = None) -> None:
        """Update progress bar and status label"""
        self.progress_bar.setValue(percentage)
        if status:
            self.status_label.setText(status)
