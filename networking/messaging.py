import asyncio
import logging
import websockets
import socket
from aioconsole import ainput
from networking.file_transfer import send_file, receive_file

# Shared state
message_queue = asyncio.Queue()
connections = {}
peer_list = []

async def connect_to_peer(peer_ip, port=8765):
    """Establishes a WebSocket connection to a peer."""
    if peer_ip in connections:
        return None

    uri = f"ws://{peer_ip}:{port}"
    try:
        websocket = await websockets.connect(
            uri,
            ping_interval=None,  # Disable ping
            max_size=None,  # Remove message size limit
        )
        own_ip = await get_own_ip()
        await websocket.send(f"INIT {own_ip}")

        # Wait for INIT response
        response = await websocket.recv()
        if response.startswith("INIT_ACK"):
            logging.info(f"Successfully connected to {peer_ip}")
            return websocket
        else:
            await websocket.close()
            return None

    except Exception as e:
        logging.exception(f"Failed to connect to {peer_ip}: {e}")
        return None

async def handle_incoming_connection(websocket, peer_ip):
    """Handle new incoming connection setup."""
    try:
        message = await websocket.recv()
        if message.startswith("INIT "):
            _, sender_ip = message.split(" ", 1)
            own_ip = await get_own_ip()

            # Only accept connection if we don't already have one
            if peer_ip not in connections:
                await websocket.send("INIT_ACK")
                connections[peer_ip] = websocket
                logging.info(f"Accepted connection from {peer_ip}")
                return True
            else:
                await websocket.close()
                return False
    except Exception as e:
        logging.exception(f"Error in connection handshake: {e}")
        return False

async def maintain_peer_list():
    """Periodically clean up disconnected peers"""
    while True:
        try:
            # Remove dead connections
            for peer_ip in list(connections.keys()):
                if connections[peer_ip].closed:
                    del connections[peer_ip]
            
            # Update peer list from discovery
            global peer_list
            peer_list = discovery.peer_list.copy()
            
            await asyncio.sleep(5)
        except Exception as e:
            logging.exception(f"Error in maintain_peer_list: {e}")
            await asyncio.sleep(5)

async def user_input():
    """Handles user input and sends messages to all connected peers."""
    while True:
        try:
            message = await ainput("> ")

            # Add new connection management commands
            if message == "/list":
                print("\nAvailable peers:")
                for peer in peer_list:
                    status = "Connected" if peer in connections else "Available"
                    print(f"- {peer} ({status})")
                continue
                
            if message.startswith("/connect "):
                peer_ip = message[9:].strip()
                if peer_ip == await get_own_ip():
                    print("Cannot connect to self")
                    continue
                    
                if peer_ip in connections:
                    print(f"Already connected to {peer_ip}")
                    continue
                    
                print(f"Attempting connection to {peer_ip}...")
                websocket = await connect_to_peer(peer_ip)
                if websocket:
                    connections[peer_ip] = websocket
                    asyncio.create_task(receive_peer_messages(websocket, peer_ip))
                continue
                
            if message.startswith("/disconnect "):
                peer_ip = message[12:].strip()
                if peer_ip in connections:
                    try:
                        await connections[peer_ip].close()
                    except:
                        pass
                    del connections[peer_ip]
                    print(f"Disconnected from {peer_ip}")
                else:
                    print(f"Not connected to {peer_ip}")
                continue

            if message.startswith("/send "):
    parts = message[6:].split(" ", 1)
    if len(parts) < 2:
        print("Usage: /send <peer_ip> <file_path>")
        continue
        
    peer_ip, file_path = parts
    file_path = file_path.strip()
    
    if peer_ip not in connections:
        print(f"Not connected to {peer_ip}")
        continue
        
    if not os.path.exists(file_path):
        print(f"File not found: {file_path}")
        continue
        
    # Send to single peer
    await send_file(file_path, {peer_ip: connections[peer_ip]})

            if connections:
                for peer_ip, websocket in list(connections.items()):
                    try:
                        await websocket.send(f"MESSAGE {message}")
                    except Exception as e:
                        logging.exception(f"Error sending to {peer_ip}: {e}")
                        if peer_ip in connections:
                            del connections[peer_ip]
            else:
                print("No peers connected to send message to.")

        except Exception as e:
            logging.exception(f"Error in user_input: {e}")
            await asyncio.sleep(1)

async def receive_peer_messages(websocket, peer_ip):
    """Receives and processes messages from a connected peer."""
    try:
        while True:
            message = await websocket.recv()

            if message.startswith("FILE "):
                try:
                    _, file_name, file_size, start_byte = message.split(" ", 3)
                    await receive_file(websocket, file_name, int(file_size), int(start_byte))
                except Exception as e:
                    logging.exception(f"Error receiving file: {e}")
            elif message.startswith("MESSAGE "):
                await message_queue.put(f"{peer_ip}: {message[8:]}")
    except websockets.exceptions.ConnectionClosed:
        logging.info(f"Connection closed with {peer_ip}")
    except Exception as e:
        logging.exception(f"Error receiving from {peer_ip}: {e}")
    finally:
        if peer_ip in connections:
            del connections[peer_ip]
        logging.info(f"Disconnected from {peer_ip}")


async def get_own_ip():
    """Get the IP address of the current machine."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"

async def display_messages():
    """Displays messages from the message queue."""
    while True:
        try:
            message = await message_queue.get()
            print(f"\n{message}")
            print("> ", end="", flush=True)
        except Exception as e:
            logging.exception(f"Error displaying message: {e}")
            await asyncio.sleep(1)