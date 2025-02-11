import socket
import struct
import logging
import asyncio

DISCOVERY_PORT = 50001
MULTICAST_GROUP = "224.0.0.1"
DISCOVERY_MESSAGE = "P2P_DISCOVERY"

async def receive_broadcasts(peer_list):
    """Receives multicast messages and updates the peer list."""
    loop = asyncio.get_event_loop()
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_socket.bind(("", DISCOVERY_PORT))

    mreq = struct.pack("4sl", socket.inet_aton(MULTICAST_GROUP), socket.INADDR_ANY)
    server_socket.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)

    server_socket.setblocking(False) # Set the socket to non-blocking
    try:
        while True:
            try:
                data, addr = await loop.sock_recvfrom(server_socket, 1024) # await reading a message
                if data.decode() == DISCOVERY_MESSAGE and addr[0] != await get_own_ip():
                    if addr[0] not in peer_list:
                        logging.info(f"Found peer at {addr[0]}")
                        peer_list.append(addr[0])
            except BlockingIOError:
                await asyncio.sleep(0.1)  # Wait a bit if no data is ready
            except Exception as e:
                logging.error(f"Error in receive_broadcasts: {e}")
                await asyncio.sleep(1)
    finally:
        server_socket.close()

async def send_broadcasts():
    """Sends a multicast discovery message."""
    loop = asyncio.get_event_loop()
    client_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    client_socket.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    client_socket.setblocking(False)
    try:
        while True:
            try:
                await loop.sock_sendto(client_socket, DISCOVERY_MESSAGE.encode(), (MULTICAST_GROUP, DISCOVERY_PORT)) # await sending
                logging.info("Broadcasted Discovery Message")
                await asyncio.sleep(5)
            except BlockingIOError:
                await asyncio.sleep(0.1)
            except Exception as e:
                logging.error(f"Error sending broadcast: {e}")
                await asyncio.sleep(1)
    finally:
        client_socket.close()

async def get_own_ip():
    """Get the IP address of the current machine."""
    try:
        # Create a temporary socket to get the local IP
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception: # Catch a more specific exception like OSError
        logging.error("Could not get own IP address.")
        return "127.0.0.1"