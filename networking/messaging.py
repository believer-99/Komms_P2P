# networking/messaging.py
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

# MODIFIED: Import from local modules/shared state
from networking.utils import get_own_ip
from networking.shared_state import (
    active_transfers, message_queue, connections, user_data, peer_public_keys,
    peer_usernames, peer_device_ids, shutdown_event, groups, pending_invites,
    pending_join_requests, pending_approvals, connection_denials
)
from networking.file_transfer import FileTransfer, TransferState, compute_hash
# Group function imports (assuming they exist)
from networking.groups import (
     send_group_create_message, send_group_invite_message, send_group_invite_response,
     send_group_join_request, send_group_join_response, send_group_update_message
)
# Utility function imports
from networking.utils import get_peer_display_name # Keep utils separate? Or merge? Assuming separate for now.
from networking.utils import get_own_display_name # <-- **** ADD THIS LINE ****

logger = logging.getLogger(__name__)

# ... (rest of messaging.py remains the same) ...

# --- Configuration ---
def get_config_directory():
    """Determine the appropriate config directory based on the OS."""
    # Using False for appauthor to avoid extra directory level
    return user_config_dir("P2PChat", False)

async def initialize_user_config():
    """Load or create user configuration. Called by NetworkingThread."""
    config_dir = get_config_directory()
    os.makedirs(config_dir, exist_ok=True)
    # Use a device-specific or generic config file name
    # Using generic for simplicity here, assuming one user per OS user account
    config_file_path = os.path.join(config_dir, "user_config.json")
    logger.info(f"Using config file: {config_file_path}")

    if os.path.exists(config_file_path):
        try:
            with open(config_file_path, "r") as f: loaded_data = json.load(f)
            # Validate essential keys
            required_keys = ["original_username", "device_id", "public_key", "private_key"]
            if not all(key in loaded_data for key in required_keys):
                raise ValueError("Config file missing required keys.")

            user_data.update(loaded_data) # Load into shared state
            user_data["public_key"] = serialization.load_pem_public_key(user_data["public_key"].encode())
            user_data["private_key"] = serialization.load_pem_private_key(user_data["private_key"].encode(), password=None)
            logger.info(f"User config loaded for {user_data['original_username']}")
            # ADDED: Put status message for GUI
            await message_queue.put({"type": "log", "message": f"Welcome back, {get_own_display_name()}!"})
        except (json.JSONDecodeError, KeyError, ValueError, TypeError, FileNotFoundError) as e:
            logger.error(f"Error loading config: {e}. Creating new one.")
            await message_queue.put({"type": "log", "message": f"Config error: {e}. Creating new config."})
            # If loading fails, trigger creation using the username possibly set by LoginWindow
            await create_new_user_config(config_file_path, user_data.get("original_username"))
    else:
        logger.info("No config file found, creating new one.")
        # Trigger creation using the username set by LoginWindow
        await create_new_user_config(config_file_path, user_data.get("original_username"))
        await message_queue.put({"type": "log", "message": f"Welcome, {get_own_display_name()}! Config created."})

async def create_new_user_config(config_file_path, provided_username=None):
    """Create a new user configuration file. Called by initialize_user_config."""
    if not provided_username:
        # This should ideally not happen if LoginWindow sets it
        logger.error("Cannot create config: Username not provided.")
        await message_queue.put({"type": "log", "message": "FATAL: Username missing for config creation.", "level": logging.CRITICAL})
        # Possibly raise an exception or shut down?
        raise ValueError("Username required for new config creation")

    original_username = provided_username
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()
    device_id = str(uuid.uuid4()) # Unique ID for this installation

    # Update shared state immediately
    user_data.clear()
    user_data.update({
        "original_username": original_username,
        "device_id": device_id,
        "public_key": public_key,
        "private_key": private_key,
        # Store PEM versions as well? No, load keys into objects directly.
    })

    # Prepare data for JSON storage
    config_data_to_save = {
        "original_username": original_username,
        "device_id": device_id,
        "public_key": public_key.public_bytes(encoding=serialization.Encoding.PEM, format=serialization.PublicFormat.SubjectPublicKeyInfo).decode(),
        "private_key": private_key.private_bytes(encoding=serialization.Encoding.PEM, format=serialization.PrivateFormat.PKCS8, encryption_algorithm=serialization.NoEncryption()).decode(),
    }

    try:
        with open(config_file_path, "w") as f:
            json.dump(config_data_to_save, f, indent=4)
        logger.info(f"New user config created for {original_username} at {config_file_path}")
    except IOError as e:
        logger.exception(f"FATAL: Could not write config file {config_file_path}: {e}")
        await message_queue.put({"type": "log", "message": f"FATAL: Cannot write config file: {e}", "level": logging.CRITICAL})
        # Consider raising exception or shutting down


