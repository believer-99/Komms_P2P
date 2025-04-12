import asyncio
import logging
import websockets
import os
import platform
import json
import hashlib
import aiofiles
import netifaces
import uuid
import time

from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric import rsa, padding
from appdirs import user_config_dir
from websockets.connection import State


from networking.utils import get_own_ip
from networking.shared_state import (
    active_transfers, message_queue, connections, user_data, peer_public_keys,
    peer_usernames, peer_device_ids, shutdown_event, groups, pending_invites,
    pending_join_requests, pending_approvals, connection_denials
)
from networking.file_transfer import FileTransfer, TransferState, compute_hash

from networking.groups import (
     send_group_create_message, send_group_invite_message, send_group_invite_response,
     send_group_join_request, send_group_join_response, send_group_update_message
)

from networking.utils import get_peer_display_name, get_own_display_name

logger = logging.getLogger(__name__)


def get_config_directory():
    """Determine the appropriate config directory based on the OS."""
    try:

        from appdirs import user_config_dir

        return user_config_dir("P2PChat", False)
    except ImportError:
        logger.warning("appdirs not found. Using simple '.config/P2PChat' directory in home folder.")
        home = os.path.expanduser("~")
        if sys.platform == "win32":
            appdata = os.environ.get('APPDATA')
            if appdata: return os.path.join(appdata, "P2PChat")
            else: return os.path.join(home, ".p2pchat_config")
        else: return os.path.join(home, ".config", "P2PChat")


async def initialize_user_config():
    """Load or create user configuration. Called by NetworkingThread."""
    config_dir = get_config_directory()
    os.makedirs(config_dir, exist_ok=True)
    config_file_path = os.path.join(config_dir, "user_config.json")
    logger.info(f"Using config file: {config_file_path}")

    if os.path.exists(config_file_path):
        try:
            with open(config_file_path, "r") as f: loaded_data = json.load(f)
            required_keys = ["original_username", "device_id", "public_key", "private_key"]
            if not all(key in loaded_data for key in required_keys): raise ValueError("Config missing keys.")
            user_data.update(loaded_data)
            user_data["public_key"] = serialization.load_pem_public_key(user_data["public_key"].encode())
            user_data["private_key"] = serialization.load_pem_private_key(user_data["private_key"].encode(), password=None)
            logger.info(f"User config loaded for {user_data['original_username']}")
            await message_queue.put({"type": "log", "message": f"Welcome back, {get_own_display_name()}!"})
        except Exception as e:
            logger.error(f"Error loading config: {e}. Creating new one.")
            await message_queue.put({"type": "log", "message": f"Config error: {e}. Creating new config."})
            await create_new_user_config(config_file_path, user_data.get("original_username"))
    else:
        logger.info("No config file found, creating new one.")
        await create_new_user_config(config_file_path, user_data.get("original_username"))
        await message_queue.put({"type": "log", "message": f"Welcome, {get_own_display_name()}! Config created."})

async def create_new_user_config(config_file_path, provided_username=None):
    """Create a new user configuration file. Called by initialize_user_config."""
    if not provided_username:


        logger.critical("Username not set in user_data before config creation attempt.")
        await message_queue.put({"type": "log", "message": "FATAL: Username not available for config creation.", "level": logging.CRITICAL})
        raise ValueError("Username required for new config creation")

    original_username = provided_username
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key(); device_id = str(uuid.uuid4())
    user_data.clear(); user_data.update({
        "original_username": original_username, "device_id": device_id,
        "public_key": public_key, "private_key": private_key,
    })
    config_data_to_save = {
        "original_username": original_username, "device_id": device_id,
        "public_key": public_key.public_bytes(encoding=serialization.Encoding.PEM, format=serialization.PublicFormat.SubjectPublicKeyInfo).decode(),
        "private_key": private_key.private_bytes(encoding=serialization.Encoding.PEM, format=serialization.PrivateFormat.PKCS8, encryption_algorithm=serialization.NoEncryption()).decode(),
    }
    try:
        with open(config_file_path, "w") as f: json.dump(config_data_to_save, f, indent=4)
        logger.info(f"New user config created for {original_username} at {config_file_path}")
    except IOError as e:
        logger.exception(f"FATAL: Could not write config file {config_file_path}: {e}")
        await message_queue.put({"type": "log", "message": f"FATAL: Cannot write config file: {e}", "level": logging.CRITICAL})
        raise


