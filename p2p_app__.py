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
from collections import defaultdict 

from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric import rsa, padding

from PyQt6.QtCore import (QCoreApplication, QObject, QRunnable, QSettings,
                          QThreadPool, pyqtSignal, pyqtSlot, Qt, QThread, QTimer, QSize) 
from PyQt6.QtWidgets import (QApplication, QCheckBox, QFileDialog, QLabel,
                             QLineEdit, QListWidget, QListWidgetItem,
                             QMainWindow, QMessageBox, QPushButton,
                             QProgressBar, QVBoxLayout, QWidget, QTabWidget,
                             QTextEdit, QHBoxLayout, QStatusBar, QMenuBar, QMenu,
                             QStyle, QSplitter, QStackedWidget, QFrame) 
from PyQt6.QtGui import QIcon, QFont, QCloseEvent, QPalette, QColor, QTextCursor 


try:
    from networking.discovery import PeerDiscovery
    from networking.messaging.core import (
        handle_incoming_connection, receive_peer_messages, send_message_to_peers,
        maintain_peer_list, connections
    )
    from networking.messaging.utils import (
         initialize_user_config, connect_to_peer, disconnect_from_peer,
         get_peer_display_name, resolve_peer_target, get_own_display_name
    )
    from networking.messaging.groups import ( # Import group functions if used by UI later
         send_group_create_message, send_group_invite_message, send_group_invite_response,
         send_group_join_request, send_group_join_response, send_group_update_message
    )
    from networking.file_transfer import (
        send_file, FileTransfer, TransferState, compute_hash, update_transfer_progress
    )
    from networking.shared_state import (
        peer_usernames, peer_device_ids, peer_public_keys, shutdown_event,
        user_data, active_transfers, message_queue, groups, pending_invites,
        pending_join_requests, pending_approvals, connection_denials
    )
    NETWORKING_AVAILABLE = True
except ImportError as e:
    # ... (rest of the except block remains the same) ...
    from networking.messaging.utils import (
         initialize_user_config, connect_to_peer, disconnect_from_peer,
         get_peer_display_name, resolve_peer_target, get_own_display_name
    )
    from networking.messaging.groups import ( # Import group functions if used by UI later
         send_group_create_message, send_group_invite_message, send_group_invite_response,
         send_group_join_request, send_group_join_response, send_group_update_message
    )
    from networking.file_transfer import (
        send_file, FileTransfer, TransferState, compute_hash, update_transfer_progress
    )
    from networking.shared_state import (
        peer_usernames, peer_device_ids, peer_public_keys, shutdown_event,
        user_data, active_transfers, message_queue, groups, pending_invites,
        pending_join_requests, pending_approvals, connection_denials
    )
    NETWORKING_AVAILABLE = True
except ImportError as e:
    print(f"ERROR: Could not import networking modules: {e}. Running with dummy networking.")
    NETWORKING_AVAILABLE = False
    # --- Dummy Imports (Keep for fallback/UI testing if needed) ---
    class PeerDiscovery:
        def __init__(self): self.peer_list = {"Alice": ("192.168.1.10", time.time()), "Bob": ("192.168.1.11", time.time())}
        def start(self): print("Dummy Discovery Start")
        def stop(self): print("Dummy Discovery Stop")
    async def user_input(*args): await asyncio.sleep(1)
    async def display_messages(): await asyncio.sleep(1)
    async def update_transfer_progress(): await asyncio.sleep(1)
    async def maintain_peer_list(*args): await asyncio.sleep(1)
    async def connect_to_peer(*args): print("Dummy connect_to_peer"); await asyncio.sleep(0.1); return True
    async def disconnect_from_peer(*args): print("Dummy disconnect_from_peer"); await asyncio.sleep(0.1); return True
    async def send_message_to_peers(*args): print("Dummy send_message_to_peers"); await asyncio.sleep(0.1); return True
    async def send_file(*args): print("Dummy send_file"); await asyncio.sleep(0.1); return True
    async def initialize_user_config(): print("Dummy init user config"); user_data.update({"original_username": "DummyUser", "device_id": "dummy123"})
    def get_peer_display_name(ip): return f"Peer_{ip.replace('.', '_')}"
    def get_own_display_name(): return "You(dummy)"
    peer_usernames = {"Alice": "192.168.1.10", "Bob": "192.168.1.11"}
    peer_device_ids = {"192.168.1.10": "alice_dev", "192.168.1.11": "bob_dev"}
    connections = {"192.168.1.10": object(), "192.168.1.11": object()}
    active_transfers = {"transfer1": type('obj', (object,), {'file_path': '/path/to/file.txt','peer_ip':'192.168.1.10', 'state': type('obj', (object,), {'value': 'Sending'})(), 'progress': 50, 'transferred_size': 512*1024, 'total_size': 1024*1024, 'direction': 'send'})()}
    shutdown_event = asyncio.Event()
    user_data = {}
    message_queue = asyncio.Queue()
    pending_approvals = {}
    connection_denials = {}
    # --- End Dummy Imports ---

# --- Logging ---
log_queue = asyncio.Queue() # Queue for logs from async to Qt
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(threadName)s] %(levelname)s - %(message)s",
                    stream=sys.stdout) # Keep console logging

# Configure a specific logger for the application if desired
logger = logging.getLogger("P2PChatApp")
# You could add file handlers etc. here if needed

# --- Worker and Signals ---
class WorkerSignals(QObject):
    finished = pyqtSignal()
    error = pyqtSignal(tuple)
    result = pyqtSignal(object)
    # progress = pyqtSignal(int) # Keep if needed for specific tasks
    # message = pyqtSignal(str) # Use specific signals instead

class Worker(QRunnable):
    """Executes a function in a separate thread using QThreadPool."""
    def __init__(self, fn, *args, **kwargs):
        super().__init__()
        self.fn = fn
        self.args = args
        self.kwargs = kwargs
        self.signals = WorkerSignals()
        self.is_async = asyncio.iscoroutinefunction(fn) # Check if target is async

    @pyqtSlot()
    def run(self):
        """Execute the assigned function."""
        # Note: This runs in a QThreadPool thread, NOT the main GUI thread.
        # It also doesn't have direct access to the asyncio loop running in NetworkingThread.
        # If self.fn is async, it needs to be scheduled onto that specific loop.
        # This Worker is best suited for *synchronous* tasks or *triggering* async tasks.
        backend_instance = self.kwargs.pop('backend_instance', None) # Pop backend if passed
        loop = self.kwargs.pop('loop', None) # Pop loop if passed

        try:
            if self.is_async:
                if loop and loop.is_running():
                    # Schedule the async function on the dedicated asyncio loop
                    future = asyncio.run_coroutine_threadsafe(self.fn(*self.args, **self.kwargs), loop)
                    # Block this worker thread until the coroutine completes
                    result = future.result() # Add timeout?
                else:
                    raise RuntimeError("Async function called but no running asyncio loop provided.")
            else:
                 # Execute synchronous function directly
                 result = self.fn(*self.args, **self.kwargs)

        except Exception as e:
            logger.error(f"Error in worker running {self.fn.__name__}: {e}", exc_info=True)
            exctype, value = sys.exc_info()[:2]
            self.signals.error.emit((exctype, value, traceback.format_exc()))
        else:
            self.signals.result.emit(result)
        finally:
            self.signals.finished.emit()

