# networking/file_transfer.py
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

from networking.shared_state import active_transfers, shutdown_event, message_queue
from websockets.connection import State

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
        async with self.condition:
            if self.state == TransferState.IN_PROGRESS:
                self.state = TransferState.PAUSED
                logger.info(f"Transfer {self.transfer_id[:8]} paused.")
                await message_queue.put({"type": "transfer_update"})

    async def resume(self):
        async with self.condition:
            if self.state == TransferState.PAUSED:
                self.state = TransferState.IN_PROGRESS
                logger.info(f"Transfer {self.transfer_id[:8]} resumed.")
                self.condition.notify_all()
                await message_queue.put({"type": "transfer_update"})

    async def fail(self, reason="Unknown"):
        async with self.condition:
             if self.state not in [TransferState.COMPLETED, TransferState.FAILED]:
                logger.error(f"Transfer {self.transfer_id[:8]} failed: {reason}")
                self.state = TransferState.FAILED
                if self.file_handle:
                    try: await self.file_handle.close()
                    except Exception: pass
                    self.file_handle = None
                await message_queue.put({"type": "transfer_update"})

async def compute_hash(file_path):
    """Compute the SHA-256 hash of a file asynchronously."""
    hash_algo = hashlib.sha256()
    try:
        async with aiofiles.open(file_path, "rb") as f:
            while True:
                chunk = await f.read(1024 * 1024) # Read in 1MB chunks
                if not chunk:
                    break # Exit loop if end of file is reached
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

    peer_ip, websocket = next(iter(peers.items()))
    if not websocket or websocket.state != State.OPEN:
         await message_queue.put({"type": "log", "message": f"Send Error: Peer {peer_ip} not connected.", "level": logging.ERROR})
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

    try:
        async with aiofiles.open(file_path, "rb") as f:
            transfer.file_handle = f
            chunk_size = 1024 * 1024
            while not shutdown_event.is_set() and transfer.state != TransferState.FAILED:
                async with transfer.condition:
                    while transfer.state == TransferState.PAUSED and not shutdown_event.is_set():
                        await transfer.condition.wait()
                    if shutdown_event.is_set() or transfer.state == TransferState.FAILED: break
                    if transfer.state != TransferState.IN_PROGRESS: break

                chunk = await f.read(chunk_size)
                if not chunk:
                    await asyncio.sleep(0.1) # Allow last chunk send to process
                    transfer.state = TransferState.COMPLETED
                    break

                transfer.transferred_size += len(chunk)

                # --- Send chunk as binary --- #
                try:
                    # Check connection state *before* sending
                    if websocket.state != State.OPEN:
                         logger.warning(f"Peer {peer_ip} disconnected during send.")
                         await transfer.fail("Peer disconnected during send")
                         break # Exit send loop

                    # --- *** CORRECTED: Send raw bytes directly *** ---
                    await websocket.send(chunk)
                    # --- *** /CORRECTED *** ---

                # --- CORRECTED Exception Handling for send ---
                except websockets.exceptions.ConnectionClosedOK:
                    logger.warning(f"Peer {peer_ip} closed connection (OK) during send.")
                    await transfer.fail("Peer closed connection")
                    break # Exit send loop
                except websockets.exceptions.ConnectionClosedError as e:
                     logger.warning(f"Peer {peer_ip} closed connection abruptly during send: {e}")
                     await transfer.fail("Peer connection lost abruptly")
                     break # Exit send loop
                except Exception as e: # Catch other potential send errors
                    logger.error(f"Error sending chunk to {peer_ip}: {e}", exc_info=True) # Log traceback
                    await transfer.fail(f"Send error: {e}")
                    break # Exit send loop
                # --- End Corrected Exception Handling ---

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
            try: await transfer.file_handle.close()
            except Exception: pass
            transfer.file_handle = None
        # Ensure final state update is sent to GUI
        await message_queue.put({"type": "transfer_update"})


async def update_transfer_progress():
    """Periodically send transfer progress updates to the GUI queue."""
    while not shutdown_event.is_set():
        try:
            transfers_to_remove = []
            updated = False
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
            if transfers_to_remove:
                for tid in transfers_to_remove:
                     # Check again before deleting, state might have changed
                     if tid in active_transfers and active_transfers[tid].state in (TransferState.COMPLETED, TransferState.FAILED):
                         del active_transfers[tid]
                         logger.info(f"Removed finished/failed transfer {tid[:8]}")
                # Send full update after removing items
                await message_queue.put({"type": "transfer_update"})
            elif updated: # If state changed but wasn't removed (e.g., paused)
                 await message_queue.put({"type": "transfer_update"})
            await asyncio.sleep(1)
        except asyncio.CancelledError: logger.info("update_transfer_progress task cancelled."); break
        except Exception as e: logger.exception(f"Error in update_transfer_progress: {e}"); await asyncio.sleep(5)
    logger.info("update_transfer_progress stopped.")