async def connect_to_peer(peer_ip, requesting_username, target_username, port=8765):
    """Establish a WebSocket connection to a peer. Called by Backend."""
    if peer_ip in connections:
        logger.warning(f"Already connected to {peer_ip}. Aborting connect.")
        await message_queue.put({"type": "log", "message": f"Already connected to {target_username}.", "level": logging.WARNING})
        return False

    uri = f"ws://{peer_ip}:{port}"; websocket = None
    try:
        logger.info(f"Attempting to connect to {target_username} at {uri}")
        websocket = await asyncio.wait_for(websockets.connect(uri, ping_interval=None, max_size=10 * 1024 * 1024), timeout=15.0)
        logger.debug(f"WebSocket connection opened to {peer_ip}")
        own_ip = await get_own_ip(); await websocket.send(f"INIT {own_ip}")
        ack = await asyncio.wait_for(websocket.recv(), timeout=10.0)
        if ack != "INIT_ACK": raise ConnectionAbortedError(f"Invalid INIT_ACK from {peer_ip}: {ack}")
        logger.debug(f"INIT_ACK received from {peer_ip}")
        public_key_pem = user_data["public_key"].public_bytes(encoding=serialization.Encoding.PEM, format=serialization.PublicFormat.SubjectPublicKeyInfo).decode()
        req_msg = json.dumps({"type": "CONNECTION_REQUEST", "requesting_username": requesting_username, "device_id": user_data["device_id"], "target_username": target_username, "key": public_key_pem})
        await websocket.send(req_msg)
        response_raw = await asyncio.wait_for(websocket.recv(), timeout=45.0)
        response_data = json.loads(response_raw)
        if response_data["type"] != "CONNECTION_RESPONSE" or not response_data.get("approved"):
            reason = response_data.get("reason", "No reason provided"); raise ConnectionRefusedError(f"Connection denied by {target_username}: {reason}")
        logger.info(f"Connection approved by {target_username} ({peer_ip})")
        await websocket.send(json.dumps({"type": "IDENTITY", "username": user_data["original_username"], "device_id": user_data["device_id"], "key": public_key_pem}))
        identity_raw = await asyncio.wait_for(websocket.recv(), timeout=10.0)
        identity_data = json.loads(identity_raw)
        if identity_data["type"] == "IDENTITY":
            peer_key_pem = identity_data["key"]; peer_uname = identity_data["username"]; peer_dev_id = identity_data["device_id"]
            peer_public_keys[peer_ip] = serialization.load_pem_public_key(peer_key_pem.encode())
            peer_usernames[peer_uname] = peer_ip; peer_device_ids[peer_ip] = peer_dev_id; connections[peer_ip] = websocket
            display_name = get_peer_display_name(peer_ip)
            logger.info(f"Connection established with {display_name} ({peer_ip})")
            await message_queue.put({"type": "connection_status", "peer_ip": peer_ip, "connected": True})
            await message_queue.put({"type": "log", "message": f"Connected to {display_name}"})
            asyncio.create_task(receive_peer_messages(websocket, peer_ip), name=f"RecvMsg-{peer_ip}")
            return True
        else: raise ConnectionAbortedError("Invalid IDENTITY response from peer.")
    except (asyncio.TimeoutError, websockets.exceptions.WebSocketException, json.JSONDecodeError, ConnectionRefusedError, ConnectionAbortedError, OSError) as e:
        logger.error(f"Failed to connect to {target_username} ({peer_ip}): {e}")
        await message_queue.put({"type": "log", "message": f"Failed to connect to {target_username}: {e}", "level": logging.ERROR})
        if websocket and websocket.state == State.OPEN: await websocket.close(code=1011, reason=f"Connection Error: {e}")
        return False
    except Exception as e:
        logger.exception(f"Unexpected error connecting to {peer_ip}: {e}")
        await message_queue.put({"type": "log", "message": f"Unexpected connect error: {e}", "level": logging.ERROR})
        if websocket and websocket.state == State.OPEN: await websocket.close(code=1011, reason=f"Unexpected Error: {e}")
        return False

