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
                        async def chunk_generator(file, chunk_size): # Added stream data with chunk size
                            while True:
                                part = await file.read(chunk_size)
                                if not part:
                                    break
                                yield part

                        chunk_hash = None
                        full_chunk = bytearray()
                        chunk_hashes = [] # append each part into this array, to verify parts with parts on the receive function
                        async for part in chunk_generator(file, 1024): # yield the chunk in 1024 bits
                            chunk_hash_temp = self._calculate_chunk_hash(part) # temporary chunk data
                            chunk_hashes.append(chunk_hash_temp) # append chunk temporary data
                            full_chunk.extend(part)
                            #await websocket.send(part) # remove sending each part as the part has to be hashed first

                        full_chunk_bytes = bytes(full_chunk)
                        chunk_hash = self._calculate_chunk_hash(full_chunk_bytes) # hash the full data that was in the yield
                        await websocket.send(f"CHUNK {chunk_index} {chunk_hash} {','.join(chunk_hashes)}") # Sends the hash of the chunk before sending the data, include chunk hashes
                        await websocket.send(full_chunk_bytes)  # remove sending each part, it has to send the full thing after the yield

                        ack = await websocket.recv()
                        if ack == f"ACK {chunk_index}":
                            transfer_state['sent_chunks'].add(chunk_index)
                            bytes_sent += len(full_chunk_bytes)
                            progress.update(bytes_sent)
                    else:
                        bytes_sent += min(self.chunk_size, file_size - bytes_sent)
                        progress.update(bytes_sent)

                    chunk_index += 1

                    if chunk_index % 16 == 0:
                        await asyncio.sleep(0.01)

            await websocket.send("FILE_END")
            print(f"\nSuccessfully sent file '{file_name}' to {peer_ip}")
            #remove transfer when successful
            if file_id in self.active_transfers:
                del self.active_transfers[file_id]

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
            file_id = self._generate_file_id(file_path, file_size)

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
                        parts = message.split(" ", 2)
                        _, chunk_index, expected_hash_and_parts = parts
                        chunk_index = int(chunk_index)

                        expected_hash, expected_hashes_str = expected_hash_and_parts.rsplit(" ", 1)  # Split to get both hashes
                        expected_hashes = expected_hashes_str.split(',') # Split at "," to put inside the array to verify

                        chunk = await websocket.recv()
                        actual_hash = self._calculate_chunk_hash(chunk)

                        async def chunk_generator(chunk, chunk_size):
                            for i in range(0, len(chunk), chunk_size):
                                yield chunk[i:i + chunk_size]

                        actual_hashes = [] # append the parts into the array
                        index = 0 # index of yield
                        async for part in chunk_generator(chunk, 1024): # append the chunks with 1024 bits
                            actual_hash_temp = self._calculate_chunk_hash(part)
                            actual_hashes.append(actual_hash_temp)
                            index += 1

                        if actual_hash == expected_hash and actual_hashes == expected_hashes: # verify the chunks and parts
                            if chunk_index not in received_chunks:
                                await f.seek(chunk_index * chunk_size)
                                await f.write(chunk)
                                received_chunks.add(chunk_index)
                                bytes_received += len(chunk)
                                progress.update(bytes_received)
                                await websocket.send(f"ACK {chunk_index}")
                        else:
                            logging.error(f"Chunk {chunk_index} hash mismatch")
                            await websocket.send(f"RESEND {chunk_index}") # Request resend
                            continue  # Get the next chunk and come back here

            print(f"\nFile saved as: {file_path}")

            if file_id in self.active_transfers:
               del self.active_transfers[file_id]
            if file_id in self.paused_transfers:
               del self.paused_transfers[file_id]

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
            await asyncio.sleep(0)
            return True
        return False

    async def resume_transfer(self, file_id: str) -> bool:
        if file_id in self.paused_transfers:
            transfer_state = self.paused_transfers[file_id]
            transfer_state['paused'] = False
            self.active_transfers[file_id] = transfer_state
            del self.paused_transfers[file_id]
            print(f"\nTransfer of {file_id} resumed.")
            await asyncio.sleep(0)
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