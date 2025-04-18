import asyncio
import logging
import websockets
import os
import platform
import json
import hashlib
import aiofiles
import uuid
import time
import ssl # <-- Added ssl import
from datetime import datetime, timedelta # <-- Added datetime imports for cert generation

from cryptography import x509 # <-- Added cryptography imports for cert generation
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives import hashes as crypto_hashes # <-- Use alias for crypto hashes
from cryptography.hazmat.primitives import serialization, hashes # <-- Keep original hashes import for RSA padding
from cryptography.hazmat.primitives.asymmetric import rsa, padding
from appdirs import user_config_dir
from websockets.connection import State

from utils.file_validation import check_file_size, check_disk_space, safe_close_file

# --- Import Core Networking Components ---
from networking.utils import get_own_ip, get_peer_display_name, get_own_display_name # <-- Keep utils import
from networking.shared_state import (
    active_transfers, message_queue, connections, user_data, peer_public_keys,
    peer_usernames, peer_device_ids, shutdown_event, groups, pending_invites,
    pending_join_requests, pending_approvals, connection_denials,
    # --- Added Locks and New State ---
    connections_lock, active_transfers_lock, peer_data_lock,
    groups_lock, pending_lock, outgoing_transfers_by_peer, # <-- Added for transfer mgmt
    connection_attempts, connection_attempts_lock # <-- Added for tie-breaking
)
from networking.file_transfer import FileTransfer, TransferState, compute_hash # <-- Keep file_transfer import

from networking.groups import ( # <-- Keep groups import
     send_group_create_message, send_group_invite_message, send_group_invite_response,
     send_group_join_request, send_group_join_response, send_group_update_message
)
# --- End Imports ---

logger = logging.getLogger(__name__)

# --- Configuration and Certificate Paths ---
CONFIG_DIR = user_config_dir("P2PChat", "YourOrg") # Using YourOrg namespace now
# Define SSL Cert/Key file paths relative to CONFIG_DIR
KEY_FILE = os.path.join(CONFIG_DIR, "key.pem") # SSL Private Key path
CERT_FILE = os.path.join(CONFIG_DIR, "cert.pem") # SSL Certificate path
# --- End Configuration Paths ---

async def initialize_user_config():
    """Initialize user config: username, keys, device ID, and SSL certs."""
    global user_data # Ensure we modify the global user_data
    global KEY_FILE # Allow modification if key path changes

    os.makedirs(CONFIG_DIR, exist_ok=True)
    config_path = os.path.join(CONFIG_DIR, "config.json")
    key_path = os.path.join(CONFIG_DIR, "p2p_key.pem") # Path for the main RSA key
    pub_key_path = os.path.join(CONFIG_DIR, "p2p_key.pub") # Path for the main RSA public key

    # --- Load Existing Config JSON ---
    if os.path.exists(config_path):
        try:
            async with aiofiles.open(config_path, "r") as f:
                content = await f.read()
                loaded_data = json.loads(content)
                # Update global user_data, don't overwrite completely
                user_data.update(loaded_data)
                logger.info(f"Loaded config from {config_path}")
        except (json.JSONDecodeError, FileNotFoundError, TypeError) as e:
            logger.warning(f"Could not load or parse config file {config_path}: {e}. Will re-initialize missing parts.")
            # Don't reset user_data here, let subsequent checks handle missing keys
    # --- End Load Config JSON ---

    # --- Generate/Load RSA Keys ---
    # Check if keys are missing or not deserialized yet
    keys_need_processing = False
    if "private_key" not in user_data or isinstance(user_data.get("private_key"), str):
        keys_need_processing = True
    if "public_key" not in user_data or isinstance(user_data.get("public_key"), str):
        keys_need_processing = True

    if keys_need_processing:
        if os.path.exists(key_path) and os.path.exists(pub_key_path):
            # Try loading from dedicated key files first
            try:
                async with aiofiles.open(key_path, "rb") as f:
                    private_pem = await f.read()
                    user_data["private_key"] = serialization.load_pem_private_key(private_pem, password=None)
                async with aiofiles.open(pub_key_path, "rb") as f:
                    public_pem = await f.read()
                    user_data["public_key"] = serialization.load_pem_public_key(public_pem)
                logger.info(f"Loaded existing RSA keys from {key_path} and {pub_key_path}")
                keys_need_processing = False # Keys loaded successfully
            except Exception as e:
                logger.error(f"Failed to load existing keys from files: {e}. Will attempt generation.")
                user_data.pop("private_key", None) # Clear potentially corrupted data
                user_data.pop("public_key", None)
                keys_need_processing = True # Force regeneration check

        # If still need processing (not found in files or file load failed), try loading from JSON or generate
        if keys_need_processing:
            # Attempt to deserialize from JSON string if present
            deserialized_from_json = False
            try:
                if isinstance(user_data.get("private_key"), str):
                    user_data["private_key"] = serialization.load_pem_private_key(user_data["private_key"].encode(), password=None)
                    deserialized_from_json = True
                if isinstance(user_data.get("public_key"), str):
                    user_data["public_key"] = serialization.load_pem_public_key(user_data["public_key"].encode())
                    deserialized_from_json = True
                if deserialized_from_json:
                    logger.info("Deserialized RSA keys from loaded config JSON.")
                    keys_need_processing = False
            except Exception as e:
                logger.error(f"Failed to deserialize keys from config JSON: {e}. Generating new keys.")
                user_data.pop("private_key", None)
                user_data.pop("public_key", None)
                keys_need_processing = True # Force generation

        # Generate new keys if they are still missing after all load attempts
        if keys_need_processing:
            logger.info("Generating new RSA key pair...")
            private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            public_key = private_key.public_key()
            user_data["private_key"] = private_key
            user_data["public_key"] = public_key
            try:
                private_pem = private_key.private_bytes(
                    encoding=serialization.Encoding.PEM,
                    format=serialization.PrivateFormat.PKCS8,
                    encryption_algorithm=serialization.NoEncryption()
                )
                public_pem = public_key.public_bytes(
                    encoding=serialization.Encoding.PEM,
                    format=serialization.PublicFormat.SubjectPublicKeyInfo
                )
                # Save to dedicated key files
                async with aiofiles.open(key_path, "wb") as f: await f.write(private_pem)
                async with aiofiles.open(pub_key_path, "wb") as f: await f.write(public_pem)
                logger.info(f"Saved new RSA keys to {key_path} and {pub_key_path}")
            except Exception as e:
                logger.error(f"Failed to save newly generated keys: {e}")
                # Critical error? Decide if app can continue without saved keys.
    # --- End Generate/Load RSA Keys ---


    # --- Generate/Load Device ID ---
    if "device_id" not in user_data:
        user_data["device_id"] = str(uuid.uuid4())
        logger.info(f"Generated new device ID: {user_data['device_id']}")
        # Config needs saving if ID was generated
        await save_config_json(config_path, user_data, "new device ID")
    # --- End Device ID ---


    # --- Generate/Load SSL Certificate and Key ---
    user_data['key_path'] = KEY_FILE # Store expected path in user_data
    user_data['cert_path'] = CERT_FILE

    # Check if the certificate exists and is valid (optional: add validity check)
    # For simplicity, just check existence here
    if not os.path.exists(CERT_FILE) or not os.path.exists(KEY_FILE):
        logger.info(f"Generating self-signed SSL certificate ({CERT_FILE}) and ensuring key ({KEY_FILE})...")
        try:
            # Ensure RSA keys are loaded/generated before using them for the cert
            if "private_key" not in user_data or not isinstance(user_data["private_key"], rsa.RSAPrivateKey):
                 logger.error("RSA private key missing or not deserialized, cannot generate SSL certificate.")
                 raise ValueError("RSA private key required for SSL certificate generation.")
            if "public_key" not in user_data or not isinstance(user_data["public_key"], rsa.RSAPublicKey):
                 logger.error("RSA public key missing or not deserialized, cannot generate SSL certificate.")
                 raise ValueError("RSA public key required for SSL certificate generation.")

            ssl_private_key = user_data["private_key"] # Reuse existing RSA key
            ssl_public_key = user_data["public_key"]

            # --- Generate Self-Signed Certificate ---
            subject = issuer = x509.Name([
                x509.NameAttribute(NameOID.COUNTRY_NAME, u"US"),
                x509.NameAttribute(NameOID.STATE_OR_PROVINCE_NAME, u"CA"), # Example values
                x509.NameAttribute(NameOID.LOCALITY_NAME, u"Localhost"),
                x509.NameAttribute(NameOID.ORGANIZATION_NAME, u"P2PChatOrg"),
                # Use Device ID or Username in Common Name for some uniqueness
                x509.NameAttribute(NameOID.COMMON_NAME, f"p2pchat-{user_data.get('device_id', 'unknown-peer')}.local"),
            ])
            cert = x509.CertificateBuilder().subject_name(
                subject
            ).issuer_name(
                issuer
            ).public_key(
                ssl_public_key
            ).serial_number(
                x509.random_serial_number() # Generate a random serial number
            ).not_valid_before(
                datetime.utcnow()
            ).not_valid_after(
                # Make cert valid for 1 year (adjust as needed)
                datetime.utcnow() + timedelta(days=365)
            ).add_extension( # Add Subject Alternative Name (SAN) for localhost/IPs
                 # Allows connection via localhost, 127.0.0.1, and potentially the discovered IP
                 # Note: Discovering local IP might be async, so using placeholders here is safer for init
                 x509.SubjectAlternativeName([
                     x509.DNSName(u"localhost"),
                     # x509.IPAddress(ip_address("127.0.0.1")) # Requires ipaddress module
                 ]),
                 critical=False,
            # Sign the certificate with our private key.
            ).sign(ssl_private_key, crypto_hashes.SHA256()) # Use aliased crypto hashes

            # --- Save Key and Cert ---
            # Save the certificate
            cert_pem = cert.public_bytes(serialization.Encoding.PEM)
            async with aiofiles.open(CERT_FILE, "wb") as f:
                await f.write(cert_pem)
            logger.info(f"Saved self-signed certificate to {CERT_FILE}")

            # Ensure the private key exists at KEY_FILE path
            # If KEY_FILE is different from the main key_path, save it explicitly
            if KEY_FILE != key_path:
                logger.info(f"Saving private key specifically for SSL to {KEY_FILE}")
                private_pem_ssl = ssl_private_key.private_bytes(
                    encoding=serialization.Encoding.PEM,
                    format=serialization.PrivateFormat.PKCS8,
                    encryption_algorithm=serialization.NoEncryption()
                )
                async with aiofiles.open(KEY_FILE, "wb") as f:
                    await f.write(private_pem_ssl)
            else:
                # If reusing the main key, ensure it exists (should have been saved earlier)
                if not os.path.exists(key_path):
                    logger.error(f"Main private key file {key_path} missing after generation attempt!")
                    # Optionally try saving it again here
                logger.info(f"Using existing private key {KEY_FILE} for SSL.")

            user_data['key_path'] = KEY_FILE # Update path in user_data just in case

        except ValueError as ve: # Catch specific error for missing keys
            logger.error(f"Cannot generate SSL cert: {ve}")
            user_data.pop('key_path', None)
            user_data.pop('cert_path', None)
        except Exception as e:
            logger.exception(f"Failed to generate or save SSL certificate/key: {e}")
            # Decide how to handle failure - maybe run without WSS?
            user_data.pop('key_path', None)
            user_data.pop('cert_path', None)
    else:
        logger.info(f"Found existing SSL certificate ({CERT_FILE}) and key ({KEY_FILE}).")
    # --- End SSL Cert/Key ---


    # --- Set Default Username if Missing ---
    # Username is expected to be set by LoginWindow before networking starts
    if "original_username" not in user_data:
        # Provide a fallback default, though this shouldn't ideally be needed if LoginWindow runs first
        user_data["original_username"] = f"User_{platform.node()[:8]}" # Default placeholder
        logger.warning(f"Username not found, set default: {user_data['original_username']}")
        # Save config if default username was set
        await save_config_json(config_path, user_data, "default username")
    # --- End Default Username ---

    logger.info("User config initialization complete.")
    # Avoid logging sensitive key details, show paths instead
    log_safe_data = {
        k: (v if k not in ["private_key", "public_key"] else f"<{type(v).__name__}>")
        for k, v in user_data.items()
    }
    logger.debug(f"User Data State: {log_safe_data}")


