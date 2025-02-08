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
message_queue = asyncio.Queue()

async def handle_peer_connection(websocket, path=None):
    """Handles incoming connections from websockets.serve."""
    peer_ip = websocket.remote_address[0]
    logging.info(f"New connection from {peer_ip} (server-initiated)")
    connections[peer_ip] = websocket

    # Start receiving messages from this peer
    await receive_peer_messages(websocket, peer_ip)

async def receive_peer_messages(websocket, peer_ip):
    """Receives messages from a peer and puts them in the message queue."""
    try:
        async for message in websocket:
            await message_queue.put((peer_ip, message))
    except websockets.exceptions.ConnectionClosed:
        logging.info(f"Connection closed to {peer_ip}")
        if peer_ip in connections:
            del connections[peer_ip]
    finally:
        logging.info(f"Stopped receiving messages from {peer_ip}")

async def display_messages():
    """Asynchronously displays messages from the queue."""
    while True:
        try:
            peer_ip, message = await message_queue.get()
            logging.info(f"Message from {peer_ip}: {message}")
            print(f"\n{peer_ip}: {message}")
            await display_peers_and_prompt()
            message_queue.task_done()
        except Exception as e:
            logging.error(f"Error displaying message: {e}")
            await asyncio.sleep(1)

async def display_peers_and_prompt():
    """Display available peers and input prompt."""
    print("\nAvailable peers:")
    list_peers()
    print("Enter recipient number (or 'all') and message: ", end="", flush=True)

def list_peers():
    """List all connected peers."""
    if not connections:
        print("No peers connected")
        return
    for i, peer in enumerate(connections.keys(), 1):
        print(f"{i}. {peer}")

async def connect_to_peers():
    """Actively connects to discovered peers."""
    while True:
        try:
            # Create a copy of peer_list while holding the lock
            with peer_list_lock:
                current_peers = peer_list.copy()
            
            # Process peers without holding the lock
            for peer_ip in current_peers:
                if peer_ip not in connections:
                    try:
                        websocket = await connect_to_peer(peer_ip)
                        if websocket:
                            logging.info(f"Connected to {peer_ip} (client-initiated)")
                            connections[peer_ip] = websocket
                            asyncio.create_task(receive_peer_messages(websocket, peer_ip))
                            await display_peers_and_prompt()
                    except Exception as e:
                        logging.error(f"Failed to connect to {peer_ip}: {e}")
            
            await asyncio.sleep(5)
        except Exception as e:
            logging.error(f"Error in connect_to_peers: {e}")
            await asyncio.sleep(1)

async def user_input():
    """Handle user input for sending messages."""
    while True:
        try:
            if not connections:
                print("\rWaiting for peers to connect...", end="", flush=True)
                await asyncio.sleep(1)
                continue

            await display_peers_and_prompt()
            user_input_str = await aioconsole.ainput()

            if not user_input_str:
                continue

            if user_input_str.lower() == "exit":
                return

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
                    if peer_ip in connections:
                        del connections[peer_ip]
                    print(f"Failed to send to {peer_ip}")

        except Exception as e:
            logging.error(f"Error in user input: {e}")
            await asyncio.sleep(1)

async def main():
    """Main function to run the P2P chat application."""
    # Start broadcast threads
    broadcast_thread = threading.Thread(target=send_broadcasts, daemon=True)
    broadcast_thread.start()

    discovery_thread = threading.Thread(
        target=receive_broadcasts, args=(peer_list, peer_list_lock), daemon=True
    )
    discovery_thread.start()

    # Start WebSocket server and main tasks
    server = await websockets.serve(handle_peer_connection, "0.0.0.0", 8765)
    logging.info("WebSocket server started")

    try:
        # Create and gather all tasks
        tasks = [
            asyncio.create_task(connect_to_peers()),
            asyncio.create_task(user_input()),
            asyncio.create_task(display_messages())
        ]
        
        # Wait for all tasks to complete
        await asyncio.gather(*tasks)
    finally:
        server.close()
        await server.wait_closed()

if __name__ == "__main__":  # Fixed typo here
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nShutting down...")
    except Exception as e:
        logging.error(f"Fatal error: {e}")
