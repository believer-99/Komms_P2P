# networking/messaging.py
import asyncio
import logging
import websockets
from aioconsole import ainput
from networking.file_transfer import send_file, receive_file

# Shared state
message_queue = asyncio.Queue()  # Queue for incoming messages
connections = {}  # Dictionary to hold active connections
peer_list = []  # List to store discovered peers

async def connect_to_peer(peer_ip, port=8765):
    """Establishes a WebSocket connection to a peer."""
    uri = f"ws://{peer_ip}:{port}"
    try:
        websocket = await websockets.connect(
            uri,
            ping_interval=20,
            ping_timeout=10
        )
        logging.info(f"Successfully connected to {peer_ip}")
        return websocket
    except Exception as e:
        logging.error(f"Failed to connect to {peer_ip}: {e}")
        return None

async def connect_to_peers():
    """Continuously attempts to connect to discovered peers."""
    while True:
        try:
            # Try to connect to all discovered peers that we're not already connected to
            for peer_ip in peer_list[:]:  # Create a copy of the list to iterate
                if peer_ip not in connections:
                    websocket = await connect_to_peer(peer_ip)
                    if websocket:
                        connections[peer_ip] = websocket
                        # Start receiving messages from this peer
                        asyncio.create_task(receive_peer_messages(websocket, peer_ip))

            await asyncio.sleep(5)  # Wait before next connection attempt
        except Exception as e:
            logging.error(f"Error in connect_to_peers: {e}")
            await asyncio.sleep(5)  # Wait before retrying

# Rest of your messaging.py code remains the same...