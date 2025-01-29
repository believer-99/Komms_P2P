import socket
import json

DISCOVERY_PORT = 50001  # Port for discovery
DISCOVERY_MESSAGE = "P2P_DISCOVERY" # The message sent for discovery
# Function for receiving broadcasts
def receive_broadcasts(peer_list):
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    server_socket.bind(('0.0.0.0', DISCOVERY_PORT))
    while True:
        data, addr = server_socket.recvfrom(1024)
        if data.decode() == DISCOVERY_MESSAGE:
          if addr[0] not in peer_list:
           print(f"Found peer at {addr[0]}")
           peer_list.append(addr[0])


# Function for sending broadcasts
def send_broadcasts():
    client_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    client_socket.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    broadcast_address = ('255.255.255.255', DISCOVERY_PORT) # Send message to all the machines in the local network
    try:
         client_socket.sendto(DISCOVERY_MESSAGE.encode(), broadcast_address)
    except Exception as e:
          print(f"Error Sending Broadcast {e}")
    print("Broadcasted Discovery Message")