import asyncio
import socket
import struct
import time
import logging

# Constants
MULTICAST_GROUP = "224.0.0.1"  # Multicast group address
DISCOVERY_PORT = 8766          # Port for discovery
DISCOVERY_MESSAGE = "P2P_DISCOVERY"  # Base discovery message

class PeerDiscovery:
    def __init__(self):
        self.peer_list = []
        self.last_seen = {}
        self._lock = asyncio.Lock()

    async def _get_own_ip(self):
        """Get the local IP address of the machine."""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            # Connect to a remote address to determine the local IP (doesn’t send data)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception as e:
            logging.error(f"Error getting own IP: {e}")
            return "127.0.0.1"  # Fallback to localhost

    async def send_broadcasts(self):
        """Send periodic multicast discovery messages with the local IP."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 0)  # Disable loopback
        sock.setblocking(False)
        loop = asyncio.get_event_loop()

        try:
            own_ip = await self._get_own_ip()
            discovery_message = f"{DISCOVERY_MESSAGE} {own_ip}"
            logging.info("Starting discovery broadcasts...")
            while True:
                try:
                    await loop.sock_sendto(sock, discovery_message.encode(), (MULTICAST_GROUP, DISCOVERY_PORT))
                    logging.debug("Broadcasted Discovery Message")
                    await asyncio.sleep(5)  # Send every 5 seconds
                except BlockingIOError:
                    await asyncio.sleep(0.1)
                except Exception as e:
                    logging.exception(f"Error sending broadcast: {e}")
                    await asyncio.sleep(1)
        finally:
            sock.close()

    async def receive_broadcasts(self):
        """Receive multicast discovery messages and update peer list."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(('', DISCOVERY_PORT))
        mreq = struct.pack("4sl", socket.inet_aton(MULTICAST_GROUP), socket.INADDR_ANY)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 0)  # Disable loopback
        sock.setblocking(False)
        loop = asyncio.get_event_loop()

        try:
            own_ip = await self._get_own_ip()
            while True:
                try:
                    data, addr = await loop.sock_recvfrom(sock, 1024)
                    message = data.decode()
                    if message.startswith(DISCOVERY_MESSAGE):
                        parts = message.split(" ", 1)
                        if len(parts) == 2:
                            sender_ip = parts[1]
                            if sender_ip != own_ip:
                                async with self._lock:
                                    ip = addr[0]
                                    self.last_seen[ip] = time.time()
                                    if ip not in self.peer_list:
                                        logging.info(f"Found peer at {ip}")
                                        self.peer_list.append(ip)
                except BlockingIOError:
                    await asyncio.sleep(0.1)
                except Exception as e:
                    logging.exception(f"Error in receive_broadcasts: {e}")
                    await asyncio.sleep(1)
        finally:
            sock.close()

    async def start(self):
        """Start the peer discovery process."""
        await asyncio.gather(
            self.send_broadcasts(),
            self.receive_broadcasts()
        )

# Example usage (if run standalone)
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    discovery = PeerDiscovery()
    asyncio.run(discovery.start())