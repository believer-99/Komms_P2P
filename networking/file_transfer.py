import asyncio
import logging
import os
import aiofiles
from tqdm.asyncio import tqdm
import hashlib
import platform
import socket
import uuid
import json
import time
import math
from networking.messaging import active_transfers

class TransferState:
    PENDING = 'pending'
    IN_PROGRESS = 'in_progress'
    PAUSED = 'paused'
    COMPLETED = 'completed'
    FAILED = 'failed'

class FileTransferManager:
    @staticmethod
    def calculate_file_hash(path, algorithm='sha256', chunk_size=4096):
        """
        Calculate file hash with support for large files
        
        Args:
            path (str): Path to the file
            algorithm (str): Hash algorithm to use
            chunk_size (int): Size of chunks to read
        
        Returns:
            str: Hexadecimal hash of the file
        """
        try:
            hash_algo = hashlib.new(algorithm)
            with open(path, 'rb') as f:
                for chunk in iter(lambda: f.read(chunk_size), b""):
                    hash_algo.update(chunk)
            return hash_algo.hexdigest()
        except Exception as e:
            logging.error(f"Hash calculation failed for {path}: {e}")
            return None

    @staticmethod
    def get_download_directory():
        """
        Get a cross-platform safe download directory
        
        Returns:
            str: Path to download directory
        """
        system = platform.system()
        
        if system == "Windows":
            download_dir = os.path.join(os.path.expanduser("~"), "Downloads")
        elif system == "Darwin":  # MacOS
            download_dir = os.path.join(os.path.expanduser("~"), "Downloads")
        else:  # Linux and others
            download_dir = os.path.join(os.path.expanduser("~"), "Downloads")
        
        p2p_download_dir = os.path.join(download_dir, "P2P_Downloads")
        os.makedirs(p2p_download_dir, exist_ok=True)
        return p2p_download_dir

class FileTransfer:
    def __init__(self, file_path, peer_ip):
        """
        Initialize a file transfer
        
        Args:
            file_path (str): Path to the file to transfer
            peer_ip (str): IP address of the peer
        """
        self.file_path = file_path
        self.peer_ip = peer_ip
        self.total_size = os.path.getsize(file_path)
        self.transferred_size = 0
        self.state = TransferState.PENDING
        self.transfer_id = str(uuid.uuid4())
        self.chunk_size = 64 * 1024  # 64 KB
        self.paused_event = asyncio.Event()
        self.paused = False 
        self.resume_event = asyncio.Event()  

    async def pause(self):
        """Pause the transfer"""
        if self.state == TransferState.IN_PROGRESS:
            self.paused = True
            self.state = TransferState.PAUSED
            logging.info(f"Transfer {self.transfer_id} paused")

    async def resume(self):
        """Resume the transfer"""
        if self.state == TransferState.PAUSED:
            self.paused = False
            self.resume_event.set()  # Signal to resume
            self.state = TransferState.IN_PROGRESS
            logging.info(f"Transfer {self.transfer_id} resumed")

