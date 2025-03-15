import asyncio
import logging
import os
import aiofiles
import hashlib
import platform
import uuid
import json
from networking.shared_state import active_transfers

class TransferState:
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"

class FileTransferManager:
    @staticmethod
    def calculate_file_hash(path, algorithm="sha256", chunk_size=4096):
        try:
            hash_algo = hashlib.new(algorithm)
            with open(path, "rb") as f:
                for chunk in iter(lambda: f.read(chunk_size), b""):
                    hash_algo.update(chunk)
            return hash_algo.hexdigest()
        except Exception as e:
            logging.error(f"Hash calculation failed for {path}: {e}")
            return None

    @staticmethod
    def get_download_directory():
        system = platform.system()
        if system in ("Windows", "Darwin"):
            download_dir = os.path.join(os.path.expanduser("~"), "Downloads")
        else:
            download_dir = os.path.join(os.path.expanduser("~"), "Downloads")
        p2p_download_dir = os.path.join(download_dir, "P2P_Downloads")
        os.makedirs(p2p_download_dir, exist_ok=True)
        return p2p_download_dir

class FileTransfer:
    def __init__(self, file_path, peer_ip, direction="send"):
        self.file_path = file_path
        self.peer_ip = peer_ip
        self.direction = direction
        self.total_size = 0
        if direction == "send" and os.path.exists(file_path):
            self.total_size = os.path.getsize(file_path)
        self.transferred_size = 0
        self.state = TransferState.PENDING
        self.transfer_id = str(uuid.uuid4())
        self.chunk_size = 64 * 1024
        self.paused = False
        self.condition = asyncio.Condition()
        self.file_handle = None  # Used for receiving
        self.expected_hash = None  # Used for receiving
        self.hash_algo = None  # Used for receiving

    async def pause(self) -> None:
        async with self.condition:
            if self.state == TransferState.IN_PROGRESS:
                self.paused = True
                self.state = TransferState.PAUSED
                logging.info(f"Transfer {self.transfer_id} paused")

    async def resume(self) -> None:
        async with self.condition:
            if self.state == TransferState.PAUSED:
                self.paused = False
                self.state = TransferState.IN_PROGRESS
                self.condition.notify_all()
                logging.info(f"Transfer {self.transfer_id} resumed")

async def send_file(file_path: str, connections: dict) -> dict:
    if not connections:
        logging.warning("No peers connected to send the file.")
        return {}

    if not os.path.exists(file_path):
        logging.error(f"File not found: {file_path}")
        return {}

    file_size = os.path.getsize(file_path)
    file_name = os.path.basename(file_path)
    file_hash = FileTransferManager.calculate_file_hash(file_path)
    if not file_hash:
        logging.error("Failed to calculate file hash")
        return {}

    transfer_results = {}
    for peer_ip, websocket in list(connections.items()):
        transfer = FileTransfer(file_path, peer_ip, direction="send")
        active_transfers[transfer.transfer_id] = transfer
        transfer.state = TransferState.IN_PROGRESS

        try:
            print(f"\nSending '{file_name}' ({file_size} bytes) to {peer_ip}")
            await websocket.send(
                json.dumps(
                    {
                        "type": "file_transfer_init",
                        "transfer_id": transfer.transfer_id,
                        "filename": file_name,
                        "filesize": file_size,
                        "file_hash": file_hash,
                    }
                )
            )
            await asyncio.sleep(0.5)

            async with aiofiles.open(file_path, "rb") as file:
                bytes_sent = 0
                while bytes_sent < file_size:
                    async with transfer.condition:
                        while transfer.paused:
                            await transfer.condition.wait()
                    chunk = await file.read(transfer.chunk_size)
                    if not chunk:
                        break
                    await websocket.send(
                        json.dumps(
                            {
                                "type": "file_chunk",
                                "transfer_id": transfer.transfer_id,
                                "chunk": chunk.hex(),
                                "offset": bytes_sent,
                            }
                        )
                    )
                    bytes_sent += len(chunk)
                    transfer.transferred_size = bytes_sent
                    if bytes_sent % (64 * 1024 * 16) == 0:
                        await asyncio.sleep(0.01)

            print(f"✅ Successfully sent '{file_name}' to {peer_ip}")
            transfer.state = TransferState.COMPLETED
            transfer_results[peer_ip] = {"status": "success", "transfer_id": transfer.transfer_id}

        except Exception as e:
            logging.exception(f"Error sending to {peer_ip}: {e}")
            transfer_results[peer_ip] = {"status": "failed", "transfer_id": transfer.transfer_id, "error": str(e)}
            transfer.state = TransferState.FAILED

    return transfer_results

async def update_transfer_progress():
    while True:
        if active_transfers:
            print("\nActive Transfers:")
            for transfer_id, transfer in active_transfers.items():
                if isinstance(transfer, FileTransfer):
                    percentage = (
                        (transfer.transferred_size / transfer.total_size * 100)
                        if transfer.total_size > 0
                        else 0
                    )
                    print(
                        f"- ID: {transfer_id} | State: {transfer.state} | "
                        f"Progress: {transfer.transferred_size}/{transfer.total_size} bytes "
                        f"({percentage:.2f}%) | Peer: {transfer.peer_ip}"
                    )
        await asyncio.sleep(5)