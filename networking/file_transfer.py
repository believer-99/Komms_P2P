import asyncio
import hashlib
import os
import json
import aiofiles
from enum import Enum
from networking.shared_state import active_transfers, message_queue, connections, shutdown_event

class TransferState(Enum):
    IN_PROGRESS = "In Progress"
    PAUSED = "Paused"
    COMPLETED = "Completed"
    FAILED = "Failed"

class FileTransfer:
    def __init__(self, file_path, peer_ip, direction="send"):
        self.file_path = file_path
        self.peer_ip = peer_ip
        self.direction = direction
        self.transfer_id = None
        self.total_size = os.path.getsize(file_path) if os.path.exists(file_path) else 0
        self.transferred_size = 0
        self.state = TransferState.IN_PROGRESS
        self.file_handle = None
        self.hash_algo = None
        self.expected_hash = None
        self.condition = asyncio.Condition()

    async def pause(self):
        async with self.condition:
            self.state = TransferState.PAUSED
            self.condition.notify_all()

    async def resume(self):
        async with self.condition:
            self.state = TransferState.IN_PROGRESS
            self.condition.notify_all()

class FileTransferManager:
    @staticmethod
    def get_download_directory():
        return os.path.join(os.getcwd(), "downloads")

async def send_file(file_path, peers):
    transfer_id = hashlib.sha256(file_path.encode()).hexdigest()[:8]
    if transfer_id in active_transfers:
        print(f"Transfer ID {transfer_id} already in use.")
        return

    file_size = os.path.getsize(file_path)
    chunk_size = 1024 * 1024
    transfer = FileTransfer(file_path, list(peers.keys())[0], direction="send")
    transfer.transfer_id = transfer_id
    transfer.hash_algo = hashlib.sha256()
    active_transfers[transfer_id] = transfer

    try:
        async with aiofiles.open(file_path, "rb") as f:
            transfer.file_handle = f
            for peer_ip, websocket in peers.items():
                file_hash = hashlib.sha256()
                async with aiofiles.open(file_path, "rb") as hash_file:
                    while not shutdown_event.is_set():
                        chunk = await hash_file.read(65536)
                        if not chunk:
                            break
                        file_hash.update(chunk)
                    if shutdown_event.is_set():
                        break
                file_hash_hex = file_hash.hexdigest()
                await websocket.send(json.dumps({
                    "type": "file_transfer_init",
                    "transfer_id": transfer_id,
                    "filename": os.path.basename(file_path),
                    "filesize": file_size,
                    "file_hash": file_hash_hex
                }))
                while transfer.transferred_size < transfer.total_size and not shutdown_event.is_set():
                    async with transfer.condition:
                        while transfer.state == TransferState.PAUSED and not shutdown_event.is_set():
                            await transfer.condition.wait()
                        if shutdown_event.is_set():
                            break
                        chunk = await f.read(chunk_size)
                        if not chunk:
                            break
                        transfer.hash_algo.update(chunk)
                        await websocket.send(json.dumps({
                            "type": "file_chunk",
                            "transfer_id": transfer_id,
                            "chunk": chunk.hex()
                        }))
                        transfer.transferred_size += len(chunk)
                if transfer.transferred_size >= transfer.total_size:
                    transfer.state = TransferState.COMPLETED
                    await message_queue.put(f"File transfer completed: {file_path} to {peer_ip}")
    except Exception as e:
        transfer.state = TransferState.FAILED
        await message_queue.put(f"File transfer failed: {e}")
    finally:
        if transfer.file_handle:
            await transfer.file_handle.close()
            logging.info(f"Closed file handle for transfer {transfer_id}")
        if shutdown_event.is_set():
            del active_transfers[transfer_id]

async def update_transfer_progress():
    while not shutdown_event.is_set():
        try:
            if active_transfers:
                for transfer_id, transfer in list(active_transfers.items()):
                    if isinstance(transfer, FileTransfer):
                        if transfer.state in (TransferState.COMPLETED, TransferState.FAILED):
                            del active_transfers[transfer_id]
        except Exception as e:
            await message_queue.put(f"Error updating transfer progress: {e}")
        await asyncio.sleep(1)
    logging.info("update_transfer_progress exited due to shutdown.")