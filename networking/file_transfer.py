import asyncio
import logging

# Define connections dictionary here if not imported
connections = {}  # This will be shared with messaging.py

async def send_file(file_path: str):
    """Send a file to connected peers."""
    if not connections:
        print("No peers connected to send the file.")
        return

    try:
        with open(file_path, 'rb') as file:
            file_data = file.read()
            file_name = file_path.split("/")[-1]  # Get the file name
            file_size = len(file_data)

            # Send file metadata first
            for peer_ip in list(connections.keys()):  # Create a copy of keys to iterate
                try:
                    await connections[peer_ip].send(f"FILE {file_name} {file_size}")
                    # Send the file data
                    await connections[peer_ip].send(file_data)
                    print(f"Sent file '{file_name}' to {peer_ip}")
                except Exception as e:
                    logging.error(f"Error sending file to {peer_ip}: {e}")

    except Exception as e:
        logging.error(f"Error sending file: {e}")

async def receive_file(websocket, file_name, file_size):
    """Receive a file from a peer."""
    try:
        file_data = await websocket.recv()  # Receive the file data
        with open(file_name, 'wb') as f:
            f.write(file_data)
        logging.info(f"Received file '{file_name}' from peer.")
    except Exception as e:
        logging.error(f"Error receiving file: {e}")