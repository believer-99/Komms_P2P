import socket
import struct
import logging
import sys

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    stream=sys.stdout,
)

DISCOVERY_PORT = 50001
MULTICAST_GROUP = "224.0.0.1"  # Choose a multicast group
DISCOVERY_MESSAGE = "P2P_DISCOVERY"


def receive_broadcasts(peer_list, lock):
    """Receives multicast messages and updates the peer list"""
    server_socket = socket.socket(
        socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP
    )  # Added IPPROTO_UDP
    server_socket.setsockopt(
        socket.SOL_SOCKET, socket.SO_REUSEADDR, 1
    )  # Allow multiple processes listen to the same port
    try:
        server_socket.bind(("", DISCOVERY_PORT))
    except OSError as e:
        logging.error(f"Failed to bind to port {DISCOVERY_PORT}: {e}")
        return

    # Multicast Options
    mreq = struct.pack("4sl", socket.inet_aton(MULTICAST_GROUP), socket.INADDR_ANY)
    server_socket.setsockopt(
        socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq
    )  # Set the multicast options for sending the message to specific group

    while True:
        try:
            data, addr = server_socket.recvfrom(1024)
            if data.decode() == DISCOVERY_MESSAGE:
                with lock:  # Acquire the lock before modifying peer_list
                    if addr[0] not in peer_list:
                        logging.info(f"Found peer at {addr[0]}")
                        peer_list.append(addr[0])
        except Exception as e:
            logging.error(f"Error receiving broadcast: {e}")


def send_broadcasts():
    """Sends a multicast message"""
    client_socket = socket.socket(
        socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP
    )  # Added IPPROTO_UDP
    client_socket.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)  # Set Time to Live to 2, to keep the broadcast in local network
    try:
        client_socket.sendto(
            DISCOVERY_MESSAGE.encode(), (MULTICAST_GROUP, DISCOVERY_PORT)
        )
    except Exception as e:
        logging.error(f"Error Sending Broadcast {e}")
    logging.info("Broadcasted Discovery Message")
