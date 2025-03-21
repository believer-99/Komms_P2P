import asyncio
import logging
import websockets
import socket
import os
import json
import hashlib
import aiofiles
import netifaces
import uuid
from aioconsole import ainput
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric import rsa, padding
from networking.discovery import PeerDiscovery
from networking.shared_state import active_transfers, message_queue, connections, user_data, peer_public_keys, peer_usernames
from networking.file_transfer import send_file, FileTransfer, TransferState, FileTransferManager
from websockets.connection import State

peer_list = []

async def initialize_user_config():
    config_file = "user_config.json"
    if os.path.exists(config_file):
        with open(config_file, "r") as f:
            user_data.update(json.load(f))
        user_data["public_key"] = serialization.load_pem_public_key(user_data["public_key"].encode())
        user_data["private_key"] = serialization.load_pem_private_key(user_data["private_key"].encode(), password=None)
    else:
        original_username = await ainput("Enter your username: ")
        internal_username = f"{original_username}_{uuid.uuid4()}"
        device_id = str(uuid.uuid4())
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        public_key = private_key.public_key()
        user_data.update({
            "original_username": original_username,
            "internal_username": internal_username,
            "device_id": device_id,
            "public_key": public_key,
            "private_key": private_key,
        })
        with open(config_file, "w") as f:
            json.dump({
                "original_username": original_username,
                "internal_username": internal_username,
                "device_id": device_id,
                "public_key": public_key.public_bytes(
                    encoding=serialization.Encoding.PEM,
                    format=serialization.PublicFormat.SubjectPublicKeyInfo
                ).decode(),
                "private_key": private_key.private_bytes(
                    encoding=serialization.Encoding.PEM,
                    format=serialization.PrivateFormat.PKCS8,
                    encryption_algorithm=serialization.NoEncryption()
                ).decode(),
            }, f)
    print(f"Welcome, {user_data['original_username']} (Device ID: {user_data['device_id']})")

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
            public_key_pem = user_data["public_key"].public_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PublicFormat.SubjectPublicKeyInfo
            ).decode()
            await websocket.send(json.dumps({
                "type": "IDENTITY",
                "username": user_data["original_username"],
                "device_id": user_data["device_id"],
                "key": public_key_pem
            }))
            identity_message = await websocket.recv()
            identity_data = json.loads(identity_message)
            if identity_data["type"] == "IDENTITY":
                peer_username = identity_data["username"]
                peer_public_keys[peer_ip] = serialization.load_pem_public_key(identity_data["key"].encode())
                peer_usernames[peer_username] = peer_ip
            connections[peer_ip] = websocket
            logging.info(f"{user_data['original_username']} connected to {peer_username} ({peer_ip})")
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
                public_key_pem = user_data["public_key"].public_bytes(
                    encoding=serialization.Encoding.PEM,
                    format=serialization.PublicFormat.SubjectPublicKeyInfo
                ).decode()
                await websocket.send(json.dumps({
                    "type": "IDENTITY",
                    "username": user_data["original_username"],
                    "device_id": user_data["device_id"],
                    "key": public_key_pem
                }))
                identity_message = await websocket.recv()
                identity_data = json.loads(identity_message)
                if identity_data["type"] == "IDENTITY":
                    peer_username = identity_data["username"]
                    peer_public_keys[peer_ip] = serialization.load_pem_public_key(identity_data["key"].encode())
                    peer_usernames[peer_username] = peer_ip
                connections[peer_ip] = websocket
                logging.info(f"{user_data['original_username']} accepted connection from {peer_username} ({peer_ip})")
                return True
            else:
                await websocket.close()
                return False
    except Exception as e:
        logging.exception(f"Error in connection handshake: {e}")
        return False

async def maintain_peer_list(discovery_instance):
    global peer_list
    while True:
        try:
            for peer_ip in list(connections.keys()):
                try:
                    await connections[peer_ip].ping()
                except websockets.exceptions.ConnectionClosed:
                    del connections[peer_ip]
                    del peer_public_keys[peer_ip]
                    for username, ip in list(peer_usernames.items()):
                        if ip == peer_ip:
                            del peer_usernames[username]
                except Exception as e:
                    logging.exception(f"Unexpected error checking connection to {peer_ip}: {e}")
                    del connections[peer_ip]
                    del peer_public_keys[peer_ip]
                    for username, ip in list(peer_usernames.items()):
                        if ip == peer_ip:
                            del peer_usernames[username]
            peer_list = discovery_instance.peer_list.copy()
            await asyncio.sleep(5)
        except Exception as e:
            logging.exception(f"Error in maintain_peer_list: {e}")
            await asyncio.sleep(5)