async def disconnect_from_peer(peer_ip):
    """Disconnect from a specific peer by IP. Called by Backend."""
    display_name = get_peer_display_name(peer_ip); websocket = connections.pop(peer_ip, None)
    peer_public_keys.pop(peer_ip, None); peer_device_ids.pop(peer_ip, None)
    username_to_remove = next((uname for uname, ip_addr in peer_usernames.items() if ip_addr == peer_ip), None)
    if username_to_remove: peer_usernames.pop(username_to_remove, None)
    closed_successfully = False

    if websocket and websocket.state == State.OPEN:
        try: await websocket.close(code=1000, reason="User initiated disconnect"); logger.info(f"Closed connection to {display_name} ({peer_ip})"); closed_successfully = True
        except Exception as e: logger.error(f"Error closing connection to {peer_ip}: {e}")
    elif websocket: logger.info(f"Connection to {display_name} ({peer_ip}) was already closed."); closed_successfully = True
    else: logger.warning(f"No active connection found for {peer_ip} to disconnect."); closed_successfully = True
    await message_queue.put({"type": "connection_status", "peer_ip": peer_ip, "connected": False})
    if closed_successfully: await message_queue.put({"type": "log", "message": f"Disconnected from {display_name}"})
    else: await message_queue.put({"type": "log", "message": f"Could not find connection to {display_name}.", "level": logging.WARNING})
    return closed_successfully

async def handle_incoming_connection(websocket, peer_ip):
    """Handle handshake for new incoming connections. Called by websockets.serve handler."""
    approved = False; requesting_display_name = f"Peer@{peer_ip}"
    requesting_username = "Unknown"; approval_key = None
    try:

        init_message = await asyncio.wait_for(websocket.recv(), timeout=30.0)
        if shutdown_event.is_set(): await websocket.close(1001); return False
        if not isinstance(init_message, str) or not init_message.startswith("INIT "): raise ValueError("Invalid INIT message")
        _, sender_ip = init_message.split(" ", 1); logger.info(f"Received INIT from {peer_ip}")
        if peer_ip in connections: logger.warning(f"Duplicate connection from {peer_ip}"); await websocket.close(1008); return False
        await websocket.send("INIT_ACK")
        request_raw = await asyncio.wait_for(websocket.recv(), timeout=30.0)
        request_data = json.loads(request_raw)
        if request_data["type"] == "CONNECTION_REQUEST":
            requesting_username = request_data["requesting_username"]; target_username = request_data["target_username"]
            peer_key_pem = request_data["key"]; req_dev_id = request_data["device_id"]
            requesting_display_name = f"{requesting_username}({req_dev_id[:8]})"
            logger.info(f"Connection request from {requesting_display_name} targeting {target_username}")
            if target_username != user_data["original_username"]: raise ConnectionRefusedError("Incorrect target username")
            approval_key = (peer_ip, requesting_username); denial_key = (target_username, requesting_username); denial_count = connection_denials.get(denial_key, 0)
            if denial_count >= 3: raise ConnectionRefusedError("Connection blocked (previous denials)")
            approval_future = asyncio.Future(); pending_approvals[approval_key] = approval_future
            await message_queue.put({"type": "approval_request", "peer_ip": peer_ip, "requesting_username": requesting_display_name})
            try: approved = await asyncio.wait_for(approval_future, timeout=60.0)
            except asyncio.TimeoutError: logger.info(f"Approval timeout for {requesting_display_name}"); approved = False
            finally: pending_approvals.pop(approval_key, None)
            if not approved:
                 current_denials = connection_denials.get(denial_key, 0) + 1; connection_denials[denial_key] = current_denials
                 await message_queue.put({"type": "log", "message": f"Denied connection from {requesting_display_name} ({current_denials}/3)"})
                 if current_denials >= 3: await message_queue.put({"type": "log", "message": f"{requesting_display_name} blocked."})
                 raise ConnectionRefusedError("Connection denied by user or timeout")
            logger.info(f"Connection approved for {requesting_display_name}")
            await websocket.send(json.dumps({"type": "CONNECTION_RESPONSE", "approved": True}))
            own_public_key_pem = user_data["public_key"].public_bytes(encoding=serialization.Encoding.PEM, format=serialization.PublicFormat.SubjectPublicKeyInfo).decode()
            await websocket.send(json.dumps({"type": "IDENTITY", "username": user_data["original_username"], "device_id": user_data["device_id"], "key": own_public_key_pem}))
            identity_raw = await asyncio.wait_for(websocket.recv(), timeout=15.0)
            identity_data = json.loads(identity_raw)
            if identity_data["type"] == "IDENTITY" and identity_data["username"] == requesting_username and identity_data["device_id"] == req_dev_id:
                 peer_public_keys[peer_ip] = serialization.load_pem_public_key(peer_key_pem.encode())
                 peer_usernames[requesting_username] = peer_ip; peer_device_ids[peer_ip] = req_dev_id; connections[peer_ip] = websocket
                 final_display_name = get_peer_display_name(peer_ip); logger.info(f"Connection established with {final_display_name} ({peer_ip})")
                 await message_queue.put({"type": "connection_status", "peer_ip": peer_ip, "connected": True})
                 await message_queue.put({"type": "log", "message": f"Connected to {final_display_name}"})
                 return True
            else: raise ConnectionAbortedError("Invalid final IDENTITY received")
        else: raise ValueError(f"Unexpected message type after INIT_ACK: {request_data.get('type')}")
    except (asyncio.TimeoutError, websockets.exceptions.WebSocketException, json.JSONDecodeError, ConnectionRefusedError, ConnectionAbortedError, ValueError, OSError) as e:
        logger.warning(f"Handshake failed with {peer_ip} ({requesting_username}): {e}")
        await message_queue.put({"type": "log", "message": f"Connection failed with {requesting_display_name}: {e}", "level": logging.WARNING})
        if websocket and websocket.state == State.OPEN: await websocket.close(1002, f"Handshake error: {e}")
        if approval_key and approval_key in pending_approvals: pending_approvals.pop(approval_key, None)
        return False
    except Exception as e:
        logger.exception(f"Unexpected error during handshake with {peer_ip}: {e}")
        await message_queue.put({"type": "log", "message": f"Internal handshake error: {e}", "level": logging.ERROR})
        if websocket and websocket.state == State.OPEN: await websocket.close(1011, "Internal handshake error")
        if approval_key and approval_key in pending_approvals: pending_approvals.pop(approval_key, None)
        return False


