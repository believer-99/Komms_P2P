import socket
import struct
import logging
import asyncio
import time
import netifaces
import json


DISCOVERY_PORT = 50001
MULTICAST_GROUP = "224.0.0.1"
DISCOVERY_MESSAGE = "P2P_DISCOVERY"


class PeerDiscovery:
    def __init__(self):
        self.peer_list = []  # List of peer IPs
        self.last_seen = {}  # Dictionary of peer IPs to last-seen timestamps
        self._lock = asyncio.Lock()  # For thread-safe list operations

    async def _get_own_ip(self):
        # Placeholder for getting the local IP address
        # This could involve connecting to a remote server or checking interfaces
        return "192.168.1.100"  # Example IP; replace with actual implementation

    async def send_broadcasts(self):
        # Placeholder for sending broadcasts to announce presence
        pass

    async def receive_broadcasts(self):
        own_ip = await self._get_own_ip()
        # Simulated broadcast receiving loop (actual socket code omitted for brevity)
        while True:
            # Example: data, addr = await loop.sock_recvfrom(sock, 1024)
            # For demonstration, assume addr[0] is the sender's IP
            sender_ip = "192.168.1.101"  # Replace with actual received addr[0]
            DISCOVERY_MESSAGE = "PEER_DISCOVERY"  # Example message
            data = DISCOVERY_MESSAGE.encode()  # Simulated received data

            if data.decode() == DISCOVERY_MESSAGE and sender_ip != own_ip:
                async with self._lock:
                    self.last_seen[sender_ip] = time.time()
                    if sender_ip not in self.peer_list:
                        logging.info(f"Found peer at {sender_ip}")
                        self.peer_list.append(sender_ip)
            await asyncio.sleep(1)  # Simulate async operation

    async def cleanup_stale_peers(self):
        while True:
            async with self._lock:
                now = time.time()
                # Identify peers not seen for over 30 seconds
                stale = [ip for ip, ts in self.last_seen.items() if now - ts > 30]
                for ip in stale:
                    if ip in self.peer_list:
                        logging.info(f"Removing stale peer: {ip}")
                        self.peer_list.remove(ip)
                    if ip in self.last_seen:
                        del self.last_seen[ip]
            await asyncio.sleep(5)  # Check every 10 seconds

    async def cleanup_stale_peers(self):
        async with self._lock:
            now = time.time()
            stale = [ip for ip, ts in self.last_seen.items() if now - ts > 30]
            for ip in stale:
                if ip in self.peer_list:
                    logging.info(f"Removing stale peer: {ip}")
                    self.peer_list.remove(ip)
                if ip in self.last_seen:
                    del self.last_seen[ip]

    async def send_broadcasts(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        sock.setblocking(False)
        loop = asyncio.get_event_loop()

        try:
            logging.info("Starting discovery broadcasts...")
            while True:
                try:
                    await loop.sock_sendto(sock, DISCOVERY_MESSAGE.encode(), (MULTICAST_GROUP, DISCOVERY_PORT))
                    logging.debug("Broadcasted Discovery Message")
                    await asyncio.sleep(5)
                except BlockingIOError:
                    await asyncio.sleep(0.1)
                except Exception as e:
                    logging.exception(f"Error sending broadcast: {e}")
                    await asyncio.sleep(1)
        finally:
            sock.close()

    async def _get_own_ip(self):
        try:
          
            for interface in netifaces.interfaces():
                try:
                    addrs = netifaces.ifaddresses(interface)
                    if netifaces.AF_INET in addrs:
                        ip = addrs[netifaces.AF_INET][0]['addr']
                     
                        if not (ip.startswith('127.') or ip.startswith('169.254.')):
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

discovery = PeerDiscovery()
