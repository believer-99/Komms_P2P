# networking/messaging.py
import asyncio
import logging
import websockets
import socket
import os
import json
import hashlib
import aiofiles
import netifaces
from aioconsole import ainput
from networking.discovery import PeerDiscovery
from networking.shared_state import active_transfers, message_queue, connections
from networking.file_transfer import send_file, FileTransfer, TransferState, FileTransferManager

peer_list = []

async def connect_to_peer(peer_ip, port=8765):
    if peer_ip in connections:
        return None
    uri = f"ws://{peer_ip}:{port}"
    try:
        websocket = await websockets.connect(
            uri,
            ping_interval=None,
            max_size=None,
        )
        own_ip = await get_own_ip()
        await websocket.send(f"INIT {own_ip}")
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
    try:
        message = await websocket.recv()
        if message.startswith("INIT "):
            _, sender_ip = message.split(" ", 1)
            own_ip = await get_own_ip()
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

aasync def maintain_peer_list(discovery_instance):
    global peer_list
    while True:
        try:
            for peer_ip in list(connections.keys()):
                try:
                    await connections[peer_ip].ping()  # Send a ping message
                except websockets.exceptions.ConnectionClosed:
                    del connections[peer_ip]
                except Exception as e:
                    logging.exception(f"Unexpected error checking connection to {peer_ip}: {e}")
                    del connections[peer_ip] #remove the possibly corrupted entry
            peer_list = discovery_instance.peer_list.copy()
            await asyncio.sleep(5)
        except Exception as e:
            logging.exception(f"Error in maintain_peer_list: {e}")
            await asyncio.sleep(5)

async def send_message_to_peers(message, target_ip=None):
    if target_ip is not None:
        if target_ip in connections:
            # CHECK IF WEBSOCKET IS STILL OPEN
            if connections[target_ip].open: # ADDED CHECK HERE
                try:
                    await connections[target_ip].send(
                        json.dumps({"type": "MESSAGE", "message": message})
                    )
                    return True
                except Exception as e:
                    logging.error(f"Failed to send message to {target_ip}: {e}")
                    if not connections[target_ip].open:
                        if target_ip in connections:
                            del connections[target_ip]
                    return False
            else:
                logging.warning(f"Websocket to {target_ip} is closed.")
                del connections[target_ip]  # Remove closed connection
                return False


        else:
            print(f"No peers connected. Use /connect <ip> to connect.")
            return False
    
    for peer_ip, websocket in list(connections.items()):
        try:
            await websocket.send(
                json.dumps({"type": "MESSAGE", "message": message})
            )
        except Exception as e:
            logging.error(f"Failed to send message to {peer_ip}: {e}")
            if not websocket.open:
                if peer_ip in connections:
                    del connections[peer_ip]
    return True

async def receive_peer_messages(websocket, peer_ip):
    try:
        async for message in websocket:
            try:
                data = json.loads(message)
                message_type = data.get("type")

                if message_type == "file_transfer_init":
                    transfer_id = data["transfer_id"]
                    if transfer_id in active_transfers:
                        logging.warning(f"Transfer ID {transfer_id} already exists")
                        continue

                    file_name = data["filename"]
                    file_size = data["filesize"]
                    expected_hash = data.get("file_hash")

                    download_dir = FileTransferManager.get_download_directory()
                    file_path = os.path.join(download_dir, file_name)

                    transfer = FileTransfer(file_path, peer_ip, direction="receive")
                    transfer.transfer_id = transfer_id
                    transfer.total_size = file_size
                    transfer.expected_hash = expected_hash
                    transfer.hash_algo = hashlib.sha256() if expected_hash else None
                    transfer.state = TransferState.IN_PROGRESS
                    transfer.file_handle = await aiofiles.open(file_path, "wb")

                    active_transfers[transfer_id] = transfer
                    await message_queue.put(f"📥 Receiving '{file_name}' from {peer_ip} (Transfer ID: {transfer_id})")
                    print(f"\nReceiving '{file_name}' ({file_size} bytes) from {peer_ip}")

                elif message_type == "file_chunk":
                    transfer_id = data["transfer_id"]
                    transfer = active_transfers.get(transfer_id)
                    if transfer and transfer.direction == "receive":
                        async with transfer.condition:
                            while transfer.paused:
                                await transfer.condition.wait()
                        chunk = bytes.fromhex(data["chunk"])
                        await transfer.file_handle.write(chunk)
                        transfer.transferred_size += len(chunk)
                        if transfer.hash_algo:
                            transfer.hash_algo.update(chunk)

                        if transfer.transferred_size >= transfer.total_size:
                            await transfer.file_handle.close()
                            transfer.file_handle = None
                            if transfer.expected_hash:
                                calculated_hash = transfer.hash_algo.hexdigest()
                                if calculated_hash != transfer.expected_hash:
                                    logging.error(f"File integrity check failed for {file_path}")
                                    os.remove(file_path)
                                    transfer.state = TransferState.FAILED
                                    await message_queue.put(f"❌ File transfer failed: integrity check failed")
                                else:
                                    transfer.state = TransferState.COMPLETED
                                    await message_queue.put(f"✅ File saved as: {file_path}")
                            else:
                                transfer.state = TransferState.COMPLETED
                                await message_queue.put(f"✅ File saved as: {file_path}")
                            print(f"✅ File saved as: {file_path}")

                elif message_type == "MESSAGE":
                    await message_queue.put(f"{peer_ip}: {data['message']}")

            except json.JSONDecodeError:
                if message.startswith("MESSAGE "):
                    await message_queue.put(f"{peer_ip}: {message[8:]}")

    except websockets.exceptions.ConnectionClosed:
        logging.info(f"Connection closed with {peer_ip}")
    except Exception as e:
        logging.exception(f"Error receiving messages from {peer_ip}: {e}")
    finally:
        if peer_ip in connections:
            del connections[peer_ip]
        await message_queue.put(f"Disconnected from {peer_ip}")

