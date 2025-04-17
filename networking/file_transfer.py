import asyncio
import os
import uuid
import aiofiles
import hashlib
import logging
import json
import time
import websockets 
from enum import Enum

from networking.shared_state import (
    active_transfers, shutdown_event, message_queue,
    active_transfers_lock, outgoing_transfers_by_peer
)
from websockets.connection import State
from utils.file_validation import check_file_size, check_disk_space, safe_close_file

logger = logging.getLogger(__name__)

class TransferState(Enum):
    IN_PROGRESS = "in_progress"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"

class FileTransfer:
    def __init__(self, file_path, peer_ip, direction="send", transfer_id=None): # Allow passing ID
        self.file_path = file_path; self.peer_ip = peer_ip; self.direction = direction
        self.transfer_id = transfer_id if transfer_id else str(uuid.uuid4())
        self.total_size = 0; self.transferred_size = 0; self.file_handle = None
        self.state = TransferState.IN_PROGRESS; self.hash_algo = hashlib.sha256()
        self.expected_hash = None; self.condition = asyncio.Condition(); self.start_time = time.time()
        try:
            if direction == "send" and os.path.exists(file_path): self.total_size = os.path.getsize(file_path)
        except OSError as e: logger.error(f"Error getting size for {file_path}: {e}"); self.state = TransferState.FAILED

    async def pause(self):
        """Pauses a transfer and notifies the peer if we're the sender."""
        paused_successfully = False
        
        async with self.condition:
            if self.state == TransferState.IN_PROGRESS:
                self.state = TransferState.PAUSED
                paused_successfully = True
                logger.info(f"Transfer {self.transfer_id[:8]} paused.")
                
                # Notify UI about state change within the lock
                await message_queue.put({"type": "transfer_update"})
                
                # Send pause notification to peer if we're the sender
                if self.direction == "send":
                    try:
                        from networking.shared_state import connections
                        ws = connections.get(self.peer_ip)
                        if ws and ws.state == State.OPEN:
                            pause_msg = json.dumps({
                                "type": "TRANSFER_PAUSE",
                                "transfer_id": self.transfer_id
                            })
                            await ws.send(pause_msg)
                            logger.info(f"Sent pause notification to peer for transfer {self.transfer_id[:8]}")
                        else:
                            logger.warning(f"Could not notify peer about pause: connection unavailable for {self.peer_ip}")
                    except Exception as e:
                        logger.error(f"Error sending pause notification to peer: {e}")
                        # Continue with local pause even if notification fails
            else:
                logger.warning(f"Cannot pause transfer {self.transfer_id[:8]}: not in progress (current state: {self.state.value})")
        
        # Outside the lock, send progress update if we successfully paused
        if paused_successfully:
            await message_queue.put({
                "type": "transfer_progress",
                "transfer_id": self.transfer_id,
                "progress": int((self.transferred_size / self.total_size) * 100) if self.total_size > 0 else 0,
                "state": "paused"  # Add state information
            })
        
        return paused_successfully

    async def resume(self):
        """Resumes a paused transfer and notifies the peer if we're the sender."""
        resumed_successfully = False
        
        async with self.condition:
            if self.state == TransferState.PAUSED:
                self.state = TransferState.IN_PROGRESS
                resumed_successfully = True
                logger.info(f"Transfer {self.transfer_id[:8]} resumed.")
                
                # Notify UI about state change within the lock
                await message_queue.put({"type": "transfer_update"})
                
                # Send resume notification to peer if we're the sender
                if self.direction == "send":
                    try:
                        from networking.shared_state import connections
                        ws = connections.get(self.peer_ip)
                        if ws and ws.state == State.OPEN:
                            resume_msg = json.dumps({
                                "type": "TRANSFER_RESUME",
                                "transfer_id": self.transfer_id
                            })
                            await ws.send(resume_msg)
                            logger.info(f"Sent resume notification to peer for transfer {self.transfer_id[:8]}")
                        else:
                            logger.warning(f"Could not notify peer: connection unavailable for {self.peer_ip}")
                    except Exception as e:
                        logger.error(f"Error sending resume notification to peer: {e}")
                        # Continue with local resume even if notification fails
                
                # Notify all waiters
                self.condition.notify_all()
            else:
                logger.warning(f"Cannot resume transfer {self.transfer_id[:8]}: not paused (current state: {self.state.value})")
        
        # Outside the lock, send progress update if we successfully resumed
        if resumed_successfully:
            await message_queue.put({
                "type": "transfer_progress",
                "transfer_id": self.transfer_id,
                "progress": int((self.transferred_size / self.total_size) * 100) if self.total_size > 0 else 0,
                "state": "in_progress"  # Add state information
            })
        
        return resumed_successfully

    async def fail(self, reason="Unknown"):
        """Marks a transfer as failed and performs cleanup."""
        async with self.condition:
            if self.state not in [TransferState.COMPLETED, TransferState.FAILED]:
                logger.error(f"Transfer {self.transfer_id[:8]} failed: {reason}")
                self.state = TransferState.FAILED
                
                # Close file handle if open
                if self.file_handle:
                    safe_close_file(self.file_handle)
                    self.file_handle = None
                
                # Notify UI about state change
                await message_queue.put({"type": "transfer_update"})
        return True