# --- Connection Handling ---
async def connect_to_peer(peer_ip, requesting_username, target_username, port=8765):
    """Establish a WebSocket connection to a peer. Called by Backend."""
    if peer_ip in connections:
        logger.warning(f"Already connected to {peer_ip}. Aborting connect.")
        await message_queue.put({"type": "log", "message": f"Already connected to {target_username}.", "level": logging.WARNING})
        return False # Indicate connection already exists or failed

    uri = f"ws://{peer_ip}:{port}"
    websocket = None
    try:
        logger.info(f"Attempting to connect to {target_username} at {uri}")
        # Connect with timeout
        websocket = await asyncio.wait_for(websockets.connect(uri, ping_interval=None, max_size=10 * 1024 * 1024), timeout=15.0)
        logger.debug(f"WebSocket connection opened to {peer_ip}")

        own_ip = await get_own_ip()
        await websocket.send(f"INIT {own_ip}")
        ack = await asyncio.wait_for(websocket.recv(), timeout=10.0)

        if ack != "INIT_ACK":
            raise ConnectionAbortedError(f"Invalid INIT_ACK from {peer_ip}: {ack}")

        logger.debug(f"INIT_ACK received from {peer_ip}")
        public_key_pem = user_data["public_key"].public_bytes(encoding=serialization.Encoding.PEM, format=serialization.PublicFormat.SubjectPublicKeyInfo).decode()
        req_msg = json.dumps({
            "type": "CONNECTION_REQUEST", "requesting_username": requesting_username,
            "device_id": user_data["device_id"], "target_username": target_username, "key": public_key_pem
        })
        await websocket.send(req_msg)
        response_raw = await asyncio.wait_for(websocket.recv(), timeout=45.0) # Longer timeout for user approval
        response_data = json.loads(response_raw)

        if response_data["type"] != "CONNECTION_RESPONSE" or not response_data.get("approved"):
            reason = response_data.get("reason", "No reason provided")
            raise ConnectionRefusedError(f"Connection denied by {target_username}: {reason}")

        logger.info(f"Connection approved by {target_username} ({peer_ip})")
        # Send own identity
        await websocket.send(json.dumps({
            "type": "IDENTITY", "username": user_data["original_username"],
            "device_id": user_data["device_id"], "key": public_key_pem
        }))
        # Receive peer's identity
        identity_raw = await asyncio.wait_for(websocket.recv(), timeout=10.0)
        identity_data = json.loads(identity_raw)

        if identity_data["type"] == "IDENTITY":
            peer_key_pem = identity_data["key"]
            peer_uname = identity_data["username"]
            peer_dev_id = identity_data["device_id"]

            # Store peer info in shared state
            peer_public_keys[peer_ip] = serialization.load_pem_public_key(peer_key_pem.encode())
            peer_usernames[peer_uname] = peer_ip # Map username to IP
            peer_device_ids[peer_ip] = peer_dev_id
            connections[peer_ip] = websocket # Add to active connections

            display_name = get_peer_display_name(peer_ip)
            logger.info(f"Connection established with {display_name} ({peer_ip})")
            # ADDED: Notify GUI
            await message_queue.put({"type": "connection_status", "peer_ip": peer_ip, "connected": True})
            await message_queue.put({"type": "log", "message": f"Connected to {display_name}"})

            # Start receiving messages for this connection
            # This task will run until the connection closes or an error occurs
            # We don't await it here; it runs concurrently
            asyncio.create_task(receive_peer_messages(websocket, peer_ip), name=f"RecvMsg-{peer_ip}")
            return True # Indicate success

        else:
            raise ConnectionAbortedError("Invalid IDENTITY response from peer.")

    except (asyncio.TimeoutError, websockets.exceptions.WebSocketException, json.JSONDecodeError, ConnectionRefusedError, ConnectionAbortedError, OSError) as e:
        logger.error(f"Failed to connect to {target_username} ({peer_ip}): {e}")
        await message_queue.put({"type": "log", "message": f"Failed to connect to {target_username}: {e}", "level": logging.ERROR})
        # --- *** CORRECTED CHECK *** ---
        if websocket and websocket.state == State.OPEN:
             await websocket.close(code=1011, reason=f"Connection Error: {e}")
        # --- *** /CORRECTED CHECK *** ---
        return False # Indicate failure
    except Exception as e: # Catch unexpected errors
        logger.exception(f"Unexpected error connecting to {peer_ip}: {e}")
        await message_queue.put({"type": "log", "message": f"Unexpected connect error: {e}", "level": logging.ERROR})
        # --- *** CORRECTED CHECK *** ---
        if websocket and websocket.state == State.OPEN:
            await websocket.close(code=1011, reason=f"Unexpected Error: {e}")
        # --- *** /CORRECTED CHECK *** ---
        return False

