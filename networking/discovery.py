import asyncio
import socket
import json
import logging
import netifaces
import time 

from networking.utils import get_own_ip
from networking.shared_state import user_data, shutdown_event, message_queue

logger = logging.getLogger(__name__) 

class DiscoveryProtocol(asyncio.DatagramProtocol):
    """Protocol for handling incoming discovery UDP datagrams."""
    def __init__(self, discovery_instance):
        self.discovery = discovery_instance
        self.transport = None
        self.loop = asyncio.get_running_loop()

    def connection_made(self, transport):
        self.transport = transport; logger.debug("Discovery receive protocol started.")
    def datagram_received(self, data, addr):
        sender_ip = addr[0]
        asyncio.create_task(self.discovery.handle_received_datagram(data, sender_ip))
    def error_received(self, exc): logger.error(f"Discovery protocol error: {exc}")
    def connection_lost(self, exc): logger.debug(f"Discovery protocol connection lost: {exc}")

class PeerDiscovery:
    def __init__(self, broadcast_interval=5, cleanup_interval=60):
        self.broadcast_port = 37020
        self.peer_list = {}
        self.broadcast_interval = broadcast_interval
        self.cleanup_interval = cleanup_interval
        self.running = True
        self.own_ip = None
        self.receive_transport = None
        self._broadcast_sock = None
        self._last_peer_list_state_json = "null" # To check if list actually changed

    async def _initialize_resources(self):
        """Initialize network resources."""
        try:
            self.own_ip = await get_own_ip()
            self._broadcast_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._broadcast_sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            self._broadcast_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            logger.debug("Broadcast sending socket created.")
        except Exception as e:
             logger.exception("Failed to initialize discovery resources")
             self.running = False # Can't run without resources

    async def _check_and_notify_peer_update(self):
        """Checks if peer list changed and puts update on queue."""
        try:
            current_state_json = json.dumps(self.peer_list, sort_keys=True)
            if current_state_json != self._last_peer_list_state_json:
                logger.debug("Peer list changed, putting update on queue.")
                await message_queue.put({"type": "peer_update"})
                self._last_peer_list_state_json = current_state_json
        except Exception as e:
             logger.error(f"Error checking/notifying peer update: {e}")

    async def send_broadcasts(self):
        """Periodically sends broadcast messages."""
        if not self._broadcast_sock: await self._initialize_resources()
        if not self.running: return 

        sock = self._broadcast_sock
        sent_at_least_once = False
        try:
            while self.running and not shutdown_event.is_set():
                sent_this_round = False
                try:
                    if self.own_ip is None: self.own_ip = await get_own_ip() 
                    username = user_data.get("original_username", "unknown")
                    message = json.dumps({"ip": self.own_ip, "username": username}).encode()
                    for interface in netifaces.interfaces():
                        if interface == 'lo' or not (netifaces.ifaddresses(interface).get(netifaces.AF_INET)): continue
                        try:
                            addrs = netifaces.ifaddresses(interface).get(netifaces.AF_INET)
                            if addrs:
                                broadcast_addr = addrs[0].get("broadcast")
                                if broadcast_addr:
                                    sock.sendto(message, (broadcast_addr, self.broadcast_port))
                                    logger.debug(f"Broadcast sent on {interface} to {broadcast_addr}:{self.broadcast_port}")
                                    sent_this_round = True; sent_at_least_once = True
                        except OSError as e:
                             if e.errno in [101, 99]: logger.debug(f"Broadcast OS error on {interface}: {e.strerror}")
                             else: logger.warning(f"Network error broadcasting on {interface}: {e}")
                        except KeyError: logger.debug(f"No broadcast address for {interface}")
                        except Exception as e: logger.warning(f"Unexpected error broadcasting on {interface}: {e}")
                except Exception as outer_e: logger.error(f"Error preparing broadcast message: {outer_e}")
                if not sent_this_round and sent_at_least_once: logger.warning("Could not send broadcast on any interface this round.")
                elif not sent_at_least_once and not sent_this_round: logger.warning("Could not send initial broadcast on any interface.")

                try: await asyncio.wait_for(shutdown_event.wait(), timeout=self.broadcast_interval)
                except asyncio.TimeoutError: continue
                except asyncio.CancelledError: break
                break # Shutdown triggered

        except asyncio.CancelledError: logger.info("send_broadcasts task cancelled.")
        except Exception as e: logger.exception(f"Fatal error in send_broadcasts loop: {e}")
        finally: logger.info("send_broadcasts loop finished.") # Socket closed in stop()

    async def receive_broadcasts(self):
        """Listens for broadcast messages using asyncio DatagramProtocol."""
        loop = asyncio.get_running_loop()
        if self.own_ip is None: await self._initialize_resources()
        if not self.running: return

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try: sock.bind(("", self.broadcast_port)); sock.setblocking(False); logger.info(f"Discovery service bound to UDP port {self.broadcast_port}")
        except OSError as e: logger.critical(f"Could not bind discovery receive socket port {self.broadcast_port}: {e}"); self.running = False; sock.close(); return

        transport = None
        try:
            transport, protocol = await loop.create_datagram_endpoint(lambda: DiscoveryProtocol(self), sock=sock)
            self.receive_transport = transport; logger.info(f"Discovery listener started on {sock.getsockname()}")
            await shutdown_event.wait()
        except asyncio.CancelledError: logger.info("receive_broadcasts task cancelled.")
        except Exception as e: logger.exception(f"Error creating/running datagram endpoint: {e}"); self.running = False
        finally:
            if transport: transport.close()
            elif sock.fileno() != -1: sock.close() # Ensure socket is closed
            self.receive_transport = None; logger.info("receive_broadcasts stopped.")

    async def handle_received_datagram(self, data, sender_ip):
        """Processes a received UDP datagram."""
        if not self.running or shutdown_event.is_set() or sender_ip == self.own_ip: return
        try:
            message = json.loads(data.decode())
            peer_ip = message["ip"]; username = message["username"]
            if peer_ip not in self.peer_list or self.peer_list[peer_ip][0] != username:
                logger.info(f"Discovered/Updated peer: {username} at {peer_ip}")
                self.peer_list[peer_ip] = (username, time.time()) 
                await self._check_and_notify_peer_update() 
            else:
                self.peer_list[peer_ip] = (username, time.time())

        except (json.JSONDecodeError, KeyError, UnicodeDecodeError) as e:
            logger.warning(f"Invalid/Malformed broadcast from {sender_ip}: {e}")
        except Exception as e: logger.exception(f"Error processing datagram from {sender_ip}: {e}")

    async def cleanup_stale_peers(self):
        """Removes peers that haven't been heard from recently."""
        try:
            while not shutdown_event.is_set():
                stale_found = False
                try:
                    current_time = time.time()
                    stale_peers = [ip for ip, (_, last) in self.peer_list.items() if current_time - last > self.cleanup_interval]
                    for peer_ip in stale_peers:
                        if peer_ip in self.peer_list:
                            removed_username = self.peer_list.pop(peer_ip)[0] 
                            logger.info(f"Removed stale peer: {removed_username} ({peer_ip})")
                            stale_found = True
                    if stale_found: await self._check_and_notify_peer_update() 
                except Exception as e: logger.exception(f"Error during stale peer cleanup: {e}")
                try: await asyncio.wait_for(shutdown_event.wait(), timeout=self.cleanup_interval / 2) 
                except asyncio.TimeoutError: continue
                break # Shutdown triggered
        except asyncio.CancelledError: logger.info("cleanup_stale_peers task cancelled.")
        finally: logger.info("cleanup_stale_peers stopped.")

    def stop(self):
        """Stops the discovery service."""
        logger.info("Stopping PeerDiscovery...")
        self.running = False
        if self._broadcast_sock:
            self._broadcast_sock.close(); self._broadcast_sock = None; logger.debug("Broadcast sending socket closed.")
