import socket
import struct

MULTICAST_GROUP = '224.0.0.1'  # Multicast group address
DISCOVERY_PORT = 50001  # Port for discovery
DISCOVERY_MESSAGE = "P2P_DISCOVERY" # The message sent for discovery

def receive_broadcasts(peer_list):
    """
    Receives multicast discovery messages.

    Args:
        peer_list (list): List to store discovered peer IP addresses.
    """
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    # Bind to all interfaces on the specified port
    server_socket.bind(('0.0.0.0', DISCOVERY_PORT))

    # Join the multicast group.
    group = socket.inet_aton(MULTICAST_GROUP)
    mreq = struct.pack('4sL', group, socket.INADDR_ANY)
    server_socket.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)

    while True:
        data, addr = server_socket.recvfrom(1024)
        if data.decode() == DISCOVERY_MESSAGE:
            if addr[0] not in peer_list:
                print(f"Found peer at {addr[0]}")
                peer_list.append(addr[0])


def send_broadcasts():
    """
    Sends multicast discovery messages.
    """
    client_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)

    try:
        client_socket.sendto(DISCOVERY_MESSAGE.encode(), (MULTICAST_GROUP, DISCOVERY_PORT))
    except Exception as e:
        print(f"Error Sending Multicast Message {e}")

    print(f"Broadcasted Discovery Message to {MULTICAST_GROUP}")