# --- Backend Logic Controller ---
class Backend(QObject):
    # Signals for UI updates FROM backend
    message_received_signal = pyqtSignal(str, str) # sender_display_name, message
    log_message_signal = pyqtSignal(str) # For general status/info messages
    peer_list_updated_signal = pyqtSignal(dict) # Send current peer list {ip: (username, is_connected)}
    transfers_updated_signal = pyqtSignal(dict) # Send current transfers {id: transfer_info_dict}
    connection_status_signal = pyqtSignal(str, bool) # peer_ip, is_connected
    connection_request_signal = pyqtSignal(str, str) # requesting_display_name, base_username_for_cmd
    transfer_progress_signal = pyqtSignal(str, int) # transfer_id, progress_percentage

    # Signal emitted when backend stop is complete
    stopped_signal = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.discovery = None
        self.loop = None # Will be set by NetworkingThread
        self.networking_tasks = []
        self.selected_file = None
        self._is_running = False

    def set_loop(self, loop):
        self.loop = loop

    def start(self):
        """Initializes and starts the networking components."""
        if not NETWORKING_AVAILABLE:
             self.log_message_signal.emit("Networking unavailable. Running in dummy mode.")
             return
        if self._is_running:
             logger.warning("Backend start called while already running.")
             return

        logger.info("Backend: Starting...")
        try:
            # Initialize user config (this might involve sync I/O, but should be quick)
            # If it becomes slow, move to worker or make it async
            # asyncio.run(initialize_user_config()) # Can't use asyncio.run here
            # Let's assume initialize_user_config can be called synchronously for now
            # Or better: make initialize_user_config an async func and call it first in the async loop
            logger.info("User config initialized (or will be by async loop).")

            self.discovery = PeerDiscovery()
            self._is_running = True

            # Schedule the main async tasks to run on the loop
            # Need the loop object which is managed by NetworkingThread
            if self.loop and self.loop.is_running():
                 # Pass necessary signals to async functions if they need to emit directly
                 # Or use the message_queue pattern more heavily

                 # Task to process the standard asyncio message_queue for GUI updates
                 self.networking_tasks.append(asyncio.run_coroutine_threadsafe(self._process_message_queue(), self.loop))

                 # Core networking tasks
                 self.networking_tasks.append(asyncio.run_coroutine_threadsafe(self.discovery.send_broadcasts(), self.loop))
                 self.networking_tasks.append(asyncio.run_coroutine_threadsafe(self.discovery.receive_broadcasts(), self.loop))
                 self.networking_tasks.append(asyncio.run_coroutine_threadsafe(self.discovery.cleanup_stale_peers(), self.loop))
                 self.networking_tasks.append(asyncio.run_coroutine_threadsafe(update_transfer_progress(), self.loop))
                 # Pass self.discovery instance to maintain_peer_list
                 self.networking_tasks.append(asyncio.run_coroutine_threadsafe(maintain_peer_list(self.discovery), self.loop))

                 logger.info("Backend: Core networking tasks scheduled.")
                 self.log_message_signal.emit("Network backend started.")

                 # Emit initial states
                 self.emit_peer_list_update()
                 self.emit_transfers_update()

            else:
                 logger.error("Backend start called but asyncio loop is not available/running.")
                 self.log_message_signal.emit("Error: Could not start networking loop.")

        except Exception as e:
            logger.exception("Error during backend start")
            self.log_message_signal.emit(f"Backend start error: {e}")
            self._is_running = False


    def stop(self):
        """Stops the networking components gracefully."""
        if not self._is_running:
             logger.warning("Backend stop called but not running.")
             self.stopped_signal.emit() # Signal completion even if not running
             return

        logger.info("Backend: Stopping...")
        self.log_message_signal.emit("Shutting down network...")
        shutdown_event.set() # Signal async tasks to stop

        if self.discovery:
            try:
                 self.discovery.stop() # Call sync stop methods
                 logger.info("PeerDiscovery stopped.")
            except Exception as e:
                 logger.error(f"Error stopping PeerDiscovery: {e}")

        # We don't explicitly cancel tasks here; NetworkingThread handles that
        # when the loop stops. Shutdown event should cause tasks to exit cleanly.

        self._is_running = False
        logger.info("Backend: Stop sequence initiated.")
        # The actual loop stopping happens in NetworkingThread
        # Emit stopped signal once the NetworkingThread confirms loop closure.


    async def _process_message_queue(self):
        """Processes items from the asyncio message_queue and emits Qt signals."""
        logger.debug("Starting message queue processor.")
        while not shutdown_event.is_set():
            try:
                # Wait for an item with a timeout to allow checking shutdown_event
                item = await asyncio.wait_for(message_queue.get(), timeout=1.0)
                if item:
                    try:
                        if isinstance(item, str):
                            # Assume general log/status messages
                            self.log_message_signal.emit(item)
                        elif isinstance(item, dict):
                            msg_type = item.get("type")
                            if msg_type == "approval_request":
                                req_disp_name = item.get("requesting_username", "Unknown")
                                peer_ip = item.get("peer_ip")
                                base_user = req_disp_name.split("(")[0] # Get base username for cmd
                                self.connection_request_signal.emit(req_disp_name, base_user)
                            elif msg_type == "log": # Allow specific log messages
                                level = item.get("level", logging.INFO)
                                message = item.get("message", "")
                                logger.log(level, f"From Queue: {message}") # Log internally
                                self.log_message_signal.emit(message) # Show in UI
                            elif msg_type == "message": # A chat message received
                                sender = item.get("sender_display_name", "Unknown")
                                content = item.get("content", "")
                                self.message_received_signal.emit(sender, content)
                            elif msg_type == "transfer_update": # Transfer status/progress
                                self.emit_transfers_update() # Just re-emit the whole dict for now
                            elif msg_type == "transfer_progress":
                                t_id = item.get("transfer_id")
                                progress = item.get("progress")
                                if t_id is not None and progress is not None:
                                     self.transfer_progress_signal.emit(t_id, int(progress))
                            elif msg_type == "peer_update": # Peer connected/disconnected/discovered
                                self.emit_peer_list_update() # Re-emit the peer list
                            elif msg_type == "connection_status":
                                 peer_ip = item.get("peer_ip")
                                 status = item.get("connected", False)
                                 if peer_ip:
                                      self.connection_status_signal.emit(peer_ip, status)
                                      self.emit_peer_list_update() # Also update list view
                            else:
                                logger.warning(f"Unknown message type in queue: {msg_type} - {item}")
                                self.log_message_signal.emit(str(item)) # Display raw item as fallback
                    except Exception as e:
                         logger.exception(f"Error processing item from message_queue: {item}")
                    finally:
                         if hasattr(message_queue, 'task_done'):
                             message_queue.task_done() # Mark item as processed
            except asyncio.TimeoutError:
                continue # No item received, check shutdown_event again
            except Exception as e:
                 logger.exception(f"Error in message queue processor loop: {e}")
                 await asyncio.sleep(1) # Avoid tight loop on unexpected error

        logger.info("Message queue processor stopped.")

    def emit_peer_list_update(self):
        """Constructs peer status dict and emits signal."""
        if not NETWORKING_AVAILABLE: # Handle dummy mode
            status_dict = {ip: (name, ip in connections) for name, ip in peer_usernames.items()}
            self.peer_list_updated_signal.emit(status_dict)
            return

        if self.loop and self.loop.is_running() and self.discovery:
            def get_peers_sync():
                peers = {}
                # Discovered peers
                if self.discovery:
                    for ip, (uname, _) in self.discovery.peer_list.items():
                        peers[ip] = (uname, ip in connections) # Check if connected
                # Ensure connected peers are included even if not recently discovered
                for ip in connections:
                    if ip not in peers:
                        # Find username for this connected IP
                        uname = "Unknown"
                        for stored_uname, stored_ip in peer_usernames.items():
                            if stored_ip == ip:
                                uname = stored_uname
                                break
                        peers[ip] = (uname, True)
                return peers

            # Schedule the synchronous dictionary creation and signal emission
            # back in the Qt thread from the asyncio thread is tricky.
            # It's safer to emit from the Qt thread based on a trigger.
            # For now, let's risk calling it directly if needed, but ideally
            # the _process_message_queue or a QTimer handles periodic updates.
            # --- Let's use call_soon_threadsafe to emit from the correct thread ---
            try:
                peers_dict = get_peers_sync()
                # Emit the signal from the main Qt thread via the loop if possible
                # self.loop.call_soon_threadsafe(self.peer_list_updated_signal.emit, peers_dict)
                # --- Or, if called from Qt thread already, emit directly ---
                QCoreApplication.instance().postEvent(self, PeerUpdateEvent(peers_dict)) # Use event posting

            except Exception as e:
                logger.error(f"Error emitting peer list update: {e}")


    def emit_transfers_update(self):
        """Constructs transfer info dict and emits signal."""
        if not NETWORKING_AVAILABLE:
             self.transfers_updated_signal.emit(active_transfers) # Send dummy data
             return

        if self.loop and self.loop.is_running():
            def get_transfers_sync():
                transfers_info = {}
                for tid, t in active_transfers.items():
                    # Create a dictionary representation for the signal
                    transfers_info[tid] = {
                        "id": tid,
                        "file_path": getattr(t, 'file_path', 'N/A'),
                        "peer_ip": getattr(t, 'peer_ip', 'N/A'),
                        "direction": getattr(t, 'direction', 'N/A'),
                        "state": getattr(getattr(t, 'state', None), 'value', 'Unknown'),
                        "total_size": getattr(t, 'total_size', 0),
                        "transferred_size": getattr(t, 'transferred_size', 0),
                        "progress": int((getattr(t, 'transferred_size', 0) / getattr(t, 'total_size', 1)) * 100) if getattr(t, 'total_size', 0) > 0 else 0
                    }
                return transfers_info
            try:
                transfers_dict = get_transfers_sync()
                # self.loop.call_soon_threadsafe(self.transfers_updated_signal.emit, transfers_dict)
                QCoreApplication.instance().postEvent(self, TransferUpdateEvent(transfers_dict)) # Use event posting
            except Exception as e:
                logger.error(f"Error emitting transfers update: {e}")

    # --- Methods to be called from GUI (via Worker/run_coroutine_threadsafe) ---

    def trigger_connect_to_peer(self, peer_ip, requesting_username, target_username):
        """Schedules connect_to_peer coroutine."""
        if self.loop and self.loop.is_running():
             logger.info(f"Scheduling connection to {target_username} ({peer_ip})")
             # Pass self.loop for run_coroutine_threadsafe
             worker = Worker(connect_to_peer, peer_ip, requesting_username, target_username, loop=self.loop)
             # Connect signals if you need result/error back in Backend or direct to UI
             # worker.signals.result.connect(...)
             # worker.signals.error.connect(...)
             QThreadPool.globalInstance().start(worker) # Use global thread pool
             return True # Indicates scheduling attempt
        else:
             logger.error("Cannot connect: Asyncio loop not running.")
             self.log_message_signal.emit("Cannot connect: Network thread not running.")
             return False

    def trigger_disconnect_from_peer(self, peer_ip):
        """Schedules disconnect_from_peer coroutine."""
        if self.loop and self.loop.is_running():
             logger.info(f"Scheduling disconnection from {peer_ip}")
             worker = Worker(disconnect_from_peer, peer_ip, loop=self.loop)
             # Connect signals if needed
             QThreadPool.globalInstance().start(worker)
             return True
        else:
             logger.error("Cannot disconnect: Asyncio loop not running.")
             self.log_message_signal.emit("Cannot disconnect: Network thread not running.")
             return False

    def trigger_send_message(self, message, target_peer_ip=None):
         """Schedules send_message_to_peers coroutine."""
         if self.loop and self.loop.is_running():
             logger.info(f"Scheduling message send to {target_peer_ip or 'all'}")
             # Note: send_message_to_peers might need adjustments to be purely async
             # For now, assume it is and schedule it
             worker = Worker(send_message_to_peers, message, target_peer_ip, loop=self.loop)
             # Connect signals if needed
             QThreadPool.globalInstance().start(worker)
             return True
         else:
             logger.error("Cannot send message: Asyncio loop not running.")
             self.log_message_signal.emit("Cannot send message: Network thread not running.")
             return False

    def choose_file(self, parent_widget=None):
        """Opens file dialog. MUST be called from GUI thread."""
        # This is synchronous and UI related, so call it directly from MainWindow.
        # It updates self.selected_file within the Backend instance.
        if QThread.currentThread() != QCoreApplication.instance().thread():
             logger.error("choose_file called from wrong thread!")
             return None

        file_dialog = QFileDialog(parent_widget)
        selected_file, _ = file_dialog.getOpenFileName(parent_widget, "Choose File")
        if selected_file:
            logger.info(f"File selected: {selected_file}")
            self.selected_file = selected_file
            return selected_file
        else:
            logger.info("No File Selected")
            self.selected_file = None
            return None

    def trigger_send_file(self, file_path, peers_dict):
         """Schedules send_file coroutine."""
         if not file_path:
             self.log_message_signal.emit("Error: No file selected to send.")
             return False
         if not peers_dict:
             self.log_message_signal.emit("Error: No peer selected to send file to.")
             return False

         if self.loop and self.loop.is_running():
             peer_ip = list(peers_dict.keys())[0] # Assuming single peer for now
             logger.info(f"Scheduling file send '{os.path.basename(file_path)}' to {peer_ip}")
             worker = Worker(send_file, file_path, peers_dict, loop=self.loop)
             # Connect signals if needed
             QThreadPool.globalInstance().start(worker)
             return True
         else:
             logger.error("Cannot send file: Asyncio loop not running.")
             self.log_message_signal.emit("Cannot send file: Network thread not running.")
             return False

    # --- Approval Handling ---
    def approve_connection(self, peer_ip, requesting_username):
         if self.loop and self.loop.is_running():
             approval_key = (peer_ip, requesting_username)
             future = pending_approvals.get(approval_key)
             if future and not future.done():
                 # Set result from the correct thread
                 self.loop.call_soon_threadsafe(future.set_result, True)
                 logger.info(f"Connection approved for {requesting_username} ({peer_ip})")
                 return True
             else:
                  logger.warning(f"Could not approve connection for {requesting_username}: No pending request found or already handled.")
                  return False
         return False

    def deny_connection(self, peer_ip, requesting_username):
         if self.loop and self.loop.is_running():
             approval_key = (peer_ip, requesting_username)
             future = pending_approvals.get(approval_key)
             if future and not future.done():
                 self.loop.call_soon_threadsafe(future.set_result, False)
                 logger.info(f"Connection denied for {requesting_username} ({peer_ip})")
                 return True
             else:
                 logger.warning(f"Could not deny connection for {requesting_username}: No pending request found or already handled.")
                 return False
         return False

    # --- Custom Event Handling ---
    def event(self, event):
        """Handle custom events posted from other threads."""
        if event.type() == PeerUpdateEvent.TypeId:
            self.peer_list_updated_signal.emit(event.peers)
            return True
        elif event.type() == TransferUpdateEvent.TypeId:
            self.transfers_updated_signal.emit(event.transfers)
            return True
        return super().event(event)

