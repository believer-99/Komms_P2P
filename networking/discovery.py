import asyncio
import socket
import struct
import time
import logging

MULTICAST_GROUP = "224.0.0.1"
DISCOVERY_PORT = 50001
DISCOVERY_MESSAGE = "PEER_DISCOVERY"

class PeerDiscovery:
    def __init__(self):
        self.peer_list = []
        self.last_seen = {}
        self._lock = asyncio.Lock()
        self.own_ip = self.get_own_ip()  # Get own IP at initialization

    def get_own_ip(self):
        """Determine the peer's own IP address by connecting to an external server."""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception as e:
            logging.error(f"Error getting own IP: {e}")
            return "127.0.0.1"  # Fallback to localhost if it fails

    async def send_broadcasts(self):
        """Send periodic multicast discovery messages with the peer's own IP."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        sock.setblocking(False)
        loop = asyncio.get_event_loop()

        try:
            discovery_message = f"{DISCOVERY_MESSAGE} {self.own_ip}"
            logging.info("Starting discovery broadcasts...")
            while True:
                await loop.sock_sendto(sock, discovery_message.encode(), (MULTICAST_GROUP, DISCOVERY_PORT))
                logging.debug("Broadcasted Discovery Message")
                await asyncio.sleep(5)  # Send every 5 seconds
        finally:
            sock.close()

    async def receive_broadcasts(self):
        """Receive multicast discovery messages and update peer list, ignoring self-messages."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(('', DISCOVERY_PORT))
        mreq = struct.pack("4sl", socket.inet_aton(MULTICAST_GROUP), socket.INADDR_ANY)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        sock.setblocking(False)
        loop = asyncio.get_event_loop()

        try:
            while True:
                data, addr = await loop.sock_recvfrom(sock, 1024)
                message = data.decode()
                if message.startswith(DISCOVERY_MESSAGE):
                    parts = message.split(" ", 1)
                    if len(parts) == 2:
                        sender_ip = parts[1]
                        if sender_ip != self.own_ip:  # Ignore self-messages
                            async with self._lock:
                                self.last_seen[sender_ip] = time.time()
                                if sender_ip not in self.peer_list:
                                    logging.info(f"Found peer at {sender_ip}")
                                    self.peer_list.append(sender_ip)
        finally:
            sock.close()

    async def cleanup_stale_peers(self):
        """Periodically remove peers that haven't been seen for over 30 seconds."""
        while True:
            async with self._lock:
                now = time.time()
                stale = [ip for ip, ts in self.last_seen.items() if now - ts > 30]
                for ip in stale:
                    if ip in self.peer_list:
                        logging.info(f"Removing stale peer: {ip}")
                        self.peer_list.remove(ip)
                    if ip in self.last_seen:
                        del self.last_seen[ip]
            await asyncio.sleep(10)  # Check every 10 seconds

    async def start(self):
        """Start the peer discovery process, including sending, receiving, and cleaning up peers."""
        await asyncio.gather(
            self.send_broadcasts(),
            self.receive_broadcasts(),
            self.cleanup_stale_peers()
        )

# Example usage (if run standalone)
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    discovery = PeerDiscovery()
    asyncio.run(discovery.start())