async def compute_hash(file_path):
    """Compute the SHA-256 hash of a file asynchronously."""
    hash_algo = hashlib.sha256()
    try:
        async with aiofiles.open(file_path, "rb") as f:
            while True:
                chunk = await f.read(1024 * 1024) # Read in 1MB chunks
                if not chunk:
                    break
                hash_algo.update(chunk) # Update hash ONLY if chunk has data
        return hash_algo.hexdigest()
    except FileNotFoundError: logger.error(f"File not found during hash computation: {file_path}"); return None
    except Exception as e: logger.error(f"Error computing hash for {file_path}: {e}", exc_info=True); return None

async def send_file(file_path, peers):
    """Send a file to specified peers (assuming single peer in current GUI)."""
    if not os.path.isfile(file_path):
        await message_queue.put({"type": "log", "message": f"Send Error: File not found '{file_path}'", "level": logging.ERROR})
        return
    if not peers:
        await message_queue.put({"type": "log", "message": "Send Error: No peer specified.", "level": logging.ERROR})
        return

    # Validate file size
    is_valid, message = check_file_size(file_path)
    if not is_valid:
        await message_queue.put({"type": "log", "message": f"Send Error: {message}", "level": logging.ERROR})
        return

    peer_ip, websocket = next(iter(peers.items()))
    if not websocket or websocket.state != State.OPEN:
         await message_queue.put({"type": "log", "message": f"Send Error: Peer {peer_ip} not connected.", "level": logging.ERROR})
         return

    # Check if there's already an outgoing transfer to this peer
    async with active_transfers_lock:
        if peer_ip in outgoing_transfers_by_peer:
            existing_transfer_id = outgoing_transfers_by_peer[peer_ip]
            await message_queue.put({
                "type": "log", 
                "message": f"Send Error: Already sending a file to this peer (Transfer ID: {existing_transfer_id[:8]}). Please wait for it to complete.", 
                "level": logging.ERROR
            })
            return

    transfer_id = str(uuid.uuid4())
    file_name = os.path.basename(file_path)
    file_size = os.path.getsize(file_path)
    file_hash = await compute_hash(file_path)
    if file_hash is None:
        await message_queue.put({"type": "log", "message": f"Send Error: Could not compute hash for '{file_name}'.", "level": logging.ERROR})
        return

    transfer = FileTransfer(file_path, peer_ip, direction="send", transfer_id=transfer_id)
    if transfer.state == TransferState.FAILED:
         await message_queue.put({"type": "log", "message": f"Send Error: Could not initialize transfer for '{file_name}'.", "level": logging.ERROR})
         return

    transfer.total_size = file_size
    transfer.expected_hash = file_hash

    # Register this as an outgoing transfer to this peer
    async with active_transfers_lock:
        outgoing_transfers_by_peer[peer_ip] = transfer_id
        active_transfers[transfer_id] = transfer

    await message_queue.put({"type": "transfer_update"})
    await message_queue.put({"type": "log", "message": f"Starting send '{file_name}' to {peer_ip} (ID: {transfer_id[:8]})"})

    init_message = json.dumps({
        "type": "file_transfer_init", "transfer_id": transfer_id,
        "filename": file_name, "filesize": file_size, "file_hash": file_hash
    })

    try:
        await websocket.send(init_message)
    except Exception as e:
        logger.error(f"Failed to send file init to {peer_ip}: {e}")
        if transfer_id in active_transfers: del active_transfers[transfer_id]
        await message_queue.put({"type": "transfer_update"})
        await message_queue.put({"type": "log", "message": f"Send Error: Failed to initiate transfer with {peer_ip}.", "level": logging.ERROR})
        return
    await message_queue.put({"type": "transfer_update"})
    try:
        async with aiofiles.open(file_path, "rb") as f:
            transfer.file_handle = f
            chunk_size = 1024 * 1024
            while not shutdown_event.is_set() and transfer.state != TransferState.FAILED:
                try:
                    # Handle pause state efficiently
                    if transfer.state == TransferState.PAUSED:
                        logger.info(f"Transfer {transfer_id[:8]} is paused, waiting to resume...")
                        
                        # Use a series of short waits instead of one long wait
                        # This allows for more responsive resuming
                        pause_wait_count = 0
                        while transfer.state == TransferState.PAUSED and not shutdown_event.is_set():
                            try:
                                async with transfer.condition:
                                    # Wait with a shorter timeout to prevent deadlocks
                                    await asyncio.wait_for(transfer.condition.wait(), timeout=5.0)
                            except asyncio.TimeoutError:
                                # If waiting times out, simply check state again
                                pause_wait_count += 1
                                if pause_wait_count % 12 == 0:  # Log only every minute (12 x 5 seconds)
                                    logger.debug(f"Still waiting for resume on transfer {transfer_id[:8]}")
                        
                        # After pause loop, check if we should continue
                        if shutdown_event.is_set() or transfer.state == TransferState.FAILED:
                            logger.info(f"Transfer {transfer_id[:8]} not continuing after pause: shutdown={shutdown_event.is_set()}, state={transfer.state}")
                            break
                        if transfer.state == TransferState.IN_PROGRESS:
                            logger.info(f"Transfer {transfer_id[:8]} resumed, continuing...")
                        else:
                            logger.warning(f"Transfer {transfer_id[:8]} in unexpected state after pause: {transfer.state}")
                            await asyncio.sleep(1.0)
                            continue

                    # Read chunk outside the lock
                    chunk = await f.read(chunk_size)
                    if not chunk:
                        # End of file reached
                        async with transfer.condition:
                            transfer.state = TransferState.COMPLETED
                        await asyncio.sleep(0.1)  # Allow last chunk send to process
                        break

                    # Update transfer size under lock
                    async with transfer.condition:
                        # Verify state hasn't changed during file read
                        if transfer.state != TransferState.IN_PROGRESS:
                            logger.debug(f"Transfer {transfer_id[:8]} state changed during read to {transfer.state}")
                            continue
                            
                        transfer.transferred_size += len(chunk)
                        transfer.hash_algo.update(chunk)  # Update hash if being calculated

                    # Check connection state before sending
                    if websocket.state != State.OPEN:
                        logger.warning(f"Peer {peer_ip} disconnected during send.")
                        await transfer.fail("Peer disconnected during send")
                        break

                    # Send with timeout and error handling
                    try:
                        await asyncio.wait_for(websocket.send(chunk), timeout=30.0)
                    except asyncio.TimeoutError:
                        logger.error(f"Timeout sending chunk to {peer_ip}")
                        await transfer.fail("Send timeout")
                        break
                    except websockets.exceptions.ConnectionClosed as e:
                        logger.warning(f"Connection closed during send: {e}")
                        await transfer.fail("Connection closed during send")
                        break
                    except Exception as e:
                        logger.error(f"Error sending chunk: {e}", exc_info=True)
                        await transfer.fail(f"Send error: {e}")
                        break

                    # Throttle sending if needed (can be adjusted based on network conditions)
                    await asyncio.sleep(0.001)
                except asyncio.CancelledError:
                    logger.info(f"Transfer {transfer_id[:8]} cancelled during send")
                    await transfer.fail("Transfer cancelled")
                    break
                except Exception as e:
                    logger.exception(f"Unexpected error in transfer loop for {transfer_id[:8]}")
                    await transfer.fail(f"Unexpected error: {e}")
                    break

            # --- Loop finished ---
            if transfer.state == TransferState.COMPLETED:
                logger.info(f"File transfer {transfer_id[:8]} completed sending.")
                await message_queue.put({"type": "log", "message": f"Sent '{file_name}' successfully."})
            elif shutdown_event.is_set():
                 await message_queue.put({"type": "log", "message": f"Send '{file_name}' cancelled by shutdown."})
                 await transfer.fail("Shutdown initiated")
            # Failure case handled within loop by calling transfer.fail()
    except Exception as e:
        logger.exception(f"Error during file send for {transfer_id[:8]}")
        await transfer.fail(f"Send error: {e}")
    finally:
        if transfer.file_handle:
            safe_close_file(transfer.file_handle)
            transfer.file_handle = None
        # Ensure final state update is sent to GUI
        await message_queue.put({"type": "transfer_update"})
        # Remove from outgoing transfers tracking
        async with active_transfers_lock:
            if peer_ip in outgoing_transfers_by_peer and outgoing_transfers_by_peer[peer_ip] == transfer_id:
                outgoing_transfers_by_peer.pop(peer_ip, None)

