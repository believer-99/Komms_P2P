import asyncio
import logging
import os
import aiofiles

async def send_file(file_path: str, connections: dict):
    """Send a file to connected peers asynchronously."""
    if not connections:
        print("No peers connected to send the file.")
        return

    if not os.path.exists(file_path):
        print(f"File not found: {file_path}")
        return

    file_size = os.path.getsize(file_path)
    chunk_size = 64 * 1024  # 64KB chunks

    try:
        async with aiofiles.open(file_path, mode='rb') as file: # Opens the file with aiofiles
            file_name = os.path.basename(file_path)

            for peer_ip, websocket in list(connections.items()):
                try:
                    print(f"Sending file '{file_name}' ({file_size} bytes) to {peer_ip}")
                    await websocket.send(f"FILE {file_name} {file_size} 0")
                    await asyncio.sleep(0.5)  # Wait for receiver to prepare

                    bytes_sent = 0
                    while bytes_sent < file_size:
                        chunk = await file.read(chunk_size)
                        if not chunk:
                            break
                        try:
                            await websocket.send(chunk)
                            bytes_sent += len(chunk)
                        except Exception as e:
                            logging.error(f"Error sending chunk to {peer_ip}: {e}")
                            break

                        # Progress update
                        progress = (bytes_sent / file_size) * 100
                        print(f"\rProgress: {progress:.2f}%", end="", flush=True)

                        # Flow control
                        if bytes_sent % (chunk_size * 16) == 0:  # Every 1MB
                            await asyncio.sleep(0.01)  # Small pause

                    print(f"\nSuccessfully sent file '{file_name}' to {peer_ip}")

                except Exception as e:
                    logging.error(f"Error sending file to {peer_ip}: {e}")
                    # Try to gracefully close the connection
                    try:
                        await websocket.close()
                    except:
                        pass

    except FileNotFoundError:
        logging.error(f"File not found at path: {file_path}")
    except Exception as e:
        logging.error(f"Error reading file: {e}")

async def receive_file(websocket, file_name, file_size, start_byte=0):
    try:
        print(f"\nReceiving file: {file_name} ({file_size} bytes)")
        received_bytes = start_byte

        os.makedirs('downloads', exist_ok=True)
        file_path = os.path.join('downloads', file_name)

        mode = 'ab' if start_byte > 0 else 'wb'
        async with aiofiles.open(file_path, mode=mode) as f:
            while received_bytes < file_size:
                try:
                    chunk = await websocket.recv()
                    await f.write(chunk)
                    received_bytes += len(chunk)

                    progress = (received_bytes / file_size) * 100
                    print(f"\rProgress: {progress:.2f}%", end="", flush=True)

                except Exception as e:
                    logging.error(f"Error during file receive: {e}")
                    break

            print(f"\nFile saved as: {file_path}")

    except Exception as e:
        logging.error(f"Error receiving file: {e}")