# --- Custom Events for Thread-Safe Signal Emission ---
from PyQt6.QtCore import QEvent

class PeerUpdateEvent(QEvent):
    TypeId = QEvent.Type(QEvent.Type.User + 1)
    def __init__(self, peers_dict):
        super().__init__(PeerUpdateEvent.TypeId)
        self.peers = peers_dict

class TransferUpdateEvent(QEvent):
    TypeId = QEvent.Type(QEvent.Type.User + 2)
    def __init__(self, transfers_dict):
        super().__init__(TransferUpdateEvent.TypeId)
        self.transfers = transfers_dict


# --- Networking Thread ---
class NetworkingThread(QThread):
    """Manages the asyncio event loop."""
    loop_ready = pyqtSignal(object) # Emits the loop object when ready
    thread_finished = pyqtSignal() # Signal when run() method finishes

    def __init__(self, backend_ref):
        super().__init__()
        self.backend = backend_ref
        self.loop = None

    def run(self):
        logger.info("NetworkingThread: Starting...")
        try:
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            self.backend.set_loop(self.loop) # Give backend access to the loop
            self.loop_ready.emit(self.loop) # Signal that loop is ready
            # Initialize user config asynchronously
            self.loop.run_until_complete(initialize_user_config())
            # Start the backend's main async tasks *after* loop is ready and config loaded
            self.backend.start()
            # Run the loop forever until stop() is called
            self.loop.run_forever()

        except Exception as e:
             logger.exception(f"NetworkingThread Error in run_forever: {e}")
        finally:
            logger.info("NetworkingThread: run_forever finished.")
            if self.loop and self.loop.is_running():
                 logger.info("NetworkingThread: Stopping loop...")
                 # Schedule tasks cancellation within the loop before stopping
                 self.loop.run_until_complete(self.shutdown_tasks())
                 self.loop.call_soon_threadsafe(self.loop.stop)
                 # Closing the loop should happen after it stops
                 # Try closing immediately after stop is processed
                 time.sleep(0.1) # Brief pause to allow stop to process
            if self.loop and not self.loop.is_closed():
                 logger.info("NetworkingThread: Closing loop...")
                 self.loop.close()
                 logger.info("NetworkingThread: Loop closed.")
            self.loop = None
            self.backend.set_loop(None)
            self.thread_finished.emit() # Signal completion
            logger.info("NetworkingThread: Finished.")

    async def shutdown_tasks(self):
        """Cancel all running tasks in the loop."""
        logger.info("NetworkingThread: Cancelling running asyncio tasks...")
        tasks = [t for t in asyncio.all_tasks(loop=self.loop) if t is not asyncio.current_task()]
        if not tasks:
             logger.info("NetworkingThread: No tasks to cancel.")
             return

        logger.info(f"NetworkingThread: Cancelling {len(tasks)} tasks.")
        for task in tasks:
             task.cancel()

        results = await asyncio.gather(*tasks, return_exceptions=True)
        for i, result in enumerate(results):
             if isinstance(result, asyncio.CancelledError):
                 logger.debug(f"Task {i} cancelled successfully.")
             elif isinstance(result, Exception):
                 logger.error(f"Error during task cancellation/shutdown: {result}", exc_info=result)
        logger.info("NetworkingThread: Task cancellation complete.")

    def request_stop(self):
        """Requests the event loop to stop."""
        logger.info("NetworkingThread: Stop requested.")
        if self.loop and self.loop.is_running():
             # Signal async tasks via shared event first
             shutdown_event.set()
             # Schedule the loop stop from the Qt thread
             self.loop.call_soon_threadsafe(self.loop.stop)
        else:
             logger.warning("NetworkingThread: Stop requested but loop not running.")


