import asyncio
import logging
import os
import aiofiles
import hashlib
from typing import Dict, Set
from .progress import ProgressBar

class FileTransferManager:
    def __init__(self):
        self.active_transfers: Dict[str, Dict] = {}
        self.paused_transfers: Dict[str, Dict] = {}
        self.chunk_size = 64 * 1024  # 64KB chunks
        
    def _calculate_chunk_hash(self, chunk: bytes) -> str:
        return hashlib.md5(chunk).hexdigest()
    
    def _generate_file_id(self, file_path: str, file_size: int) -> str:
        return f"{os.path.basename(file_path)}_{file_size}"

    async def send_file(self, file_path: str, websocket, peer_ip: str):
        if not os.path.exists(file_path):
            logging.error(f"File not found: {file_path}")
            return False

        file_size = os.path.getsize(file_path)
        file_id = self._generate_file_id(file_path, file_size)
        file_name = os.path.basename(file_path)

        if file_id not in self.active_transfers:
            self.active_transfers[file_id] = {
                'sent_chunks': set(),
                'file_size': file_size,
                'total_chunks': (file_size + self.chunk_size - 1) // self.chunk_size,
                'paused': False
            }

        transfer_state = self.active_transfers[file_id]
        
        try:
            await websocket.send(f"FILE_START {file_name} {file_size} {self.chunk_size}")
            response = await websocket.recv()
            
            progress = ProgressBar(
                total=file_size,
                prefix=f"Sending {file_name} to {peer_ip}"
            )
            
            if response.startswith("RESUME"):
                _, received_chunks = response.split(" ", 1)
                received_set = set(int(x) for x in received_chunks.split(",") if x)
                transfer_state['sent_chunks'] = received_set
                bytes_done = len(received_set) * self.chunk_size
                progress.update(min(bytes_done, file_size))
            
            async with aiofiles.open(file_path, mode='rb') as file:
                chunk_index = 0
                bytes_sent = 0

                while bytes_sent < file_size:
                    if transfer_state.get('paused', False):
                        await asyncio.sleep(1)
                        continue
                        
                    if chunk_index not in transfer_state['sent_chunks']:
                        await file.seek(chunk_index * self.chunk_size)
                        chunk = await file.read(self.chunk_size)
                        
                        if not chunk:
                            break
                            
                        chunk_hash = self._calculate_chunk_hash(chunk)
                        await websocket.send(f"CHUNK {chunk_index} {chunk_hash}")
                        await websocket.send(chunk)
                        
                        ack = await websocket.recv()
                        if ack == f"ACK {chunk_index}":
                            transfer_state['sent_chunks'].add(chunk_index)
                            bytes_sent += len(chunk)
                            progress.update(bytes_sent)
                    else:
                        bytes_sent += min(self.chunk_size, file_size - bytes_sent)
                        progress.update(bytes_sent)
                    
                    chunk_index += 1
                    
                    if chunk_index % 16 == 0:
                        await asyncio.sleep(0.01)

            await websocket.send("FILE_END")
            print(f"\nSuccessfully sent file '{file_name}' to {peer_ip}")
            return True

        except Exception as e:
            logging.exception(f"Error sending file to {peer_ip}: {e}")
            return False

    async def receive_file(self, websocket, initial_message: str):
        try:
            _, file_name, file_size, chunk_size = initial_message.split(" ")
            file_size = int(file_size)
            chunk_size = int(chunk_size)
            
            os.makedirs('downloads', exist_ok=True)
            file_path = os.path.join('downloads', file_name)
            
            progress = ProgressBar(
                total=file_size,
                prefix=f"Receiving {file_name}"
            )
            
            received_chunks = set()
            if os.path.exists(file_path):
                file_size_on_disk = os.path.getsize(file_path)
                complete_chunks = file_size_on_disk // chunk_size
                received_chunks = set(range(complete_chunks))
                progress.update(file_size_on_disk)
            
            await websocket.send(f"RESUME {','.join(map(str, received_chunks))}")
            
            async with aiofiles.open(file_path, mode='ab' if received_chunks else 'wb') as f:
                bytes_received = len(received_chunks) * chunk_size
                
                while True:
                    message = await websocket.recv()
                    
                    if message == "FILE_END":
                        break
                        
                    if message.startswith("CHUNK"):
                        _, chunk_index, expected_hash = message.split(" ")
                        chunk_index = int(chunk_index)
                        
                        chunk = await websocket.recv()
                        actual_hash = self._calculate_chunk_hash(chunk)
                        
                        if actual_hash == expected_hash:
                            if chunk_index not in received_chunks:
                                await f.seek(chunk_index * chunk_size)
                                await f.write(chunk)
                                received_chunks.add(chunk_index)
                                bytes_received += len(chunk)
                                progress.update(bytes_received)
                                await websocket.send(f"ACK {chunk_index}")
                        else:
                            logging.error(f"Chunk {chunk_index} hash mismatch")
                            
            print(f"\nFile saved as: {file_path}")
            return True
            
        except Exception as e:
            logging.exception(f"Error receiving file: {e}")
            return False

    async def pause_transfer(self, file_id: str) -> bool:
        if file_id in self.active_transfers:
            transfer_state = self.active_transfers[file_id]
            transfer_state['paused'] = True
            self.paused_transfers[file_id] = transfer_state
            print(f"\nTransfer of {file_id} paused.")
            return True
        return False
        
    async def resume_transfer(self, file_id: str) -> bool:
        if file_id in self.paused_transfers:
            transfer_state = self.paused_transfers[file_id]
            transfer_state['paused'] = False
            self.active_transfers[file_id] = transfer_state
            del self.paused_transfers[file_id]
            print(f"\nTransfer of {file_id} resumed.")
            return True
        return False
        
    def list_transfers(self) -> Dict[str, Dict]:
        all_transfers = {}
        for file_id, state in self.active_transfers.items():
            all_transfers[file_id] = {'status': 'active', **state}
        for file_id, state in self.paused_transfers.items():
            all_transfers[file_id] = {'status': 'paused', **state}
        return all_transfers

file_transfer_manager = FileTransferManager()