async def save_config_json(config_path, data_to_save, reason="update"):
    """Helper function to save the config JSON, serializing keys."""
    try:
        # Create a copy to modify for saving
        save_data = data_to_save.copy()
        # Serialize keys if they are objects
        if isinstance(save_data.get("private_key"), rsa.RSAPrivateKey):
            save_data["private_key"] = save_data["private_key"].private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption()
            ).decode()
        if isinstance(save_data.get("public_key"), rsa.RSAPublicKey):
            save_data["public_key"] = save_data["public_key"].public_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PublicFormat.SubjectPublicKeyInfo
            ).decode()

        async with aiofiles.open(config_path, "w") as f:
            await f.write(json.dumps(save_data, indent=4))
        logger.info(f"Saved config to {config_path} (Reason: {reason})")
    except Exception as e:
        logger.error(f"Failed to save config to {config_path}: {e}")


# NOTE: create_new_user_config might be less relevant now as initialize_user_config handles generation
# but keep it for potential explicit calls if needed. Ensure it saves keys correctly too.
async def create_new_user_config(config_file_path, provided_username=None):
    """(Potentially Deprecated) Create a new user configuration file."""
    # This function logic might need updating to align with initialize_user_config's file handling
    # for key_path and pub_key_path if used directly.
    # For now, it primarily focuses on generating data for user_data dictionary.
    logger.warning("create_new_user_config called - ensure initialize_user_config handles saving.")
    if not provided_username:
        logger.critical("Username not provided for new config creation.")
        raise ValueError("Username required for new config creation")

    original_username = provided_username
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()
    device_id = str(uuid.uuid4())

    # Prepare data structure (keys as objects initially)
    new_user_data = {
        "original_username": original_username,
        "device_id": device_id,
        "public_key": public_key, # Store object
        "private_key": private_key, # Store object
    }

    # Prepare data for saving (keys as PEM strings)
    config_data_to_save = {
        "original_username": original_username,
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
    }

    try:
        # Save the JSON config file
        json_string = json.dumps(config_data_to_save, indent=4) # Correctly dump data to string
        async with aiofiles.open(config_file_path, "w") as f:
            await f.write(json_string) # Write the string to the file
        logger.info(f"New user config JSON created for {original_username} at {config_file_path}")

        # Also save separate key files
        key_path = os.path.join(CONFIG_DIR, "p2p_key.pem")
        pub_key_path = os.path.join(CONFIG_DIR, "p2p_key.pub")
        private_pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption()
        )
        public_pem = public_key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo
        )
        async with aiofiles.open(key_path, "wb") as f: await f.write(private_pem)
        async with aiofiles.open(pub_key_path, "wb") as f: await f.write(public_pem)
        logger.info(f"Saved separate keys to {key_path} and {pub_key_path}")

        # Update the global user_data AFTER saving successfully
        user_data.clear()
        user_data.update(new_user_data)

    except Exception as e:
        logger.exception(f"FATAL: Could not write config/key files for {original_username}: {e}")
        await message_queue.put({"type": "log", "message": f"FATAL: Cannot write config/key file: {e}", "level": logging.CRITICAL})
        raise # Re-raise after logging