async def disconnect_from_peer(peer_ip):
    """Disconnect from a specific peer by IP. Called by Backend."""
    display_name = get_peer_display_name(peer_ip) # Get name before removing state
    websocket = connections.pop(peer_ip, None) # Remove and get websocket
    peer_public_keys.pop(peer_ip, None)
    peer_device_ids.pop(peer_ip, None)
    # Remove username mapping
    username_to_remove = None
    for uname, ip_addr in list(peer_usernames.items()):
        if ip_addr == peer_ip: username_to_remove = uname; break
    if username_to_remove: peer_usernames.pop(username_to_remove, None)

    closed_successfully = False
    if websocket and websocket.open:
        try:
            await websocket.close(code=1000, reason="User initiated disconnect")
            logger.info(f"Closed connection to {display_name} ({peer_ip})")
            closed_successfully = True
        except Exception as e:
            logger.error(f"Error closing connection to {peer_ip}: {e}")
    elif websocket:
         logger.info(f"Connection to {display_name} ({peer_ip}) was already closed.")
         closed_successfully = True # Considered successful if already closed
    else:
         logger.warning(f"No active connection found for {peer_ip} to disconnect.")
         # No connection existed, arguably not a failure of the disconnect *action*
         closed_successfully = True # Or False depending on desired feedback

    # ADDED: Notify GUI regardless of whether WS was open, state is now disconnected
    await message_queue.put({"type": "connection_status", "peer_ip": peer_ip, "connected": False})
    if closed_successfully:
        await message_queue.put({"type": "log", "message": f"Disconnected from {display_name}"})
    else:
         await message_queue.put({"type": "log", "message": f"Could not find connection to {display_name}.", "level": logging.WARNING})

    return closed_successfully


