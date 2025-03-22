import asyncio
import socket
import json
import logging
import netifaces
from networking.utils import get_own_ip
from networking.shared_state import user_data, shutdown_event

class PeerDiscovery:
    def __init__(self, broadcast_interval=5, cleanup_interval=60):
        self.broadcast_port = 37020
        self.peer_list = {}  # {ip: (username, last_seen)}
        self.broadcast_interval = broadcast_interval
        self.cleanup_interval = cleanup_interval
        self.running = True

    async def send_broadcasts(self):
        """Send periodic UDP broadcasts to announce presence."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            while self.running and not shutdown_event.is_set():
                own_ip = await get_own_ip()
                username = user_data.get("original_username", "unknown")
                message = json.dumps({"ip": own_ip, "username": username}).encode()
                for interface in netifaces.interfaces():
                    try:
                        if netifaces.AF_INET in netifaces.ifaddresses(interface):
                            broadcast_addr = netifaces.ifaddresses(interface)[netifaces.AF_INET][0]["broadcast"]
                            sock.sendto(message, (broadcast_addr, self.broadcast_port))
                    except Exception as e:
                        logging.debug(f"Error broadcasting on {interface}: {e}")
                await asyncio.sleep(self.broadcast_interval)
        finally:
            sock.close()
        logging.info("send_broadcasts stopped.")

    async def receive_broadcasts(self):
        """Receive UDP broadcasts from peers."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("", self.broadcast_port))
        sock.setblocking(False)
        loop = asyncio.get_event_loop()
        own_ip = await get_own_ip()
        try:
            while self.running and not shutdown_event.is_set():
                try:
                    data, (sender_ip, _) = await loop.sock_recvfrom(sock, 1024)
                    if sender_ip == own_ip:
                        continue
                    message = json.loads(data.decode())
                    peer_ip = message["ip"]
                    username = message["username"]
                    self.peer_list[peer_ip] = (username, asyncio.get_event_loop().time())
                except json.JSONDecodeError:
                    logging.warning(f"Invalid broadcast received from {sender_ip}")
                except Exception as e:
                    logging.error(f"Error receiving broadcast: {e}")
                await asyncio.sleep(0.1)
        finally:
            sock.close()
        logging.info("receive_broadcasts stopped.")

    async def cleanup_stale_peers(self):
        """Remove peers not seen recently."""
        while self.running and not shutdown_event.is_set():
            current_time = asyncio.get_event_loop().time()
            for peer_ip in list(self.peer_list.keys()):
                _, last_seen = self.peer_list[peer_ip]
                if current_time - last_seen > self.cleanup_interval:
                    del self.peer_list[peer_ip]
                    logging.info(f"Removed stale peer: {peer_ip}")
            await asyncio.sleep(self.cleanup_interval)
        logging.info("cleanup_stale_peers stopped.")

    async def send_immediate_broadcast(self):
        """Send an immediate broadcast to announce a username change."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            own_ip = await get_own_ip()
            username = user_data.get("original_username", "unknown")
            message = json.dumps({"ip": own_ip, "username": username}).encode()
            for interface in netifaces.interfaces():
                try:
                    if netifaces.AF_INET in netifaces.ifaddresses(interface):
                        broadcast_addr = netifaces.ifaddresses(interface)[netifaces.AF_INET][0]["broadcast"]
                        sock.sendto(message, (broadcast_addr, self.broadcast_port))
                except Exception as e:
                    logging.debug(f"Error broadcasting on {interface}: {e}")
        finally:
            sock.close()

    def stop(self):
        """Stop the discovery process."""
        self.running = False