async def connect_to_peer(peer_ip, requesting_username, target_username, port=8765):
    """Establish a WSS connection to a peer. Called by Backend."""
    # --- Initial Check (Already Connected?) ---
    async with connections_lock:
        if peer_ip in connections:
            logger.warning(f"Already connected to {peer_ip} ({target_username}). Aborting connect.")
            await message_queue.put({"type": "log", "message": f"Already connected to {target_username}.", "level": logging.WARNING})
            return False
    # --- End Initial Check ---

    # --- Prepare for Tie-breaking ---
    connection_timestamp = time.time() # Unique timestamp for this attempt
    my_device_id = user_data.get("device_id", "")
    async with connection_attempts_lock:
        # Record the start time of this *outgoing* attempt
        connection_attempts[peer_ip] = ("outgoing", connection_timestamp, my_device_id)
        logger.debug(f"Recorded outgoing connection attempt to {peer_ip} at {connection_timestamp}")
    # --- End Tie-breaking Prep ---

    # --- Create SSL Context for Client ---
    # WARNING: This context trusts any certificate presented by the peer.
    # Suitable for LANs where encryption is the primary goal over authentication.
    # In a real-world scenario, you'd want proper certificate validation.
    ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ssl_context.check_hostname = False # Don't verify hostname matches cert CN/SAN
    ssl_context.verify_mode = ssl.CERT_NONE # Don't verify the certificate chain
    # To add security: load CA certs and set verify_mode = ssl.CERT_REQUIRED
    # ssl_context.load_verify_locations(cafile='path/to/ca.pem')
    # --- End SSL Context ---

    uri = f"wss://{peer_ip}:{port}"; websocket = None # Use wss:// for secure connection
    try:
        logger.info(f"Attempting to connect securely to {target_username} at {uri}")
        # Pass ssl context to connect
        websocket = await asyncio.wait_for(
            websockets.connect(
                uri,
                ping_interval=20, # Add ping interval for keepalive
                ping_timeout=15,  # Timeout for pong response
                max_size=10 * 1024 * 1024, # Keep max size
                ssl=ssl_context # Use the SSL context
            ),
            timeout=20.0 # Connection attempt timeout
        )
        logger.debug(f"Secure WebSocket connection opened to {peer_ip}")

        # --- Handshake Protocol ---
        own_ip = await get_own_ip() # Get own IP (best effort)
        await websocket.send(f"INIT {own_ip}") # Send INIT
        ack = await asyncio.wait_for(websocket.recv(), timeout=10.0) # Wait for INIT_ACK
        if ack != "INIT_ACK": raise ConnectionAbortedError(f"Invalid INIT_ACK from {peer_ip}: {ack}")
        logger.debug(f"INIT_ACK received from {peer_ip}")

        # Send CONNECTION_REQUEST
        public_key_pem = user_data["public_key"].public_bytes(
            encoding=serialization.Encoding.PEM, format=serialization.PublicFormat.SubjectPublicKeyInfo
        ).decode()
        req_msg = json.dumps({
            "type": "CONNECTION_REQUEST",
            "requesting_username": requesting_username,
            "device_id": user_data["device_id"],
            "target_username": target_username,
            "key": public_key_pem
        })
        await websocket.send(req_msg)

        # Wait for CONNECTION_RESPONSE (longer timeout for user approval)
        response_raw = await asyncio.wait_for(websocket.recv(), timeout=65.0)
        response_data = json.loads(response_raw)
        if response_data["type"] != "CONNECTION_RESPONSE" or not response_data.get("approved"):
            reason = response_data.get("reason", "No reason provided")
            raise ConnectionRefusedError(f"Connection denied by {target_username}: {reason}")
        logger.info(f"Connection approved by {target_username} ({peer_ip})")

        # Send IDENTITY
        await websocket.send(json.dumps({
            "type": "IDENTITY",
            "username": user_data["original_username"],
            "device_id": user_data["device_id"],
            "key": public_key_pem
        }))

        # Receive peer's IDENTITY
        identity_raw = await asyncio.wait_for(websocket.recv(), timeout=15.0)
        identity_data = json.loads(identity_raw)
        # --- End Handshake Protocol ---

        if identity_data["type"] == "IDENTITY":
            peer_key_pem = identity_data["key"]
            peer_uname = identity_data["username"]
            peer_dev_id = identity_data["device_id"]

            # --- Final Tie-breaking Check (Outgoing Wins Condition) ---
            should_abort = False
            async with connection_attempts_lock:
                # Check if peer recorded an *incoming* attempt from us that might win
                peer_attempt = connection_attempts.get(peer_ip)
                if peer_attempt and peer_attempt[0] == "incoming" and peer_attempt[2] == peer_dev_id:
                    # Peer recorded an incoming connection from us. Check IDs.
                    if my_device_id < peer_dev_id:
                        # Our ID is lower, this outgoing connection should win. Peer should abort incoming.
                        logger.info(f"Tie-breaking: Continuing outgoing connection to {target_username} (My ID {my_device_id} < Peer ID {peer_dev_id}).")
                    else:
                        # Our ID is higher/equal, the incoming connection at their end should win. Abort this.
                        logger.info(f"Tie-breaking: Aborting outgoing connection to {target_username} (My ID {my_device_id} >= Peer ID {peer_dev_id}). Peer should keep incoming.")
                        should_abort = True
                # Also check if an incoming connection was *already fully established* while we were connecting
                async with connections_lock:
                     if peer_ip in connections:
                         logger.warning(f"Tie-breaking/Race: An incoming connection from {target_username} was established while connecting. Aborting outgoing.")
                         should_abort = True

            if should_abort:
                await websocket.close(1000, reason="Connection tie-breaking resolution")
                return False
            # --- End Final Tie-breaking Check ---

            # --- Finalize Connection State (If not aborted) ---
            async with connections_lock:
                # Double-check connection doesn't exist just before adding
                if peer_ip in connections:
                    logger.warning(f"Race condition: Connection to {peer_ip} established by another task just before finalization. Aborting this one.")
                    await websocket.close(1000, reason="Connection race condition")
                    return False
                connections[peer_ip] = websocket # Add connection

            async with peer_data_lock: # Add peer data
                peer_public_keys[peer_ip] = serialization.load_pem_public_key(peer_key_pem.encode())
                peer_usernames[peer_uname] = peer_ip
                peer_device_ids[peer_ip] = peer_dev_id

            display_name = get_peer_display_name(peer_ip) # Get final name
            logger.info(f"Secure connection established with {display_name} ({peer_ip})")
            await message_queue.put({"type": "connection_status", "peer_ip": peer_ip, "connected": True})
            await message_queue.put({"type": "log", "message": f"Connected securely to {display_name}"})

            # Start the message receiver task for this connection
            asyncio.create_task(receive_peer_messages(websocket, peer_ip), name=f"RecvMsg-{peer_ip}")
            return True # Connection successful
            # --- End Finalize Connection ---

        else:
            # Invalid final IDENTITY message
            raise ConnectionAbortedError("Invalid IDENTITY response from peer.")

    # --- Exception Handling ---
    except (ConnectionRefusedError, ConnectionAbortedError) as e:
        # Specific handshake failures (denied, protocol error)
        logger.warning(f"Connection to {target_username} ({peer_ip}) failed: {e}")
        await message_queue.put({"type": "log", "message": f"Connection to {target_username} failed: {e}", "level": logging.WARNING})
        if websocket and websocket.state == State.OPEN: await websocket.close(1000, reason=str(e))
        return False
    except (websockets.exceptions.InvalidURI, websockets.exceptions.WebSocketException, ssl.SSLError, OSError, asyncio.TimeoutError) as e:
        # Network/SSL/Timeout errors during connection attempt
        logger.error(f"Connection attempt to {target_username} ({peer_ip}) failed: {type(e).__name__}: {e}")
        await message_queue.put({"type": "log", "message": f"Could not connect to {target_username}: {type(e).__name__}", "level": logging.ERROR})
        if websocket and websocket.state == State.OPEN: await websocket.close(1011, reason="Connection error")
        return False
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse JSON response from {target_username} ({peer_ip}): {e}")
        await message_queue.put({"type": "log", "message": f"Invalid response from {target_username}.", "level": logging.ERROR})
        if websocket and websocket.state == State.OPEN: await websocket.close(1002, reason="Invalid JSON response")
        return False
    except Exception as e:
        # Catch-all for unexpected errors
        logger.exception(f"Unexpected error connecting to {target_username} ({peer_ip}): {e}")
        await message_queue.put({"type": "log", "message": f"Error connecting to {target_username}: {e}", "level": logging.ERROR})
        if websocket and websocket.state == State.OPEN: await websocket.close(1011, reason="Unexpected error")
        return False
    # --- End Exception Handling ---
    finally:
        # --- Cleanup Connection Attempt Record ---
        async with connection_attempts_lock:
            attempt_info = connection_attempts.get(peer_ip)
            # Only remove if it's *this specific* outgoing attempt
            if attempt_info and attempt_info[0] == "outgoing" and attempt_info[1] == connection_timestamp:
                connection_attempts.pop(peer_ip, None)
                logger.debug(f"Cleaned up outgoing connection attempt record for {peer_ip}")
        # --- End Cleanup ---