# --- Login Window (Themed) ---
class LoginWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = QSettings("P2PChat", "Login")
        self.setWindowTitle("P2P Chat - Login")
        self.setGeometry(200, 200, 380, 280) # Adjusted size
        self.setWindowIcon(QIcon.fromTheme("network-transmit-receive")) # Example Icon

        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        layout = QVBoxLayout(central_widget)
        layout.setSpacing(15)
        layout.setContentsMargins(25, 25, 25, 25)

        # Apply styles directly here or use the main window's method if possible
        self.apply_styles()

        self.username_label = QLabel("Username:")
        self.username_input = QLineEdit()
        self.username_input.setPlaceholderText("Enter your username")
        layout.addWidget(self.username_label)
        layout.addWidget(self.username_input)

        # We don't use password for P2P identification, remove password field
        # self.password_label = QLabel("Password:")
        # self.password_input = QLineEdit()
        # self.password_input.setEchoMode(QLineEdit.EchoMode.Password)
        # layout.addWidget(self.password_label)
        # layout.addWidget(self.password_input)

        # Keep "Remember me" for username persistence
        self.remember_me_checkbox = QCheckBox("Remember username")
        layout.addWidget(self.remember_me_checkbox)

        button_layout = QHBoxLayout()
        button_layout.setSpacing(10)
        self.login_button = QPushButton("Login / Register") # Combine button
        self.login_button.setObjectName("login_button") # Use specific ID if needed
        # self.signup_button = QPushButton("Signup") # Remove signup
        button_layout.addStretch()
        button_layout.addWidget(self.login_button)
        button_layout.addStretch()
        layout.addLayout(button_layout)

        self.error_label = QLabel("")
        self.error_label.setObjectName("error_label")
        layout.addWidget(self.error_label)

        self.login_button.clicked.connect(self.login_or_register)
        # self.signup_button.clicked.connect(self.signup)

        # Load saved username
        if self.settings.value("remember_me") == "true":
            self.remember_me_checkbox.setChecked(True)
            self.username_input.setText(self.settings.value("username", ""))
            # self.password_input.setText(self.settings.value("password", ""))


    def login_or_register(self):
        username = self.username_input.text().strip()
        if username:
            if self.remember_me_checkbox.isChecked():
                self.settings.setValue("remember_me", "true")
                self.settings.setValue("username", username)
            else:
                self.settings.setValue("remember_me", "false")
                self.settings.remove("username")

            self.error_label.setText("")
            # Basic check: Does config exist? If not, it's a "registration".
            # In reality, initialize_user_config handles this.
            # We just need to pass the username to MainWindow.
            self.main_window = MainWindow(username) # Pass username
            self.main_window.show()
            self.close()
        else:
            self.error_label.setText("Username cannot be empty.")

    # def signup(self):
    #     # Simplified: Treat signup the same as login for now
    #     self.login_or_register()

    def apply_styles(self):
        """Apply the dark theme style sheet to LoginWindow."""
        # Use same colors as MainWindow for consistency
        dark_bg="#1e1e1e"; medium_bg="#252526"; light_bg="#2d2d2d"; dark_border="#333333"; medium_border="#444444"; text_color="#e0e0e0"; dim_text_color="#a0a0a0"; accent_color="#ff6600"; accent_hover="#e65c00"; accent_pressed="#cc5200"; font_family = "Segoe UI, Arial, sans-serif"

        # Simplified Stylesheet for Login
        self.setStyleSheet(f"""
            QMainWindow {{
                background-color: {dark_bg};
                color: {text_color};
                font-family: {font_family};
            }}
            QWidget {{ /* Default text color */
                color: {text_color};
                font-size: 13px;
            }}
            QLabel {{
                font-size: 14px;
                padding-bottom: 5px; /* Add some space below labels */
            }}
            QLineEdit {{
                background-color: {light_bg};
                border: 1px solid {dark_border};
                border-radius: 5px;
                padding: 10px;
                font-size: 14px;
                color: {text_color};
            }}
            QLineEdit:focus {{
                border: 1px solid {accent_color};
            }}
            QCheckBox {{
                font-size: 12px;
                color: {dim_text_color};
                padding-top: 5px; /* Add space above checkbox */
            }}
            QCheckBox::indicator {{
                width: 16px;
                height: 16px;
            }}
             QCheckBox::indicator:unchecked {{
                 /* Optional: custom unchecked box style */
                 border: 1px solid {medium_border}; background-color: {light_bg}; border-radius: 3px;
             }}
             QCheckBox::indicator:checked {{
                 /* Optional: custom checked box style */
                 background-color: {accent_color}; border: 1px solid {accent_hover}; border-radius: 3px;
                 /* image: url(path/to/checkmark.png); */ /* Or use an image */
             }}
            QPushButton {{
                background-color: {accent_color};
                color: white;
                border: none;
                border-radius: 5px;
                padding: 10px 25px; /* More padding */
                font-size: 14px;
                font-weight: bold;
                min-width: 120px;
            }}
            QPushButton:hover {{
                background-color: {accent_hover};
            }}
            QPushButton:pressed {{
                background-color: {accent_pressed};
            }}
            QLabel#error_label {{ /* Style for the error message */
                color: #FFAAAA; /* Light red */
                font-size: 12px;
                padding-top: 10px;
                font-weight: bold;
                qproperty-alignment: 'AlignCenter';
            }}
        """)


