import threading
import time
from peer_discovery import receive_broadcasts, send_broadcasts

peer_list = []

if __name__ == "__main__":
    # Start a thread that continuously sends broadcasts
    broadcast_thread = threading.Thread(target=send_broadcasts, daemon=True)
    broadcast_thread.start()
    # Start a thread that continuously receives broadcasts
    discovery_thread = threading.Thread(target=receive_broadcasts, args=(peer_list,), daemon=True)
    discovery_thread.start()
    # Main thread will just output the discovered peers to the console.
    while True:
        time.sleep(5)
        if peer_list:
            print("Discovered peers:", peer_list)
        else:
            print("No peers Discovered yet")