async def send_message_to_peers(message, target_peer_ip=None):
    """Send an encrypted message to one or all connected peers. Called by Backend."""
    if not isinstance(message, str) or not message: logger.warning("Attempted send empty message."); return False
    peers_to_send = {}; sent_count = 0
    if target_peer_ip:
        if target_peer_ip in connections: peers_to_send[target_peer_ip] = connections[target_peer_ip]
        else: logger.warning(f"Msg Send Fail: Not connected to {target_peer_ip}"); await message_queue.put({"type":"log","message":f"Not connected to {get_peer_display_name(target_peer_ip)}","level":logging.WARNING}); return False
    else: peers_to_send = connections
    if not peers_to_send:
         if target_peer_ip is None: await message_queue.put({"type":"log","message":"No peers to send msg to."}); logger.info("Msg Send Fail: No connected peers.")
         return False
    for ip, ws in list(peers_to_send.items()):
        display_name = get_peer_display_name(ip)
        if ws.state == State.OPEN:
            key = peer_public_keys.get(ip);
            if not key: logger.warning(f"No key for {display_name} ({ip}). Skip send."); continue
            try:
                encrypted = key.encrypt(message.encode(), padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None)).hex()
                payload = json.dumps({"type": "MESSAGE", "message": encrypted})
                await asyncio.wait_for(ws.send(payload), timeout=10.0); sent_count += 1
            except asyncio.TimeoutError: logger.error(f"Timeout sending msg to {display_name} ({ip}).")
            except Exception as e: logger.error(f"Failed send msg to {display_name} ({ip}): {e}")
        else: logger.warning(f"Attempted send to closed conn: {display_name} ({ip})")
    return sent_count > 0

