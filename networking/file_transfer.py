import asyncio
import logging
import os
import aiofiles
from tqdm.asyncio import tqdm
import hashlib
import platform
import socket

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

async def send_file(file_path: str, connections: dict):
    """
    Send a file to connected peers with enhanced progress tracking
    
    Args:
        file_path (str): Path to the file to send
        connections (dict): Dictionary of peer connections
    
    Returns:
        bool: True if file transfer successful, False otherwise
    """
    if not connections:
        logging.warning("No peers connected to send the file.")
        return False
    
    if not os.path.exists(file_path):
        logging.error(f"File not found: {file_path}")
        return False

    file_size = os.path.getsize(file_path)
    file_name = os.path.basename(file_path)
    file_hash = FileTransferManager.calculate_file_hash(file_path)

    if not file_hash:
        logging.error("Failed to calculate file hash")
        return False

    overall_success = True

    for peer_ip, websocket in list(connections.items()):
        try:
            print(f"\nSending '{file_name}' ({file_size} bytes) to {peer_ip}")
            
            # Send file metadata
            await websocket.send(f"FILE {file_name} {file_size} 0 {file_hash}")
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
                    chunk = await file.read(64 * 1024)  
                    if not chunk:
                        break
                    await websocket.send(chunk)
                    bytes_sent += len(chunk)
                    progress_bar.update(len(chunk))

                    # Adaptive flow control
                    if bytes_sent % (64 * 1024 * 16) == 0:
                        await asyncio.sleep(0.01)

            progress_bar.close()
            print(f"✅ Successfully sent '{file_name}' to {peer_ip}")

        except asyncio.CancelledError:
            logging.info(f"File transfer to {peer_ip} cancelled")
            overall_success = False
            break
        except Exception as e:
            logging.exception(f"Error sending to {peer_ip}: {e}")
            overall_success = False
            try:
                await websocket.close()
            except:
                pass
            finally:
                progress_bar.close()

    return overall_success

async def receive_file(websocket, file_name, file_size, start_byte=0, expected_hash=None):
    """
    Receive a file with advanced progress tracking and integrity verification
    
    Args:
        websocket: WebSocket connection
        file_name (str): Name of the file to receive
        file_size (int): Total file size
        start_byte (int): Resuming transfer from this byte
        expected_hash (str, optional): Expected file hash for verification
    
    Returns:
        bool: True if file transfer successful, False otherwise
    """
    try:
        peer_ip = websocket.remote_address[0]
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
                try:
                    chunk = await websocket.recv()
                    await f.write(chunk)
                    
                    # Update progress
                    received_bytes += len(chunk)
                    progress_bar.update(len(chunk))
                    
                    # Hash calculation for integrity check
                    if hash_algo:
                        hash_algo.update(chunk)

                except asyncio.TimeoutError:
                    logging.warning(f"Timeout during file transfer from {peer_ip}")
                    return False

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