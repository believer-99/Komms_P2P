import asyncio
import logging
import os
import aiofiles
from tqdm.asyncio import tqdm

async def send_file(file_path: str, connections: dict):
    """Send a file to connected peers asynchronously with progress tracking."""
    if not connections:
        print("No peers connected to send the file.")
        return

    if not os.path.exists(file_path):
        print(f"File not found: {file_path}")
        return

    file_size = os.path.getsize(file_path)
    file_name = os.path.basename(file_path)

    for peer_ip, websocket in list(connections.items()):
        try:
            print(f"\nSending '{file_name}' ({file_size} bytes) to {peer_ip}")
            await websocket.send(f"FILE {file_name} {file_size} 0")
            await asyncio.sleep(0.5)  # Let receiver prepare

            progress_bar = tqdm(
                total=file_size,
                unit='B',
                unit_scale=True,
                unit_divisor=1024,
                desc=f"Sending to {peer_ip}",
                ascii=True,
                ncols=80,
                bar_format='{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}]',
                colour='GREEN'
            )

            async with aiofiles.open(file_path, 'rb') as file:
                bytes_sent = 0
                while bytes_sent < file_size:
                    chunk = await file.read(64 * 1024)  # 64KB chunks
                    if not chunk:
                        break
                    await websocket.send(chunk)
                    bytes_sent += len(chunk)
                    progress_bar.update(len(chunk))

                    # Flow control
                    if bytes_sent % (64 * 1024 * 16) == 0:  # Every 1MB
                        await asyncio.sleep(0.01)

            progress_bar.close()
            print(f"✅ Successfully sent '{file_name}' to {peer_ip}")

        except Exception as e:
            logging.exception(f"Error sending to {peer_ip}: {e}")
            try:
                await websocket.close()
            except:
                pass
            finally:
                progress_bar.close()

async def receive_file(websocket, file_name, file_size, start_byte=0):
    """Receive a file with progress tracking using tqdm."""
    try:
        peer_ip = websocket.remote_address[0]
        print(f"\nReceiving '{file_name}' ({file_size} bytes) from {peer_ip}")
        
        progress_bar = tqdm(
            total=file_size,
            unit='B',
            unit_scale=True,
            unit_divisor=1024,
            desc=f"Receiving from {peer_ip}",
            ascii=True,
            ncols=80,
            initial=start_byte,
            bar_format='{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}]',
            colour='GREEN'
        )

        os.makedirs('downloads', exist_ok=True)
        file_path = os.path.join('downloads', file_name)
        mode = 'ab' if start_byte > 0 else 'wb'

        async with aiofiles.open(file_path, mode=mode) as f:
            while progress_bar.n < file_size:
                chunk = await websocket.recv()
                await f.write(chunk)
                progress_bar.update(len(chunk))

        progress_bar.close()
        print(f"✅ File saved as: {file_path}")

    except Exception as e:
        logging.exception(f"Error receiving file: {e}")
        progress_bar.close()