import asyncio
from collections import defaultdict

shutdown_event = asyncio.Event()
active_transfers = {}
message_queue = asyncio.Queue()
connections = {} 
user_data = {}
peer_public_keys = {}  
peer_usernames = {}  
peer_device_ids = {} 

# --- Group State ---
groups = defaultdict(lambda: {"admin": None, "members": set()}) # {groupname: {"admin": admin_ip, "members": {ip1, ip2}}}
pending_invites = [] # [{"groupname": name, "inviter_ip": ip}]
pending_join_requests = defaultdict(list) # {groupname: [{"requester_ip": ip, "requester_username": name}]}

# --- Connection Management State ---
pending_approvals = {} # { (peer_ip, requesting_username) : asyncio.Future }
connection_denials = {} # { (target_username, requesting_username): denial_count }

# --- Locks for Thread Safety ---
connections_lock = asyncio.Lock()
active_transfers_lock = asyncio.Lock()
peer_data_lock = asyncio.Lock()  # For peer_usernames, peer_public_keys, peer_device_ids
groups_lock = asyncio.Lock()
pending_lock = asyncio.Lock()  # For pending_invites, pending_join_requests, etc.

# Dictionary to track outgoing transfers by peer_ip to prevent multiple concurrent transfers
outgoing_transfers_by_peer = {}  # {peer_ip: transfer_id}

# Track ongoing connection attempts to handle simultaneous connections
connection_attempts = {}  # {peer_ip: timestamp}
connection_attempts_lock = asyncio.Lock()