async def send_message_to_peers(message, target_username=None):
    if target_username:
        if target_username in peer_usernames:
            peer_ip = peer_usernames[target_username]
            if peer_ip in connections and connections[peer_ip].state == State.OPEN:
                encrypted_message = peer_public_keys[peer_ip].encrypt(
                    message.encode(),
                    padding.OAEP(
                        mgf=padding.MGF1(algorithm=hashes.SHA256()),
                        algorithm=hashes.SHA256(),
                        label=None
                    )
                ).hex()
                try:
                    await connections[peer_ip].send(
                        json.dumps({"type": "MESSAGE", "message": encrypted_message})
                    )
                    logging.info(f"{user_data['original_username']} sent message to {target_username} ({peer_ip})")
                    return True
                except Exception as e:
                    logging.error(f"Failed to send message to {target_username} ({peer_ip}): {e}")
                    if connections[peer_ip].state != State.OPEN:
                        del connections[peer_ip]
                        del peer_public_keys[peer_ip]
                        del peer_usernames[target_username]
                    return False
            else:
                logging.warning(f"No active connection to {target_username}")
                return False
        else:
            return False  # Error handled in user_input
    
    for peer_ip, websocket in list(connections.items()):
        if websocket.state == State.OPEN:
            try:
                peer_username = next(u for u, ip in peer_usernames.items() if ip == peer_ip)
                encrypted_msg = peer_public_keys[peer_ip].encrypt(
                    message.encode(),
                    padding.OAEP(
                        mgf=padding.MGF1(algorithm=hashes.SHA256()),
                        algorithm=hashes.SHA256(),
                        label=None
                    )
                ).hex()
                await websocket.send(
                    json.dumps({"type": "MESSAGE", "message": encrypted_msg})
                )
                logging.info(f"{user_data['original_username']} sent message to {peer_username} ({peer_ip})")
            except Exception as e:
                logging.error(f"Failed to send message to {peer_ip}: {e}")
                if websocket.state != State.OPEN:
                    del connections[peer_ip]
                    del peer_public_keys[peer_ip]
                    for username, ip in list(peer_usernames.items()):
                        if ip == peer_ip:
                            del peer_usernames[username]
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
                    peer_username = next(u for u, ip in peer_usernames.items() if ip == peer_ip)
                    await message_queue.put(f"{user_data['original_username']} receiving '{file_name}' from {peer_username} (Transfer ID: {transfer_id})")
                    print(f"\nReceiving '{file_name}' ({file_size} bytes) from {peer_username}")

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
                                    await message_queue.put(f"{user_data['original_username']} file transfer failed: integrity check failed")
                                else:
                                    transfer.state = TransferState.COMPLETED
                                    await message_queue.put(f"{user_data['original_username']} file saved as: {file_path}")
                            else:
                                transfer.state = TransferState.COMPLETED
                                await message_queue.put(f"{user_data['original_username']} file saved as: {file_path}")
                            print(f"✅ File saved as: {file_path}")

                elif message_type == "MESSAGE":
                    decrypted_message = data["message"]
                    if peer_ip in peer_public_keys:
                        try:
                            decrypted_message = user_data["private_key"].decrypt(
                                bytes.fromhex(data["message"]),
                                padding.OAEP(
                                    mgf=padding.MGF1(algorithm=hashes.SHA256()),
                                    algorithm=hashes.SHA256(),
                                    label=None
                                )
                            ).decode()
                        except Exception as e:
                            logging.error(f"Failed to decrypt message from {peer_ip}: {e}")
                    peer_username = next(u for u, ip in peer_usernames.items() if ip == peer_ip)
                    await message_queue.put(f"{peer_username}: {decrypted_message}")

            except json.JSONDecodeError:
                if message.startswith("MESSAGE "):
                    peer_username = next(u for u, ip in peer_usernames.items() if ip == peer_ip)
                    await message_queue.put(f"{peer_username}: {message[8:]}")

    except websockets.exceptions.ConnectionClosed:
        peer_username = next((u for u, ip in peer_usernames.items() if ip == peer_ip), "unknown")
        logging.info(f"{user_data['original_username']} connection closed with {peer_username} ({peer_ip})")
    except Exception as e:
        logging.exception(f"Error receiving messages from {peer_ip}: {e}")
    finally:
        if peer_ip in connections:
            del connections[peer_ip]
            del peer_public_keys[peer_ip]
            for username, ip in list(peer_usernames.items()):
                if ip == peer_ip:
                    del peer_usernames[username]
        peer_username = next((u for u, ip in peer_usernames.items() if ip == peer_ip), "unknown")
        await message_queue.put(f"{user_data['original_username']} disconnected from {peer_username}")

