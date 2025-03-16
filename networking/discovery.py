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
        self.peer_list = []
        self._lock = asyncio.Lock()
        self.last_seen = {}  

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

    async def cleanup_stale_peers(self):
        """Remove peers not seen in 30 seconds"""
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
                    logging.exception(f"Error sending broadcast: {e}")
                    await asyncio.sleep(1)
        finally:
            sock.close()

    async def _get_own_ip(self):
        """Get the most appropriate IP address"""
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