async def handle_incoming_connection(websocket, peer_ip):
    """Handle handshake for new incoming connections. Called by websockets.serve handler."""
    approved = False; requesting_display_name = f"Peer@{peer_ip}"
    requesting_username = "Unknown"; approval_key = None

    try:
        # Timeout for initial handshake phases (INIT + CONNECTION_REQUEST)
        # --- *** CORRECTED: Use wait_for instead of timeout *** ---
        init_message = await asyncio.wait_for(websocket.recv(), timeout=30.0)
        # --- *** /CORRECTED *** ---

        if shutdown_event.is_set(): await websocket.close(1001); return False
        if not isinstance(init_message, str) or not init_message.startswith("INIT "): raise ValueError("Invalid INIT message")
        _, sender_ip = init_message.split(" ", 1); logger.info(f"Received INIT from {peer_ip}")

        if peer_ip in connections: logger.warning(f"Duplicate connection from {peer_ip}"); await websocket.close(1008); return False

        await websocket.send("INIT_ACK")

        # --- *** CORRECTED: Use wait_for instead of timeout *** ---
        request_raw = await asyncio.wait_for(websocket.recv(), timeout=30.0) # Timeout for request
        # --- *** /CORRECTED *** ---
        request_data = json.loads(request_raw)

        # --- Keep the rest of the try block the same, using asyncio.wait_for ---
        # --- for the approval_future as well (it was already correct there) ---
        if request_data["type"] == "CONNECTION_REQUEST":
            # ... (rest of connection request logic, including the asyncio.wait_for(approval_future, ...))
            requesting_username = request_data["requesting_username"] # Assign here
            target_username = request_data["target_username"]
            peer_key_pem = request_data["key"]
            req_dev_id = request_data["device_id"]
            requesting_display_name = f"{requesting_username}({req_dev_id[:8]})"
            logger.info(f"Connection request from {requesting_display_name} targeting {target_username}")

            if target_username != user_data["original_username"]: raise ConnectionRefusedError("Incorrect target username")
            approval_key = (peer_ip, requesting_username); denial_key = (target_username, requesting_username); denial_count = connection_denials.get(denial_key, 0)
            if denial_count >= 3: raise ConnectionRefusedError("Connection blocked (previous denials)")

            approval_future = asyncio.Future(); pending_approvals[approval_key] = approval_future
            await message_queue.put({"type": "approval_request", "peer_ip": peer_ip, "requesting_username": requesting_display_name})
            try:
                # This wait_for was likely already correct, but confirm timeout value
                approved = await asyncio.wait_for(approval_future, timeout=60.0) # Example: 60s for user response
            except asyncio.TimeoutError:
                 logger.info(f"Approval timeout for {requesting_display_name}"); approved = False # Ensure False on timeout
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
            # --- *** CORRECTED: Use wait_for instead of timeout *** ---
            identity_raw = await asyncio.wait_for(websocket.recv(), timeout=15.0) # Timeout for identity
            # --- *** /CORRECTED *** ---
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

    # --- Keep the main except block as before, but ensure websocket.state check ---
    except (asyncio.TimeoutError, websockets.exceptions.WebSocketException, json.JSONDecodeError, ConnectionRefusedError, ConnectionAbortedError, ValueError, OSError) as e:
        logger.warning(f"Handshake failed with {peer_ip} ({requesting_username}): {e}")
        await message_queue.put({"type": "log", "message": f"Connection failed with {requesting_display_name}: {e}", "level": logging.WARNING})
        # --- *** CORRECTED CHECK *** ---
        if websocket and websocket.state == State.OPEN: await websocket.close(1002, f"Handshake error: {e}")
        # --- *** /CORRECTED CHECK *** ---
        if approval_key and approval_key in pending_approvals: pending_approvals.pop(approval_key, None)
        return False # Handshake failed
    except Exception as e: # Catch unexpected errors
        logger.exception(f"Unexpected error during handshake with {peer_ip}: {e}")
        await message_queue.put({"type": "log", "message": f"Internal handshake error: {e}", "level": logging.ERROR})
         # --- *** CORRECTED CHECK *** ---
        if websocket and websocket.state == State.OPEN: await websocket.close(1011, "Internal handshake error")
        # --- *** /CORRECTED CHECK *** ---
        if approval_key and approval_key in pending_approvals: pending_approvals.pop(approval_key, None)
        return False

# --- Message Sending/Receiving ---
async def send_message_to_peers(message, target_peer_ip=None):
    """Send an encrypted message to one or all connected peers. Called by Backend."""
    if not isinstance(message, str) or not message: logger.warning("Attempted send empty message."); return False

    targets = []
    peers_to_send = {}
    if target_peer_ip:
        if target_peer_ip in connections: peers_to_send[target_peer_ip] = connections[target_peer_ip]
        else: logger.warning(f"Cannot send message: Not connected to {target_peer_ip}"); await message_queue.put({"type":"log","message":f"Not connected to peer {get_peer_display_name(target_peer_ip)}","level":logging.WARNING}); return False
    else:
        peers_to_send = connections # Send to all

    if not peers_to_send:
         if target_peer_ip is None: await message_queue.put({"type":"log","message":"No peers connected to send message to."}); logger.info("Send message failed: No connected peers.")
         return False

    sent_count = 0
    for ip, ws in list(peers_to_send.items()):
        display_name = get_peer_display_name(ip)
        if ws.state == State.OPEN:
            key = peer_public_keys.get(ip)
            if not key: logger.warning(f"Missing public key for {display_name} ({ip}). Cannot encrypt."); continue
            try:
                encrypted_message = key.encrypt(message.encode(), padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None)).hex()
                payload = json.dumps({"type": "MESSAGE", "message": encrypted_message})
                await asyncio.wait_for(ws.send(payload), timeout=10.0)
                sent_count += 1
            except asyncio.TimeoutError: logger.error(f"Timeout sending message to {display_name} ({ip}).")
            except Exception as e: logger.error(f"Failed send message to {display_name} ({ip}): {e}") # Includes ConnectionClosed
        else: logger.warning(f"Attempted send to closed connection: {display_name} ({ip})")

    return sent_count > 0


