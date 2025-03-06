import asyncio
import logging
import websockets
import socket
import os
import json  
import netifaces  
from aioconsole import ainput
from networking.discovery import discovery
from networking.shared_state import active_transfers
from networking.file_transfer import send_file, receive_file

message_queue = asyncio.Queue()
connections = {}
peer_list = []
active_transfers = {}

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
    """Periodically clean up disconnected peers."""
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

async def send_message_to_peers(message):
    """Send a message to all connected peers."""
    for peer_ip, websocket in connections.items():
        try:
            await websocket.send(f"MESSAGE {message}")
        except Exception as e:
            logging.error(f"Failed to send message to {peer_ip}: {e}")

async def user_input():
    """Handles user input and sends messages to all connected peers."""
    while True:
        try:
            message = await ainput("> ")

            if message == "/help":
                print("""
                Available commands:
                /connect <ip>    - Connect to specific peer
                /disconnect <ip>  - Disconnect from peer
                /send <ip> <file> - Send file to peer
                /pause <ip> <tranfer_id> - Pause the File Transfer
                /resume <ip> <tranfer_id> - Resume the File Transfer
                /list             - Show available peers
                /help             - Show this help
                """)
                continue

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

                # Send file to a single peer
                await send_file(file_path, {peer_ip: connections[peer_ip]})
                continue
            
            if message.startswith("/pause "):
                parts = message[7:].split()
                if len(parts) < 1:
                    print("Usage: /pause <transfer_id>")
                    continue
            
                transfer_id = parts[0]
                if transfer_id in active_transfers:
                    await active_transfers[transfer_id].pause()
                    print(f"Transfer {transfer_id} paused")
                else:
                    print("Invalid transfer ID")
                continue

            if message.startswith("/resume "):
                parts = message[8:].split()
                if len(parts) < 1:
                    print("Usage: /resume <transfer_id>")
                    continue
                
                transfer_id = parts[0]
                if transfer_id in active_transfers:
                    await active_transfers[transfer_id].resume()
                    print(f"Transfer {transfer_id} resumed")
                else:
                    print("Invalid transfer ID")
                continue
                
            if message == "/transfers":
                print("\nActive Transfers:")
                for transfer_id, transfer in active_transfers.items():
                    status = transfer.state
                    progress = f"{transfer.transferred_size}/{transfer.total_size}"
                    print(f"- ID: {transfer_id} | {status} | Progress: {progress}")

            if not message.startswith("/"):
                    if connections:
                        await send_message_to_peers(message)
                    else:
                        print("No peers connected. Use /connect <ip> to connect.")    
                

            # # Send chat message to all peers
            # if connections:
            #     for peer_ip, websocket in list(connections.items()):
            #         try:
            #             await websocket.send(f"MESSAGE {message}")
            #         except Exception as e:
            #             logging.exception(f"Error sending to {peer_ip}: {e}")
            #             if peer_ip in connections:
            #                 del connections[peer_ip]
            # else:
            #     print("No peers connected to send message to.")

        except Exception as e:
            logging.exception(f"Error in user_input: {e}")
            await asyncio.sleep(1)

async def receive_peer_messages(websocket, peer_ip):
    """Enhanced message and file transfer handling"""
    try:
        while True:
            message = await websocket.recv()
            
            # Handle different message types
            try:
                data = json.loads(message)
            except json.JSONDecodeError:
                # Fallback to original message handling
                if message.startswith("MESSAGE "):
                    await message_queue.put(f"{peer_ip}: {message[8:]}")
                continue

            # File transfer initialization
            if data.get('type') == 'file_transfer_init':
                # Prepare for file transfer
                transfer_metadata = {
                    'filename': data['filename'],
                    'filesize': data['filesize'],
                    'transfer_id': data['transfer_id'],
                    'file_hash': data.get('file_hash')
                }
                
                # Store transfer metadata
                active_transfers[data['transfer_id']] = {
                    'metadata': transfer_metadata,
                    'status': 'pending'
                }
                
                await message_queue.put(f"📥 Preparing to receive '{data['filename']}' from {peer_ip}")
                asyncio.create_task(receive_file(websocket, transfer_metadata))

            # File chunk transfer
            elif data.get('type') == 'file_chunk':
                transfer_id = data.get('transfer_id')
                if transfer_id not in active_transfers:
                    logging.warning(f"Unknown transfer ID: {transfer_id}")
                    continue
                # Receive file chunks
                transfer_metadata = active_transfers[transfer_id]['metadata']
                await receive_file(websocket, transfer_metadata)

            # Transfer pause request
            elif data.get('type') == 'transfer_pause':
                transfer_id = data.get('transfer_id')
                if transfer_id in active_transfers:
                    active_transfers[transfer_id]['status'] = 'paused'
                    await message_queue.put(f"⏸️ Transfer {transfer_id} paused")
                else:
                    logging.warning(f"Pause request for unknown transfer: {transfer_id}")

            # Transfer resume request
            elif data.get('type') == 'transfer_resume':
                transfer_id = data.get('transfer_id')
                if transfer_id in active_transfers:
                    # Check if transfer was previously paused
                    if active_transfers[transfer_id]['status'] == 'paused':
                        active_transfers[transfer_id]['status'] = 'in_progress'
                        await message_queue.put(f"▶️ Transfer {transfer_id} resumed")
                    else:
                        logging.warning(f"Resume request for non-paused transfer: {transfer_id}")
                else:
                    logging.warning(f"Resume request for unknown transfer: {transfer_id}")

            # Message handling remains the same
            elif data.get('type') == 'MESSAGE':
                await message_queue.put(f"{peer_ip}: {data['message']}")
    
    except websockets.exceptions.ConnectionClosed:
        logging.info(f"Connection closed with {peer_ip}")
    finally:
        if peer_ip in connections:
            del connections[peer_ip]

async def get_own_ip():
        """Get the most appropriate IP address"""
        try:
            # Prefer non-loopback, non-local IPs
            for interface in netifaces.interfaces():
                try:
                    addrs = netifaces.ifaddresses(interface)
                    if netifaces.AF_INET in addrs:
                        ip = addrs[netifaces.AF_INET][0]['addr']
                        # Skip loopback and local addresses
                        if not (ip.startswith('127.') or ip.startswith('169.254.')):
                            return ip
                except ValueError:
                    continue

            # Fallback method
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.connect(("8.8.8.8", 80))
                return s.getsockname()[0]
            except Exception:
                return "127.0.0.1"
        except Exception as e:
            logging.error(f"IP detection failed: {e}")
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