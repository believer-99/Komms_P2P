import asyncio
import threading
import logging
import sys
import websockets

from networking.discovery import receive_broadcasts, send_broadcasts, discovery_lock
from networking.connection import connect_to_peer
from networking.messaging import (
    user_input, 
    display_messages, 
    connect_to_peers, 
    receive_peer_messages,
    connections,
    peer_list  # Import peer_list from messaging
)
from networking.file_transfer import send_file, receive_file

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s", stream=sys.stdout)

async def handle_peer_connection(websocket, path=None):
    """Handles incoming connections from websockets.serve."""
    peer_ip = websocket.remote_address[0]
    logging.info(f"New connection from {peer_ip} (server-initiated)")
    connections[peer_ip] = websocket

    # Start receiving messages from this peer
    asyncio.create_task(receive_peer_messages(websocket, peer_ip))

async def main():
    """Main function to run the P2P chat application."""
    # Start broadcast threads with proper arguments
    broadcast_thread = threading.Thread(target=send_broadcasts, daemon=True)
    broadcast_thread.start()

    discovery_thread = threading.Thread(
        target=receive_broadcasts,
        args=(peer_list, discovery_lock),
        daemon=True
    )
    discovery_thread.start()

    # Start WebSocket server and main tasks
    server = await websockets.serve(handle_peer_connection, "0.0.0.0", 8765)
    logging.info("WebSocket server started")

    try:
        await asyncio.gather(connect_to_peers(), user_input(), display_messages())
    finally:
        server.close()
        await server.wait_closed()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nShutting down...")