async def disconnect_from_peer(peer_ip):
    """Disconnect from a specific peer by IP. Ensures state cleanup."""
    display_name = get_peer_display_name(peer_ip) # Get name before potentially removing state
    logger.info(f"Initiating disconnect from {display_name} ({peer_ip})...")

    websocket = None
    # --- Safely Remove Connection Object ---
    async with connections_lock:
        websocket = connections.pop(peer_ip, None)
    # --- End Remove Connection ---

    # --- Clean Up Peer Data ---
    async with peer_data_lock:
        peer_public_keys.pop(peer_ip, None)
        peer_device_ids.pop(peer_ip, None)
        # Find and remove username mapping for this IP
        username_to_remove = next((uname for uname, ip_addr in peer_usernames.items() if ip_addr == peer_ip), None)
        if username_to_remove:
            peer_usernames.pop(username_to_remove, None)
            logger.debug(f"Removed username mapping for {username_to_remove} ({peer_ip})")
    # --- End Clean Up Peer Data ---

    # --- Clean Up Transfer Tracking ---
    async with active_transfers_lock:
        # Remove tracking of outgoing transfers *to* this peer
        removed_count = len(outgoing_transfers_by_peer.pop(peer_ip, []))
        if removed_count > 0:
             logger.debug(f"Cleared tracking for {removed_count} outgoing transfers to {peer_ip}.")
        # Note: Active *receiving* transfers are handled by receive_peer_messages finally block
    # --- End Clean Up Transfer Tracking ---

    closed_successfully = False
    # --- Close WebSocket if Exists ---
    if websocket:
        if websocket.state == State.OPEN:
            try:
                # Attempt graceful close with timeout
                await asyncio.wait_for(websocket.close(code=1000, reason="User initiated disconnect"), timeout=5.0)
                logger.info(f"Successfully closed connection to {display_name} ({peer_ip})")
                closed_successfully = True
            except asyncio.TimeoutError:
                 logger.warning(f"Timeout closing connection to {peer_ip}. May already be closed.")
                 closed_successfully = False # Mark as not gracefully closed
            except Exception as e:
                 logger.error(f"Error during websocket close for {peer_ip}: {e}")
                 closed_successfully = False
        else:
            logger.info(f"Connection to {display_name} ({peer_ip}) was already closed (State: {websocket.state}).")
            closed_successfully = True # Consider it 'done' if not open
    else:
        logger.info(f"No active connection object found for {peer_ip} to disconnect.")
        closed_successfully = True # No action needed
    # --- End Close WebSocket ---

    # --- Notify UI ---
    # Always send status update regardless of close success
    await message_queue.put({"type": "connection_status", "peer_ip": peer_ip, "connected": False})

    if websocket: # If we actually had a connection object
        if closed_successfully and websocket.state != State.OPEN : # Check state again after close attempt
             await message_queue.put({"type": "log", "message": f"Disconnected from {display_name}"})
        else:
             await message_queue.put({"type": "log", "message": f"Finished disconnect process for {display_name} (may not have closed gracefully).", "level": logging.WARNING})
    else: # If no connection object was found initially
         await message_queue.put({"type": "log", "message": f"No active connection to {display_name} to disconnect.", "level": logging.INFO})
    # --- End Notify UI ---

    return closed_successfully


async def handle_incoming_connection(websocket, peer_ip):
    """Handle handshake for new incoming WSS connections."""
    approved = False; requesting_display_name = f"Peer@{peer_ip}"
    requesting_username = "Unknown"; approval_key = None; req_dev_id = None
    peer_key_pem = None; connection_stored = False

    # --- Initial Duplicate Check ---
    async with connections_lock:
        if peer_ip in connections:
            logger.warning(f"Duplicate incoming connection attempt from {peer_ip}. Closing new one.")
            await websocket.close(1008, reason="Already connected")
            return False
    # --- End Initial Check ---

    try:
        # --- Basic Handshake Steps ---
        init_message = await asyncio.wait_for(websocket.recv(), timeout=30.0)
        if shutdown_event.is_set(): await websocket.close(1001); return False
        if not isinstance(init_message, str) or not init_message.startswith("INIT "): raise ValueError("Invalid INIT message")
        _, sender_ip = init_message.split(" ", 1); logger.info(f"Received INIT from {peer_ip}")

        # Double check connection hasn't snuck in
        async with connections_lock:
            if peer_ip in connections: logger.warning(f"Duplicate connection from {peer_ip} (race)."); await websocket.close(1008); return False

        await websocket.send("INIT_ACK")
        request_raw = await asyncio.wait_for(websocket.recv(), timeout=30.0)
        request_data = json.loads(request_raw)
        # --- End Basic Handshake ---

        if request_data["type"] == "CONNECTION_REQUEST":
            requesting_username = request_data["requesting_username"]
            target_username = request_data["target_username"]
            peer_key_pem = request_data["key"]
            req_dev_id = request_data["device_id"]
            requesting_display_name = f"{requesting_username}({req_dev_id[:8]})"
            logger.info(f"Connection request from {requesting_display_name} targeting {target_username}")

            # --- Validate Target ---
            if target_username != user_data["original_username"]:
                raise ConnectionRefusedError(f"Request targets incorrect user '{target_username}'")
            # --- End Validate Target ---

            # --- Tie-breaking Logic (Incoming) ---
            connection_timestamp = time.time() # Time this incoming request arrived
            my_device_id = user_data.get("device_id", "")
            handle_this_connection = True # Assume we handle unless tie-breaking says otherwise

            async with connection_attempts_lock:
                # Record this incoming attempt before checking outgoing
                connection_attempts[peer_ip] = ("incoming", connection_timestamp, my_device_id)
                logger.debug(f"Recorded incoming connection attempt from {peer_ip} at {connection_timestamp}")

                # Check if we have an *outgoing* attempt recorded for the same peer
                outgoing_attempt = connection_attempts.get(peer_ip) # Re-fetch after potentially updating
                # Important: Ensure it's actually an *outgoing* record and compare device IDs
                if outgoing_attempt and outgoing_attempt[0] == "outgoing" and outgoing_attempt[2]:
                    peer_recorded_dev_id = outgoing_attempt[2] # Device ID from our outgoing record
                    if my_device_id and req_dev_id:
                        if my_device_id > req_dev_id:
                            # Our ID is higher, this incoming connection wins. Allow it.
                            logger.info(f"Tie-breaking: Accepting incoming connection from {requesting_display_name} (My ID {my_device_id} > Peer ID {req_dev_id}).")
                            handle_this_connection = True
                        else:
                            # Our ID is lower/equal, the outgoing connection should win. Reject this incoming one.
                            logger.info(f"Tie-breaking: Rejecting incoming connection from {requesting_display_name} (My ID {my_device_id} <= Peer ID {req_dev_id}). Outgoing should win.")
                            handle_this_connection = False
                            await websocket.close(1000, reason="Connection superseded by outgoing connection")
                    else:
                        logger.warning(f"Cannot perform tie-breaking for {requesting_display_name} due to missing device IDs. Accepting incoming.")
                        handle_this_connection = True # Default to accepting

            if not handle_this_connection:
                return False # Tie-breaking decided against this connection
            # --- End Tie-breaking ---

            # --- Approval Logic ---
            approval_key = (peer_ip, requesting_username) # Key for pending approvals
            denial_key = (target_username, requesting_username) # Key for denial tracking
            denial_count = connection_denials.get(denial_key, 0)

            if denial_count >= 3:
                raise ConnectionRefusedError("Connection blocked (previously denied 3+ times)")

            approval_future = asyncio.Future()
            pending_approvals[approval_key] = approval_future
            # Notify UI to ask for approval
            await message_queue.put({
                "type": "approval_request",
                "peer_ip": peer_ip,
                "requesting_username": requesting_display_name
            })

            try:
                # Wait for the UI to set the future's result
                approved = await asyncio.wait_for(approval_future, timeout=60.0) # 60s for user response
            except asyncio.TimeoutError:
                logger.info(f"Approval timeout for {requesting_display_name}")
                approved = False
            finally:
                pending_approvals.pop(approval_key, None) # Clean up pending request
                approval_key = None # Ensure key is cleared

            if not approved:
                 # Track denial
                 current_denials = connection_denials.get(denial_key, 0) + 1
                 connection_denials[denial_key] = current_denials
                 deny_reason = "Connection denied by user or timeout"
                 await message_queue.put({"type": "log", "message": f"Denied connection from {requesting_display_name} ({current_denials}/3)"})
                 if current_denials >= 3:
                     await message_queue.put({"type": "log", "message": f"{requesting_display_name} blocked due to repeated denials."})
                     deny_reason = "Connection blocked (previous denials)"
                 # Send denial response
                 try:
                     await websocket.send(json.dumps({"type": "CONNECTION_RESPONSE", "approved": False, "reason": deny_reason}))
                 except Exception as send_err:
                      logger.warning(f"Could not send denial response to {peer_ip}: {send_err}")
                 raise ConnectionRefusedError(deny_reason) # Raise to trigger cleanup
            # --- End Approval Logic ---

            # --- Proceed if Approved ---
            logger.info(f"Connection approved for {requesting_display_name}")
            await websocket.send(json.dumps({"type": "CONNECTION_RESPONSE", "approved": True}))

            # Send own IDENTITY
            own_public_key_pem = user_data["public_key"].public_bytes(
                encoding=serialization.Encoding.PEM, format=serialization.PublicFormat.SubjectPublicKeyInfo
            ).decode()
            await websocket.send(json.dumps({
                "type": "IDENTITY",
                "username": user_data["original_username"],
                "device_id": user_data["device_id"],
                "key": own_public_key_pem
            }))

            # Receive peer's final IDENTITY
            identity_raw = await asyncio.wait_for(websocket.recv(), timeout=15.0)
            identity_data = json.loads(identity_raw)

            # Validate final IDENTITY
            if identity_data["type"] == "IDENTITY" and \
               identity_data["username"] == requesting_username and \
               identity_data["device_id"] == req_dev_id:

                 # --- Store Connection State Securely ---
                 async with connections_lock:
                     # Final check before storing
                     if peer_ip in connections:
                         logger.warning(f"Race condition: Another connection to {peer_ip} stored before this one finished. Closing.")
                         await websocket.close(1008, reason="Connection race condition")
                         return False
                     connections[peer_ip] = websocket
                     connection_stored = True # Mark that we stored it

                 async with peer_data_lock:
                     peer_public_keys[peer_ip] = serialization.load_pem_public_key(peer_key_pem.encode())
                     peer_usernames[requesting_username] = peer_ip
                     peer_device_ids[peer_ip] = req_dev_id
                 # --- End Store State ---

                 final_display_name = get_peer_display_name(peer_ip)
                 logger.info(f"Secure connection established with {final_display_name} ({peer_ip})")
                 await message_queue.put({"type": "connection_status", "peer_ip": peer_ip, "connected": True})
                 await message_queue.put({"type": "log", "message": f"Connected securely to {final_display_name}"})
                 return True # Handshake successful

            else:
                # Invalid final IDENTITY received
                logger.error(f"Received invalid final IDENTITY from {requesting_display_name}. Expected: {requesting_username}/{req_dev_id}, Got: {identity_data.get('username')}/{identity_data.get('device_id')}")
                raise ConnectionAbortedError("Invalid final IDENTITY received from peer")
        else:
            # Unexpected message type after INIT_ACK
            raise ValueError(f"Unexpected message type after INIT_ACK: {request_data.get('type')}")

    # --- Exception Handling (Incoming Connection) ---
    except (ConnectionRefusedError, ConnectionAbortedError, ValueError) as e:
        # Handshake logic errors or denials
        logger.warning(f"Incoming connection from {peer_ip} ({requesting_display_name}) failed: {e}")
        # Attempt to close gracefully if possible
        if websocket and websocket.state == State.OPEN:
            try: await websocket.close(1002, reason=f"Handshake error: {e}") # Protocol error
            except: pass # Ignore errors during close after failure
        return False # Handshake failed
    except (websockets.exceptions.ConnectionClosed, websockets.exceptions.WebSocketException, ssl.SSLError, OSError, asyncio.TimeoutError) as e:
        # Network, SSL, or Timeout errors during handshake
        logger.warning(f"Network/SSL error during handshake with {peer_ip}: {type(e).__name__}: {e}")
        if websocket and websocket.state == State.OPEN:
            try: await websocket.close(1011, reason="Handshake network error")
            except: pass
        return False # Handshake failed
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse JSON during handshake with {peer_ip}: {e}")
        if websocket and websocket.state == State.OPEN:
            try: await websocket.close(1002, reason="Invalid JSON during handshake")
            except: pass
        return False
    except Exception as e:
        # Catch-all for unexpected errors
        logger.exception(f"Unexpected error during incoming handshake with {peer_ip}: {e}")
        if websocket and websocket.state == State.OPEN:
            try: await websocket.close(1011, reason="Unexpected server error during handshake")
            except: pass
        return False # Handshake failed
    # --- End Exception Handling ---
    finally:
        # --- Cleanup Logic ---
        # Ensure pending approval future is removed if an error occurred before it resolved
        if approval_key and approval_key in pending_approvals:
            pending_approvals.pop(approval_key, None)
            logger.debug(f"Cleaned up pending approval key {approval_key} in finally block.")

        # Clean up connection attempt record for this incoming request
        async with connection_attempts_lock:
            attempt_info = connection_attempts.get(peer_ip)
            # Only remove if it's *this specific* incoming attempt
            if attempt_info and attempt_info[0] == "incoming" and attempt_info[1] == connection_timestamp:
                 connection_attempts.pop(peer_ip, None)
                 logger.debug(f"Cleaned up incoming connection attempt record for {peer_ip}")

        # If handshake failed *after* connection state was potentially stored (e.g., invalid final IDENTITY)
        if not connection_stored:
            async with connections_lock:
                # Check if it was somehow stored despite failure flag
                if peer_ip in connections:
                     logger.debug(f"Cleaning up potentially stored connection for {peer_ip} after handshake failure in finally block.")
                     connections.pop(peer_ip, None)
                     # Also clean associated peer data if connection was wrongly stored
                     async with peer_data_lock:
                         peer_public_keys.pop(peer_ip, None)
                         peer_device_ids.pop(peer_ip, None)
                         uname = next((u for u, ip in peer_usernames.items() if ip == peer_ip), None)
                         if uname: peer_usernames.pop(uname, None)
        # --- End Cleanup ---


