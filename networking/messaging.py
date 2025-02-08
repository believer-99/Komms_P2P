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

async def user_input():
    """Handles user input and sends messages to all connected peers."""
    while True:
        try:
            message = await ainput("> ")  # Use aioconsole for async input
            
            # Check if it's a file transfer command
            if message.startswith("/send "):
                file_path = message[6:].strip()
                await send_file(file_path)
                continue
                
            # Send the message to all connected peers
            for peer_ip, websocket in list(connections.items()):
                try:
                    await websocket.send(f"MESSAGE {message}")
                except Exception as e:
                    logging.error(f"Error sending to {peer_ip}: {e}")
                    del connections[peer_ip]
        except Exception as e:
            logging.error(f"Error in user_input: {e}")
            await asyncio.sleep(1)

async def receive_peer_messages(websocket, peer_ip):
    """Receives and processes messages from a connected peer."""
    try:
        async for message in websocket:
            try:
                if message.startswith("FILE "):
                    # Handle file transfer
                    _, file_name, file_size = message.split(" ", 2)
                    await receive_file(websocket, file_name, int(file_size))
                else:
                    # Handle regular message
                    await message_queue.put(f"{peer_ip}: {message[8:]}")  # Remove "MESSAGE " prefix
            except Exception as e:
                logging.error(f"Error processing message from {peer_ip}: {e}")
    except websockets.exceptions.ConnectionClosed:
        logging.info(f"Connection closed with {peer_ip}")
    except Exception as e:
        logging.error(f"Error receiving from {peer_ip}: {e}")
    finally:
        if peer_ip in connections:
            del connections[peer_ip]
        logging.info(f"Disconnected from {peer_ip}")

async def display_messages():
    """Displays messages from the message queue."""
    while True:
        try:
            message = await message_queue.get()
            print(f"\n{message}")
            print("> ", end="", flush=True)  # Redraw the prompt
        except Exception as e:
            logging.error(f"Error displaying message: {e}")
            await asyncio.sleep(1)