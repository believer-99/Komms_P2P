import asyncio
import logging
import os
import aiofiles
from networking.progress import ProgressBar

async def send_file(file_path: str, connections: dict):
    if not connections:
        print("No peers connected to send the file.")
        return

    if not os.path.exists(file_path):
        print(f"File not found: {file_path}")
        return

    try:
        for peer_ip, websocket in list(connections.items()):
            try:
                async with aiofiles.open(file_path, 'rb') as file:
                    file_name = os.path.basename(file_path)
                    file_size = os.path.getsize(file_path)
                    chunk_size = 64 * 1024  # 64KB chunks

                    progress_bar = ProgressBar(file_size, prefix=f"Sending to {peer_ip}")
                    print(f"Sending '{file_name}' ({file_size} bytes) to {peer_ip}")

                    await websocket.send(f"FILE {file_name} {file_size} 0")
                    await asyncio.sleep(0.5)  # Let receiver prepare

                    bytes_sent = 0
                    while bytes_sent < file_size:
                        chunk = await file.read(chunk_size)
                        if not chunk:
                            break
                        await websocket.send(chunk)
                        bytes_sent += len(chunk)
                        progress_bar.update(bytes_sent)

                        # Flow control
                        if bytes_sent % (chunk_size * 16) == 0:
                            await asyncio.sleep(0.01)

                    progress_bar.update(file_size)  # Final update
                    print(f"\nSuccessfully sent '{file_name}' to {peer_ip}")

            except Exception as e:
                logging.exception(f"Error sending to {peer_ip}: {e}")
                try:
                    await websocket.close()
                except:
                    pass

    except Exception as e:
        logging.exception(f"Error reading file: {e}")

async def receive_file(websocket, file_name, file_size, start_byte=0):
    try:
        peer_ip = websocket.remote_address[0]
        progress_bar = ProgressBar(file_size, prefix=f"Receiving from {peer_ip}")
        progress_bar.update(start_byte)
        
        print(f"\nReceiving '{file_name}' ({file_size} bytes) from {peer_ip}")
        received_bytes = start_byte

        os.makedirs('downloads', exist_ok=True)
        file_path = os.path.join('downloads', file_name)

        mode = 'ab' if start_byte > 0 else 'wb'
        async with aiofiles.open(file_path, mode=mode) as f:
            while received_bytes < file_size:
                chunk = await websocket.recv()
                await f.write(chunk)
                received_bytes += len(chunk)
                progress_bar.update(received_bytes)

        progress_bar.update(file_size)
        print(f"\nFile saved as: {file_path}")

    except Exception as e:
        logging.exception(f"Error receiving file: {e}")