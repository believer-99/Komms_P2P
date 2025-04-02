import asyncio
import json
import logging
import os
import sys
import uuid
from enum import Enum
import hashlib
import netifaces
import traceback
import time

from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric import rsa, padding

from PyQt6.QtCore import (QCoreApplication, QObject, QRunnable, QSettings,
                          QThreadPool, pyqtSignal, pyqtSlot, Qt, QThread)
from PyQt6.QtWidgets import (QApplication, QCheckBox, QFileDialog, QLabel,
                             QLineEdit, QListWidget, QListWidgetItem,
                             QMainWindow, QMessageBox, QPushButton,
                             QProgressBar, QVBoxLayout, QWidget, QTabWidget,
                             QTextEdit, QHBoxLayout, QStatusBar, QMenuBar, QMenu, QStyle, QListView)
from PyQt6.QtGui import QIcon, QFont, QCloseEvent

from networking.discovery import PeerDiscovery  # Import your networking modules
from networking.messaging import (user_input, display_messages, receive_peer_messages, handle_incoming_connection, connections, maintain_peer_list, initialize_user_config)
from networking.file_transfer import update_transfer_progress, FileTransfer, TransferState
from networking.shared_state import peer_usernames, peer_public_keys, shutdown_event, user_data, active_transfers, message_queue

logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s - %(levelname)s - %(message)s",
                        stream=sys.stdout)

class WorkerSignals(QObject):
    '''
    Defines the signals available from a running worker thread.

    Supported signals are:

    finished
        No data

    error
        `tuple` (exctype, value, traceback.format_exc() )

    result
        `object` data returned from processing, anything

    progress
        `int` indicating % progress

    message
        `str` message to display
    '''
    finished = pyqtSignal()
    error = pyqtSignal(tuple)
    result = pyqtSignal(object)
    progress = pyqtSignal(int)
    message = pyqtSignal(str)

class Worker(QRunnable):
    '''
    Worker thread

    Inherits from QRunnable to handler worker thread setup, signals and wrap-up.

    :param callback: The function callback to run on this worker thread. Supplied args and
                     kwargs will be passed through to the runner.
    :type callback: function
    :param args: Arguments to pass to the callback function
    :param kwargs: Keywords to pass to the callback function

    '''

    def __init__(self, fn, *args, **kwargs):
        super(Worker, self).__init__()

        # Store constructor arguments (re-used for processing)
        self.fn = fn
        self.args = args
        self.kwargs = kwargs
        self.signals = WorkerSignals()

        # Add the callback to our kwargs
        self.kwargs['progress_callback'] = self.signals.progress
        self.kwargs['message_callback'] = self.signals.message

    @pyqtSlot()
    def run(self):
        '''
        Initialise the runner function with passed args, kwargs.
        '''

        # Retrieve args/kwargs here; and define anything needed by the running
        # code.
        try:
            result = self.fn(*self.args, **self.kwargs)
        except:
            # Catch all exceptions
            exctype, value = sys.exc_info()[:2]
            self.signals.error.emit((exctype, value, traceback.format_exc()))
        else:
            # Return the result
            self.signals.result.emit(result)  # Return the result of the task
        finally:
            # Done
            self.signals.finished.emit()  # Indicate that the task is complete
class Backend(QObject):
    '''
    Backend that can perform any backend operation
    '''
    message_received_signal = pyqtSignal(str)
    peer_list_updated_signal = pyqtSignal()
    transfers_updated_signal = pyqtSignal()
    finished = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.discovery = PeerDiscovery()

    def start(self):
        self.discovery.start()
        self.thread = NetworkingThread()
        self.thread.start()

    def stop(self):
        self.discovery.stop()
        shutdown_event.set()
        self.thread.quit()
        self.thread.wait()
        self.finished.emit()

    def connect_to_peer(self, peer_ip, requesting_username, target_username):
        return asyncio.run(connect_to_peer(peer_ip, requesting_username, target_username))

    def disconnect_from_peer(self, peer_username):
        return asyncio.run(disconnect_from_peer(peer_username))

    def send_message_to_peers(self, message, target_username=None):
        return asyncio.run(send_message_to_peers(message, target_username))

    def choose_file(self):
        file_dialog = QFileDialog()
        self.selected_file, _ = file_dialog.getOpenFileName(self, "Choose File")
        if self.selected_file:
            print("File Selected")
            return self.selected_file
        else:
            print("No File Selected")
            return None

    def send_selected_file(self, file_path, peers):
        return asyncio.run(send_file(file_path, peers))