async def send_file(file_path: str, connections: dict):
    """
    Send a file to connected peers with enhanced transfer management
    
    Args:
        file_path (str): Path to the file to send
        connections (dict): Dictionary of peer connections
    
    Returns:
        dict: Transfer results for each peer
    """
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
        transfer = FileTransfer(file_path, peer_ip)
        active_transfers[transfer.transfer_id] = transfer

        try:
            # Create transfer object
            transfer = FileTransfer(file_path, peer_ip)
            transfer.state = TransferState.IN_PROGRESS

            print(f"\nSending '{file_name}' ({file_size} bytes) to {peer_ip}")
            
            # Send file metadata with transfer ID
            await websocket.send(json.dumps({
                'type': 'file_transfer_init',
                'transfer_id': transfer.transfer_id,
                'filename': file_name,
                'filesize': file_size,
                'file_hash': file_hash
            }))
            await asyncio.sleep(0.5)

            progress_bar = tqdm(
                total=file_size,
                unit='B',
                unit_scale=True,
                unit_divisor=1024,
                desc=f"🚀 Sending to {peer_ip}",
                ascii=True,
                ncols=100,
                bar_format='{desc}: {percentage:3.0f}%|{bar}| {n_fmt}/{total_fmt} '
                            '[⏱️ {elapsed}<{remaining}, 📶 {rate_fmt}]',
                colour='GREEN'
            )

            async with aiofiles.open(file_path, 'rb') as file:
                bytes_sent = 0
                while bytes_sent < file_size:
                    # Handle pause/resume
                    if transfer.paused:
                        await transfer.resume_event.wait()
                        transfer.resume_event.clear()

                    chunk = await file.read(transfer.chunk_size)
                    if not chunk:
                        break

                    # Send chunk with transfer metadata
                    await websocket.send(json.dumps({
                        'type': 'file_chunk',
                        'transfer_id': transfer.transfer_id,
                        'chunk': chunk.hex(),  # Convert to hex for JSON serialization
                        'offset': bytes_sent
                    }))

                    bytes_sent += len(chunk)
                    progress_bar.update(len(chunk))
                    transfer.transferred_size = bytes_sent

                    # Adaptive flow control
                    if bytes_sent % (64 * 1024 * 16) == 0:
                        await asyncio.sleep(0.01)

            progress_bar.close()
            print(f"✅ Successfully sent '{file_name}' to {peer_ip}")
            
            transfer.state = TransferState.COMPLETED
            transfer_results[peer_ip] = {
                'status': 'success',
                'transfer_id': transfer.transfer_id
            }

        except asyncio.CancelledError:
            if transfer.paused:
                logging.info("Transfer cancelled while paused")

            logging.info(f"File transfer to {peer_ip} cancelled")
            transfer_results[peer_ip] = {
                'status': 'cancelled',
                'transfer_id': transfer.transfer_id
            }
            transfer.state = TransferState.FAILED
            break
        except Exception as e:
            logging.exception(f"Error sending to {peer_ip}: {e}")
            transfer_results[peer_ip] = {
                'status': 'failed',
                'transfer_id': transfer.transfer_id,
                'error': str(e)
            }
            transfer.state = TransferState.FAILED
            try:
                await websocket.close()
            except:
                pass

    return transfer_results

async def receive_file(websocket, metadata, start_byte=0):
    """
    Receive a file with advanced transfer management
    
    Args:
        websocket: WebSocket connection
        metadata (dict): File transfer metadata
        start_byte (int): Resuming transfer from this byte
    
    Returns:
        bool: Transfer success status
    """
    try:
        peer_ip = websocket.remote_address[0]
        file_name = metadata['filename']
        file_size = metadata['filesize']
        transfer_id = metadata['transfer_id']
        expected_hash = metadata.get('file_hash')

        print(f"\nReceiving '{file_name}' ({file_size} bytes) from {peer_ip}")
        
        progress_bar = tqdm(
            total=file_size,
            unit='B',
            unit_scale=True,
            unit_divisor=1024,
            desc=f"📥 Receiving from {peer_ip}",
            ascii=True,
            ncols=100,
            initial=start_byte,
            bar_format='{desc}: {percentage:3.0f}%|{bar}| {n_fmt}/{total_fmt} '
                        '[⏱️ {elapsed}<{remaining}, 📶 {rate_fmt}]',
            colour='GREEN'
        )

        download_dir = FileTransferManager.get_download_directory()
        file_path = os.path.join(download_dir, file_name)
        
        # Handle file resume logic
        mode = 'ab' if start_byte > 0 else 'wb'
        
        # Integrity verification setup
        hash_algo = hashlib.sha256() if expected_hash else None

        async with aiofiles.open(file_path, mode=mode) as f:
            # Seek to start byte if resuming
            if mode == 'ab':
                await f.seek(start_byte)

            received_bytes = start_byte
            while received_bytes < file_size:
                if active_transfers[transfer_id]['status'] == 'paused':
                    # Wait for resume signal
                    while active_transfers[transfer_id]['status'] == 'paused':
                        await asyncio.sleep(1)

                chunk_data = await websocket.recv()
                chunk_metadata = json.loads(chunk_data)

                if chunk_metadata['type'] != 'file_chunk':
                    continue

                # Verify transfer ID
                if chunk_metadata['transfer_id'] != transfer_id:
                    logging.warning(f"Mismatched transfer ID for {file_name}")
                    continue

                # Convert hex chunk back to bytes
                chunk = bytes.fromhex(chunk_metadata['chunk'])
                
                await f.write(chunk)
                
                # Update progress
                received_bytes += len(chunk)
                progress_bar.update(len(chunk))
                
                # Hash calculation for integrity check
                if hash_algo:
                    hash_algo.update(chunk)

            progress_bar.close()

            # Verify file integrity if hash provided
            if expected_hash and hash_algo:
                calculated_hash = hash_algo.hexdigest()
                if calculated_hash != expected_hash:
                    logging.error(f"File integrity check failed for {file_name}")
                    os.remove(file_path)
                    return False

            print(f"✅ File saved as: {file_path}")
            return True

    except Exception as e:
        logging.exception(f"Error receiving file from {peer_ip}: {e}")
        progress_bar.close()
        return False
