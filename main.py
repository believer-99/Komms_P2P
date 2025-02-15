import asyncio
import logging
import sys
import websockets

from networking.discovery import PeerDiscovery
from networking.messaging import (
    user_input,
    display_messages,
    connect_to_peers,
    receive_peer_messages,
    handle_incoming_connection,
    connections,
    send_queues,  # Import send_queues
)

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

    # Create send queue for the peer if not exists
    if peer_ip not in send_queues:
        send_queues[peer_ip] = asyncio.Queue()

    if await handle_incoming_connection(websocket, peer_ip):
        # The receive_peer_messages is now started in handle_incoming_connection
        pass
    else:
        # If connection handling failed, ensure websocket is closed
        await websocket.close()

async def main():
    """Main function to run the P2P chat application."""
    # Initialize PeerDiscovery
    discovery = PeerDiscovery()

    # Start broadcast tasks
    broadcast_task = asyncio.create_task(discovery.send_broadcasts())
    discovery_task = asyncio.create_task(discovery.receive_broadcasts())

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
            connect_to_peers(discovery.peer_list),
            user_input(),
            display_messages(),
            broadcast_task,
            discovery_task
        )
    finally:
        server.close()
        await server.wait_closed()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nShutting down...")