async def receive_peer_messages(websocket, peer_ip):
    """Receive messages from a connected peer. Handles JSON and Binary (File Chunks)."""
    display_name = get_peer_display_name(peer_ip)
    logger.info(f"Starting message receiver for {display_name} ({peer_ip})")
    current_receiving_transfer = None
    try:
        async for message in websocket:
            if shutdown_event.is_set(): break
            is_binary = isinstance(message, bytes)


            if is_binary and current_receiving_transfer and current_receiving_transfer.state == TransferState.IN_PROGRESS:
                transfer = current_receiving_transfer
                try:
                    async with transfer.condition:
                        if transfer.state != TransferState.IN_PROGRESS: logger.warning(f"Bin data for non-active transfer {transfer.transfer_id[:8]}"); continue
                        await transfer.file_handle.write(message); transfer.transferred_size += len(message)
                        if transfer.hash_algo: transfer.hash_algo.update(message)
                        if transfer.transferred_size >= transfer.total_size:
                            await transfer.file_handle.close(); transfer.file_handle = None
                            final_state = TransferState.COMPLETED; msg = f"Received '{os.path.basename(transfer.file_path)}' from {display_name}."
                            if transfer.expected_hash:
                                calc_hash = transfer.hash_algo.hexdigest()
                                if calc_hash == transfer.expected_hash:
                                    logger.info(f"Hash verified for {transfer.transfer_id[:8]}")
                                else:

                                    final_state = TransferState.FAILED
                                    completion_msg = f"Hash FAILED '{os.path.basename(transfer.file_path)}'. Deleted."
                                    logger.error(f"Hash mismatch {transfer.transfer_id[:8]}. Exp {transfer.expected_hash}, Got {calc_hash}")
                                    try:
                                        os.remove(transfer.file_path)
                                        logger.info(f"Deleted corrupted file: {transfer.file_path}")
                                    except OSError as rm_err:
                                         logger.error(f"Failed delete corrupted file {transfer.file_path}: {rm_err}")

                            else:
                                completion_msg += " (No hash check)."
                            await message_queue.put({"type":"log","message":msg,"level": logging.ERROR if final_state==TransferState.FAILED else logging.INFO})
                            await message_queue.put({"type": "transfer_update"}); current_receiving_transfer = None
                except Exception as write_err: logger.exception(f"Err writing chunk {transfer.transfer_id[:8]}: {write_err}"); await transfer.fail(f"Write err: {write_err}"); current_receiving_transfer = None


            elif not is_binary:
                try:
                    data = json.loads(message); msg_type = data.get("type"); logger.debug(f"JSON recv from {display_name}: type={msg_type}")
                    if msg_type == "MESSAGE":
                        try: decrypted = user_data["private_key"].decrypt(bytes.fromhex(data["message"]), padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None)).decode(); await message_queue.put({"type":"message", "sender_display_name":display_name, "content":decrypted})
                        except Exception as dec_err: logger.error(f"Decrypt error from {display_name}: {dec_err}"); await message_queue.put({"type":"log","message":f"[Decrypt Err from {display_name}]","level":logging.WARNING})
                    elif msg_type == "file_transfer_init":
                        if current_receiving_transfer: logger.warning(f"New transfer init overriding active {current_receiving_transfer.transfer_id[:8]}"); await current_receiving_transfer.fail("Superseded")
                        tid=data["transfer_id"]; fname=data["filename"]; fsize=data["filesize"]; fhash=data.get("file_hash"); path=os.path.join("downloads", os.path.basename(fname)); os.makedirs("downloads", exist_ok=True)
                        counter=1; base, ext = os.path.splitext(path);
                        while os.path.exists(path): path=f"{base}({counter}){ext}"; counter += 1
                        transfer = FileTransfer(path, peer_ip, "receive", tid); transfer.total_size=fsize; transfer.expected_hash=fhash; transfer.state=TransferState.IN_PROGRESS
                        try: transfer.file_handle = await aiofiles.open(path, "wb"); active_transfers[tid] = transfer; current_receiving_transfer = transfer; logger.info(f"Receiving '{os.path.basename(path)}' ({tid[:8]}) from {display_name}"); await message_queue.put({"type":"transfer_update"}); await message_queue.put({"type":"log", "message":f"Receiving '{os.path.basename(path)}' from {display_name} (ID: {tid[:8]})"})
                        except OSError as e: logger.error(f"Failed open file for transfer {tid}: {e}"); await message_queue.put({"type":"log","message":f"Error receiving from {display_name}: Cannot open file","level":logging.ERROR}); current_receiving_transfer = None
                    elif msg_type == "TRANSFER_PAUSE": tid=data.get("transfer_id"); transfer=active_transfers.get(tid);
                    if transfer and transfer.peer_ip == peer_ip and transfer.state == TransferState.IN_PROGRESS: await transfer.pause(); await message_queue.put({"type":"log","message":f"{display_name} paused {tid[:8]}"})
                    elif msg_type == "TRANSFER_RESUME": tid=data.get("transfer_id"); transfer=active_transfers.get(tid);
                    if transfer and transfer.peer_ip == peer_ip and transfer.state == TransferState.PAUSED: await transfer.resume(); await message_queue.put({"type":"log","message":f"{display_name} resumed {tid[:8]}"})

                    elif msg_type == "GROUP_CREATE": groups[data["groupname"]] = {"admin":data["admin_ip"],"members":{data["admin_ip"]}}; await message_queue.put({"type":"group_list_update"}); await message_queue.put({"type":"log","message":f"Group '{data['groupname']}' created by {get_peer_display_name(data['admin_ip'])}"})
                    elif msg_type == "GROUP_INVITE": pending_invites.append(data); await message_queue.put({"type":"pending_invites_update"}); await message_queue.put({"type":"log","message":f"Invite to join '{data['groupname']}' from {get_peer_display_name(data['inviter_ip'])}"})
                    elif msg_type == "GROUP_INVITE_RESPONSE":
                         gn=data["groupname"]; invitee=data["invitee_ip"]; accepted=data["accepted"]
                         if gn in groups and groups[gn]["admin"] == await get_own_ip():
                              if accepted: groups[gn]["members"].add(invitee); await send_group_update_message(gn, list(groups[gn]["members"])); await message_queue.put({"type":"log","message":f"{get_peer_display_name(invitee)} joined '{gn}'"})
                              else: await message_queue.put({"type":"log","message":f"{get_peer_display_name(invitee)} declined invite to '{gn}'"})
                         else: logger.warning("Received invite response for group not admin of.")
                    elif msg_type == "GROUP_JOIN_REQUEST":
                         gn=data["groupname"]; req_ip=data["requester_ip"]; req_uname=data["requester_username"]
                         if gn in groups and groups[gn]["admin"] == await get_own_ip():
                              if not any(r["requester_ip"] == req_ip for r in pending_join_requests[gn]): pending_join_requests[gn].append({"requester_ip":req_ip,"requester_username":req_uname}); await message_queue.put({"type":"join_requests_update"}); await message_queue.put({"type":"log","message":f"Join request for '{gn}' from {req_uname}"})
                              else: logger.info(f"Duplicate join request ignored for {gn} from {req_uname}")
                         else: logger.warning("Received join request for group not admin of.")
                    elif msg_type == "GROUP_JOIN_RESPONSE":
                         gn=data["groupname"]; req_ip=data["requester_ip"]; approved=data["approved"]; admin_ip=data["admin_ip"]
                         if req_ip == await get_own_ip():
                              if approved: groups[gn] = {"admin":admin_ip,"members":{admin_ip, req_ip}}; await message_queue.put({"type":"group_list_update"}); await message_queue.put({"type":"log","message":f"Join request for '{gn}' approved."})
                              else: await message_queue.put({"type":"log","message":f"Join request for '{gn}' denied."})
                         else: logger.warning("Received join response for someone else.")
                    elif msg_type == "GROUP_UPDATE":
                         gn=data["groupname"]; members=set(data["members"]); admin=data["admin"]
                         if await get_own_ip() in members:
                              if gn not in groups: logger.warning(f"GROUP_UPDATE for unknown group {gn}. Adding."); groups[gn]={"admin":admin,"members":members}
                              else: groups[gn]["members"] = members; groups[gn]["admin"] = admin
                              await message_queue.put({"type":"group_list_update"}); await message_queue.put({"type":"log","message":f"Group '{gn}' updated."})
                         elif gn in groups:
                              del groups[gn]; await message_queue.put({"type":"group_list_update"}); await message_queue.put({"type":"log","message":f"Removed from group '{gn}'."})
                    else: logger.warning(f"Unknown JSON type '{msg_type}' from {display_name}")
                except json.JSONDecodeError: logger.warning(f"Recv non-JSON from {display_name}: {message[:100]}..."); await message_queue.put({"type":"message","sender_display_name":display_name,"content":f"[INVALID JSON] {message[:100]}..."})
                except Exception as proc_err: logger.exception(f"Error processing JSON msg from {display_name}: {proc_err}")


            elif is_binary: logger.warning(f"Unexpected binary data from {display_name}")

    except websockets.exceptions.ConnectionClosedOK: logger.info(f"Conn closed normally by {display_name} ({peer_ip})")
    except websockets.exceptions.ConnectionClosedError as e: logger.warning(f"Conn closed abruptly by {display_name} ({peer_ip}): {e}")
    except asyncio.CancelledError: logger.info(f"Receive loop cancelled for {display_name} ({peer_ip})")
    except Exception as e: logger.exception(f"Unexpected error in receive loop for {display_name} ({peer_ip}): {e}")
    finally:
        logger.debug(f"Cleaning up connection state for {peer_ip}")
        connections.pop(peer_ip, None); peer_public_keys.pop(peer_ip, None); peer_device_ids.pop(peer_ip, None)
        username = next((u for u, ip in peer_usernames.items() if ip == peer_ip), None)
        if username: peer_usernames.pop(username, None)
        if current_receiving_transfer: await current_receiving_transfer.fail("Connection lost")
        if not shutdown_event.is_set():
            await message_queue.put({"type": "connection_status", "peer_ip": peer_ip, "connected": False})
            await message_queue.put({"type": "log", "message": f"Disconnected from {display_name}"})
        logger.info(f"Receive loop finished for {display_name} ({peer_ip})")


