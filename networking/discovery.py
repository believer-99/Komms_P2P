import socket
import struct
import logging
import asyncio

DISCOVERY_PORT = 50001
MULTICAST_GROUP = "224.0.0.1"
DISCOVERY_MESSAGE = "P2P_DISCOVERY"


class PeerDiscovery:
    def __init__(self):
        self.peer_list = []
        self._lock = asyncio.Lock()

    async def receive_broadcasts(self):
        """Receives multicast messages and updates the peer list."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(('', DISCOVERY_PORT))
        mreq = struct.pack("4sl", socket.inet_aton(MULTICAST_GROUP), socket.INADDR_ANY)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        sock.setblocking(False)
        loop = asyncio.get_event_loop()

        try:
            while True:
                try:
                    data, addr = await loop.sock_recvfrom(sock, 1024)
                    if data.decode() == DISCOVERY_MESSAGE and addr[0] != await self._get_own_ip():
                        async with self._lock:
                            if addr[0] not in self.peer_list:
                                logging.info(f"Found peer at {addr[0]}")
                                self.peer_list.append(addr[0])
                except BlockingIOError:
                    await asyncio.sleep(0.1)
                except Exception as e:
                    logging.error(f"Error in receive_broadcasts: {e}")
                    await asyncio.sleep(1)
        finally:
            sock.close()

    async def send_broadcasts(self):
        """Sends a multicast discovery message."""
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
                    logging.error(f"Error sending broadcast: {e}")
                    await asyncio.sleep(1)
        finally:
            sock.close()

    async def _get_own_ip(self):
        """Get the IP address of the current machine."""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))  # Use Google's public DNS server
            return s.getsockname()[0]
        except Exception:
            logging.error("Could not get own IP address, using loopback")
            return "127.0.0.1"

# Usage example
discovery = PeerDiscovery()
# To access use await discovery.peer_list