class NetworkingThread(QThread):

    def run(self):

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self.main_loop())
        finally:
            loop.close()
            print("Closed Loop")

    async def main_loop(self):
        input_task = asyncio.create_task(user_input(PeerDiscovery()))
        display_task = asyncio.create_task(display_messages())
        transfer_task = asyncio.create_task(update_transfer_progress())
        maintain_task = asyncio.create_task(maintain_peer_list(PeerDiscovery()))

        await asyncio.gather(input_task, display_task, transfer_task, maintain_task)

class LoginWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = QSettings("P2PChat", "Login")  # Store settings

        self.setWindowTitle("Login")
        self.setGeometry(200, 200, 350, 250)  # Increased size

        central_widget = QWidget()
        self.setCentralWidget(central_widget)

        layout = QVBoxLayout(central_widget)
        layout.setSpacing(15)  # Add spacing between elements
        layout.setContentsMargins(20, 20, 20, 20)  # Add margins around the layout

        # Style sheet for the Login Window
        self.setStyleSheet("""
            QMainWindow {
                background-color: #f0f0f0;
            }
            QLabel {
                font-size: 14px;
                color: #333;
            }
            QLineEdit {
                border: 1px solid #ccc;
                border-radius: 5px;
                padding: 8px;
                font-size: 14px;
            }
            QCheckBox {
                font-size: 12px;
                color: #555;
            }
            QPushButton {
                background-color: #4CAF50;
                color: white;
                border: none;
                border-radius: 5px;
                padding: 10px 20px;
                font-size: 14px;
            }
            QPushButton:hover {
                background-color: #367C39;
            }
            QLabel#error_label {
                color: red;
                font-size: 12px;
            }
        """)

        self.username_label = QLabel("Username:")
        self.username_input = QLineEdit()
        layout.addWidget(self.username_label)
        layout.addWidget(self.username_input)

        self.password_label = QLabel("Password:")
        self.password_input = QLineEdit()
        self.password_input.setEchoMode(QLineEdit.EchoMode.Password)
        layout.addWidget(self.password_label)
        layout.addWidget(self.password_input)

        self.remember_me_checkbox = QCheckBox("Remember me")
        layout.addWidget(self.remember_me_checkbox)

        button_layout = QHBoxLayout()
        button_layout.setSpacing(10)

        self.login_button = QPushButton("Login")
        self.signup_button = QPushButton("Signup")
        button_layout.addWidget(self.login_button)
        button_layout.addWidget(self.signup_button)
        layout.addLayout(button_layout)

        self.error_label = QLabel("")
        self.error_label.setObjectName("error_label")  # Set object name for style sheet
        layout.addWidget(self.error_label)

        self.login_button.clicked.connect(self.login)  # Ensure this line is present and correct
        self.signup_button.clicked.connect(self.signup)  # Ensure this line is present and correct

        self.threadpool = QThreadPool()

        # Load saved credentials
        if self.settings.value("remember_me") == "true":
            self.remember_me_checkbox.setChecked(True)
            self.username_input.setText(self.settings.value("username", ""))
            self.password_input.setText(self.settings.value("password", ""))

    def login(self):  # Make sure this method is defined correctly
        username = self.username_input.text()
        password = self.password_input.text()
        # In a real application, you'd validate the username and password.
        if username and password:
            # Save credentials if "Remember me" is checked
            if self.remember_me_checkbox.isChecked():
                self.settings.setValue("remember_me", "true")
                self.settings.setValue("username", username)
                self.settings.setValue("password", password)  # Store securely in production
            else:
                self.settings.setValue("remember_me", "false")
                self.settings.remove("username")
                self.settings.remove("password")

            self.error_label.setText("")
            self.main_window = MainWindow(username)
            self.main_window.show()
            self.close()
        else:
            self.error_label.setText("Invalid username or password.")

    def signup(self): # Make sure this method is defined correctly
        username = self.username_input.text()
        password = self.password_input.text()
        if username and password:
            if self.remember_me_checkbox.isChecked():
                self.settings.setValue("remember_me", "true")
                self.settings.setValue("username", username)
                self.settings.setValue("password", password)  # Store securely in production
            else:
                self.settings.setValue("remember_me", "false")
                self.settings.remove("username")
                self.settings.remove("password")
            self.error_label.setText("")
            self.main_window = MainWindow(username)
            self.main_window.show()
            self.close()
        else:
            self.error_label.setText("Invalid username or password.")