# --- MODIFIED MainWindow (with Chat UI) ---
class MainWindow(QMainWindow):
    # Keep internal signals if direct calls are needed
    # message_received_signal = pyqtSignal(str) # Replaced by backend signal
    # peer_list_updated_signal = pyqtSignal() # Replaced by backend signal
    # transfers_updated_signal = pyqtSignal() # Replaced by backend signal

    def __init__(self, username): # Accept username from LoginWindow
        super().__init__()

        self.username = username # Store passed username
        self.current_chat_peer_username = None # Track selected chat
        self.chat_widgets = {} # username -> {'history': QTextEdit, 'input': QLineEdit, 'send_btn': QPushButton}

        self.setWindowTitle(f"P2P Chat - {get_own_display_name()}") # Use display name
        self.setGeometry(100, 100, 1000, 800) # Larger window
        self.selected_file = None

        self.central_widget = QWidget()
        self.setCentralWidget(self.central_widget)
        main_layout = QVBoxLayout(self.central_widget)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # --- Backend Setup ---
        self.backend = Backend()
        self.network_thread = NetworkingThread(self.backend) # Pass backend ref

        # --- Tab Widget Setup ---
        self.tab_widget = QTabWidget()
        self.chat_tab = QWidget() # Renamed from message_tab
        self.transfers_tab = QWidget()
        self.peers_tab = QWidget() # For discovery list & connect/disconnect

        self.tab_widget.addTab(self.chat_tab, "Chat") # Default tab
        self.tab_widget.addTab(self.transfers_tab, "Transfers")
        self.tab_widget.addTab(self.peers_tab, "Network Peers")
        main_layout.addWidget(self.tab_widget)

        # Setup Tabs (UI elements)
        self.setup_chat_tab() # New chat layout
        self.setup_transfers_tab()
        self.setup_peers_tab() # Now for discovery/connection management

        # Styling
        self.apply_styles() # Apply the theme

        # Menu Bar & Status Bar
        self.setup_menu_bar()
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("Initializing...")

        # --- Connect Backend Signals to UI Slots ---
        self.backend.log_message_signal.connect(self.update_status_bar)
        self.backend.peer_list_updated_signal.connect(self.update_peer_list_display) # Updates Peers Tab list
        self.backend.transfers_updated_signal.connect(self.update_transfer_list_display)
        self.backend.message_received_signal.connect(self.display_received_message)
        self.backend.connection_status_signal.connect(self.handle_connection_status_update)
        self.backend.connection_request_signal.connect(self.show_connection_request)
        self.backend.transfer_progress_signal.connect(self.update_transfer_progress_display)
        self.backend.stopped_signal.connect(self.on_backend_stopped) # Handle backend stop confirmation

        # Connect thread signals
        self.network_thread.thread_finished.connect(self.on_network_thread_finished)

        # Periodic UI updates using QTimer (alternative to signals for some things)
        self.ui_update_timer = QTimer(self)
        self.ui_update_timer.timeout.connect(self.periodic_ui_update)
        self.ui_update_timer.start(2000) # Update every 2 seconds

    def setup_menu_bar(self):
        # (Same as before)
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

    def showEvent(self, event):
        super().showEvent(event)
        self.startNetwork() # Start network when window appears

    def closeEvent(self, event: QCloseEvent):
        logger.info("MainWindow: Close event triggered.")
        self.update_status_bar("Shutting down...")
        self.ui_update_timer.stop() # Stop timer
        # Request network thread to stop, DO NOT wait here (causes GUI freeze)
        self.network_thread.request_stop()
        # Accept the event now. Actual cleanup happens when thread signals finished.
        event.accept()
        # Note: If the network thread takes too long, the app might exit before full cleanup.
        # A more robust solution involves waiting for the thread_finished signal
        # in a way that doesn't block the event loop, possibly by scheduling app.quit().

    def startNetwork(self):
        logger.info("MainWindow: Starting network thread...")
        if not self.network_thread.isRunning():
             self.network_thread.start()
             self.update_status_bar("Starting network...")
        else:
             logger.warning("MainWindow: Network thread already running.")

    def on_network_thread_finished(self):
         """Called when the NetworkingThread's run() method finishes."""
         logger.info("MainWindow: Detected NetworkingThread finished.")
         self.update_status_bar("Network stopped.")
         # If we need to close the app after the network stops cleanly:
         # QCoreApplication.instance().quit()

    def on_backend_stopped(self):
        """Called when the backend explicitly signals it has stopped its internal logic."""
        # This might be redundant if thread_finished covers it, but can be useful
        logger.info("MainWindow: Backend signalled stopped.")
        self.update_status_bar("Backend shutdown complete.")


    def setup_chat_tab(self):
        """Sets up the WhatsApp-like chat interface."""
        layout = QHBoxLayout(self.chat_tab)
        layout.setContentsMargins(0, 0, 0, 0) # No margins for the main chat layout
        layout.setSpacing(0) # No spacing between splitter panes

        # Use QSplitter for resizable panes
        splitter = QSplitter(Qt.Orientation.Horizontal)
        layout.addWidget(splitter)

        # --- Left Pane: Peer List for Chat ---
        self.chat_peer_list = QListWidget()
        self.chat_peer_list.setObjectName("chat_peer_list")
        self.chat_peer_list.setFixedWidth(250) # Set a fixed width for the peer list
        self.chat_peer_list.currentItemChanged.connect(self.on_chat_peer_selected)
        splitter.addWidget(self.chat_peer_list)

        # --- Right Pane: Chat Area ---
        right_pane_widget = QWidget()
        right_pane_layout = QVBoxLayout(right_pane_widget)
        right_pane_layout.setContentsMargins(10, 10, 10, 10) # Padding inside the right pane
        right_pane_layout.setSpacing(10)

        # Stacked widget to hold individual chat histories/inputs
        self.chat_stack = QStackedWidget()
        right_pane_layout.addWidget(self.chat_stack, 1) # Make chat area stretch

        # Placeholder widget when no chat is selected
        self.no_chat_selected_widget = QLabel("Select a peer to start chatting.")
        self.no_chat_selected_widget.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.no_chat_selected_widget.setStyleSheet("color: #888;")
        self.chat_stack.addWidget(self.no_chat_selected_widget)

        splitter.addWidget(right_pane_widget)

        # Adjust splitter sizes (optional)
        splitter.setSizes([250, 750]) # Initial sizes for left and right panes

        # Populate initial chat peer list (will be updated by signals/timer)
        self.update_chat_peer_list()

    def create_chat_widget(self, peer_username):
        """Creates the widget containing history and input for a single peer chat."""
        chat_widget = QWidget()
        layout = QVBoxLayout(chat_widget)
        layout.setContentsMargins(0, 0, 0, 0) # No internal margins for this widget
        layout.setSpacing(10) # Space between history and input

        history = QTextEdit()
        history.setReadOnly(True)
        history.setObjectName("chat_history_" + peer_username) # Unique name
        layout.addWidget(history, 1) # Stretch history

        input_layout = QHBoxLayout()
        input_layout.setSpacing(5)
        msg_input = QLineEdit()
        msg_input.setPlaceholderText(f"Message {peer_username}...")
        msg_input.setObjectName("chat_input_" + peer_username)
        send_btn = QPushButton() # No text, just icon
        send_btn.setObjectName("chat_send_button")
        send_btn.setIcon(QIcon.fromTheme("mail-send", QIcon("./icons/send.png"))) # Provide fallback
        send_btn.setFixedSize(QSize(32, 32)) # Make button square-ish
        send_btn.setIconSize(QSize(20, 20)) # Adjust icon size within button
        send_btn.setToolTip(f"Send message to {peer_username}")

        input_layout.addWidget(msg_input)
        input_layout.addWidget(send_btn)
        layout.addLayout(input_layout)

        # Connect signals for this specific chat widget
        send_btn.clicked.connect(lambda: self.send_chat_message(peer_username))
        msg_input.returnPressed.connect(lambda: self.send_chat_message(peer_username))

        # Store references
        self.chat_widgets[peer_username] = {
            'widget': chat_widget,
            'history': history,
            'input': msg_input,
            'send_btn': send_btn
        }
        return chat_widget

    def on_chat_peer_selected(self, current, previous):
        """Switches the chat view when a peer is selected in the left list."""
        if current:
            peer_username = current.data(Qt.ItemDataRole.UserRole)
            self.current_chat_peer_username = peer_username
            logger.info(f"Chat peer selected: {peer_username}")

            # If chat widget doesn't exist, create it
            if peer_username not in self.chat_widgets:
                chat_widget = self.create_chat_widget(peer_username)
                self.chat_stack.addWidget(chat_widget)

            # Find the widget and switch to it
            widget_to_show = self.chat_widgets[peer_username]['widget']
            self.chat_stack.setCurrentWidget(widget_to_show)

        else:
            # No selection, show placeholder
            self.current_chat_peer_username = None
            self.chat_stack.setCurrentWidget(self.no_chat_selected_widget)


    def setup_transfers_tab(self):
        # (Largely same as before, ensure object names are set for styling)
        layout = QVBoxLayout(self.transfers_tab)
        layout.setSpacing(10)
        layout.setContentsMargins(15, 15, 15, 15)

        transfer_label = QLabel("Active Transfers:")
        transfer_label.setStyleSheet("font-weight: bold; font-size: 14px; margin-bottom: 5px;")
        layout.addWidget(transfer_label)

        self.transfer_list = QListWidget()
        self.transfer_list.setObjectName("transfer_list") # Set object name
        layout.addWidget(self.transfer_list, 1)

        self.progress_bar = QProgressBar()
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(True)
        layout.addWidget(self.progress_bar)

        button_layout = QHBoxLayout()
        button_layout.setSpacing(10)
        button_layout.addStretch()
        self.pause_button = QPushButton("Pause")
        self.pause_button.setObjectName("pause_button")
        self.pause_button.setIcon(QIcon.fromTheme("media-playback-pause", QIcon("./icons/pause.png")))
        self.resume_button = QPushButton("Resume")
        self.resume_button.setObjectName("resume_button")
        self.resume_button.setIcon(QIcon.fromTheme("media-playback-start", QIcon("./icons/resume.png")))
        button_layout.addWidget(self.pause_button)
        button_layout.addWidget(self.resume_button)
        layout.addLayout(button_layout)

        self.transfer_list.currentItemChanged.connect(self.on_transfer_selection_changed)
        self.pause_button.clicked.connect(self.pause_transfer)
        self.resume_button.clicked.connect(self.resume_transfer)

        self.update_transfer_list_display({}) # Initial empty state


    def setup_peers_tab(self):
        # (Largely same as before, ensure object names are set for styling)
        layout = QVBoxLayout(self.peers_tab)
        layout.setSpacing(15)
        layout.setContentsMargins(15, 15, 15, 15)

        peer_label = QLabel("Discovered Network Peers:")
        peer_label.setStyleSheet("font-weight: bold; font-size: 14px; margin-bottom: 5px;")
        layout.addWidget(peer_label)

        self.network_peer_list = QListWidget() # Renamed from self.peer_list
        self.network_peer_list.setObjectName("network_peer_list") # Set object name
        layout.addWidget(self.network_peer_list, 1)

        conn_button_layout = QHBoxLayout()
        conn_button_layout.setSpacing(10)
        conn_button_layout.addStretch()
        self.connect_button = QPushButton("Connect")
        self.connect_button.setObjectName("connect_button")
        self.connect_button.setIcon(QIcon.fromTheme("network-connect", QIcon("./icons/connect.png")))
        self.disconnect_button = QPushButton("Disconnect")
        self.disconnect_button.setObjectName("disconnect_button")
        self.disconnect_button.setIcon(QIcon.fromTheme("network-disconnect", QIcon("./icons/disconnect.png")))
        conn_button_layout.addWidget(self.connect_button)
        conn_button_layout.addWidget(self.disconnect_button)
        layout.addLayout(conn_button_layout)

        # Separator remains useful
        separator = QFrame() # Use QFrame for separator
        separator.setFrameShape(QFrame.Shape.HLine)
        separator.setFrameShadow(QFrame.Shadow.Sunken)
        separator.setStyleSheet("border-color: #444;") # Style the separator line
        layout.addWidget(separator)
        layout.addSpacing(10)

        file_label = QLabel("Send File to Selected Peer:")
        file_label.setStyleSheet("font-weight: bold; font-size: 14px; margin-bottom: 5px;")
        layout.addWidget(file_label)

        file_layout = QHBoxLayout()
        file_layout.setSpacing(10)
        self.selected_file_label = QLabel("No file chosen") # Label to show chosen file
        self.selected_file_label.setStyleSheet("color: #aaa;")
        self.choose_file_button = QPushButton("Choose File")
        self.choose_file_button.setObjectName("choose_file_button")
        self.choose_file_button.setIcon(QIcon.fromTheme("document-open", QIcon("./icons/open.png")))
        self.send_file_button = QPushButton("Send File")
        self.send_file_button.setObjectName("send_file_button")
        self.send_file_button.setIcon(QIcon.fromTheme("document-send", QIcon("./icons/send_file.png")))
        file_layout.addWidget(self.selected_file_label, 1) # Let label stretch
        file_layout.addWidget(self.choose_file_button)
        file_layout.addWidget(self.send_file_button)
        layout.addLayout(file_layout)

        self.connect_button.setEnabled(False)
        self.disconnect_button.setEnabled(False)
        self.send_file_button.setEnabled(False)

        self.network_peer_list.currentItemChanged.connect(self.on_network_peer_selection_changed)
        self.connect_button.clicked.connect(self.connect_to_selected_peer)
        self.disconnect_button.clicked.connect(self.disconnect_from_selected_peer)
        self.choose_file_button.clicked.connect(self.choose_file_action)
        self.send_file_button.clicked.connect(self.send_selected_file_action)

        self.update_peer_list_display({}) # Initial empty state


    # --- Slot Methods for UI Updates ---

    @pyqtSlot(str)
    def update_status_bar(self, message):
        """Updates the status bar message."""
        self.status_bar.showMessage(message, 5000) # Show for 5 seconds

    @pyqtSlot(dict)
    def update_peer_list_display(self, peers_status):
        """Updates the Network Peers list display based on backend signal."""
        logger.debug(f"Updating network peer list display: {peers_status}")
        self.network_peer_list.clear()
        if not peers_status:
             item = QListWidgetItem("No peers discovered")
             item.setForeground(QColor("#888"))
             item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsSelectable)
             self.network_peer_list.addItem(item)
        else:
            for ip, (username, is_connected) in peers_status.items():
                if ip == self.backend.discovery.own_ip: continue # Don't list self here
                status = " (Connected)" if is_connected else " (Disconnected)"
                item_text = f"{username} [{ip}]{status}"
                item = QListWidgetItem(item_text)
                # Store both IP and username for easier access
                item.setData(Qt.ItemDataRole.UserRole, {"ip": ip, "username": username, "connected": is_connected})
                self.network_peer_list.addItem(item)
        self.update_chat_peer_list() # Also update chat list based on connected peers
        self.on_network_peer_selection_changed(self.network_peer_list.currentItem(), None) # Re-eval button states


    def update_chat_peer_list(self):
         """Updates the Chat Peers list (only shows connected peers)."""
         self.chat_peer_list.clear()
         connected_peer_usernames = set()

         # Get connected peers from the `connections` state
         if NETWORKING_AVAILABLE:
             for ip in connections.keys():
                 # Find the primary username associated with this IP
                 uname = "Unknown"
                 for stored_uname, stored_ip in peer_usernames.items():
                     if stored_ip == ip:
                         uname = stored_uname
                         break
                 if uname != "Unknown":
                    connected_peer_usernames.add(uname)
         else: # Dummy data
              connected_peer_usernames.update(peer_usernames.keys())


         if not connected_peer_usernames:
              item = QListWidgetItem("No connected peers")
              item.setForeground(QColor("#888"))
              item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsSelectable)
              self.chat_peer_list.addItem(item)
         else:
              for username in sorted(list(connected_peer_usernames)):
                   item = QListWidgetItem(username)
                   item.setData(Qt.ItemDataRole.UserRole, username) # Store username
                   self.chat_peer_list.addItem(item)

         # Check if current chat peer is still valid
         if self.current_chat_peer_username and self.current_chat_peer_username not in connected_peer_usernames:
              logger.info(f"Current chat peer '{self.current_chat_peer_username}' disconnected.")
              self.current_chat_peer_username = None
              self.chat_stack.setCurrentWidget(self.no_chat_selected_widget)
         elif self.current_chat_peer_username:
              # Reselect the current item if it still exists to refresh view
              items = self.chat_peer_list.findItems(self.current_chat_peer_username, Qt.MatchFlag.MatchExactly)
              if items:
                   self.chat_peer_list.setCurrentItem(items[0])


    @pyqtSlot(dict)
    def update_transfer_list_display(self, transfers_info):
        """Updates the Transfers list display based on backend signal."""
        logger.debug("Updating transfer list display.")
        self.transfer_list.clear()
        current_selection_id = None
        current_item = self.transfer_list.currentItem()
        if current_item:
             current_selection_id = current_item.data(Qt.ItemDataRole.UserRole) # Get ID if item selected

        if not transfers_info:
            item = QListWidgetItem("No active transfers")
            item.setForeground(QColor("#888"))
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            self.transfer_list.addItem(item)
        else:
            new_selection_item = None
            for tid, t_info in transfers_info.items():
                file_name = os.path.basename(t_info.get('file_path', 'Unknown'))
                state = t_info.get('state', 'Unknown')
                progress = t_info.get('progress', 0)
                direction = t_info.get('direction', '??')
                peer_ip = t_info.get('peer_ip', '??')
                peer_name = get_peer_display_name(peer_ip) if NETWORKING_AVAILABLE else f"Peer_{peer_ip}"

                direction_symbol = "⬆️" if direction == "send" else "⬇️"
                item_text = f"{direction_symbol} {file_name} ({peer_name}) - {state} [{progress}%]"
                item = QListWidgetItem(item_text)
                item.setData(Qt.ItemDataRole.UserRole, tid) # Store ID
                self.transfer_list.addItem(item)
                if tid == current_selection_id:
                     new_selection_item = item # Store item to reselect

            if new_selection_item:
                 self.transfer_list.setCurrentItem(new_selection_item) # Reselect

        self.on_transfer_selection_changed(self.transfer_list.currentItem(), None) # Update button/progress states

    @pyqtSlot(str, int)
    def update_transfer_progress_display(self, transfer_id, progress):
         """Updates progress bar if the corresponding transfer is selected."""
         current_item = self.transfer_list.currentItem()
         if current_item and current_item.data(Qt.ItemDataRole.UserRole) == transfer_id:
              self.progress_bar.setValue(progress)
              # Also update the text in the list item?
              # This can be slow if updates are very frequent. Consider throttling.
              # item_text = current_item.text() # Needs parsing and updating carefully


    @pyqtSlot(str, str)
    def display_received_message(self, sender_display_name, message):
        """Displays a received message in the correct chat window."""
        timestamp = time.strftime("%H:%M:%S")
        formatted_message = f"[{timestamp}] {sender_display_name}: {message}"

        # Find the base username (without device ID) to match chat_widgets key
        base_sender_username = sender_display_name.split("(")[0]

        if base_sender_username in self.chat_widgets:
            history_widget = self.chat_widgets[base_sender_username]['history']
            history_widget.append(formatted_message)
            history_widget.ensureCursorVisible() # Scroll down
            # Optional: Indicate unread if not the current chat
            if self.current_chat_peer_username != base_sender_username:
                 items = self.chat_peer_list.findItems(base_sender_username, Qt.MatchFlag.MatchExactly)
                 if items:
                      # Modify item appearance (e.g., bold font) - requires custom delegate or careful styling
                      font = items[0].font()
                      font.setBold(True)
                      items[0].setFont(font)
                      items[0].setForeground(QColor(self.backend.accent_color)) # Use theme color

        else:
            # Handle message from a peer we don't have a chat widget for yet?
            # Could be a broadcast message, or from a newly connected peer.
            # Append to a general log or create chat widget dynamically?
            logger.warning(f"Received message from '{sender_display_name}' but no chat widget found.")
            # For now, maybe log it to status bar?
            self.update_status_bar(f"Message from {sender_display_name}: {message[:30]}...")


    def display_sent_message(self, recipient_display_name, message):
        """Displays a message sent by the user."""
        timestamp = time.strftime("%H:%M:%S")
        # Use own display name
        own_name = get_own_display_name() if NETWORKING_AVAILABLE else self.username
        formatted_message = f"[{timestamp}] {own_name}: {message}"

        # Find the base username of the recipient
        base_recipient_username = recipient_display_name.split("(")[0]

        if base_recipient_username in self.chat_widgets:
            history_widget = self.chat_widgets[base_recipient_username]['history']
            # Prepend with a different style/color?
            # history_widget.setTextColor(QColor("#aaddaa")) # Light green for sent?
            history_widget.append(formatted_message)
            # history_widget.setTextColor(QColor(self.backend.text_color)) # Reset color
            history_widget.ensureCursorVisible()
        else:
            logger.warning(f"Sent message to '{recipient_display_name}' but no chat widget found.")


    @pyqtSlot(str, bool)
    def handle_connection_status_update(self, peer_ip, is_connected):
        """Handles backend signals about connection changes."""
        logger.info(f"Connection status update: IP={peer_ip}, Connected={is_connected}")
        # We already trigger a full peer list update, which handles the display.
        # This slot could be used for more specific actions if needed,
        # like showing a notification.
        peer_name = get_peer_display_name(peer_ip) if NETWORKING_AVAILABLE else f"Peer_{peer_ip}"
        status_msg = f"{peer_name} has {'connected' if is_connected else 'disconnected'}."
        self.update_status_bar(status_msg)
        # Update the chat peer list as well
        self.update_chat_peer_list()


    @pyqtSlot(str, str)
    def show_connection_request(self, requesting_display_name, base_username_for_cmd):
         """Shows a popup asking the user to approve/deny a connection."""
         reply = QMessageBox.question(
             self,
             "Connection Request",
             f"Incoming connection request from:\n\n{requesting_display_name}\n\nDo you want to accept?",
             QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
             QMessageBox.StandardButton.No # Default to No
         )

         if reply == QMessageBox.StandardButton.Yes:
             logger.info(f"User approved connection from {requesting_display_name}")
             # Find the peer_ip associated with the requesting_username (might need a lookup)
             # This is tricky because the backend approval needs the IP.
             # The backend should ideally handle the mapping. Let's assume backend can find IP.
             # We need the original requesting_username and IP for the backend call.
             # Let's refine the signal or backend logic if needed.
             # For now, assume backend handles approval based on username from the signal? Risky.
             # Let's modify the backend signal/logic if this doesn't work.
             # --- Assuming backend needs IP ---
             # We need to find the IP matching base_username_for_cmd from discovery list maybe?
             peer_ip_to_approve = None
             if self.backend.discovery:
                  for ip, (uname, _) in self.backend.discovery.peer_list.items():
                       if uname == base_username_for_cmd:
                            peer_ip_to_approve = ip
                            break

             if peer_ip_to_approve:
                 self.backend.approve_connection(peer_ip_to_approve, base_username_for_cmd) # Use base username? Backend needs clarity.
                 self.update_status_bar(f"Approved connection from {requesting_display_name}")
             else:
                  logger.error(f"Could not find IP for pending approval: {base_username_for_cmd}")
                  self.update_status_bar(f"Error approving {requesting_display_name}: IP not found.")

         else:
             logger.info(f"User denied connection from {requesting_display_name}")
             # Find IP again...
             peer_ip_to_deny = None
             if self.backend.discovery:
                   for ip, (uname, _) in self.backend.discovery.peer_list.items():
                       if uname == base_username_for_cmd:
                            peer_ip_to_deny = ip
                            break
             if peer_ip_to_deny:
                 self.backend.deny_connection(peer_ip_to_deny, base_username_for_cmd) # Use base username?
                 self.update_status_bar(f"Denied connection from {requesting_display_name}")
             else:
                   logger.error(f"Could not find IP for pending denial: {base_username_for_cmd}")
                   self.update_status_bar(f"Error denying {requesting_display_name}: IP not found.")


    # --- Methods Triggered by User Actions ---

    def on_network_peer_selection_changed(self, current, previous):
        """Enable/disable buttons based on network peer selection and connection status."""
        can_connect = False
        can_disconnect = False
        can_send_file = False

        if current:
            peer_data = current.data(Qt.ItemDataRole.UserRole)
            if peer_data:
                is_connected = peer_data.get("connected", False)
                can_connect = not is_connected
                can_disconnect = is_connected
                can_send_file = is_connected and self.selected_file is not None

        self.connect_button.setEnabled(can_connect)
        self.disconnect_button.setEnabled(can_disconnect)
        self.send_file_button.setEnabled(can_send_file)

    def on_transfer_selection_changed(self, current, previous):
        """Enable/disable pause/resume based on transfer selection and state."""
        can_pause = False
        can_resume = False
        progress = 0

        if current:
            transfer_id = current.data(Qt.ItemDataRole.UserRole)
            if NETWORKING_AVAILABLE and transfer_id in active_transfers:
                # Get state from the *actual* transfer object
                transfer_obj = active_transfers[transfer_id]
                state_value = getattr(getattr(transfer_obj, 'state', None), 'value', 'Unknown')
                progress = int((getattr(transfer_obj, 'transferred_size', 0) / getattr(transfer_obj, 'total_size', 1)) * 100) if getattr(transfer_obj, 'total_size', 0) > 0 else 0
                is_active = state_value == TransferState.IN_PROGRESS.value
                is_paused = state_value == TransferState.PAUSED.value
                can_pause = is_active
                can_resume = is_paused
            elif not NETWORKING_AVAILABLE and transfer_id in active_transfers: # Dummy mode
                 state_value = active_transfers[transfer_id].state.value
                 progress = active_transfers[transfer_id].progress
                 can_pause = state_value == "Sending" # Example dummy logic
                 can_resume = state_value == "Paused"


        self.pause_button.setEnabled(can_pause)
        self.resume_button.setEnabled(can_resume)
        self.progress_bar.setValue(progress)

    def connect_to_selected_peer(self):
        selected_item = self.network_peer_list.currentItem()
        if selected_item:
            peer_data = selected_item.data(Qt.ItemDataRole.UserRole)
            peer_ip = peer_data.get("ip")
            target_username = peer_data.get("username")
            if not peer_ip or not target_username:
                self.update_status_bar("Error: Peer data incomplete.")
                return

            self.update_status_bar(f"Connecting to {target_username}...")
            # Use the backend trigger method
            self.backend.trigger_connect_to_peer(peer_ip, self.username, target_username)
        else:
            self.update_status_bar("No peer selected.")


    def disconnect_from_selected_peer(self):
        selected_item = self.network_peer_list.currentItem()
        if selected_item:
            peer_data = selected_item.data(Qt.ItemDataRole.UserRole)
            peer_ip = peer_data.get("ip")
            target_username = peer_data.get("username", peer_ip) # Fallback display
            if not peer_ip:
                 self.update_status_bar("Error: Peer IP not found.")
                 return

            self.update_status_bar(f"Disconnecting from {target_username}...")
            self.backend.trigger_disconnect_from_peer(peer_ip)
        else:
            self.update_status_bar("No peer selected.")

    def send_chat_message(self, peer_username):
        """Sends the message from the input field of the specified chat."""
        if not peer_username or peer_username not in self.chat_widgets:
             logger.error(f"Attempted to send message from invalid chat: {peer_username}")
             return

        widgets = self.chat_widgets[peer_username]
        message = widgets['input'].text().strip()
        if not message:
            return # Don't send empty

        # Display locally first
        self.display_sent_message(peer_username, message) # Use base username for display lookup

        # Find target IP
        target_ip = None
        if NETWORKING_AVAILABLE:
             for uname, ip in peer_usernames.items():
                 if uname == peer_username:
                      target_ip = ip
                      break
        else: # Dummy mode
             target_ip = f"dummy_ip_{peer_username}"

        if target_ip:
            # Send via backend
            success = self.backend.trigger_send_message(message, target_peer_ip=target_ip)
            if success:
                 widgets['input'].clear()
                 # self.update_status_bar(f"Message sent to {peer_username}") # Maybe too verbose
            else:
                 self.update_status_bar(f"Failed to send message to {peer_username}")
        else:
            self.update_status_bar(f"Error: Could not find IP for {peer_username}")


    def choose_file_action(self):
        """Handles the 'Choose File' button click."""
        selected_file_path = self.backend.choose_file(self) # Runs in GUI thread
        if selected_file_path:
            self.selected_file = selected_file_path
            self.selected_file_label.setText(os.path.basename(self.selected_file))
            self.selected_file_label.setStyleSheet("color: #e0e0e0;") # Regular text color
            self.update_status_bar(f"File chosen: {os.path.basename(self.selected_file)}")
            # Enable send button only if a peer is also selected
            self.on_network_peer_selection_changed(self.network_peer_list.currentItem(), None)
        else:
            self.selected_file = None
            self.selected_file_label.setText("No file chosen")
            self.selected_file_label.setStyleSheet("color: #aaa;") # Dim color
            self.update_status_bar("File selection cancelled.")
            self.send_file_button.setEnabled(False)

    def send_selected_file_action(self):
        """Handles the 'Send File' button click."""
        selected_item = self.network_peer_list.currentItem()
        if not self.selected_file:
             self.update_status_bar("No file chosen to send.")
             return
        if not selected_item:
             self.update_status_bar("No peer selected to send file to.")
             return

        peer_data = selected_item.data(Qt.ItemDataRole.UserRole)
        peer_ip = peer_data.get("ip")
        username = peer_data.get("username", peer_ip)
        if not peer_ip:
             self.update_status_bar(f"Cannot send file: IP not found for {username}.")
             return

        # Check connection status from peer_data (which should be up-to-date)
        if not peer_data.get("connected", False):
             self.update_status_bar(f"Cannot send file: Not connected to {username}.")
             return

        # Get the actual websocket connection object from shared state
        peer_ws = connections.get(peer_ip) if NETWORKING_AVAILABLE else object() # Dummy object
        if not peer_ws:
             self.update_status_bar(f"Cannot send file: Connection object missing for {username}.")
             logger.error(f"Connection object missing for {peer_ip} despite listed as connected.")
             return

        peers_dict = {peer_ip: peer_ws}
        file_basename = os.path.basename(self.selected_file)
        self.update_status_bar(f"Initiating send {file_basename} to {username}...")

        # Use backend trigger
        success = self.backend.trigger_send_file(self.selected_file, peers_dict)
        if success:
             # Optionally clear selection? Or just wait for transfer list update
             # self.selected_file = None
             # self.selected_file_label.setText("No file chosen")
             # self.send_file_button.setEnabled(False)
             pass # Wait for transfer list update signal
        else:
             self.update_status_bar(f"Failed to start sending {file_basename} to {username}.")


    def pause_transfer(self):
        # TODO: Implement backend call via trigger_... method
        selected_item = self.transfer_list.currentItem()
        if selected_item:
            transfer_id = selected_item.data(Qt.ItemDataRole.UserRole)
            self.update_status_bar(f"Requesting pause for transfer {transfer_id[:8]}...")
            logger.info(f"UI Request: Pause transfer {transfer_id}")
            # Example: self.backend.trigger_pause_transfer(transfer_id)
        else:
            self.update_status_bar("No transfer selected.")

    def resume_transfer(self):
        # TODO: Implement backend call via trigger_... method
        selected_item = self.transfer_list.currentItem()
        if selected_item:
            transfer_id = selected_item.data(Qt.ItemDataRole.UserRole)
            self.update_status_bar(f"Requesting resume for transfer {transfer_id[:8]}...")
            logger.info(f"UI Request: Resume transfer {transfer_id}")
            # Example: self.backend.trigger_resume_transfer(transfer_id)
        else:
            self.update_status_bar("No transfer selected.")

    def show_about_dialog(self):
        own_name = get_own_display_name() if NETWORKING_AVAILABLE else self.username
        QMessageBox.about(self, "About P2P Chat",
                          f"P2P Chat Application\nVersion 0.2\nUser: {own_name}\n\n"
                          "Built with PyQt6 and Python asyncio.")

    def periodic_ui_update(self):
        """Periodically requests updates from the backend."""
        logger.debug("Periodic UI update triggered.")
        self.backend.emit_peer_list_update()
        self.backend.emit_transfers_update()


    def apply_styles(self):
        """Apply the dark theme style sheet."""
        # (Same stylesheet generation as before)
        font_family = "Segoe UI, Arial, sans-serif"
        dark_bg="#1e1e1e"; medium_bg="#252526"; light_bg="#2d2d2d"; dark_border="#333333"; medium_border="#444444"; text_color="#e0e0e0"; dim_text_color="#a0a0a0"; accent_color="#ff6600"; accent_hover="#e65c00"; accent_pressed="#cc5200"; secondary_btn_bg="#555555"; secondary_btn_hover="#666666"; secondary_btn_pressed="#444444"

        stylesheet_template = """
            QMainWindow {{ background-color: {dark_bg}; color: {text_color}; font-family: {font_family}; }}
            QWidget {{ color: {text_color}; font-size: 13px; }}

            QTabWidget::pane {{ border: none; background-color: {medium_bg}; }}
            QTabBar::tab {{ background: {dark_border}; color: {dim_text_color}; border: none; padding: 10px 20px; font-size: 14px; font-weight: bold; margin-right: 2px; border-top-left-radius: 5px; border-top-right-radius: 5px; }}
            QTabBar::tab:selected {{ background: {accent_color}; color: #ffffff; }}
            QTabBar::tab:!selected {{ margin-top: 2px; padding: 8px 20px; background: #3a3a3a; }} /* Darker inactive tabs */
            QTabBar::tab:!selected:hover {{ background: {medium_border}; color: {text_color}; }}

            /* Lists: Network Peers, Chat Peers, Transfers */
            QListWidget {{ background-color: {medium_bg}; border: 1px solid {dark_border}; border-radius: 5px; padding: 5px; font-size: 14px; outline: none; }}
            QListWidget::item {{ padding: 7px 5px; border-radius: 3px; }}
            QListWidget::item:selected {{ background-color: {accent_color}; color: #000000; font-weight: bold; }} /* Black text on orange */
            QListWidget::item:!selected:hover {{ background-color: {medium_border}; }}
            QListWidget#chat_peer_list {{ border-right: 2px solid {dark_border}; }} /* Separator line */

            /* Chat History */
            QTextEdit[objectName^="chat_history"] {{ background-color: {medium_bg}; border: none; padding: 10px; font-size: 14px; color: {text_color}; }}

            /* Input Fields */
            QLineEdit {{ background-color: {light_bg}; border: 1px solid {dark_border}; border-radius: 5px; padding: 8px; font-size: 14px; color: {text_color}; }}
            QLineEdit:focus {{ border: 1px solid {accent_color}; }}
            QLineEdit[objectName^="chat_input"] {{ border-radius: 15px; padding-left: 15px; padding-right: 10px; }} /* Rounded chat input */

            QPushButton {{ background-color: {accent_color}; color: white; border: none; border-radius: 5px; padding: 8px 15px; font-size: 14px; font-weight: bold; min-width: 90px; outline: none; }}
            QPushButton:hover {{ background-color: {accent_hover}; }}
            QPushButton:pressed {{ background-color: {accent_pressed}; }}
            QPushButton:disabled {{ background-color: #554433; color: #aaaaaa; }}

            /* Specific Buttons */
            QPushButton#disconnect_button, QPushButton#choose_file_button, QPushButton#pause_button {{ background-color: transparent; border: 1px solid {accent_color}; color: {accent_color}; }}
            QPushButton#disconnect_button:hover, QPushButton#choose_file_button:hover, QPushButton#pause_button:hover {{ background-color: rgba(255, 102, 0, 0.1); color: {accent_hover}; border-color: {accent_hover}; }}
            QPushButton#disconnect_button:pressed, QPushButton#choose_file_button:pressed, QPushButton#pause_button:pressed {{ background-color: rgba(255, 102, 0, 0.2); color: {accent_pressed}; border-color: {accent_pressed}; }}
            QPushButton#disconnect_button:disabled, QPushButton#choose_file_button:disabled, QPushButton#pause_button:disabled {{ background-color: transparent; border-color: #666; color: #666; }}

            /* Chat Send Button (Icon Only) */
            QPushButton#chat_send_button {{ background-color: {accent_color}; border-radius: 16px; /* Make it round */ min-width: 32px; padding: 0; }}
             QPushButton#chat_send_button:hover {{ background-color: {accent_hover}; }}
             QPushButton#chat_send_button:pressed {{ background-color: {accent_pressed}; }}

            QProgressBar {{ border: 1px solid {dark_border}; border-radius: 5px; text-align: center; font-size: 12px; font-weight: bold; color: {text_color}; background-color: {light_bg}; }}
            QProgressBar::chunk {{ background-color: {accent_color}; border-radius: 4px; margin: 1px; }}

            QStatusBar {{ background-color: {dark_bg}; color: {dim_text_color}; font-size: 12px; border-top: 1px solid {dark_border}; }}
            QStatusBar::item {{ border: none; }}

            QMenuBar {{ background-color: {medium_bg}; color: {text_color}; border-bottom: 1px solid {dark_border}; }}
            QMenuBar::item {{ background: transparent; padding: 5px 10px; font-size: 13px; }}
            QMenuBar::item:selected {{ background: {medium_border}; }}
            QMenu {{ background-color: {medium_bg}; border: 1px solid {medium_border}; color: {text_color}; padding: 5px; }}
            QMenu::item {{ padding: 8px 20px; }}
            QMenu::item:selected {{ background-color: {accent_color}; color: #000000; }} /* Black text on orange */
            QMenu::separator {{ height: 1px; background: {medium_border}; margin: 5px 10px; }}

            QSplitter::handle {{ background-color: {dark_border}; }} /* Handle color */
            QSplitter::handle:horizontal {{ width: 1px; }}
            QSplitter::handle:vertical {{ height: 1px; }}
            QSplitter::handle:pressed {{ background-color: {accent_color}; }}

            QScrollBar:vertical {{ border: none; background: {medium_bg}; width: 10px; margin: 0px; }}
            QScrollBar::handle:vertical {{ background: {medium_border}; min-height: 20px; border-radius: 5px; }}
            QScrollBar::handle:vertical:hover {{ background: #555; }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ border: none; background: none; height: 0px; }}

            QScrollBar:horizontal {{ border: none; background: {medium_bg}; height: 10px; margin: 0px; }}
            QScrollBar::handle:horizontal {{ background: {medium_border}; min-width: 20px; border-radius: 5px; }}
            QScrollBar::handle:horizontal:hover {{ background: #555; }}
            QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ border: none; background: none; width: 0px; }}

            QLabel {{ /* Default label */ color: {text_color}; padding-bottom: 2px; }}
            QLabel#error_label {{ color: #FFAAAA; font-size: 12px; qproperty-alignment: 'AlignCenter'; }} /* Login error */
        """
        self.setStyleSheet(stylesheet_template.format(
            dark_bg=dark_bg, medium_bg=medium_bg, light_bg=light_bg, dark_border=dark_border,
            medium_border=medium_border, text_color=text_color, dim_text_color=dim_text_color,
            accent_color=accent_color, accent_hover=accent_hover, accent_pressed=accent_pressed,
            font_family=font_family
        ))
        font = QFont(font_family.split(',')[0].strip(), 10)
        QApplication.instance().setFont(font)


