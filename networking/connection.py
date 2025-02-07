import asyncio
import websockets
import logging
import sys

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    stream=sys.stdout,
)


async def connect_to_peer(peer_ip, port=8765):
    """
    Establishes a WebSocket connection to a peer.

    Args:
    peer_ip (str): The IP address of the peer.
    port (int, optional): The port to connect on. Defaults to 8765.

    Returns:
    websocket: The WebSocket connection if successful, None if not.
    """
    uri = f"ws://{peer_ip}:{port}"
    try:
        websocket = await websockets.connect(uri)
        logging.info(f"Successfully connected to {peer_ip}")
        return websocket
    except Exception as e:
        logging.error(f"Failed to connect to {peer_ip}: {e}")
        return None


async def send_message(websocket, message):
    """
    Sends a text message to a peer through a WebSocket.

    Args:
    websocket (websocket): The websocket connection.
    message (str): The text message to send.

    Returns:
        bool: True if success else false
    """
    if not websocket:
        logging.error("Websocket connection is not valid, could not send message")
        return False
    try:
        await websocket.send(message)
        return True
    except Exception as e:
        logging.error(f"Error sending message: {e}")
        return False


async def receive_message(websocket):
    """
    Receives a text message from a peer through a WebSocket.

    Args:
    websocket (websocket): The websocket connection.

    Returns:
        str: Received message
    """
    if not websocket:
        logging.error("Websocket connection is not valid, could not receive message")
        return False
    try:
        message = await websocket.recv()
        return message
    except Exception as e:
        logging.error(f"Error receiving message {e}")
        return False