async def user_input():
    while True:
        try:
            message = await ainput("> ")

            if message == "/help":
                print(
                    """
                Available commands:
                /connect <ip>    - Connect to specific peer
                /disconnect <ip> - Disconnect from peer
                /msg <ip> <message> - Send message to specific peer
                /send <ip> <file> - Send file to peer
                /pause <transfer_id> - Pause the File Transfer
                /resume <transfer_id> - Resume the File Transfer
                /transfers       - List active transfers
                /list             - Show available peers
                /help             - Show this help
                """
                )
                continue

            if message == "/list":
                print("\nAvailable peers:")
                for peer in peer_list:
                    status = "Connected" if peer in connections else "Discovered"
                    print(f"- {peer} ({status})")
                for peer in connections:
                    if peer not in peer_list:
                        print(f"- {peer} (Connected)")
                if not peer_list and not connections:
                    print("No peers available")
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
                    print(f"Connected to {peer_ip}")
                else:
                    print(f"Failed to connect to {peer_ip}")
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

            if message.startswith("/msg "):
                parts = message[5:].split(" ", 1)
                if len(parts) < 2:
                    print("Usage: /msg <peer_ip> <message>")
                    continue
                    
                peer_ip, msg_content = parts
                # CHECK IF PEER IS IN CONNECTIONS AND IF THE WEBSOCKET IS OPEN.
                if peer_ip not in connections or not connections[peer_ip].open:
                    print(f"Not connected to {peer_ip}")
                    continue
                
                success = await send_message_to_peers(msg_content, target_ip=peer_ip)
                if success:
                    await message_queue.put(f"You → {peer_ip}: {msg_content}")
                else:
                    print(f"Failed to send message to {peer_ip}")
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
                asyncio.create_task(send_file(file_path, {peer_ip: connections[peer_ip]}))
                print(f"Started sending {file_path} to {peer_ip}")
                continue

            if message.startswith("/pause "):
                transfer_id = message[7:].strip()
                transfer = active_transfers.get(transfer_id)
                if transfer and isinstance(transfer, FileTransfer):
                    asyncio.create_task(transfer.pause())
                    print(f"Pausing transfer {transfer_id}")
                else:
                    print(f"No transferable found with ID {transfer_id}")
                continue

            if message.startswith("/resume "):
                transfer_id = message[8:].strip()
                transfer = active_transfers.get(transfer_id)
                if transfer and isinstance(transfer, FileTransfer):
                    asyncio.create_task(transfer.resume())
                    print(f"Resuming transfer {transfer_id}")
                else:
                    print(f"No transferable found with ID {transfer_id}")
                continue

            if message == "/transfers":
                print("\nActive Transfers:")
                if not active_transfers:
                    print("No active transfers")
                else:
                    for transfer_id, transfer in active_transfers.items():
                        if isinstance(transfer, FileTransfer):
                            status = transfer.state
                            progress = f"{transfer.transferred_size}/{transfer.total_size}"
                            print(f"- ID: {transfer_id} | {status} | Progress: {progress}")
                        else:
                            print(f"- ID: {transfer_id} | Status: {transfer.get('status', 'unknown')}")
                continue

          
            if not message.startswith("/"):
                if connections:
                    await send_message_to_peers(message)
                    await message_queue.put(f"You (to all): {message}")
                else:
                    print("No peers connected. Use /connect <ip> to connect.")

        except Exception as e:
            logging.exception(f"Error in user_input: {e}")
            await asyncio.sleep(1)

async def get_own_ip():
    try:
        for interface in netifaces.interfaces():
            try:
                addrs = netifaces.ifaddresses(interface)
                if netifaces.AF_INET in addrs:
                    ip = addrs[netifaces.AF_INET][0]["addr"]
                    if not (ip.startswith("127.") or ip.startswith("169.254.")):
                        return ip
            except ValueError:
                continue
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
    while True:
        try:
            message = await message_queue.get()
            print(f"\n{message}")
            print("> ", end="", flush=True)
        except Exception as e:
            logging.exception(f"Error displaying message: {e}")
            await asyncio.sleep(1)