async def update_transfer_progress():
    """Periodically send transfer progress updates to the GUI queue."""
    while not shutdown_event.is_set():
        try:
            transfers_to_remove = []
            updated = False

            # Use lock when accessing active_transfers
            async with active_transfers_lock:
                # Use items() for safe iteration if active_transfers might change during iteration
                for transfer_id, transfer in list(active_transfers.items()):
                    if transfer.state in (TransferState.COMPLETED, TransferState.FAILED):
                        transfers_to_remove.append(transfer_id)
                        updated = True # Mark for full update
                    elif transfer.state == TransferState.IN_PROGRESS and transfer.total_size > 0:
                        progress = int((transfer.transferred_size / transfer.total_size) * 100)
                        await message_queue.put({
                            "type": "transfer_progress",
                            "transfer_id": transfer_id,
                            "progress": progress
                        })
                
                # Do removals under the same lock
                if transfers_to_remove:
                    for tid in transfers_to_remove:
                        # Check again before deleting, state might have changed
                        if tid in active_transfers and active_transfers[tid].state in (TransferState.COMPLETED, TransferState.FAILED):
                            # Also clean up outgoing_transfers_by_peer
                            transfer = active_transfers[tid]
                            if transfer.direction == "send":
                                peer_ip = transfer.peer_ip
                                if peer_ip in outgoing_transfers_by_peer and outgoing_transfers_by_peer[peer_ip] == tid:
                                    outgoing_transfers_by_peer.pop(peer_ip, None)
                            
                            del active_transfers[tid]
                            logger.info(f"Removed finished/failed transfer {tid[:8]}")
                    
                    # Send full update after removing items
                    await message_queue.put({"type": "transfer_update"})
                elif updated: # If state changed but wasn't removed (e.g., paused)
                    await message_queue.put({"type": "transfer_update"})

            await asyncio.sleep(1)
        except asyncio.CancelledError:
            logger.info("update_transfer_progress task cancelled.")
            break
        except Exception as e: 
            logger.exception(f"Error in update_transfer_progress: {e}")
            await asyncio.sleep(5)
            
    logger.info("update_transfer_progress stopped.")

