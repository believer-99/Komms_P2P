import socket
import asyncio
import logging
import netifaces 


try:
    from networking.shared_state import (
        peer_usernames, peer_device_ids, user_data, connections
    )
    # Check if networking is truly available (useful if utils is imported before main logic)
    _networking_state_available = True
except ImportError:
    # Define dummies if state cannot be imported (e.g., running utils standalone)
    peer_usernames = {}; peer_device_ids = {}; user_data = {}; connections = {}
    _networking_state_available = False


logger = logging.getLogger(__name__) # Use module-specific logger

async def get_own_ip():
    """Get the local IP address used for external communication.
       Tries multiple methods for robustness."""
    # 1. Try UDP connection trick (often works)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(0.1)
    try:
        s.connect(('10.254.254.254', 1)) # Doesn't need to be reachable
        ip = s.getsockname()[0]
        if ip and not ip.startswith('127.'): # Basic check for loopback
            logger.debug(f"Determined own IP via UDP trick: {ip}")
            return ip
    except Exception:
        logger.debug("UDP trick failed to get IP.")
    finally:
        s.close()

    # 2. Try netifaces (more reliable on multi-interface systems)
    try:
        for interface in netifaces.interfaces():
            # Skip loopback and docker interfaces usually
            if interface.startswith('lo') or interface.startswith('docker'):
                continue
            ifaddresses = netifaces.ifaddresses(interface)
            if netifaces.AF_INET in ifaddresses:
                for addr_info in ifaddresses[netifaces.AF_INET]:
                    ip = addr_info.get('addr')
                    # Check for valid private/public IPs, avoid link-local unless necessary
                    if ip and not ip.startswith('127.') and not ip.startswith('169.254.'):
                        logger.debug(f"Determined own IP via netifaces ({interface}): {ip}")
                        return ip
    except Exception as e:
        logger.warning(f"Error using netifaces: {e}")

    # 3. Fallback to hostname resolution
    try:
        hostname = socket.gethostname()
        ip = socket.gethostbyname(hostname)
        if ip and not ip.startswith('127.'):
             logger.debug(f"Determined own IP via hostname: {ip}")
             return ip
    except Exception:
        logger.warning("Could not resolve hostname.")

    # 4. Final fallback
    logger.warning("Could not determine non-loopback IP, falling back to 127.0.0.1.")
    return "127.0.0.1"


def get_peer_display_name(peer_ip):
    """Return the display name for a peer based on username and device ID."""
    if not _networking_state_available or not peer_ip: return f"Peer_{peer_ip or 'Unknown'}"

    # Find username associated with the IP
    username = next((uname for uname, ip in peer_usernames.items() if ip == peer_ip), "Unknown")
    device_id = peer_device_ids.get(peer_ip)
    device_suffix = f"({device_id[:8]})" if device_id else "" # Only add suffix if ID exists

    # Determine if suffix is needed (multiple devices with same *base* username connected)
    needs_suffix = False
    if username != "Unknown":
        count = 0
        for uname_iter, ip_iter in peer_usernames.items():
            # Check if connected peer has the same base username
            if uname_iter == username and ip_iter in connections:
                 count += 1
        # Show suffix if ID exists AND (username is Unknown OR multiple devices are connected)
        if device_suffix and (username == "Unknown" or count > 1):
             needs_suffix = True

    return f"{username}{device_suffix}" if needs_suffix else username


def get_own_display_name():
    """Return the display name for the local user."""
    if not _networking_state_available: return "You(dummy)"
    # Assumes user_data is populated by initialize_user_config
    username = user_data.get("original_username", "User")
    device_id = user_data.get("device_id")
    return f"{username}({device_id[:8]})" if device_id else username

# Add resolve_peer_target if it was previously in utils and needed by backend triggers
async def resolve_peer_target(target_identifier):
    """Resolve a target identifier (username, display name) to a connected peer IP."""
    if not _networking_state_available: return None, "not_found" # Cannot resolve in dummy mode

    if not target_identifier: return None, "not_found"

    # Exact match on IP first
    if target_identifier in connections: return target_identifier, "found"

    matches = []
  
    for peer_ip in list(connections.keys()): 
        display_name = get_peer_display_name(peer_ip)
        original_username = next((uname for uname, ip in peer_usernames.items() if ip == peer_ip), None)

        if target_identifier == display_name:
             # Exact display name match is usually unambiguous
             return peer_ip, "found"
        elif original_username and target_identifier == original_username:
             matches.append(peer_ip)

    unique_matches = list(set(matches))
    if len(unique_matches) == 1:
        return unique_matches[0], "found"
    elif len(unique_matches) > 1:
      
        return [get_peer_display_name(ip) for ip in unique_matches], "ambiguous"
    else:
       
        return None, "not_found"


def get_config_directory():
    """Determine the appropriate config directory based on the OS.
       Needed by initialize_user_config.
    """
    try:
        from appdirs import user_config_dir
     
        return user_config_dir("P2PChat", False)
    except ImportError:
        logger.warning("appdirs not found. Using simple '.config' directory.")
        
        home = os.path.expanduser("~")
        return os.path.join(home, ".config", "P2PChat")

