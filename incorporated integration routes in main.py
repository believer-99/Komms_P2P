# main.py
import asyncio
import logging
import sys
import websockets

# --- Imports needed specifically for handle_peer_connection ---
# Adjust these imports based on where the functions are *actually* defined
# (assuming they are now in networking.messaging)
try:
    from networking.messaging import (
        handle_incoming_connection,
        receive_peer_messages,
        connections
    )
    # Import any other necessary components used ONLY by handle_peer_connection
    # from networking.shared_state import ... # If needed
    NETWORKING_AVAILABLE_MAIN = True
except ImportError:
    NETWORKING_AVAILABLE_MAIN = False
    # Define dummy versions if needed for basic script execution, though GUI won't use them
    async def handle_incoming_connection(*args): return False
    async def receive_peer_messages(*args): pass
    connections = {}


# Configure logging (can be basic here, GUI configures its own logger)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout
)

# --- Keep ONLY the function imported by the GUI ---
async def handle_peer_connection(websocket, path=None):
    """Handle incoming WebSocket connections (as used by GUI)."""
    if not NETWORKING_AVAILABLE_MAIN:
        logging.error("handle_peer_connection called but networking modules failed to import.")
        await websocket.close(code=1011, reason="Server internal error (module import)")
        return

    peer_ip = websocket.remote_address[0]
    logging.info(f"New connection from {peer_ip}")
    try:
        # handle_incoming_connection now performs the handshake and approval
        if await handle_incoming_connection(websocket, peer_ip):
            # If connection approved and finalized, start receiving messages
            await receive_peer_messages(websocket, peer_ip)
        # Else: handle_incoming_connection closed the socket or returned False
    except websockets.exceptions.ConnectionClosedOK:
         logging.info(f"Connection closed normally by {peer_ip}")
    except websockets.exceptions.ConnectionClosedError as e:
         logging.warning(f"Connection closed abruptly by {peer_ip}: {e}")
    except Exception as e:
        # Log errors during the post-handshake phase
        logging.exception(f"Error during active connection with {peer_ip}: {e}")
    finally:
        # Ensure cleanup in shared state happens
        if peer_ip in connections:
            logging.debug(f"Removing connection state for {peer_ip} in handle_peer_connection finally block.")
            del connections[peer_ip]
            # TODO: Ensure peer_public_keys, peer_usernames etc. are cleaned up robustly
            # This might be better handled within receive_peer_messages's finally block
            # or maintain_peer_list. Avoid duplicate cleanup.


# --- The rest of the original main.py (CLI version) should be removed or ---
# --- guarded by `if __name__ == "__main__":` if you want to keep it runnable ---

# Example: Keep CLI part separate
async def cli_main():
    # ... (Your original async def main() content for CLI) ...
    # Make sure it imports everything it needs locally
    from networking.discovery import PeerDiscovery
    from networking.messaging import (
         user_input, display_messages, maintain_peer_list, initialize_user_config
         # Note: messaging now contains core, utils, commands, groups logic
    )
    from networking.file_transfer import update_transfer_progress
    from networking.shared_state import peer_usernames, peer_public_keys, shutdown_event

    await initialize_user_config() # CLI version still needs this

    discovery = PeerDiscovery()
    tasks = [
         asyncio.create_task(discovery.send_broadcasts(), name="CliDiscoverySend"),
         asyncio.create_task(discovery.receive_broadcasts(), name="CliDiscoveryRecv"),
         asyncio.create_task(discovery.cleanup_stale_peers(), name="CliDiscoveryCleanup"),
         asyncio.create_task(update_transfer_progress(), name="CliTransferProgress"),
         asyncio.create_task(maintain_peer_list(discovery), name="CliMaintainPeers"),
         asyncio.create_task(user_input(discovery), name="CliUserInput"), # CLI specific
         asyncio.create_task(display_messages(), name="CliDisplayMessages"), # CLI specific
    ]
    server = await websockets.serve(
         handle_peer_connection, "0.0.0.0", 8765, ping_interval=None, max_size=10*1024*1024 # Or None
    )
    logging.info("CLI WebSocket server started")
    try:
         await asyncio.gather(*tasks)
    except (KeyboardInterrupt, asyncio.CancelledError):
         logging.info("CLI shutdown triggered...")
         shutdown_event.set()
    finally:
         logging.info("CLI initiating shutdown process...")
         # --- Add robust CLI shutdown sequence here (cancel tasks, close server, cleanup) ---
         for task in tasks:
             if not task.done(): task.cancel()
         # Use asyncio.wait or gather with return_exceptions=True
         try:
             await asyncio.wait(tasks, timeout=3.0)
         except asyncio.TimeoutError:
             logger.warning("CLI tasks did not finish gracefully within timeout.")

         if server:
             server.close()
             await server.wait_closed()
             logging.info("CLI WebSocket server closed.")

         # Close connections, clear state (similar to GUI shutdown but maybe less critical)
         for ws in list(connections.values()):
              if ws.open: await ws.close()
         connections.clear()
         peer_public_keys.clear()
         peer_usernames.clear()
         if 'peer_device_ids' in globals(): peer_device_ids.clear()
         if 'active_transfers' in globals(): active_transfers.clear()
         # ... clear other state ...

         discovery.stop()
         logging.info("CLI Application fully shut down.")


if __name__ == "__main__":
    # This block only runs when main.py is executed directly (CLI mode)
    try:
        asyncio.run(cli_main())
    except KeyboardInterrupt:
        print("\nCLI Shutting down...")
    except Exception as e:
         print(f"\nCLI unexpected error: {e}")
         # Optional: Add more detailed traceback logging here
         sys.exit(1)