async def send_message_to_peers(message, target_peer_ip=None):
    """Send an encrypted message to one or all connected peers. Called by Backend."""
    if not isinstance(message, str) or not message:
        logger.warning("Attempted send empty message.")
        return False

    peers_to_send = {}
    sent_count = 0

    # --- Acquire lock to safely get connections ---
    async with connections_lock:
        if target_peer_ip:
            ws = connections.get(target_peer_ip)
            if ws:
                peers_to_send[target_peer_ip] = ws # Store the single websocket
            else:
                # Log warning and notify UI if target peer not connected
                display_name_target = get_peer_display_name(target_peer_ip) # Get name for message
                logger.warning(f"Msg Send Fail: Not connected to {display_name_target} ({target_peer_ip})")
                await message_queue.put({"type":"log","message":f"Cannot send message: Not connected to {display_name_target}","level":logging.WARNING})
                return False
        else:
            # Broadcast: Create a copy of the connections dict to iterate over
            peers_to_send = connections.copy()
    # --- Release lock ---

    if not peers_to_send:
         # Handle case where no peers are connected for broadcast
         if target_peer_ip is None:
             await message_queue.put({"type":"log","message":"No connected peers to send broadcast message to."})
             logger.info("Msg Send Fail: No connected peers for broadcast.")
         # If target_peer_ip was specified but not found, already handled above
         return False

    # --- Iterate over the selected peers (copy or single entry) ---
    for ip, ws in peers_to_send.items():
        display_name = get_peer_display_name(ip) # Get name using IP

        # Check WebSocket state before proceeding
        if ws.state == State.OPEN:
            # --- Get peer's public key safely ---
            peer_public_key = None
            async with peer_data_lock:
                peer_public_key = peer_public_keys.get(ip)
            # --- Release lock ---

            if not peer_public_key:
                logger.warning(f"No public key found for {display_name} ({ip}) while sending message. Skipping.")
                continue # Skip sending to this peer

            # --- Encrypt and Send ---
            try:
                encrypted_message_bytes = peer_public_key.encrypt(
                    message.encode('utf-8'), # Ensure message is bytes
                    padding.OAEP(
                        mgf=padding.MGF1(algorithm=hashes.SHA256()), # Use original hashes import
                        algorithm=hashes.SHA256(),
                        label=None
                    )
                )
                # Encode bytes to hex for JSON compatibility
                encrypted_message_hex = encrypted_message_bytes.hex()

                # Construct JSON payload
                payload = json.dumps({"type": "MESSAGE", "message": encrypted_message_hex})

                # Send payload with timeout
                await asyncio.wait_for(ws.send(payload), timeout=10.0)
                sent_count += 1
                logger.debug(f"Sent encrypted message to {display_name} ({ip})")

            # --- Handle Send Errors ---
            except asyncio.TimeoutError:
                logger.error(f"Timeout sending message to {display_name} ({ip}).")
                # Consider marking peer as potentially disconnected or notifying UI
            except (websockets.exceptions.ConnectionClosed, websockets.exceptions.WebSocketException) as e:
                 logger.error(f"Connection error sending message to {display_name} ({ip}): {e}")
                 # This peer is likely disconnected, subsequent sends will fail until state updates
            except Exception as e:
                # Includes potential encryption errors
                logger.error(f"Failed to encrypt or send message to {display_name} ({ip}): {e}", exc_info=True)
            # --- End Handle Send Errors ---

        else:
            # Log if trying to send to a non-open socket (e.g., closing)
            logger.warning(f"Attempted to send message to non-open connection: {display_name} ({ip}), state: {ws.state}")
    # --- End Peer Iteration ---

    return sent_count > 0 # Return True if at least one message was attempted


