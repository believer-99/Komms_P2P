import asyncio
import logging
import os

async def send_file(file_path: str, connections: dict):
    """Send a file to connected peers."""
    if not connections:
        print("No peers connected to send the file.")
        return
    
    if not os.path.exists(file_path):
        print(f"File not found: {file_path}")
        return
        
    try:
        with open(file_path, 'rb') as file:
            file_data = file.read()
            file_name = os.path.basename(file_path)  # Get just the filename
            file_size = len(file_data)
            
            # Send file metadata first
            for peer_ip, websocket in list(connections.items()):
                try:
                    logging.info(f"Sending file '{file_name}' ({file_size} bytes) to {peer_ip}")
                    await websocket.send(f"FILE {file_name} {file_size}")
                    await asyncio.sleep(0.1)  # Small delay to ensure metadata is processed
                    await websocket.send(file_data)
                    print(f"Successfully sent file '{file_name}' to {peer_ip}")
                except Exception as e:
                    logging.error(f"Error sending file to {peer_ip}: {e}")
                    
    except Exception as e:
        logging.error(f"Error reading file: {e}")

async def receive_file(websocket, file_name, file_size):
    """Receive a file from a peer."""
    try:
        print(f"\nReceiving file: {file_name} ({file_size} bytes)")
        file_data = await websocket.recv()  # Receive the file data
        
        # Create 'downloads' directory if it doesn't exist
        os.makedirs('downloads', exist_ok=True)
        
        # Save file to downloads directory
        file_path = os.path.join('downloads', file_name)
        with open(file_path, 'wb') as f:
            f.write(file_data)
        print(f"File saved as: {file_path}")
        print("> ", end="", flush=True)  # Redraw prompt
    except Exception as e:
        logging.error(f"Error receiving file: {e}")