import socket
import asyncio

async def get_own_ip():
    """Get the local IP address of this machine."""
    loop = asyncio.get_event_loop()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # Connect to an external address (doesn't send data) to determine local IP
        await loop.run_in_executor(None, sock.connect, ("8.8.8.8", 80))
        ip = sock.getsockname()[0]
    except Exception:
        ip = "127.0.0.1"  # Fallback to localhost
    finally:
        sock.close()
    return ip