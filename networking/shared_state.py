import asyncio

active_transfers = {}
message_queue = asyncio.Queue()
connections = {}  # Temporary: IP to WebSocket
user_data = {}  # Persistent: original_username, internal_username, device_id, keys
peer_public_keys = {}  # Temporary: IP to public key
peer_usernames = {}  # Temporary: username to IP