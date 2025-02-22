import asyncio
import logging
import sys
import websockets

from networking.discovery import PeerDiscovery
from networking.messaging import (
    user_input,
    display_messages,
    receive_peer_messages,
    handle_incoming_connection,
    connections
)
from networking.file_transfer import send_file, receive_file

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    stream=sys.stdout
)

async def handle_peer_connection(websocket, path=None):
    """Handles incoming connections from websockets.serve."""
    peer_ip = websocket.remote_address[0]
    logging.info(f"New connection from {peer_ip}")

    if await handle_incoming_connection(websocket, peer_ip):
        await receive_peer_messages(websocket, peer_ip)

async def main():
    """Main function to run the P2P chat application."""
    # Initialize PeerDiscovery
    discovery = PeerDiscovery()

    # Start broadcast tasks
    broadcast_task = asyncio.create_task(discovery.send_broadcasts())
    discovery_task = asyncio.create_task(discovery.receive_broadcasts())
    cleanup_task = asyncio.create_task(discovery.cleanup_stale_peers())


    # Start WebSocket server
    server = await websockets.serve(
        handle_peer_connection,
        "0.0.0.0",
        8765,
        ping_interval=None,  # Disable ping to prevent timeouts during large transfers
        max_size=None,  # Remove message size limit
    )
    logging.info("WebSocket server started")

    try:
        await asyncio.gather(
            user_input(),
            display_messages(),
            broadcast_task,
            discovery_task,
            cleanup_task
        )
    finally:
        server.close()
        await server.wait_closed()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nShutting down...")