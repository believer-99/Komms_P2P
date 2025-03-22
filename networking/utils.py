import socket
import asyncio

async def get_own_ip():
    """Get the local IP address."""
    loop = asyncio.get_event_loop()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        await loop.run_in_executor(None, sock.connect, ("8.8.8.8", 80))
        ip = sock.getsockname()[0]
        return ip
    except Exception:
        return "127.0.0.1"
    finally:
        sock.close()