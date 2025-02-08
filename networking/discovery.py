import socket
import struct
import logging

DISCOVERY_PORT = 50001
MULTICAST_GROUP = "224.0.0.1"
DISCOVERY_MESSAGE = "P2P_DISCOVERY"

def receive_broadcasts(peer_list, lock):
    """Receives multicast messages and updates the peer list."""
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_socket.bind(("", DISCOVERY_PORT))

    mreq = struct.pack("4sl", socket.inet_aton(MULTICAST_GROUP), socket.INADDR_ANY)
    server_socket.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)

    while True:
        data, addr = server_socket.recvfrom(1024)
        if data.decode() == DISCOVERY_MESSAGE:
            with lock:
                if addr[0] not in peer_list:
                    logging.info(f"Found peer at {addr[0]}")
                    peer_list.append(addr[0])