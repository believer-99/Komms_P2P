import socket
import netifaces
import logging

async def get_own_ip():
    try:
        for interface in netifaces.interfaces():
            try:
                addrs = netifaces.ifaddresses(interface)
                if netifaces.AF_INET in addrs:
                    ip = addrs[netifaces.AF_INET][0]["addr"]
                    if not (ip.startswith("127.") or ip.startswith("169.254.")):
                        return ip
            except ValueError:
                continue
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        except Exception:
            return "127.0.0.1"
    except Exception as e:
        logging.error(f"IP detection failed: {e}")
        return "127.0.0.1"