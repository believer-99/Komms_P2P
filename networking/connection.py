import asyncio
import websockets
import logging

async def connect_to_peer(peer_ip, port=8765):
    """Establishes a WebSocket connection to a peer."""
    uri = f"ws://{peer_ip}:{port}"
    try:
        websocket = await websockets.connect(uri)
        logging.info(f"Successfully connected to {peer_ip}")
        return websocket
    except Exception as e:
        logging.error(f"Failed to connect to {peer_ip}: {e}")
        return None