async def receive_peer_messages(websocket, peer_ip):
    """Receive messages from a connected peer. Started by connect_to_peer or handle_incoming_connection."""
    display_name = get_peer_display_name(peer_ip)
    logger.info(f"Starting message receiver for {display_name} ({peer_ip})")
    current_receiving_transfer = None # Track active transfer *for this connection*

    try:
        async for message in websocket:
            if shutdown_event.is_set(): break

            # --- *** MODIFIED LOGIC *** ---
            is_binary = isinstance(message, bytes)

            # 1. Handle expected binary data for ongoing transfer FIRST
            if is_binary and current_receiving_transfer and current_receiving_transfer.state == TransferState.IN_PROGRESS:
                transfer = current_receiving_transfer
                try:
                    async with transfer.condition:
                        # Double-check state inside lock
                        if transfer.state != TransferState.IN_PROGRESS:
                             logger.warning(f"Binary data for non-active transfer {transfer.transfer_id[:8]} received but state changed."); continue

                        await transfer.file_handle.write(message) # Write the raw bytes
                        transfer.transferred_size += len(message)
                        if transfer.hash_algo: transfer.hash_algo.update(message)

                        # Check completion
                        if transfer.transferred_size >= transfer.total_size:
                            await transfer.file_handle.close(); transfer.file_handle = None
                            final_state = TransferState.COMPLETED
                            completion_msg = f"Received '{os.path.basename(transfer.file_path)}' from {display_name}."
                            if transfer.expected_hash:
                                calc_hash = transfer.hash_algo.hexdigest()
                                if calc_hash == transfer.expected_hash: logger.info(f"Hash verified for {transfer.transfer_id[:8]}")
                                else:
                                    final_state = TransferState.FAILED; completion_msg = f"Hash FAILED for '{os.path.basename(transfer.file_path)}'. File deleted."; logger.error(f"Hash mismatch {transfer.transfer_id[:8]}. Exp {transfer.expected_hash}, Got {calc_hash}")
                                    try: os.remove(transfer.file_path)
                                    except OSError as rm_err: logger.error(f"Failed delete corrupted file {transfer.file_path}: {rm_err}")
                            else: completion_msg += " (No integrity check)."
                            transfer.state = final_state
                            await message_queue.put({"type": "log", "message": completion_msg, "level": logging.ERROR if final_state == TransferState.FAILED else logging.INFO})
                            await message_queue.put({"type": "transfer_update"})
                            current_receiving_transfer = None # Ready for next potential transfer
                except Exception as write_err:
                     logger.exception(f"Error writing file chunk for {transfer.transfer_id[:8]}: {write_err}")
                     await transfer.fail(f"File write error: {write_err}") # Mark as failed
                     current_receiving_transfer = None # Stop trying for this transfer

            # 2. Handle JSON messages (only if not binary or no transfer active)
            elif not is_binary:
                try:
                    data = json.loads(message) # Now we are reasonably sure it *should* be JSON
                    msg_type = data.get("type")
                    logger.debug(f"Processing JSON type '{msg_type}' from {display_name}")

                    if msg_type == "MESSAGE":
                         # ... (decrypt logic as before) ...
                         try:
                             decrypted = user_data["private_key"].decrypt(bytes.fromhex(data["message"]), padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None)).decode()
                             await message_queue.put({"type": "message", "sender_display_name": display_name, "content": decrypted})
                         except Exception as dec_err: logger.error(f"Decrypt error from {display_name}: {dec_err}"); await message_queue.put({"type":"log","message":f"[Decrypt Error from {display_name}]","level":logging.WARNING})

                    elif msg_type == "file_transfer_init":
                        # If another transfer is already in progress, maybe reject or queue? For now, overwrite.
                        if current_receiving_transfer:
                             logger.warning(f"New transfer init received while another transfer ({current_receiving_transfer.transfer_id[:8]}) is active. Overwriting.")
                             await current_receiving_transfer.fail("Superseded by new transfer") # Fail the old one

                        tid=data["transfer_id"]; fname=data["filename"]; fsize=data["filesize"]; fhash=data.get("file_hash"); path=os.path.join("downloads", os.path.basename(fname)); os.makedirs("downloads", exist_ok=True)
                        counter = 1; base, ext = os.path.splitext(path)
                        while os.path.exists(path): path = f"{base}({counter}){ext}"; counter += 1
                        transfer = FileTransfer(path, peer_ip, direction="receive", transfer_id=tid); transfer.total_size = fsize; transfer.expected_hash = fhash; transfer.state = TransferState.IN_PROGRESS
                        try:
                            transfer.file_handle = await aiofiles.open(path, "wb")
                            active_transfers[tid] = transfer; current_receiving_transfer = transfer # Set expected transfer
                            logger.info(f"Receiving '{os.path.basename(path)}' ({tid[:8]}) from {display_name}")
                            await message_queue.put({"type": "transfer_update"})
                            await message_queue.put({"type": "log", "message": f"Receiving '{os.path.basename(path)}' from {display_name} (ID: {tid[:8]})"})
                        except OSError as e: logger.error(f"Failed open file for transfer {tid}: {e}"); await message_queue.put({"type":"log","message":f"Error receiving from {display_name}: Cannot open file","level":logging.ERROR}); current_receiving_transfer = None

                    elif msg_type == "TRANSFER_PAUSE":
                        tid = data.get("transfer_id"); transfer = active_transfers.get(tid)
                        if transfer and transfer.peer_ip == peer_ip and transfer.state == TransferState.IN_PROGRESS: await transfer.pause(); await message_queue.put({"type": "log", "message": f"{display_name} paused transfer {tid[:8]}"})

                    elif msg_type == "TRANSFER_RESUME":
                         tid = data.get("transfer_id"); transfer = active_transfers.get(tid)
                         if transfer and transfer.peer_ip == peer_ip and transfer.state == TransferState.PAUSED: await transfer.resume(); await message_queue.put({"type": "log", "message": f"{display_name} resumed transfer {tid[:8]}"})

                    # --- Group Message Handling ---
                    # ... (handle GROUP_CREATE, GROUP_INVITE etc. as before) ...

                    else:
                        logger.warning(f"Received unknown JSON type '{msg_type}' from {display_name}")

                except json.JSONDecodeError:
                     # Should not happen often now if is_binary check is correct
                     logger.warning(f"Received non-JSON message from {display_name} when JSON was expected: {message[:100]}...")
                     await message_queue.put({"type": "message", "sender_display_name": display_name, "content": f"[INVALID JSON] {message[:100]}..."})
                except Exception as proc_err:
                     logger.exception(f"Error processing JSON message from {display_name}: {proc_err}")

            # 3. Handle unexpected binary data (no transfer active)
            elif is_binary:
                 logger.warning(f"Received unexpected binary data from {display_name} when no transfer active.")

            # --- End Message Processing Logic ---

    except websockets.exceptions.ConnectionClosedOK: logger.info(f"Connection closed normally by {display_name} ({peer_ip})")
    except websockets.exceptions.ConnectionClosedError as e: logger.warning(f"Connection closed abruptly by {display_name} ({peer_ip}): {e}")
    except asyncio.CancelledError: logger.info(f"Receive loop cancelled for {display_name} ({peer_ip})")
    except Exception as e: logger.exception(f"Unexpected error in receive loop for {display_name} ({peer_ip}): {e}")
    finally:
        # --- Keep cleanup logic as before ---
        logger.debug(f"Cleaning up connection state for {peer_ip}")
        connections.pop(peer_ip, None); peer_public_keys.pop(peer_ip, None); peer_device_ids.pop(peer_ip, None)
        username = next((u for u, ip in peer_usernames.items() if ip == peer_ip), None)
        if username: peer_usernames.pop(username, None)
        if current_receiving_transfer: await current_receiving_transfer.fail("Connection lost")
        if not shutdown_event.is_set():
            await message_queue.put({"type": "connection_status", "peer_ip": peer_ip, "connected": False})
            await message_queue.put({"type": "log", "message": f"Disconnected from {display_name}"})
        logger.info(f"Receive loop finished for {display_name} ({peer_ip})")
