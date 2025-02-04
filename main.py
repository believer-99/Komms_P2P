import asyncio
import threading
from networking.discovery import receive_broadcasts, send_broadcasts
from networking.connection import connect_to_peer, send_message, receive_message
import websockets
import time

peer_list = []
connections = {}

async def handle_peer_connection(websocket, path):
    peer_ip = websocket.remote_address[0]
    print(f"\nNew connection from {peer_ip}")
    connections[peer_ip] = websocket
    
    try:
        async for message in websocket:
            print(f"\nMessage from {peer_ip}: {message}")
            print("\nAvailable peers:")
            list_peers()
            print("Enter recipient number (or 'all') and message: ", end='', flush=True)
    except websockets.exceptions.ConnectionClosed:
        if peer_ip in connections:
            del connections[peer_ip]
            print(f"\nLost connection to {peer_ip}")

def list_peers():
    if not connections:
        print("No peers connected")
        return
    for i, peer in enumerate(connections.keys(), 1):
        print(f"{i}. {peer}")

async def connect_to_peers():
    while True:
        await asyncio.sleep(5)
        for peer_ip in peer_list:
            if peer_ip not in connections:
                try:
                    websocket = await connect_to_peer(peer_ip)
                    if websocket:
                        connections[peer_ip] = websocket
                except Exception as e:
                    print(f"Failed to connect to {peer_ip}: {e}")

async def user_input():
    # Wait for initial peer discovery
    while not connections:
        print("\rWaiting for peers to connect...", end='', flush=True)
        await asyncio.sleep(1)
    
    print("\nPeers found! Starting chat...")
    
    while True:
        try:
            if not connections:
                print("\rWaiting for peers to connect...", end='', flush=True)
                await asyncio.sleep(1)
                continue
                
            print("\nAvailable peers:")
            list_peers()
            print("Enter recipient number (or 'all') and message: ", end='', flush=True)
            
            user_input = await asyncio.get_event_loop().run_in_executor(None, input, '')
            if user_input.lower() == 'exit':
                break
                
            parts = user_input.split(' ', 1)
            if len(parts) != 2:
                print("Invalid format. Use: <recipient_number/all> <message>")
                continue
                
            recipient, message = parts
            peer_ips = list(connections.keys())
            
            if recipient.lower() == 'all':
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
                except Exception:
                    del connections[peer_ip]
                    print(f"Failed to send to {peer_ip}")
                    
        except Exception as e:
            print(f"Error: {e}")

async def main():
    broadcast_thread = threading.Thread(target=send_broadcasts, daemon=True)
    broadcast_thread.start()
    
    discovery_thread = threading.Thread(target=receive_broadcasts, args=(peer_list,), daemon=True)
    discovery_thread.start()
    
    server = await websockets.serve(handle_peer_connection, "0.0.0.0", 8765)
    
    await asyncio.gather(
        connect_to_peers(),
        user_input(),
    )

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nShutting down...")