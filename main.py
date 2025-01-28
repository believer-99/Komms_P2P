import threading
import time
import asyncio
from networking.discovery import receive_broadcasts, send_broadcasts
from networking.connection import connect_to_peer, send_message, receive_message

peer_list = []
connections = {}

async def handle_peer_connections():
    """
    Establishes connections with the discovered peers and starts monitoring them
    """
    while True:
        await asyncio.sleep(5) # do not use time.sleep() inside async method
        for peer_ip in peer_list:
          if peer_ip not in connections or not connections[peer_ip]:
            print(f"Trying to connect to {peer_ip}")
            connection = await connect_to_peer(peer_ip)
            if connection:
                connections[peer_ip] = connection

async def send_and_receive_test():
  """
  Tries to send and receive test messages
  """
  while True:
    await asyncio.sleep(10) # do not use time.sleep() inside async method
    for peer_ip, connection in connections.items():
      if connection:
        print(f"Sending test message to {peer_ip}")
        sent = await send_message(connection, "Test Message")
        if sent:
          response = await receive_message(connection)
          if response:
            print(f"Received response from {peer_ip}: {response}")


if __name__ == "__main__":
    # Start a thread that continuously sends broadcasts
    broadcast_thread = threading.Thread(target=send_broadcasts, daemon=True)
    broadcast_thread.start()
    # Start a thread that continuously receives broadcasts
    discovery_thread = threading.Thread(target=receive_broadcasts, args=(peer_list,), daemon=True)
    discovery_thread.start()

    # Start Async event loop for handling connections
    async def main():
      await asyncio.gather(handle_peer_connections(), send_and_receive_test())

    asyncio.run(main())