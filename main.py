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
from networking.file_transfer import send_file, update_transfer_progress

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    stream=sys.stdout
)

async def handle_peer_connection(websocket, path=None):
    peer_ip = websocket.remote_address[0]
    logging.info(f"New connection from {peer_ip}")
    try:
        if await handle_incoming_connection(websocket, peer_ip):
            await receive_peer_messages(websocket, peer_ip)
    except Exception as e:
        logging.error(f"Error handling connection from {peer_ip}: {e}")
    finally:
        if peer_ip in connections:
            del connections[peer_ip]

async def main():
    discovery = PeerDiscovery()
    broadcast_task = asyncio.create_task(discovery.send_broadcasts())
    discovery_task = asyncio.create_task(discovery.receive_broadcasts())
    cleanup_task = asyncio.create_task(discovery.cleanup_stale_peers())
    progress_task = asyncio.create_task(update_transfer_progress())

    server = await websockets.serve(
        handle_peer_connection,
        "0.0.0.0",
        8765,
        ping_interval=None,
        max_size=None,
    )
    logging.info("WebSocket server started")

    try:
        await asyncio.gather(
            user_input(),
            display_messages(),
            broadcast_task,
            discovery_task,
            cleanup_task,
            progress_task,
            maintain_peer_list(discovery)
        )
    except KeyboardInterrupt:
        logging.info("Keyboard interrupt received. Shutting down...")
    finally:
        server.close()
        await server.wait_closed()
        for websocket in list(connections.values()):
            try:
                await websocket.close()
            except Exception as e:
                logging.error(f"Error closing connection: {e}")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nShutting down...")
    except Exception as e:
        print(f"Unexpected error: {e}")
        sys.exit(1)