async def receive_peer_messages(websocket, peer_ip):
    """Receive messages from a connected peer. Handles JSON and Binary (File Chunks)."""
    display_name = get_peer_display_name(peer_ip) # Get name early for logging
    logger.info(f"Starting message receiver loop for {display_name} ({peer_ip})")
    current_receiving_transfer = None # Track file transfer specific to this connection

    try:
        # Continuously listen for messages on this specific websocket
        async for message in websocket:
            if shutdown_event.is_set(): # Check for shutdown signal
                logger.debug(f"Shutdown event set, stopping receive loop for {display_name}.")
                break

            is_binary = isinstance(message, bytes)

            # --- Handle Binary Data (File Chunks) ---
            if is_binary:
                # Get the transfer object associated with this connection
                transfer = current_receiving_transfer
                if transfer and transfer.state == TransferState.IN_PROGRESS:
                    try:
                        # Use the transfer's condition lock for safe state access/modification
                        async with transfer.condition:
                            # Re-check state after acquiring lock in case it changed
                            if transfer.state != TransferState.IN_PROGRESS:
                                logger.warning(f"Received binary data for non-active transfer {transfer.transfer_id[:8]} from {display_name}. State: {transfer.state}. Ignoring.")
                                continue

                            # Write received chunk to file
                            if transfer.file_handle:
                                await transfer.file_handle.write(message)
                            else:
                                logger.error(f"File handle closed unexpectedly for transfer {transfer.transfer_id[:8]}. Failing transfer.")
                                await transfer.fail("File handle missing")
                                current_receiving_transfer = None
                                continue

                            # Update transferred size and hash
                            transfer.transferred_size += len(message)
                            if transfer.hash_algo:
                                transfer.hash_algo.update(message)

                            # --- Check for Transfer Completion ---
                            if transfer.transferred_size >= transfer.total_size:
                                logger.info(f"Received expected size for transfer {transfer.transfer_id[:8]} ({transfer.transferred_size}/{transfer.total_size}). Closing file.")
                                # Close the file handle
                                safe_close_file(transfer.file_handle)
                                transfer.file_handle = None # Clear the handle

                                final_state = TransferState.COMPLETED
                                completion_msg = f"Successfully received '{os.path.basename(transfer.file_path)}' from {display_name}."

                                # --- Verify Hash ---
                                if transfer.expected_hash:
                                    calculated_hash = transfer.hash_algo.hexdigest()
                                    logger.debug(f"Verifying hash for {transfer.transfer_id[:8]}. Expected: {transfer.expected_hash}, Calculated: {calculated_hash}")
                                    if calculated_hash == transfer.expected_hash:
                                        logger.info(f"File hash verified successfully for transfer {transfer.transfer_id[:8]}")
                                    else:
                                        # Hash mismatch - fail transfer and delete file
                                        final_state = TransferState.FAILED
                                        completion_msg = f"Hash mismatch for received file '{os.path.basename(transfer.file_path)}' from {display_name}. File deleted."
                                        logger.error(f"Hash mismatch for transfer {transfer.transfer_id[:8]}. Expected: {transfer.expected_hash}, Got: {calculated_hash}")
                                        try:
                                            # Attempt asynchronous delete
                                            if hasattr(aiofiles, 'os') and hasattr(aiofiles.os, 'remove'):
                                                await aiofiles.os.remove(transfer.file_path)
                                            else:
                                                os.remove(transfer.file_path) # Sync fallback
                                            logger.info(f"Deleted corrupted file due to hash mismatch: {transfer.file_path}")
                                        except OSError as rm_err:
                                            logger.error(f"Failed to delete corrupted file {transfer.file_path}: {rm_err}")
                                else:
                                    # No hash provided for verification
                                    completion_msg += " (No hash provided for verification)."
                                    logger.info(f"Transfer {transfer.transfer_id[:8]} completed without hash verification.")
                                # --- End Hash Verification ---

                                # Update state and notify UI
                                transfer.state = final_state
                                await message_queue.put({"type": "log", "message": completion_msg, "level": logging.ERROR if final_state == TransferState.FAILED else logging.INFO})
                                await message_queue.put({"type": "transfer_update"}) # Signal UI to refresh list
                                current_receiving_transfer = None # Clear current transfer for this connection
                            # --- End Transfer Completion Check ---

                    except Exception as write_err:
                        # Handle errors during file writing or processing
                        logger.exception(f"Error processing file chunk for transfer {transfer.transfer_id[:8]} from {display_name}: {write_err}")
                        if transfer: # Ensure transfer object still exists
                            await transfer.fail(f"File write/processing error: {write_err}")
                        current_receiving_transfer = None # Clear potentially problematic transfer
                else:
                    # Received binary data but no transfer was active/in_progress
                    logger.warning(f"Received unexpected binary data from {display_name} when no transfer active or not in progress.")
            # --- End Handle Binary Data ---

            # --- Handle Non-Binary Data (JSON) ---
            elif not is_binary:
                try:
                    data = json.loads(message) # Parse the JSON string
                    msg_type = data.get("type")
                    logger.debug(f"Received JSON from {display_name}: type={msg_type}")

                    # --- Process Different JSON Message Types ---
                    if msg_type == "MESSAGE":
                        try:
                            # Ensure private key is loaded and is the correct type
                            if "private_key" not in user_data or not isinstance(user_data["private_key"], rsa.RSAPrivateKey):
                                logger.error("Cannot decrypt message: Private key missing or invalid.")
                                await message_queue.put({"type":"log","message":f"[Decryption Error from {display_name}: Missing Key]","level":logging.ERROR})
                                continue # Skip this message

                            encrypted_hex = data.get("message", "")
                            if not encrypted_hex:
                                logger.warning(f"Received empty MESSAGE payload from {display_name}.")
                                continue

                            decrypted_bytes = user_data["private_key"].decrypt(
                                bytes.fromhex(encrypted_hex), # Convert hex string back to bytes
                                padding.OAEP(
                                    mgf=padding.MGF1(algorithm=hashes.SHA256()), # Use original hashes import
                                    algorithm=hashes.SHA256(),
                                    label=None
                                )
                            )
                            decrypted_message = decrypted_bytes.decode('utf-8') # Decode bytes to string
                            # Put successfully decrypted message onto the queue for UI
                            await message_queue.put({"type":"message", "sender_display_name":display_name, "content":decrypted_message})

                        except (ValueError, TypeError) as e: # Handle hex decoding errors, etc.
                            logger.error(f"Decryption or decoding error for message from {display_name}: {e}")
                            await message_queue.put({"type":"log","message":f"[Decryption/Decoding Error from {display_name}]","level":logging.WARNING})
                        except Exception as dec_err: # Catch other crypto errors
                            logger.error(f"General decryption error for message from {display_name}: {dec_err}", exc_info=True)
                            await message_queue.put({"type":"log","message":f"[Decryption Error from {display_name}]","level":logging.WARNING})

                    elif msg_type == "file_transfer_init":
                        # Handle incoming file transfer request
                        if current_receiving_transfer:
                            # If already receiving a file on this connection, fail the old one
                            logger.warning(f"New transfer init from {display_name} overriding active transfer {current_receiving_transfer.transfer_id[:8]}")
                            await current_receiving_transfer.fail("Superseded by new transfer")
                            current_receiving_transfer = None # Ensure it's cleared

                        # Extract transfer details
                        tid = data.get("transfer_id")
                        fname = data.get("filename")
                        fsize = data.get("filesize")
                        fhash = data.get("file_hash") # Optional hash

                        # Validate necessary data
                        if not tid or not fname or fsize is None:
                             logger.error(f"Invalid file_transfer_init received from {display_name}: Missing fields. Data: {data}")
                             continue # Ignore invalid request

                        # Check if file size is acceptable
                        is_valid, message = check_file_size(None, max_size_mb=2000, file_size_bytes=fsize)
                        if not is_valid:
                            logger.warning(f"Rejecting file from {display_name}: {message}")
                            # Send rejection message
                            await websocket.send(json.dumps({
                                "type": "file_transfer_response",
                                "transfer_id": tid,
                                "accepted": False,
                                "reason": message
                            }))
                            continue

                        # --- Prepare Download Path ---
                        safe_fname = os.path.basename(fname) # Basic sanitization
                        download_dir = os.path.join(CONFIG_DIR, "downloads") # Store in config subdir maybe? Or keep relative? Let's keep relative for now.
                        # download_dir = "downloads" # Keep relative path
                        os.makedirs(download_dir, exist_ok=True) # Ensure directory exists
                        path = os.path.join(download_dir, safe_fname)
                        
                        # Check if we have enough disk space
                        has_space, space_message = check_disk_space(download_dir, fsize / (1024 * 1024))
                        if not has_space:
                            logger.warning(f"Rejecting file from {display_name}: {space_message}")
                            # Send rejection message
                            await websocket.send(json.dumps({
                                "type": "file_transfer_response",
                                "transfer_id": tid,
                                "accepted": False,
                                "reason": space_message
                            }))
                            continue

                        # Avoid overwriting existing files by adding counter
                        counter = 1; base, ext = os.path.splitext(path)
                        while os.path.exists(path):
                            path = f"{base}({counter}){ext}"
                            counter += 1
                        # --- End Path Prep ---

                        # --- Create and Register Transfer ---
                        logger.info(f"Initiating receive for '{os.path.basename(path)}' ({tid[:8]}) from {display_name}, Size: {fsize} bytes.")
                        transfer = FileTransfer(path, peer_ip, "receive", tid)
                        transfer.total_size = int(fsize); transfer.expected_hash = fhash
                        transfer.state = TransferState.IN_PROGRESS # Set initial state

                        try:
                            # Open file handle for writing
                            transfer.file_handle = await aiofiles.open(path, "wb")

                            # Add to global state *with lock*
                            async with active_transfers_lock:
                                active_transfers[tid] = transfer

                            # Set as the active receiving transfer *for this specific connection*
                            current_receiving_transfer = transfer

                            # Notify UI
                            await message_queue.put({"type":"transfer_update"}) # Refresh list
                            await message_queue.put({"type":"log", "message":f"Receiving '{os.path.basename(path)}' from {display_name} (ID: {tid[:8]})"})

                        except OSError as e:
                            logger.error(f"Failed to open file '{path}' for transfer {tid} from {display_name}: {e}")
                            await message_queue.put({"type":"log","message":f"Error receiving file from {display_name}: Cannot open destination file.","level":logging.ERROR})
                            # Ensure transfer object is not set if file open failed
                            current_receiving_transfer = None
                            # Clean up potentially registered transfer object
                            async with active_transfers_lock:
                                active_transfers.pop(tid, None)
                        # --- End Create and Register ---

                    elif msg_type == "TRANSFER_PAUSE":
                        from networking.file_transfer import handle_transfer_control_message
                        await handle_transfer_control_message(data, peer_ip)
                    
                    elif msg_type == "TRANSFER_RESUME":
                        from networking.file_transfer import handle_transfer_control_message
                        await handle_transfer_control_message(data, peer_ip)

                    # --- Group Messaging Handling ---
                    # (Ensure locks are used appropriately around shared group state)
                    elif msg_type == "GROUP_CREATE":
                        gn = data.get("groupname"); admin_ip = data.get("admin_ip")
                        if gn and admin_ip:
                            async with groups_lock: groups[gn] = {"admin": admin_ip, "members": {admin_ip}}
                            await message_queue.put({"type":"group_list_update"})
                            admin_display_name = get_peer_display_name(admin_ip)
                            await message_queue.put({"type":"log","message":f"Group '{gn}' created by {admin_display_name}"})
                        else: logger.warning(f"Received incomplete GROUP_CREATE from {display_name}: {data}")

                    elif msg_type == "GROUP_INVITE":
                        invite_data = data # Contains groupname, inviter_ip
                        if invite_data.get("groupname") and invite_data.get("inviter_ip"):
                            async with pending_lock: pending_invites.append(invite_data)
                            await message_queue.put({"type":"pending_invites_update"})
                            inviter_display_name = get_peer_display_name(invite_data['inviter_ip'])
                            await message_queue.put({"type":"log","message":f"Invite to join group '{invite_data['groupname']}' received from {inviter_display_name}"})
                        else: logger.warning(f"Received incomplete GROUP_INVITE from {display_name}: {data}")

                    elif msg_type == "GROUP_INVITE_RESPONSE": # Received by the admin who sent the invite
                         gn=data.get("groupname"); invitee_ip=data.get("invitee_ip"); accepted=data.get("accepted")
                         own_ip = await get_own_ip()
                         if not gn or not invitee_ip or accepted is None:
                             logger.warning(f"Received incomplete GROUP_INVITE_RESPONSE from {display_name}: {data}"); continue

                         async with groups_lock:
                             group_info = groups.get(gn)
                             if group_info and group_info.get("admin") == own_ip: # Check if I am the admin
                                  invitee_display_name = get_peer_display_name(invitee_ip)
                                  if accepted:
                                      if invitee_ip not in group_info["members"]:
                                          group_info["members"].add(invitee_ip)
                                          needs_update = True
                                          log_msg = f"{invitee_display_name} accepted invite and joined '{gn}'."
                                      else:
                                          needs_update = False # Already a member
                                          log_msg = f"{invitee_display_name} accepted invite to '{gn}' (already a member)."
                                  else:
                                      needs_update = False # Membership didn't change
                                      log_msg = f"{invitee_display_name} declined invite to '{gn}'."

                                  # Get member list for update message *outside* the lock if needed
                                  members_list_for_update = list(group_info["members"]) if needs_update else None

                         # Perform actions requiring network outside the group lock
                         if needs_update and members_list_for_update:
                             await send_group_update_message(gn, members_list_for_update) # Notify all members
                             await message_queue.put({"type":"group_list_update"}) # Update admin's UI

                         # Log the outcome regardless
                         await message_queue.put({"type":"log","message":log_msg})

                         if not group_info or group_info.get("admin") != own_ip:
                             logger.warning(f"Received invite response for group '{gn}' but not admin or group doesn't exist.")

                    elif msg_type == "GROUP_JOIN_REQUEST": # Received by the admin of the group
                         gn=data.get("groupname"); req_ip=data.get("requester_ip"); req_uname=data.get("requester_username")
                         own_ip = await get_own_ip()
                         if not gn or not req_ip or not req_uname:
                              logger.warning(f"Received incomplete GROUP_JOIN_REQUEST from {display_name}: {data}"); continue

                         async with groups_lock: group_info = groups.get(gn)

                         if group_info and group_info.get("admin") == own_ip:
                              # Add request to pending list if not already present
                              async with pending_lock:
                                   join_req_list = pending_join_requests[gn]
                                   if not any(r.get("requester_ip") == req_ip for r in join_req_list):
                                        join_req_list.append({"requester_ip":req_ip,"requester_username":req_uname})
                                        # Notify UI after lock released
                                        await message_queue.put({"type":"join_requests_update"})
                                        await message_queue.put({"type":"log","message":f"Received join request for group '{gn}' from {req_uname}"})
                                   else: logger.info(f"Duplicate join request ignored for group '{gn}' from {req_uname}")
                         else: logger.warning(f"Received join request for group '{gn}' but not admin or group doesn't exist.")

                    elif msg_type == "GROUP_JOIN_RESPONSE": # Received by the user who requested to join
                         gn=data.get("groupname"); req_ip=data.get("requester_ip"); approved=data.get("approved"); admin_ip=data.get("admin_ip")
                         own_ip = await get_own_ip()
                         if not gn or not req_ip or approved is None or not admin_ip:
                              logger.warning(f"Received incomplete GROUP_JOIN_RESPONSE from {display_name}: {data}"); continue

                         if req_ip == own_ip: # Check if this response is for me
                              admin_display_name = get_peer_display_name(admin_ip)
                              if approved:
                                   # Add group to local state
                                   async with groups_lock:
                                       # Assume initial members are admin and self
                                       groups[gn] = {"admin":admin_ip,"members":{admin_ip, req_ip}}
                                   await message_queue.put({"type":"group_list_update"}) # Update UI
                                   await message_queue.put({"type":"log","message":f"Your join request for group '{gn}' was approved by {admin_display_name}."})
                              else:
                                   await message_queue.put({"type":"log","message":f"Your join request for group '{gn}' was denied by {admin_display_name}."})
                         # else: # Ignore if response is not for me (or log warning)
                         #    logger.warning(f"Received join response intended for {req_ip}, not me ({own_ip}).")

                    elif msg_type == "GROUP_UPDATE": # Received by group members when membership changes
                         gn=data.get("groupname"); members_set=set(data.get("members", [])); admin=data.get("admin")
                         own_ip = await get_own_ip()
                         if not gn or not members_set or not admin:
                              logger.warning(f"Received incomplete GROUP_UPDATE from {display_name}: {data}"); continue

                         needs_ui_update = False
                         log_msg = None

                         async with groups_lock:
                              current_group_info = groups.get(gn)
                              am_i_member = own_ip in members_set

                              if am_i_member:
                                   # If I am in the updated list
                                   if not current_group_info or current_group_info.get("members") != members_set or current_group_info.get("admin") != admin:
                                        # Add or update group info if it changed or I wasn't aware of it
                                        groups[gn] = {"admin": admin, "members": members_set}
                                        needs_ui_update = True
                                        log_msg = f"Group '{gn}' membership or admin updated."
                              elif current_group_info:
                                   # If I am NOT in the updated list, but I previously was
                                   del groups[gn] # Remove group from my state
                                   needs_ui_update = True
                                   log_msg = f"You were removed from group '{gn}'."

                         # Notify UI outside the lock
                         if needs_ui_update:
                              await message_queue.put({"type":"group_list_update"})
                              if log_msg: await message_queue.put({"type":"log","message":log_msg})

                    # --- End Group Messaging Handling ---

                    else:
                        # Handle unknown JSON types
                        logger.warning(f"Received unknown JSON message type '{msg_type}' from {display_name}")

                # --- Handle JSON Processing Errors ---
                except json.JSONDecodeError:
                    logger.warning(f"Received non-JSON text message from {display_name}: {message[:100]}...")
                    # Optionally forward malformed message to UI for debugging:
                    # await message_queue.put({"type":"message","sender_display_name":display_name,"content":f"[INVALID JSON RECEIVED] {message[:100]}..."})
                except Exception as proc_err:
                    logger.exception(f"Error processing JSON message type '{data.get('type', 'Unknown')}' from {display_name}: {proc_err}")
                # --- End JSON Error Handling ---
            # --- End Handle Non-Binary ---

    # --- Handle Connection Closure and Errors ---
    except websockets.exceptions.ConnectionClosedOK:
        logger.info(f"Connection closed normally by {display_name} ({peer_ip})")
    except websockets.exceptions.ConnectionClosedError as e:
        logger.warning(f"Connection closed abruptly by {display_name} ({peer_ip}): {e}")
    except asyncio.CancelledError:
        # Expected during shutdown
        logger.info(f"Message receive loop cancelled for {display_name} ({peer_ip})")
    except Exception as e:
        # Catch unexpected errors in the receive loop
        logger.exception(f"Unexpected error in message receive loop for {display_name} ({peer_ip}): {e}")
    # --- End Connection Closure/Error Handling ---

    # --- Finally Block (Guaranteed Execution) ---
    finally:
        logger.debug(f"Cleaning up connection state for {peer_ip} in receive_peer_messages finally block.")

        # --- Clean up connection state (use locks) ---
        async with connections_lock:
            connections.pop(peer_ip, None) # Remove connection object
        async with peer_data_lock:
            peer_public_keys.pop(peer_ip, None) # Remove peer key
            peer_device_ids.pop(peer_ip, None) # Remove device ID
            # Find and remove username mapping
            username = next((u for u, ip in peer_usernames.items() if ip == peer_ip), None)
            if username: peer_usernames.pop(username, None)
        # --- End connection state cleanup ---

        # --- Clean up outgoing transfer tracking for this peer ---
        async with active_transfers_lock:
            outgoing_transfers_by_peer.pop(peer_ip, None)
        # --- End outgoing transfer cleanup ---

        # --- Fail any active *receiving* transfer on this connection ---
        if current_receiving_transfer:
            logger.warning(f"Failing active receiving transfer {current_receiving_transfer.transfer_id[:8]} due to connection termination with {display_name}.")
            # Ensure file handle is closed within fail method
            await current_receiving_transfer.fail("Connection lost during transfer")
            # No need to remove from active_transfers here, update_transfer_progress handles cleanup
        # --- End fail receiving transfer ---

        # --- Notify UI about disconnection if not shutting down ---
        if not shutdown_event.is_set():
            await message_queue.put({"type": "connection_status", "peer_ip": peer_ip, "connected": False})
            await message_queue.put({"type": "log", "message": f"Disconnected from {display_name}"})
        # --- End UI notification ---

        logger.info(f"Message receive loop finished for {display_name} ({peer_ip})")
    # --- End Finally Block ---