# --- Peer List Maintenance ---
async def maintain_peer_list(discovery_instance):
    """Periodically check connections and update peer list based on discovery."""
    while not shutdown_event.is_set():
        try:
            disconnected_peers = []
            # Check existing connections
            for peer_ip, ws in list(connections.items()):
                if shutdown_event.is_set(): break
                try:
                    # Send ping with timeout
                    await asyncio.wait_for(ws.ping(), timeout=10.0)
                except (asyncio.TimeoutError, websockets.exceptions.ConnectionClosed, websockets.exceptions.WebSocketException) as e:
                    logger.warning(f"Connection lost/ping failed for {get_peer_display_name(peer_ip)} ({peer_ip}): {type(e).__name__}")
                    disconnected_peers.append(peer_ip)
                    # Close socket if necessary
                    if ws.open: await ws.close(code=1011, reason="Ping failure")
                    # Remove state immediately
                    connections.pop(peer_ip, None)
                    peer_public_keys.pop(peer_ip, None)
                    peer_device_ids.pop(peer_ip, None)
                    uname = next((u for u, ip in peer_usernames.items() if ip == peer_ip), None)
                    if uname: peer_usernames.pop(uname, None)

            # Notify GUI about disconnections
            if disconnected_peers:
                for peer_ip in disconnected_peers:
                     await message_queue.put({"type": "connection_status", "peer_ip": peer_ip, "connected": False})
                     await message_queue.put({"type": "log", "message": f"Lost connection to {get_peer_display_name(peer_ip)}"})
                 # Trigger full peer list update might be redundant if connection_status does it
                 # await message_queue.put({"type": "peer_update"})

            # Short sleep before next check
            await asyncio.sleep(15)

        except asyncio.CancelledError:
            logger.info("maintain_peer_list task cancelled.")
            break
        except Exception as e:
            logger.exception(f"Error in maintain_peer_list: {e}")
            await asyncio.sleep(30) # Wait longer after error

    logger.info("maintain_peer_list stopped.")


