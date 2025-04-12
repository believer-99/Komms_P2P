import asyncio
import logging
import sys
import websockets


try:
    from networking.messaging import (
        handle_incoming_connection,
        receive_peer_messages,
        connections
    )
    
    NETWORKING_AVAILABLE_MAIN = True
except ImportError:
    NETWORKING_AVAILABLE_MAIN = False
    async def handle_incoming_connection(*args): return False
    async def receive_peer_messages(*args): pass
    connections = {}


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout
)

async def handle_peer_connection(websocket, path=None):
    """Handle incoming WebSocket connections (as used by GUI)."""
    if not NETWORKING_AVAILABLE_MAIN:
        logging.error("handle_peer_connection called but networking modules failed to import.")
        await websocket.close(code=1011, reason="Server internal error (module import)")
        return

    peer_ip = websocket.remote_address[0]
    logging.info(f"New connection from {peer_ip}")
    try:
        if await handle_incoming_connection(websocket, peer_ip):
            await receive_peer_messages(websocket, peer_ip)
    except websockets.exceptions.ConnectionClosedOK:
         logging.info(f"Connection closed normally by {peer_ip}")
    except websockets.exceptions.ConnectionClosedError as e:
         logging.warning(f"Connection closed abruptly by {peer_ip}: {e}")
    except Exception as e:
        logging.exception(f"Error during active connection with {peer_ip}: {e}")
    finally:
        if peer_ip in connections:
            logging.debug(f"Removing connection state for {peer_ip} in handle_peer_connection finally block.")
            del connections[peer_ip]
            



async def cli_main():
   
    from networking.discovery import PeerDiscovery
    from networking.messaging import (
         user_input, display_messages, maintain_peer_list, initialize_user_config
    )
    from networking.file_transfer import update_transfer_progress
    from networking.shared_state import peer_usernames, peer_public_keys, shutdown_event

    await initialize_user_config() 

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
         for task in tasks:
             if not task.done(): task.cancel()
         try:
             await asyncio.wait(tasks, timeout=3.0)
         except asyncio.TimeoutError:
             logger.warning("CLI tasks did not finish gracefully within timeout.")

         if server:
             server.close()
             await server.wait_closed()
             logging.info("CLI WebSocket server closed.")

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
    try:
        asyncio.run(cli_main())
    except KeyboardInterrupt:
        print("\nCLI Shutting down...")
    except Exception as e:
         print(f"\nCLI unexpected error: {e}")
         sys.exit(1)