async def maintain_peer_list(discovery_instance):
    """Periodically check connections via ping and update peer list based on discovery."""
    while not shutdown_event.is_set():
        try:
            disconnected_peers = []
            # --- Create a snapshot of connections under lock ---
            async with connections_lock:
                current_connections = list(connections.items()) # Get IP and websocket object
            # --- Release lock ---

            # --- Ping connected peers ---
            for peer_ip, ws in current_connections:
                if shutdown_event.is_set(): break # Exit early if shutdown requested
                display_name = get_peer_display_name(peer_ip) # Get name for logging

                try:
                    # Send ping and wait for pong with timeout
                    await asyncio.wait_for(ws.ping(), timeout=10.0)
                    # logger.debug(f"Ping successful for {display_name} ({peer_ip})") # Reduce log noise
                except (asyncio.TimeoutError, websockets.exceptions.ConnectionClosed, websockets.exceptions.WebSocketException) as e:
                    # Ping failed or connection closed
                    logger.warning(f"Connection lost or ping failed for {display_name} ({peer_ip}): {type(e).__name__}")
                    disconnected_peers.append(peer_ip) # Mark for cleanup

                    # Attempt to close socket if open, ignore errors during close
                    if ws.state == State.OPEN:
                        try:
                            await asyncio.wait_for(ws.close(code=1011, reason="Ping failure or connection lost"), timeout=2.0)
                        except: pass # Ignore close errors after failure detection

                    # --- Remove failed peer from shared state (use locks) ---
                    async with connections_lock: connections.pop(peer_ip, None)
                    async with peer_data_lock:
                        peer_public_keys.pop(peer_ip, None)
                        peer_device_ids.pop(peer_ip, None)
                        uname = next((u for u, ip in peer_usernames.items() if ip == peer_ip), None)
                        if uname: peer_usernames.pop(uname, None)
                    async with active_transfers_lock:
                         outgoing_transfers_by_peer.pop(peer_ip, None) # Clean outgoing transfer tracking
                    # --- End Remove Peer State ---
            # --- End Ping Loop ---

            # --- Notify UI about all disconnections found in this cycle ---
            if disconnected_peers:
                logger.info(f"Detected {len(disconnected_peers)} disconnected peers in maintain cycle.")
                for peer_ip in disconnected_peers:
                     # Get name again (might be just IP now if state removed)
                     display_name_disc = get_peer_display_name(peer_ip)
                     await message_queue.put({"type": "connection_status", "peer_ip": peer_ip, "connected": False})
                     await message_queue.put({"type": "log", "message": f"Lost connection to {display_name_disc}"})
            # --- End Notify UI ---

            # --- Wait before next check cycle ---
            await asyncio.sleep(15) # Interval between checks

        except asyncio.CancelledError:
            # Expected on shutdown
            logger.info("maintain_peer_list task cancelled.")
            break
        except Exception as e:
            # Log unexpected errors in the maintenance loop
            logger.exception(f"Error in maintain_peer_list loop: {e}")
            await asyncio.sleep(30) # Wait longer after an error before retrying

    logger.info("maintain_peer_list stopped.")


# --- Dummy functions for CLI mode (should not run in GUI) ---
# These remain as placeholders; their logic is in the original main.py
async def user_input(discovery):
    if 'aiconsole' not in sys.modules: # Basic check if likely in GUI mode
        logger.critical("CLI user_input task running - should not happen in GUI mode!")
        await asyncio.sleep(3600) # Sleep indefinitely
    else:
        # Placeholder for actual CLI input logic if this file were run directly
        logger.warning("CLI user_input placeholder called.")
        await asyncio.sleep(1)
        pass # Add CLI input handling here if needed standalone

async def display_messages():
     if 'aiconsole' not in sys.modules: # Basic check
        logger.critical("CLI display_messages task running - should not happen in GUI mode!")
        await asyncio.sleep(3600) # Sleep indefinitely
     else:
        # Placeholder for actual CLI output logic
        logger.warning("CLI display_messages placeholder called.")
        await asyncio.sleep(1)
        pass # Add CLI output handling here if needed standalone
# --- End Dummy