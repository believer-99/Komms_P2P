import asyncio
import threading
import aioconsole
import logging
import sys

import websockets

from networking.discovery import receive_broadcasts, send_broadcasts
from networking.connection import connect_to_peer

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    stream=sys.stdout,
)

peer_list = []
connections = {}
peer_list_lock = threading.Lock()
message_queue = asyncio.Queue()  # Queue for incoming messages


async def handle_peer_connection(websocket, path):
    """Handles incoming connections from websockets.serve."""
    peer_ip = websocket.remote_address[0]
    logging.info(f"New connection from {peer_ip} (server-initiated)")
    connections[peer_ip] = websocket

    # Start receiving messages from this peer
    asyncio.create_task(receive_peer_messages(websocket, peer_ip))


async def receive_peer_messages(websocket, peer_ip):
    """Receives messages from a peer and puts them in the message queue."""
    try:
        async for message in websocket:
            await message_queue.put((peer_ip, message))  # Enqueue the message
    except websockets.exceptions.ConnectionClosed:
        logging.info(f"Connection closed to {peer_ip}")
        if peer_ip in connections:
            del connections[peer_ip]
    finally:
        logging.info(f"Stopped receiving messages from {peer_ip}")


async def display_messages():
    """Asynchronously displays messages from the queue."""
    while True:
        peer_ip, message = await message_queue.get()  # Get message from queue
        logging.info(f"Message from {peer_ip}: {message}")
        print(f"\n{peer_ip}: {message}")  # Display the message
        await display_peers_and_prompt()  # Re-display prompt
        message_queue.task_done()  # Indicate that the task is complete


async def display_peers_and_prompt():
    print("\nAvailable peers:")
    list_peers()
    print("Enter recipient number (or 'all') and message: ", end="", flush=True)


def list_peers():
    if not connections:
        print("No peers connected")
        return
    for i, peer in enumerate(connections.keys(), 1):
        print(f"{i}. {peer}")


async def connect_to_peers():
    """Actively connects to discovered peers."""
    while True:
        await asyncio.sleep(5)
        with peer_list_lock:
            for peer_ip in peer_list:
                if peer_ip not in connections:
                    try:
                        websocket = await connect_to_peer(peer_ip)
                        if websocket:
                            logging.info(f"Connected to {peer_ip} (client-initiated)")
                            connections[peer_ip] = websocket
                            # Start receiving messages *here*, after the connection
                            asyncio.create_task(
                                receive_peer_messages(websocket, peer_ip)
                            )
                            await display_peers_and_prompt()
                    except Exception as e:
                        logging.error(f"Failed to connect to {peer_ip}: {e}")


async def user_input():
    while True:
        try:
            if not connections:
                print("\rWaiting for peers to connect...", end="", flush=True)
                await asyncio.sleep(1)
                continue

            await display_peers_and_prompt()  # Display prompt *before* input

            user_input_str = await aioconsole.ainput()

            if user_input_str.lower() == "exit":
                break

            parts = user_input_str.split(" ", 1)
            if len(parts) != 2:
                print("Invalid format. Use: <recipient_number/all> <message>")
                continue

            recipient, message = parts
            peer_ips = list(connections.keys())

            if recipient.lower() == "all":
                targets = peer_ips
            else:
                try:
                    idx = int(recipient) - 1
                    if 0 <= idx < len(peer_ips):
                        targets = [peer_ips[idx]]
                    else:
                        print("Invalid peer number")
                        continue
                except ValueError:
                    print("Invalid recipient. Use number or 'all'")
                    continue

            for peer_ip in targets:
                try:
                    await connections[peer_ip].send(message)
                    print(f"Sent to {peer_ip}")
                except Exception as e:
                    logging.error(f"Failed to send to {peer_ip}: {e}")
                    del connections[peer_ip]
                    print(f"Failed to send to {peer_ip}")

        except Exception as e:
            logging.error(f"Error: {e}")


async def main():
    broadcast_thread = threading.Thread(target=send_broadcasts, daemon=True)
    broadcast_thread.start()

    discovery_thread = threading.Thread(
        target=receive_broadcasts, args=(peer_list, peer_list_lock), daemon=True
    )
    discovery_thread.start()

    async with websockets.serve(handle_peer_connection, "0.0.0.0", 8765) as server:
        logging.info("WebSocket server started")
        await asyncio.gather(connect_to_peers(), user_input(), display_messages())



if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nShutting down...")
