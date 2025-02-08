import socket
import struct
import logging
import time
import threading

DISCOVERY_PORT = 50001
MULTICAST_GROUP = "224.0.0.1"
DISCOVERY_MESSAGE = "P2P_DISCOVERY"

# Create a shared lock for thread safety
discovery_lock = threading.Lock()

def receive_broadcasts(peer_list, lock):
    """Receives multicast messages and updates the peer list."""
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_socket.bind(("", DISCOVERY_PORT))

    mreq = struct.pack("4sl", socket.inet_aton(MULTICAST_GROUP), socket.INADDR_ANY)
    server_socket.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)

    while True:
        try:
            data, addr = server_socket.recvfrom(1024)
            if data.decode() == DISCOVERY_MESSAGE and addr[0] != get_own_ip():
                with lock:
                    if addr[0] not in peer_list:
                        logging.info(f"Found peer at {addr[0]}")
                        peer_list.append(addr[0])
        except Exception as e:
            logging.error(f"Error in receive_broadcasts: {e}")
            time.sleep(1)

def send_broadcasts():
    """Sends a multicast discovery message."""
    client_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    client_socket.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)

    while True:
        try:
            client_socket.sendto(DISCOVERY_MESSAGE.encode(), (MULTICAST_GROUP, DISCOVERY_PORT))
            logging.info("Broadcasted Discovery Message")
            time.sleep(5)
        except Exception as e:
            logging.error(f"Error sending broadcast: {e}")
            time.sleep(1)

def get_own_ip():
    """Get the IP address of the current machine."""
    try:
        # Create a temporary socket to get the local IP
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except:
        return "127.0.0.1"
