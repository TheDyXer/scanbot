"""
Pings Minecraft servers.

bot.py calls ping() itself when there's no VPN. With the VPN, this file also runs on its own
inside the VPN container, so the pings are the only thing that leaves through the tunnel:

    python pinger.py      # listens on PINGER_PORT (default 8765)

It only ever gets IP addresses: the bot looks names up (through Quad9) and checks them first.
"""
import ipaddress
import logging
import os

from aiohttp import web
from mcstatus import BedrockServer, JavaServer

PINGER_PORT = 8765
EDITIONS = ('java', 'bedrock')
MAX_TIMEOUT = 10  # Seconds; the bot asks for its DIRECT_TIMEOUT
BACKLOG = 1024    # Connections waiting to be accepted: the bot opens up to DIRECT_CONCURRENCY_TOTAL at once
OPEN_FILES = 65536  # Open-file limit the pinger asks for: every ping in flight holds two sockets

log = logging.getLogger('scanbot.pinger')


def is_public_ip(ip):
    return ip.is_global and not ip.is_multicast


def raise_file_limit(wanted):
    """
    Raises this process's open-file limit to `wanted`, or as far as the hard limit allows. Every ping in flight
    holds a socket. Returns (limit now, hard limit), or None where there is no such limit (Windows).
    """
    try:
        import resource
    except ImportError:
        return None
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    target = wanted if hard == resource.RLIM_INFINITY else min(wanted, hard)
    if soft != resource.RLIM_INFINITY and target > soft:
        try:
            resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
            soft = target
        except (ValueError, OSError):
            pass
    return soft, hard


async def ping(ip, port, edition, timeout):
    """
    Pings ip:port once. Returns the server's status as a plain dict, or None if it doesn't answer.
    The optional fields are read with getattr, so a field an mcstatus version doesn't have is None
    instead of making every server look offline.
    """
    try:
        if edition == 'java':
            status = await JavaServer(ip, port, timeout=timeout).async_status(tries=1)
            names = [p.name for p in (status.players.sample or []) if p.name]
            mods = getattr(getattr(status, 'forge_data', None), 'mods', None)  # Forge servers list their mods
            extra = {"secure_chat": getattr(status, 'enforces_secure_chat', None),
                     "modded": getattr(status, 'is_modded', None),
                     "mod_count": len(mods) if isinstance(mods, (list, tuple)) else None}
        else:
            status = await BedrockServer(ip, port, timeout=timeout).async_status(tries=1)
            names = []
            extra = {"gamemode": getattr(status, 'gamemode', None), "map": getattr(status, 'map_name', None),
                     "brand": getattr(status.version, 'brand', None)}
        latency = getattr(status, 'latency', None)
        return {"players": status.players.online, "max": status.players.max, "names": names,
                "version": status.version.name, "motd": status.motd.to_plain(),
                "latency": round(latency, 1) if isinstance(latency, (int, float)) else None,
                "protocol": getattr(status.version, 'protocol', None), **extra}
    except Exception:
        return None


def parse_request(data):
    """
    Checks a /ping request and returns (ip, port, edition, timeout). Raises ValueError otherwise.
    Checked no matter who sent it: only public addresses are ever pinged.
    """
    if not isinstance(data, dict):
        raise ValueError("expected a JSON object")
    edition = data.get('edition')
    if edition not in EDITIONS:
        raise ValueError("edition must be java or bedrock")
    ip = ipaddress.ip_address(str(data.get('ip')))
    if not is_public_ip(ip):
        raise ValueError("only public addresses are pinged")
    port = data.get('port')
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("port must be 1-65535")
    timeout = data.get('timeout', 3)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= MAX_TIMEOUT:
        raise ValueError(f"timeout must be between 0 and {MAX_TIMEOUT} seconds")
    return str(ip), port, edition, float(timeout)


async def handle_ping(request):
    try:
        ip, port, edition, timeout = parse_request(await request.json())
    except Exception as e:
        return web.json_response({"error": str(e)}, status=400)
    status = await ping(ip, port, edition, timeout)
    return web.json_response({"online": True, **status} if status else {"online": False})


async def handle_health(request):
    return web.Response(text="ok")


def make_app():
    app = web.Application()
    app.add_routes([web.post('/ping', handle_ping), web.get('/health', handle_health)])
    return app


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)-8s %(name)s %(message)s')
    port = int(os.environ.get('PINGER_PORT', PINGER_PORT))
    limits = raise_file_limit(OPEN_FILES)
    log.info("Pinger listening on :%d (pings leave through the VPN), %s files can be open at once", port,
             limits[0] if limits else "any number of")
    # No access log: a scan sends thousands of requests
    web.run_app(make_app(), port=port, print=None, access_log=None, backlog=BACKLOG)
