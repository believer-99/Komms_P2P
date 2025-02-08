# networking/messaging.py
import asyncio
import logging
import websockets
from aioconsole import ainput
from networking.connection import connect_to_peer

# Shared state
message_queue = asyncio.Queue()  # Queue for incoming messages
connections = {}  # Dictionary to hold active connections
peer_list = []  # List to store discovered peers

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

async def receive_peer_messages(websocket, peer_ip):
    """Receives messages from a peer and puts them in the message queue."""
    try:
        async for message in websocket:
            await message_queue.put((peer_ip, message))
    except websockets.exceptions.ConnectionClosed:
        logging.info(f"Connection closed to {peer_ip}")
        if peer_ip in connections:
            del connections[peer_ip]
    finally:
        logging.info(f"Stopped receiving messages from {peer_ip}")

async def display_messages():
    """Asynchronously displays messages from the queue."""
    while True:
        peer_ip, message = await message_queue.get()  # Get message from queue
        if message.startswith("FILE "):  # Handle file transfer messages
            _, file_name, file_size = message.split(" ")
            print(f"\nReceiving file '{file_name}' from {peer_ip}")
            await receive_file(connections[peer_ip], file_name, int(file_size))
        else:
            print(f"\n{peer_ip}: {message}")  # Display the message
        message_queue.task_done()

async def user_input():
    """Handle user input for sending messages or files."""
    while True:
        try:
            if not connections:
                print("\rWaiting for peers to connect...", end="", flush=True)
                await asyncio.sleep(1)
                continue

            await display_peers_and_prompt()
            user_input_str = await ainput()

            if not user_input_str:
                continue

            if user_input_str.lower() == "exit":
                return

            if user_input_str.lower().startswith("sendfile "):
                # Extract the file path
                file_path = user_input_str.split(" ", 1)[1]
                await send_file(file_path)
                continue

            parts = user_input_str.split(" ", 1)
            if len(parts) != 2:
                print("Invalid format. Use: <recipient_number/all> <message>")
                continue

            recipient, message = parts
            peer_ips = list(connections.keys())

            if recipient.lower() == "all":
                targets = peer_ips
            else:
                try:
                    idx = int(recipient) - 1
                    if 0 <= idx < len(peer_ips):
                        targets = [peer_ips[idx]]
                    else:
                        print("Invalid peer number")
                        continue
                except ValueError:
                    print("Invalid recipient. Use number or 'all'")
                    continue

            for peer_ip in targets:
                try:
                    await connections[peer_ip].send(message)
                    print(f"Sent to {peer_ip}")
                except Exception as e:
                    logging.error(f"Failed to send to {peer_ip}: {e}")
                    if peer_ip in connections:
                        del connections[peer_ip]
                    print(f"Failed to send to {peer_ip}")

        except Exception as e:
            logging.error(f"Error in user input: {e}")
            await asyncio.sleep(1)

def list_peers():
    """List all connected peers."""
    if not connections:
        print("No peers connected")
        return
    for i, peer in enumerate(connections.keys(), 1):
        print(f"{i}. {peer}")

async def display_peers_and_prompt():
    """Display available peers and input prompt."""
    print("\nAvailable peers:")
    list_peers()
    print("Enter recipient number (or 'all') and message: ", end="", flush=True)