# --- Main Execution ---
if __name__ == "__main__":
    app = QApplication(sys.argv)

    # Set Fusion style for better cross-platform consistency
    app.setStyle("Fusion")

    # Apply a dark palette globally *before* creating widgets
    dark_palette = QPalette()
    dark_palette.setColor(QPalette.ColorRole.Window, QColor(30, 30, 30)) # dark_bg
    dark_palette.setColor(QPalette.ColorRole.WindowText, QColor(224, 224, 224)) # text_color
    dark_palette.setColor(QPalette.ColorRole.Base, QColor(45, 45, 45)) # light_bg (inputs)
    dark_palette.setColor(QPalette.ColorRole.AlternateBase, QColor(37, 37, 38)) # medium_bg
    dark_palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(30, 30, 30))
    dark_palette.setColor(QPalette.ColorRole.ToolTipText, QColor(224, 224, 224))
    dark_palette.setColor(QPalette.ColorRole.Text, QColor(224, 224, 224))
    dark_palette.setColor(QPalette.ColorRole.Button, QColor(37, 37, 38)) # medium_bg for button background
    dark_palette.setColor(QPalette.ColorRole.ButtonText, QColor(224, 224, 224))
    dark_palette.setColor(QPalette.ColorRole.BrightText, QColor(255, 102, 0)) # accent_color
    dark_palette.setColor(QPalette.ColorRole.Link, QColor(42, 130, 218)) # Standard link blue
    dark_palette.setColor(QPalette.ColorRole.Highlight, QColor(255, 102, 0)) # accent_color for selection
    dark_palette.setColor(QPalette.ColorRole.HighlightedText, QColor(0, 0, 0)) # Black text on orange highlight

    dark_palette.setColor(QPalette.ColorRole.PlaceholderText, QColor(160, 160, 160)) # dim_text_color

    # Set disabled colors more distinctly
    disabled_text = QColor(120, 120, 120)
    disabled_button = QColor(60, 60, 60)
    dark_palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, disabled_text)
    dark_palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText, disabled_text)
    dark_palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, disabled_text)
    dark_palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Button, disabled_button)
    dark_palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Base, QColor(40,40,40))


    app.setPalette(dark_palette)

    # Set global application info (optional)
    app.setApplicationName("P2PChat")
    app.setOrganizationName("YourOrg") # Replace if desired

    # Set default window icon (replace path/use theme)
    app.setWindowIcon(QIcon.fromTheme("network-transmit-receive", QIcon("./icons/app_icon.png")))

    login_window = LoginWindow()
    login_window.show()
    exit_code = app.exec()
    logger.info(f"Application exiting with code {exit_code}")
    sys.exit(exit_code)