class MainWindow(QMainWindow):
    message_received_signal = pyqtSignal(str)
    peer_list_updated_signal = pyqtSignal()
    transfers_updated_signal = pyqtSignal()

    def __init__(self, username):
        super().__init__()

        self.username = username
        self.setWindowTitle(f"P2P Chat - {self.username}")
        self.setGeometry(100, 100, 900, 700)

        self.central_widget = QWidget()
        self.setCentralWidget(self.central_widget)

        # Main Layout
        main_layout = QVBoxLayout(self.central_widget)
        main_layout.setContentsMargins(10, 10, 10, 10)

        # Tab Widget
        self.tab_widget = QTabWidget()
        self.message_tab = QWidget()
        self.transfers_tab = QWidget()
        self.peers_tab = QWidget()

        self.tab_widget.addTab(self.message_tab, "Messages")
        self.tab_widget.addTab(self.transfers_tab, "Transfers")
        self.tab_widget.addTab(self.peers_tab, "Peers")
        main_layout.addWidget(self.tab_widget)

        # Setup Tabs
        self.setup_message_tab()
        self.setup_transfers_tab()
        self.setup_peers_tab()

        # Styling
        self.apply_styles()

        #Initialize Backend

        self.backend = Backend()
        # self.backend.start() #This was commented because we're already discovering with startNetwork()

        self.threadpool = QThreadPool()

        # Create menu bar
        self.menu_bar = QMenuBar()
        self.file_menu = QMenu("File", self)
        self.exit_action = self.file_menu.addAction("Exit")
        self.menu_bar.addMenu(self.file_menu)

        self.help_menu = QMenu("Help", self)
        self.about_action = self.help_menu.addAction("About")
        self.menu_bar.addMenu(self.help_menu)

        self.setMenuBar(self.menu_bar)

        self.exit_action.triggered.connect(self.close)
        self.about_action.triggered.connect(self.show_about_dialog)

        # Create status bar
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("Ready")

        # Connect signals for UI updates
        self.message_received_signal.connect(self.display_message)
        self.peer_list_updated_signal.connect(self.update_peer_list)
        self.transfers_updated_signal.connect(self.update_transfer_list)

    def showEvent(self, event):
        """Starts networking on window show."""
        super().showEvent(event) #Call the inherited function
        self.startNetwork()

    def closeEvent(self, event: QCloseEvent):
        """Stops networking and terminates the backend event loop on window close."""
        self.backend.stop() #This now needs to use a worker thread to stop the backend

    def startNetwork(self):
        worker = Worker(self.backend.start)

        worker.signals.finished.connect(self.print_output)
        self.threadpool.start(worker)

    def stopNetwork(self):
         worker = Worker(self.backend.stop)

         worker.signals.finished.connect(self.print_output)
         worker.signals.finished.connect(QCoreApplication.instance().quit)
         self.threadpool.start(worker)

    def setup_message_tab(self):
        layout = QVBoxLayout(self.message_tab)
        layout.setSpacing(10)
        layout.setContentsMargins(10, 10, 10, 10)
        # Message Input
        input_layout = QHBoxLayout()
        input_layout.setSpacing(5)
        self.message_input = QLineEdit()
        self.send_button = QPushButton("Send")
        self.send_button.setIcon(QIcon.fromTheme("mail-send"))  # Add icon
        input_layout.addWidget(self.message_input)
        input_layout.addWidget(self.send_button)
        layout.addLayout(input_layout)

        self.message_display = QTextEdit()  # Use QTextEdit for multiline display
        self.message_display.setReadOnly(True)  # Make it read-only
        layout.addWidget(self.message_display)
        self.send_button.clicked.connect(self.send_message)

    def setup_transfers_tab(self):
        layout = QVBoxLayout(self.transfers_tab)
        layout.setSpacing(10)
        layout.setContentsMargins(10, 10, 10, 10)
        # Transfer List (List View)
        self.transfer_list = QListWidget()
        layout.addWidget(self.transfer_list)

        # Progress Bar
        self.progress_bar = QProgressBar()
        layout.addWidget(self.progress_bar)

        # Buttons
        button_layout = QHBoxLayout()
        button_layout.setSpacing(5)
        self.pause_button = QPushButton("Pause")
        self.pause_button.setIcon(QIcon.fromTheme("media-playback-pause"))  # Add icon
        self.resume_button = QPushButton("Resume")
        self.resume_button.setIcon(QIcon.fromTheme("media-playback-start"))  # Add icon
        button_layout.addWidget(self.pause_button)
        button_layout.addWidget(self.resume_button)
        layout.addLayout(button_layout)

        self.pause_button.clicked.connect(self.pause_transfer)
        self.resume_button.clicked.connect(self.resume_transfer)
        self.update_transfer_list()

    def update_transfer_list(self):
        self.transfer_list.clear()
        for transfer_id, transfer in active_transfers.items():
            item = QListWidgetItem(f"{transfer_id}: {transfer.file_path} ({transfer.state.value})")
            self.transfer_list.addItem(item)

    def setup_peers_tab(self):
        layout = QVBoxLayout(self.peers_tab)
        layout.setSpacing(10)
        layout.setContentsMargins(10, 10, 10, 10)
        # Peer List (List View)
        self.peer_list = QListWidget()
        layout.addWidget(self.peer_list)

        # Buttons
        button_layout = QHBoxLayout()
        button_layout.setSpacing(5)
        self.connect_button = QPushButton("Connect")
        self.connect_button.setIcon(QIcon.fromTheme("network-connect"))  # Add icon
        self.disconnect_button = QPushButton("Disconnect")
        self.disconnect_button.setIcon(QIcon.fromTheme("network-disconnect"))  # Add icon
        button_layout.addWidget(self.connect_button)
        button_layout.addWidget(self.disconnect_button)
        layout.addLayout(button_layout)

        self.connect_button.clicked.connect(self.connect_to_selected_peer)
        self.disconnect_button.clicked.connect(self.disconnect_from_selected_peer)
        self.update_peer_list()

        # File Selection
        file_layout = QHBoxLayout()
        file_layout.setSpacing(5)
        self.choose_file_button = QPushButton("Choose File")
        self.choose_file_button.setIcon(QIcon.fromTheme("document-open"))  # Add icon
        self.send_file_button = QPushButton("Send File")
        self.send_file_button.setIcon(QIcon.fromTheme("document-send"))  # Add icon
        file_layout.addWidget(self.choose_file_button)
        file_layout.addWidget(self.send_file_button)
        layout.addLayout(file_layout)

        self.choose_file_button.clicked.connect(self.choose_file)
        self.send_file_button.clicked.connect(self.send_selected_file)

    def update_peer_list(self):
        self.peer_list.clear()
        for username, ip in peer_usernames.items():
            item = QListWidgetItem(f"{username} ({ip})")
            self.peer_list.addItem(item)

    def connect_to_selected_peer(self):
        selected_item = self.peer_list.currentItem()
        if selected_item:
            peer_info = selected_item.text()
            username = peer_info.split(" ")[0]  # Extract username

            worker = Worker(self.backend.connect_to_peer,
                              peer_ip=peer_usernames[username],
                              requesting_username=self.username,
                              target_username=username)

            worker.signals.finished.connect(self.thread_complete)
            self.threadpool.start(worker)
            self.status_bar.showMessage(f"Connecting to {username}...")
        else:
            self.status_bar.showMessage("No peer selected.")

    def disconnect_from_selected_peer(self):
        selected_item = self.peer_list.currentItem()
        if selected_item:
            peer_info = selected_item.text()
            username = peer_info.split(" ")[0]  # Extract username
            worker = Worker(self.backend.disconnect_from_peer, peer_username=username)

            worker.signals.finished.connect(self.thread_complete)
            self.threadpool.start(worker)

            self.status_bar.showMessage(f"Disconnecting from {username}...")
        else:
            self.status_bar.showMessage("No peer selected.")

    def send_message(self):
        message = self.message_input.text()
        selected_item = self.peer_list.currentItem()
        if selected_item:
            peer_info = selected_item.text()
            username = peer_info.split(" ")[0]  # Extract username
            worker = Worker(self.backend.send_message_to_peers, message=message, target_username=username)

            worker.signals.finished.connect(self.thread_complete)
            self.threadpool.start(worker)

            self.message_display.append(f"You: {message}")  # Display the message in the UI

        else:

            worker = Worker(self.backend.send_message_to_peers, message=message)

            worker.signals.finished.connect(self.thread_complete)
            self.threadpool.start(worker)
            self.message_display.append(f"You (to all): {message}")  # Display the message in the UI

        self.message_input.clear()
        self.status_bar.showMessage("Message sent.")

    def choose_file(self):
        worker = Worker(self.backend.choose_file)
        worker.signals.result.connect(self.set_selected_file)
        self.threadpool.start(worker)

    def set_selected_file(self, file_path):
        self.selected_file = file_path
        if self.selected_file:
            self.status_bar.showMessage(f"File selected: {self.selected_file}")
        else:
            self.status_bar.showMessage("No file selected.")

    def send_selected_file(self):
        selected_item = self.peer_list.currentItem()
        if not self.selected_file:
             self.status_bar.showMessage("No file has been selected")
             return

        if selected_item:
            peer_info = selected_item.text()
            username = peer_info.split(" ")[0]  # Extract username
            peers = {peer_usernames[username]: connections[peer_usernames[username]]}

            worker = Worker(self.backend.send_selected_file, file_path = self.selected_file, peers = peers)

            worker.signals.finished.connect(self.thread_complete)
            self.threadpool.start(worker)

            self.status_bar.showMessage(f"Sending file {os.path.basename(self.selected_file)} to {username}...")
        else:
            self.status_bar.showMessage("No peer selected.")

    def pause_transfer(self):
        # Add logic to pause the selected transfer
        pass

    def resume_transfer(self):
        # Add logic to resume the selected transfer
        pass

    def show_about_dialog(self):
        QMessageBox.about(self, "About P2P Chat", "P2P Chat Application\nCreated with PyQt")

    def print_output(self, s):
        print(f"print_output: {s}")
        pass

    def thread_complete(self):
        print("Thread completed!")
        self.peer_list_updated_signal.emit()  # Update peer list in UI
        self.transfers_updated_signal.emit()  # Update transfers list in UI

    def display_message(self, message):
         self.message_display.append(message)  # Update the UI

    def apply_styles(self):
        """Apply the style sheet to the main window and its elements."""

        # Style sheet for the Main Window
        self.setStyleSheet("""
            QMainWindow {
                background-color: #e6e6e6;
                font-family: Arial, sans-serif;
            }
            QTabWidget::pane {
                border: none;
                background-color: #f0f0f0;
                border-radius: 0px;  /* No rounded corners */
            }
            QTabBar::tab {
                background: #d9d9d9;
                color: #333;
                border: none;
                padding: 8px 20px;
                font-size: 14px;
                border-top-left-radius: 0px;  /* No rounded corners */
                border-top-right-radius: 0px;  /* No rounded corners */
                margin-right: 2px;
            }
            QTabBar::tab:selected {
                background: #4CAF50;
                color: white;
            }
            QListWidget {
                background: white;
                border: 1px solid #ccc;
                border-radius: 5px;
                padding: 5px;
                font-size: 14px;
            }
            QTextEdit {
                background: white;
                border: 1px solid #ccc;
                border-radius: 5px;
                padding: 5px;
                font-size: 14px;
            }
            QPushButton {
                background-color: #4CAF50;
                color: white;
                border: none;
                border-radius: 5px;
                padding: 8px 16px;
                font-size: 14px;
                min-width: 80px;
            }
            QPushButton:hover {
                background-color: #367C39;
            }
            QProgressBar {
                border: 1px solid #ccc;
                border-radius: 5px;
                text-align: center;
                font-size: 12px;
            }
            QProgressBar::chunk {
                background-color: #4CAF50;
                border-radius: 5px;
            }
            QLineEdit {
                border: 1px solid #ccc;
                border-radius: 5px;
                padding: 8px;
                font-size: 14px;
            }
            QStatusBar {
                background: #f0f0f0;
                color: #555;
                border: none;
            }
            QMenuBar {
                background: #d9d9d9;
                color: #333;
                border: none;
            }
            QMenu {
                background: #f0f0f0;
                color: #333;
                border: none;
            }
            QMenu::item:selected {
                background: #4CAF50;
                color: white;
            }
        """)

        # Set a global font
        font = QFont("Arial", 12)
        QApplication.instance().setFont(font)

if __name__ == "__main__":
    app = QApplication(sys.argv)
    login_window = LoginWindow()
    login_window.show()
    sys.exit(app.exec())
