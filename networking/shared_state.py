import asyncio
from collections import defaultdict

# Shared state variables
active_transfers = {}
message_queue = asyncio.Queue()
connections = {}
user_data = {}
peer_public_keys = {}
peer_usernames = {}

# Shutdown event for graceful exit
shutdown_event = asyncio.Event()