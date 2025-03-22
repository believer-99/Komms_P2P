import asyncio
import socket
import json
import logging
import netifaces
from networking.messaging import get_own_ip
from networking.shared_state import user_data

class PeerDiscovery:
    def __init__(self, broadcast_interval=5, cleanup_interval=60):
        self.broadcast_port = 37020
        self.peer_list = {}  # Changed to dict: {ip: (username, last_seen)}
        self.broadcast_interval = broadcast_interval
        self.cleanup_interval = cleanup_interval
        self.running = True

    async def send_broadcasts(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        try:
            own_ip = await get_own_ip()
            username = user_data.get("original_username", "unknown")
            message = json.dumps({"ip": own_ip, "username": username}).encode()

            while self.running:
                try:
                    for interface in netifaces.interfaces():
                        try:
                            if netifaces.AF_INET in netifaces.ifaddresses(interface):
                                broadcast_addr = netifaces.ifaddresses(interface)[netifaces.AF_INET][0]["broadcast"]
                                sock.sendto(message, (broadcast_addr, self.broadcast_port))
                        except Exception as e:
                            logging.debug(f"Error broadcasting on {interface}: {e}")
                except Exception as e:
                    logging.error(f"Error in send_broadcasts: {e}")
                await asyncio.sleep(self.broadcast_interval)
        finally:
            sock.close()

    async def receive_broadcasts(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("", self.broadcast_port))

        try:
            own_ip = await get_own_ip()
            while self.running:
                try:
                    data, (sender_ip, _) = await asyncio.get_event_loop().run_in_executor(None, sock.recvfrom, 1024)
                    if sender_ip == own_ip:
                        continue
                    message = json.loads(data.decode())
                    peer_ip = message.get("ip")
                    peer_username = message.get("username", "unknown")
                    if peer_ip and peer_username:
                        self.peer_list[peer_ip] = (peer_username, asyncio.get_event_loop().time())
                except Exception as e:
                    logging.error(f"Error receiving broadcast: {e}")
        finally:
            sock.close()

    async def cleanup_stale_peers(self):
        while self.running:
            try:
                current_time = asyncio.get_event_loop().time()
                stale_peers = [
                    ip for ip, (_, last_seen) in list(self.peer_list.items())
                    if current_time - last_seen > self.cleanup_interval
                ]
                for ip in stale_peers:
                    del self.peer_list[ip]
            except Exception as e:
                logging.error(f"Error cleaning up stale peers: {e}")
            await asyncio.sleep(self.cleanup_interval)

    def stop(self):
        self.running = False