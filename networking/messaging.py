import asyncio
import logging
import websockets
import socket
from aioconsole import ainput
from networking.file_transfer import file_transfer_manager
from typing import Dict, Any, Tuple

# Shared state
message_queue = asyncio.Queue()
connections: Dict[str, websockets.WebSocketClientProtocol] = {}
peer_list = []

# Queues for sending messages/files to each peer
send_queues: Dict[str, asyncio.Queue[Tuple[str, Any]]] = {}  # Tuple: ("MESSAGE" or "FILE", data)

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
        if 'websocket' in locals() and websocket:
            await websocket.close()
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
                # Create send queue for each peer
                send_queues[peer_ip] = asyncio.Queue()
                # Start receiver and sender tasks
                asyncio.create_task(receive_peer_messages(websocket, peer_ip))
                asyncio.create_task(send_peer_messages(websocket, peer_ip))
                return True
            else:
                await websocket.close()
                return False
    except Exception as e:
        logging.exception(f"Error in connection handshake: {e}")
        # Remove peer from connections if handshake fails
        if peer_ip in connections:  # check if its in connections
            del connections[peer_ip]  # deletes it from connections
        return False


async def send_peer_messages(websocket: websockets.WebSocketClientProtocol, peer_ip: str):
    """Consumes messages from the send queue and sends them to the peer."""
    try:
        while True:
            message_type, data = await send_queues[peer_ip].get()
            if message_type == "MESSAGE":
                try:
                    await websocket.send(f"MESSAGE {data}")
                except Exception as e:
                    logging.exception(f"Error sending message to {peer_ip}: {e}")
                    if peer_ip in connections:
                        del connections[peer_ip]
                        break  # Exit the loop as the connection is broken

            elif message_type == "FILE":
                try:
                    await file_transfer_manager.send_file(data, websocket, peer_ip)
                except Exception as e:
                    logging.exception(f"Error sending file to {peer_ip}: {e}")

            send_queues[peer_ip].task_done()

    except Exception as e:
        logging.exception(f"Error in send_peer_messages: {e}")
    finally:
        # (Cleanup is done during disconnection to remove peers and queues)
        if peer_ip in connections:  # Check if peer still is in connections
            del connections[peer_ip]  # Deletes it from connections dictionary
        if peer_ip in send_queues:  # Check if there is a queue for the peer
            del send_queues[peer_ip]  # Deletes the queue for the peer.
        logging.info(f"Disconnected from {peer_ip} (sender)")  # Logging statement from original
        await websocket.close()  # Cleanly close the websocket.


async def receive_peer_messages(websocket, peer_ip):
    """Receives and processes messages from a connected peer."""
    receiving_file = False  # Add flag to track file transfer state
    try:
        while True:
            message = await websocket.recv()

            if receiving_file:
                if message.startswith("CHUNK"):
                    try:
                        await file_transfer_manager.receive_file(websocket, message)
                    except Exception as e:
                        logging.exception(f"Error receiving file chunk: {e}")
                        receiving_file = False  # Ensure flag is reset in case of an error
                elif message == "FILE_END":
                    receiving_file = False  # Reset flag upon completion
                else:
                    logging.warning(f"Ignoring message during file transfer: {message}")
                    continue  # Skip processing, wait for a valid message

            elif message.startswith("FILE_START"):
                try:
                    receiving_file = True  # Set flag before receiving file
                    await file_transfer_manager.receive_file(websocket, message)
                    receiving_file = False  # Reset flag after receiving file
                except Exception as e:
                    logging.exception(f"Error receiving file: {e}")
                    receiving_file = False  # Reset flag if initial file setup fails

            elif message.startswith("MESSAGE "):
                await message_queue.put(f"{peer_ip}: {message[8:]}")

            else:
                logging.warning(f"Received unknown message: {message}")

    except websockets.exceptions.ConnectionClosed:
        logging.info(f"Connection closed with {peer_ip}")
    except Exception as e:
        logging.exception(f"Error receiving from {peer_ip}: {e}")
    finally:
        if peer_ip in connections:
            del connections[peer_ip]
        receiving_file = False  # Ensure flag is reset upon websocket close
        logging.info(f"Disconnected from {peer_ip} (receiver)")


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


async def connect_to_peers(peer_list):
    """Continuously attempts to connect to discovered peers."""
    while True:
        try:
            for peer_ip in list(peer_list): # changed from peer_list[:]
                if peer_ip not in connections:
                    websocket = await connect_to_peer(peer_ip)
                    if websocket:
                        connections[peer_ip] = websocket
                        # Create send queue for each peer
                        # Remove receive_peer_messages from here as its started during handshake
                        # asyncio.create_task(receive_peer_messages(websocket, peer_ip)) # No start here
                        # asyncio.create_task(send_peer_messages(websocket, peer_ip)) # No start here

                        pass  # Tasks already launched in handle_incoming_connection

            await asyncio.sleep(5)
        except Exception as e:
            logging.exception(f"Error in connect_to_peers: {e}")
            await asyncio.sleep(5)

async def user_input():
    """Handles user input and sends messages to all connected peers through their send queues."""
    help_text = """
Available commands:
/send <file_path> - Send a file to all connected peers
/pause <file_name> - Pause a file transfer
/resume <file_name> - Resume a paused transfer
/transfers - List all transfers and their status
/help - Show this help message
"""
    while True:
        try:
            message = await ainput("> ")

            if message.startswith("/"):
                parts = message.split(maxsplit=1)
                command = parts[0]
                args = parts[1] if len(parts) > 1 else ""

                if command == "/help":
                    print(help_text)
                    continue

                elif command == "/transfers":
                    transfers = file_transfer_manager.list_transfers()
                    if not transfers:
                        print("No active or paused transfers.")
                    else:
                        print("\nCurrent Transfers:")
                        print("-" * 50)
                        for file_id, state in transfers.items():
                            progress = (
                                len(state.get("sent_chunks", set()))
                                / state["total_chunks"]
                                * 100
                            )
                            status = state["status"].upper()
                            print(f"File: {file_id}")
                            print(f"Status: {status}")
                            print(f"Progress: {progress:.1f}%")
                            print("-" * 50)
                    continue

                elif command == "/pause":
                    if not args:
                        print("Usage: /pause <file_name>")
                        continue

                    found = False
                    for file_id in list(file_transfer_manager.active_transfers.keys()):
                        if args in file_id:
                            await file_transfer_manager.pause_transfer(file_id)
                            found = True
                            break

                    if not found:
                        print(f"No active transfer found for '{args}'")
                    continue

                elif command == "/resume":
                    if not args:
                        print("Usage: /resume <file_name>")
                        continue

                    found = False
                    for file_id in list(file_transfer_manager.paused_transfers.keys()):
                        if args in file_id:
                            await file_transfer_manager.resume_transfer(file_id)
                            found = True
                            break

                    if not found:
                        print(f"No paused transfer found for '{args}'")
                    continue

                elif command == "/send":
                    if not args:
                        print("Usage: /send <file_path>")
                        continue

                    if connections:
                        for peer_ip in list(connections.keys()):  # Iterate through peer IPs
                            await send_queues[peer_ip].put(("FILE", args))  # Add file transfer request to queue
                    else:
                        print("No peers connected to send file to.")
                    continue

            # Handle regular messages (existing code)
            if connections:
                for peer_ip in list(connections.keys()):
                    await send_queues[peer_ip].put(("MESSAGE", message))
            else:
                print("No peers connected to send message to.")

        except Exception as e:
            logging.exception(f"Error in user_input: {e}")
            await asyncio.sleep(1)