async def user_input():
    await asyncio.sleep(1)  # Ensure user_data is initialized
    while True:
        try:
            message = await ainput(f"{user_data['original_username']} > ")

            if message == "/help":
                print(
                    """
                Available commands:
                /connect <username>    - Connect to a peer by username
                /disconnect <username> - Disconnect from a peer by username
                /msg <username> <message> - Send message to a peer by username
                /send <username> <file> - Send file to a peer by username
                /pause <transfer_id>   - Pause a file transfer
                /resume <transfer_id>  - Resume a file transfer
                /transfers             - List active transfers
                /list                  - Show available peers
                /changeName            - Change your username
                /help                  - Show this help
                """
                )
                continue

            if message == "/list":
                print("\nAvailable peers:")
                discovered_ips = set(peer_list)
                connected_usernames = set(peer_usernames.keys())
                for username in connected_usernames:
                    ip = peer_usernames[username]
                    status = "Connected" if ip in connections else "Discovered"
                    print(f"- {username} ({status})")
                for ip in discovered_ips - set(peer_usernames.values()):
                    print(f"- {ip} (Discovered, not yet connected)")
                if not peer_list and not peer_usernames:
                    print("No peers available")
                continue

            if message.startswith("/connect "):
                target_username = message[9:].strip()
                if target_username == user_data["original_username"]:
                    print("Cannot connect to self")
                    continue
                if target_username in peer_usernames:
                    print(f"Already connected to {target_username}")
                    continue
                # Check if target_username is in discovered peers by IP
                discovered_ips = set(peer_list)
                peer_ip = None
                for ip in discovered_ips:
                    websocket = await connect_to_peer(ip)
                    if websocket:
                        connections[ip] = websocket
                        asyncio.create_task(receive_peer_messages(websocket, ip))
                        if target_username in peer_usernames:
                            peer_ip = peer_usernames[target_username]
                            break
                if peer_ip:
                    print(f"{user_data['original_username']} connected to {target_username}")
                else:
                    print(f"No such peer exists: {target_username}")
                continue

            if message.startswith("/disconnect "):
                target_username = message[12:].strip()
                if target_username not in peer_usernames:
                    print(f"No such peer exists: {target_username}")
                    continue
                peer_ip = peer_usernames[target_username]
                if peer_ip in connections:
                    try:
                        await connections[peer_ip].close()
                    except:
                        pass
                    del connections[peer_ip]
                    del peer_public_keys[peer_ip]
                    del peer_usernames[target_username]
                    print(f"{user_data['original_username']} disconnected from {target_username}")
                else:
                    print(f"Not connected to {target_username}")
                continue

            if message.startswith("/msg "):
                parts = message[5:].split(" ", 1)
                if len(parts) < 2:
                    print("Usage: /msg <username> <message>")
                    continue
                target_username, msg_content = parts
                if target_username not in peer_usernames:
                    print(f"No such peer exists: {target_username}")
                    continue
                peer_ip = peer_usernames[target_username]
                if peer_ip not in connections or connections[peer_ip].state != State.OPEN:
                    print(f"Not connected to {target_username}")
                    continue
                success = await send_message_to_peers(msg_content, target_username=target_username)
                if success:
                    await message_queue.put(f"{user_data['original_username']} → {target_username}: {msg_content}")
                else:
                    print(f"Failed to send message to {target_username}")
                continue

            if message.startswith("/send "):
                parts = message[6:].split(" ", 1)
                if len(parts) < 2:
                    print("Usage: /send <username> <file_path>")
                    continue
                target_username, file_path = parts
                file_path = file_path.strip()
                if target_username not in peer_usernames:
                    print(f"No such peer exists: {target_username}")
                    continue
                peer_ip = peer_usernames[target_username]
                if peer_ip not in connections:
                    print(f"Not connected to {target_username}")
                    continue
                if not os.path.exists(file_path):
                    print(f"File not found: {file_path}")
                    continue
                asyncio.create_task(send_file(file_path, {peer_ip: connections[peer_ip]}))
                print(f"{user_data['original_username']} started sending {file_path} to {target_username}")
                continue

            if message.startswith("/pause "):
                transfer_id = message[7:].strip()
                transfer = active_transfers.get(transfer_id)
                if transfer and isinstance(transfer, FileTransfer):
                    asyncio.create_task(transfer.pause())
                    print(f"{user_data['original_username']} pausing transfer {transfer_id}")
                else:
                    print(f"No transferable found with ID {transfer_id}")
                continue

            if message.startswith("/resume "):
                transfer_id = message[8:].strip()
                transfer = active_transfers.get(transfer_id)
                if transfer and isinstance(transfer, FileTransfer):
                    asyncio.create_task(transfer.resume())
                    print(f"{user_data['original_username']} resuming transfer {transfer_id}")
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

            if message == "/changeName":
                os.remove("user_config.json")
                await initialize_user_config()
                print(f"Username changed to {user_data['original_username']}")
                continue

            if not message.startswith("/"):
                if connections:
                    await send_message_to_peers(message)
                    await message_queue.put(f"{user_data['original_username']} (to all): {message}")
                else:
                    print("No peers connected. Use /connect <username> to connect.")

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
            print(f"{user_data['original_username']} > ", end="", flush=True)
        except Exception as e:
            logging.exception(f"Error displaying message: {e}")
            await asyncio.sleep(1)