# --- CLI Specific Functions (Should NOT be imported/used by GUI) ---
# These were originally in messaging.py but belong to the CLI main loop logic

async def user_input(discovery):
    """Handle CLI user input. NOT FOR GUI."""
    # This function should only be run if __name__ == "__main__" in main.py
    # It interacts directly with the console using aioconsole.
    await asyncio.sleep(1)
    logger.warning("CLI user_input task started - this should NOT happen if running the GUI.")
    # ... (Keep original CLI /command handling logic here) ...
    # ... Remember to replace print() with await message_queue.put(...) for output ...
    # ... Replace ainput() with direct input if not using aioconsole for CLI ...
    while not shutdown_event.is_set():
        try:
            # Use simple input for basic CLI, or keep aioconsole if preferred
            # message = await ainput(f"{get_own_display_name()} > ")
            message = input(f"{get_own_display_name()} > ").strip() # Sync input for simple CLI

            if message == "/exit":
                shutdown_event.set(); break
            # Handle other commands (/list, /connect, /msg, /send etc.)
            # using await connect_to_peer, await send_message_to_peers etc.
            # and putting output/status onto message_queue
            elif message == "/help": await message_queue.put("Help text...")
            # ... etc ...
            elif not message.startswith("/"):
                 await send_message_to_peers(message) # Broadcast
                 await message_queue.put(f"You (to all): {message}")
            # Add a small sleep if using sync input to prevent tight loop on error
            await asyncio.sleep(0.1)
        except (EOFError, KeyboardInterrupt, asyncio.CancelledError):
             shutdown_event.set(); break
        except Exception as e:
             logger.exception(f"CLI input error: {e}")
             await message_queue.put({"type":"log", "message":f"Input Error: {e}", "level":logging.ERROR})
             await asyncio.sleep(1)

async def display_messages():
    """Display messages from queue for CLI. NOT FOR GUI."""
    logger.warning("CLI display_messages task started - this should NOT happen if running the GUI.")
    while not shutdown_event.is_set():
        try:
            item = await message_queue.get()
            # Simple print for CLI
            if isinstance(item, dict):
                print(f"\n[STATUS] {item.get('message', str(item))}") # Basic format for dicts
            else:
                print(f"\n{item}")
            # Reprint prompt if needed (complex with async input)
            # print(f"{get_own_display_name()} > ", end="", flush=True) # Doesn't work well with concurrent prints
            message_queue.task_done()
        except asyncio.CancelledError: break
        except Exception as e: logger.exception(f"CLI display error: {e}"); await asyncio.sleep(1)