async def maintain_peer_list(discovery_instance):
    """Periodically check connections and update peer list based on discovery."""
    while not shutdown_event.is_set():
        try:
            disconnected_peers = []
            for peer_ip, ws in list(connections.items()):
                if shutdown_event.is_set(): break
                try:
                    await asyncio.wait_for(ws.ping(), timeout=10.0)
                except (asyncio.TimeoutError, websockets.exceptions.ConnectionClosed, websockets.exceptions.WebSocketException) as e:
                    logger.warning(f"Connection lost/ping failed for {get_peer_display_name(peer_ip)} ({peer_ip}): {type(e).__name__}")
                    disconnected_peers.append(peer_ip)

                    if ws.state == State.OPEN: await ws.close(code=1011, reason="Ping failure")
                    connections.pop(peer_ip, None); peer_public_keys.pop(peer_ip, None); peer_device_ids.pop(peer_ip, None)
                    uname = next((u for u, ip in peer_usernames.items() if ip == peer_ip), None)
                    if uname: peer_usernames.pop(uname, None)
            if disconnected_peers:
                for peer_ip in disconnected_peers:
                     await message_queue.put({"type": "connection_status", "peer_ip": peer_ip, "connected": False})
                     await message_queue.put({"type": "log", "message": f"Lost connection to {get_peer_display_name(peer_ip)}"})
            await asyncio.sleep(15)
        except asyncio.CancelledError: logger.info("maintain_peer_list task cancelled."); break
        except Exception as e: logger.exception(f"Error in maintain_peer_list: {e}"); await asyncio.sleep(30)
    logger.info("maintain_peer_list stopped.")


async def user_input(discovery): logger.critical("CLI user_input task running - should not happen in GUI mode!"); await asyncio.sleep(3600)
async def display_messages(): logger.critical("CLI display_messages task running - should not happen in GUI mode!"); await asyncio.sleep(3600)