# Add this function to process pause/resume messages from peers
async def handle_transfer_control_message(message_data, peer_ip):
    """Process transfer control messages like pause and resume."""
    try:
        msg_type = message_data.get("type")
        transfer_id = message_data.get("transfer_id")
        
        if not transfer_id:
            logger.warning(f"Received {msg_type} without transfer_id from {peer_ip}")
            return
            
        # Thread-safe check if transfer exists
        async with active_transfers_lock:
            if transfer_id not in active_transfers:
                logger.warning(f"Received {msg_type} for unknown transfer ID {transfer_id}")
                return
                
            transfer = active_transfers[transfer_id]
        
        # Verify this message is from the peer involved in the transfer
        if transfer.peer_ip != peer_ip:
            logger.warning(f"Received {msg_type} from {peer_ip} but transfer {transfer_id[:8]} involves {transfer.peer_ip}")
            return
            
        if msg_type == "TRANSFER_PAUSE":
            logger.info(f"Received pause request for transfer {transfer_id[:8]}")
            success = await transfer.pause()
            
            if success:
                # Notify UI about remote pause action
                display_name = get_peer_display_name(peer_ip) if 'get_peer_display_name' in globals() else peer_ip
                await message_queue.put({
                    "type": "log", 
                    "message": f"Transfer {transfer_id[:8]} paused by {display_name}",
                    "level": logging.INFO
                })
            else:
                logger.warning(f"Could not pause transfer {transfer_id[:8]} as requested by peer (current state: {transfer.state.value})")
            
        elif msg_type == "TRANSFER_RESUME":
            logger.info(f"Received resume request for transfer {transfer_id[:8]}")
            success = await transfer.resume()
            
            if success:
                # Notify UI about remote resume action
                display_name = get_peer_display_name(peer_ip) if 'get_peer_display_name' in globals() else peer_ip
                await message_queue.put({
                    "type": "log", 
                    "message": f"Transfer {transfer_id[:8]} resumed by {display_name}",
                    "level": logging.INFO
                })
            else:
                logger.warning(f"Could not resume transfer {transfer_id[:8]} as requested by peer (current state: {transfer.state.value})")
            
    except Exception as e:
        logger.error(f"Error handling transfer control message: {e}", exc_info=True)
