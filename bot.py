import discord
from discord import app_commands
from discord.ext import commands
from mcstatus import BedrockServer, JavaServer
import aiohttp
from aiohttp.abc import AbstractResolver
import asyncio
import csv
import datetime
import dns.asyncresolver
import dns.exception
import dns.nameserver
import dns.query
import dns.resolver
import gzip
import io
import ipaddress
import logging
import maxminddb
import os
import re
import socket
import sys
import time
from typing import Literal, Optional

import pinger
import vpn_switch
from pinger import is_public_ip

# --- CONFIGURATION ---
MC_API_URLS = {
    'java': 'https://api.mcstatus.io/v2/status/java/',
    'bedrock': 'https://api.mcstatus.io/v2/status/bedrock/',
}
EDITION_LABELS = {'java': 'Java', 'bedrock': 'Bedrock'}
BEDROCK_PORT = 19132      # Default port for Bedrock servers (Java's 25565 is handled by mcstatus)
GEO_BATCH_URL = 'http://ip-api.com/batch' # Fallback for IPs the offline database doesn't know
# Offline country database (DB-IP Lite, CC BY 4.0), next to bot.py unless GEO_DB_PATH is set
GEO_DB_PATH = os.environ.get('GEO_DB_PATH') or os.path.join(os.path.dirname(os.path.abspath(__file__)), 'dbip-country-lite.mmdb')
GEO_DB_URL = 'https://download.db-ip.com/free/dbip-country-lite-{month}.mmdb.gz'
USER_AGENT = 'scanbot (+https://github.com/TheDyXer/scanbot)'  # DB-IP rejects Python's default one
GEO_DB_MAX_AGE_DAYS = 40  # DB-IP publishes a new database every month
MAX_IPS_PER_SCAN = 5000
MAX_FILE_BYTES = 1_000_000  # Largest list file the bot reads (5,000 lines are about 100 KB)
MAX_CONCURRENT_SCANS = 5  # Scans running at the same time (one per user); more wait in a queue
DIRECT_CONCURRENCY = 50   # Direct pings running at the same time, per scan
DIRECT_TIMEOUT = 3        # Seconds to wait for a server to answer a direct ping
API_DELAY = 0.2           # mcstatus.io allows 5 requests/second per client IP, shared by all scans
GEO_DELAY = 4             # ip-api.com batch allows 15 requests/minute, shared by all scans
# Pinged at startup to see if direct pings work from this network: one answer is enough. Direct pings
# connect to the server's IP, so these must answer that way (Hypixel, for one, routes by hostname).
PROBE_SERVERS = ('demo.mcstatus.io', 'play.cubecraft.net', 'play.wynncraft.com')
BEDROCK_PROBE_SERVERS = ('demo.mcstatus.io', 'play.cubecraft.net', 'geo.hivebedrock.network')  # Same, over UDP
PROGRESS_INTERVAL = 3     # Seconds between progress message updates
INLINE_LIMIT = 1900       # Results longer than this are sent as files
# With the VPN, pings are sent by the pinger inside the VPN container; everything else uses this
# machine's connection. Set by docker-compose.vpn.yml; empty means the bot pings servers itself.
PINGER_URL = os.environ.get('PINGER_URL', '').strip().rstrip('/')
VPN_CHECK_DOWN = 60       # With the VPN: seconds between checks while pings through it fail
VPN_CHECK_UP = 300        # ... and while they work
VPN_SWITCH_SETTLE = 30    # Seconds to let the VPN connect after moving to another server, before checking again
# Mullvad server switching: gluetun's control server, and the cities to use, best first (set by the installer)
GLUETUN_URL = os.environ.get('GLUETUN_URL', '').strip().rstrip('/')
GLUETUN_API_KEY = os.environ.get('GLUETUN_API_KEY', '').strip()
VPN_PROVIDER = os.environ.get('VPN_PROVIDER', '').strip().strip('"')
VPN_LOCATION = os.environ.get('VPN_LOCATION', '').strip().strip('"')
VPN_FALLBACK_CITIES = os.environ.get('VPN_FALLBACK_CITIES', '').strip().strip('"')
# ---------------------

log = logging.getLogger('scanbot')
# DNS-over-HTTPS goes through httpx, which logs every request at INFO: one line per lookup
for _name in ('httpx', 'httpcore'):
    logging.getLogger(_name).setLevel(logging.WARNING)

# --- DNS: every lookup goes to Quad9, over DNS-over-TLS (port 853) or, where that port is blocked, DNS-over-HTTPS ---
QUAD9_ADDRESSES = ('9.9.9.9', '149.112.112.112')
DNS_TRANSPORTS = ('auto', 'dot', 'doh')
DNS_PROBE_HOST = 'discord.com'  # Looked up at startup to see which transport works
DNS_PROBE_LIFETIME = 3          # Seconds a blocked transport gets before the next one is tried

class DnsUnavailable(Exception):
    """No Quad9 transport could answer, so the bot can't even reach Discord."""

def parse_dns_transport(value):
    value = (value or '').strip().lower() or 'auto'
    if value not in DNS_TRANSPORTS:
        raise ValueError(f"DNS_TRANSPORT must be one of {', '.join(DNS_TRANSPORTS)}, not {value!r}")
    return value

def quad9_nameservers(transport):
    if transport == 'doh':
        # The bootstrap address means dns.quad9.net itself is never looked up through the system resolver
        return [dns.nameserver.DoHNameserver('https://dns.quad9.net/dns-query', bootstrap_address=address)
                for address in QUAD9_ADDRESSES]
    return [dns.nameserver.DoTNameserver(address, hostname='dns.quad9.net') for address in QUAD9_ADDRESSES]

QUAD9 = dns.asyncresolver.Resolver(configure=False)
QUAD9.nameservers = quad9_nameservers('dot')
QUAD9.lifetime = 5
QUAD9.cache = dns.resolver.Cache()
# mcstatus resolves server names and SRV records through dnspython's default resolver
dns.asyncresolver.default_resolver = QUAD9

class Quad9Resolver(AbstractResolver):
    """Resolves hostnames for aiohttp (Discord, mcstatus.io, ip-api.com) through Quad9."""

    async def resolve(self, host, port=0, family=socket.AF_INET):
        try:
            ips = [str(ipaddress.ip_address(host))]
        except ValueError:
            try:
                answer = await QUAD9.resolve(host, 'A')
            except dns.exception.DNSException as e:
                raise OSError(f"Could not resolve {host}: {e}") from e
            ips = [record.address for record in answer]
        return [{"hostname": host, "host": ip, "port": port, "family": socket.AF_INET,
                 "proto": 0, "flags": socket.AI_NUMERICHOST} for ip in ips]

    async def close(self):
        pass

DNS_LABELS = {'dot': 'Quad9 over TLS (port 853)', 'doh': 'Quad9 over HTTPS (port 443)'}

try:
    DNS_TRANSPORT = parse_dns_transport(os.environ.get('DNS_TRANSPORT', ''))
except ValueError as e:
    print(f"❌ Error: {e}")
    sys.exit(1)

async def probe_nameservers(nameservers):
    """Looks up DNS_PROBE_HOST through just these nameservers, leaving QUAD9 alone. Raises a DNSException if none answers."""
    resolver = dns.asyncresolver.Resolver(configure=False)
    resolver.nameservers = nameservers
    await resolver.resolve(DNS_PROBE_HOST, 'A', lifetime=DNS_PROBE_LIFETIME)

async def choose_dns_transport(mode):
    """
    Points QUAD9 at the transport that works from this network. "auto" tries TLS, then HTTPS; "dot" and "doh"
    try only that one. Raises DnsUnavailable, naming every failure, if none answers.
    """
    failures = []
    for transport in ('dot', 'doh') if mode == 'auto' else (mode,):
        nameservers = quad9_nameservers(transport)
        if transport == 'doh' and not dns.query.have_doh:
            # Without them dnspython tries HTTP/3 and every lookup fails with a baffling "not available"
            failures.append(f"{DNS_LABELS[transport]}: the httpx and h2 packages are missing (pip install -r requirements.txt)")
            continue
        try:
            await probe_nameservers(nameservers)
        except (dns.exception.DNSException, OSError) as e:
            failures.append(f"{DNS_LABELS[transport]}: {e}")
            continue
        QUAD9.nameservers = nameservers
        if failures:
            log.warning("%s; using %s instead.", failures[0], DNS_LABELS[transport])
        else:
            log.info("DNS goes through %s.", DNS_LABELS[transport])
        return transport
    hint = ("Is this machine (or, with Docker, its containers) cut off from the internet?" if mode == 'auto'
            else f"DNS_TRANSPORT={mode} allows no fallback; set it to auto to try the other transport too.")
    raise DnsUnavailable(f"DNS lookups failed, so the bot can't reach Discord. {'; '.join(failures)}. {hint}")

# Read token: DISCORD_TOKEN environment variable first, then token.txt
TOKEN = os.environ.get('DISCORD_TOKEN', '').strip()
if not TOKEN:
    try:
        with open('token.txt', 'r') as f:
            TOKEN = f.read().strip()
    except FileNotFoundError:
        pass
    except OSError as e:
        print(f"❌ Error: can't read {os.path.abspath('token.txt')}: {e}")
        sys.exit(1)
if not TOKEN:
    print(f"❌ Error: no Discord token. Set DISCORD_TOKEN or put the token in {os.path.abspath('token.txt')}.")
    sys.exit(1)

# host or IP, optionally with :port
ADDRESS_RE = re.compile(r'^[A-Za-z0-9._-]+(:\d{1,5})?$')

# Anyone in the channel can start a scan, so it must never reach the machine the bot runs on
# or the network behind it. Only addresses on the public internet are pinged.
INTERNAL_SUFFIXES = ('.localhost', '.local', '.lan', '.internal', '.home.arpa', '.localdomain')

class BlockedAddress(Exception):
    """The address is private or local, so it is never contacted."""

def is_internal_name(host):
    host = host.lower().rstrip('.')
    # Public names always contain a dot, so a bare name like "router" is a local one
    return '.' not in host or host.endswith(INTERNAL_SUFFIXES)

def is_public_entry(entry):
    """Cheap check on a line from the file, before any lookups: host or IP, optionally with :port."""
    host = entry.split(':', 1)[0]
    try:
        return is_public_ip(ipaddress.ip_address(host))
    except ValueError:
        return not is_internal_name(host)

async def resolve_public_address(host):
    """
    Resolves a host through Quad9 and returns the address to connect to.
    Returns None if the name doesn't resolve, raises BlockedAddress if it points at (or is)
    something private. A failed lookup is never guessed at.
    """
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        if is_internal_name(host):
            raise BlockedAddress(host)
        try:
            answer = await QUAD9.resolve(host, 'A', raise_on_no_answer=False)
        except dns.exception.DNSException:
            return None
        addresses = [ipaddress.ip_address(record.address) for record in answer]
        if not addresses:
            return None
        if not all(is_public_ip(a) for a in addresses):
            raise BlockedAddress(host)
        return addresses[0]
    if not is_public_ip(ip):
        raise BlockedAddress(host)
    return ip

def get_flag_emoji(country_code):
    if not country_code:
        return "🏳️"
    return "".join([chr(ord(c.upper()) + 127397) for c in country_code])

def env_flag(name):
    return os.environ.get(name, '').strip().lower() in ('1', 'true', 'yes', 'on')

# Slash commands need no privileged intent. The Message Content intent is only for the ! commands,
# so SLASH_ONLY=1 lets the bot run with that switch off in the Developer Portal.
SLASH_ONLY = env_flag('SLASH_ONLY')

def build_intents(slash_only):
    intents = discord.Intents.default()
    intents.message_content = not slash_only
    intents.dm_messages = True
    return intents

class ScanBot(commands.Bot):
    async def setup_hook(self):
        # Slash commands only show up in Discord once they've been synced
        try:
            synced = await self.tree.sync()
            log.info("Synced %d slash command(s)", len(synced))
        except discord.HTTPException as e:
            log.warning("Could not sync slash commands: %s", e)

# MOTDs and player names come from strangers' servers, so never let them ping anyone
bot = ScanBot(command_prefix=commands.when_mentioned if SLASH_ONLY else '!',
              intents=build_intents(SLASH_ONLY), help_command=None,
              allowed_mentions=discord.AllowedMentions.none())
bot.direct_ok = None  # Set by the startup probe in on_ready
bot.direct_ok_bedrock = None  # Same for Bedrock: it's UDP, so it can work when Java's TCP port is blocked
bot.probed_at = 0.0  # With the VPN: when the probes last ran (time.monotonic)
bot.vpn_switcher = None  # With Mullvad: moves the VPN off servers that are down (set in on_ready)

class Scan:
    """
    One user's scan. Each has its own stop switch and progress, so several people can scan
    at the same time without stopping or overwriting each other.
    """
    def __init__(self, owner, guild_id):
        self.owner = owner            # The user who started it
        self.guild_id = guild_id      # Server it was started in (None in DMs), for moderators' /stop
        self.stop = asyncio.Event()
        self.turn = asyncio.Event()   # Set when a queued scan may start, or was cancelled
        self.running = False
        self.stopped_by = None        # Who used /stop on it, if anyone

scans = {}  # User ID -> that user's scan, queued or running
queue = []  # Scans waiting for a free slot, oldest first

def running_count():
    return sum(1 for s in scans.values() if s.running)

def claim_slot(scan):
    """
    Starts the scan right away if a slot is free and nobody is waiting, otherwise queues it.
    Returns its place in the queue, or 0 if it can start now.
    """
    if not queue and running_count() < MAX_CONCURRENT_SCANS:
        scan.running = True
        return 0
    queue.append(scan)
    return len(queue)

def start_queued():
    """Hands free slots to queued scans, oldest first."""
    while queue and running_count() < MAX_CONCURRENT_SCANS:
        scan = queue.pop(0)
        scan.running = True
        scan.turn.set()

def request_stop(scan, by=None):
    scan.stopped_by = by
    scan.stop.set()
    if scan in queue:
        # Never started: take it out of the queue and wake it up so it can finish
        queue.remove(scan)
        scan.turn.set()

def release(scan):
    """Forgets a finished, stopped or cancelled scan and lets the next queued one start."""
    if scans.get(scan.owner.id) is scan:
        del scans[scan.owner.id]
    if scan in queue:
        queue.remove(scan)
    scan.running = False
    start_queued()

class Pacer:
    """
    Spaces out requests to a rate-limited service. mcstatus.io and ip-api.com count requests
    per client IP, so all scans running at the same time share one pacer each. Waiters are
    served in order, which makes concurrent scans take turns.
    """
    def __init__(self):
        self._lock = asyncio.Lock()
        self._next = 0.0

    async def wait(self, interval):
        async with self._lock:
            delay = self._next - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            self._next = time.monotonic() + interval

api_pacer = Pacer()  # mcstatus.io
geo_pacer = Pacer()  # ip-api.com

def to_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0

def make_result(ip, address, players, players_max, names, version, motd, edition='java'):
    return {
        "ip": ip,
        "edition": EDITION_LABELS[edition],
        "address": address if isinstance(address, str) else None,  # Resolved IP, used for geolocation
        "players": to_int(players),
        "max": to_int(players_max),
        "names": names,
        "version": version or 'Unknown',
        "motd": (motd or '').strip().replace('\n', '  '),
    }

pinger_session = None

def get_pinger_session():
    """
    HTTP session to the pinger in the VPN container. Unlike every other session here it doesn't use
    Quad9: the pinger's name ("gluetun") only exists in Docker's own DNS.
    """
    global pinger_session
    if pinger_session is None or pinger_session.closed:
        pinger_session = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(limit=0),  # 5 scans x 50 pings can be in flight at once
            timeout=aiohttp.ClientTimeout(total=DIRECT_TIMEOUT + 2))
    return pinger_session

def clean_status(data):
    """The pinger's answer, with every field forced to the type a result needs."""
    names = data.get('names')
    return {"players": data.get('players'), "max": data.get('max'),
            "names": [n for n in names if isinstance(n, str)] if isinstance(names, list) else [],
            "version": data.get('version') if isinstance(data.get('version'), str) else None,
            "motd": data.get('motd') if isinstance(data.get('motd'), str) else ''}

async def ping_server(address, port, edition):
    """
    The only place a server gets pinged. Without the VPN the bot pings it itself. With it, the pinger
    in the VPN container does, and if the pinger can't be reached the answer is None: a ping never
    falls back to this machine's own connection.
    """
    if not PINGER_URL:
        return await pinger.ping(address, port, edition, DIRECT_TIMEOUT)
    try:
        request = {"edition": edition, "ip": address, "port": port, "timeout": DIRECT_TIMEOUT}
        async with get_pinger_session().post(f"{PINGER_URL}/ping", json=request) as response:
            if response.status != 200:
                return None
            data = await response.json()
    except Exception:
        return None
    if not isinstance(data, dict) or not data.get('online'):
        return None
    return clean_status(data)

async def check_direct(ip):
    """
    Pings the server itself. Returns a result if it answers, None otherwise.
    Raises BlockedAddress, without contacting anything, if the server is at a private or local address.
    """
    if not is_public_entry(ip):
        raise BlockedAddress(ip)
    try:
        server = await JavaServer.async_lookup(ip, timeout=DIRECT_TIMEOUT)
    except Exception:
        return None

    # The lookup follows SRV records, so check where the server really is. Then connect to that
    # exact address: pinging by name would resolve it a second time, outside Quad9 and unchecked.
    try:
        address = await resolve_public_address(server.address.host)
        if address is None:
            return None
        status = await ping_server(str(address), server.address.port, 'java')
    except BlockedAddress:
        raise
    except Exception:
        return None
    if status is None:
        return None

    return make_result(ip, str(address), status['players'], status['max'],
                       status['names'], status['version'], status['motd'])

def split_entry(entry, default_port):
    """Splits "host" or "host:port" from the IP list."""
    host, _, port = entry.partition(':')
    return host, int(port) if port else default_port

async def check_direct_bedrock(entry):
    """
    Pings a Bedrock server over UDP. Returns a result if it answers, None otherwise.
    Raises BlockedAddress, without contacting anything, if the server is at a private or local address.
    Bedrock has no SRV records and its ping doesn't carry a hostname, so pinging the checked IP loses nothing.
    """
    if not is_public_entry(entry):
        raise BlockedAddress(entry)
    try:
        host, port = split_entry(entry, BEDROCK_PORT)
        address = await resolve_public_address(host)
        if address is None:
            return None
        status = await ping_server(str(address), port, 'bedrock')
    except BlockedAddress:
        raise
    except Exception:
        return None
    if status is None:
        return None

    return make_result(entry, str(address), status['players'], status['max'],
                       [], status['version'], status['motd'], edition='bedrock')

async def check_api(session, ip, edition='java'):
    """
    Asks mcstatus.io about the server. Returns a result if it's online, None otherwise.
    """
    for attempt in range(3):
        try:
            async with session.get(f"{MC_API_URLS[edition]}{ip}", timeout=aiohttp.ClientTimeout(total=10)) as response:
                if response.status == 429:
                    # Rate limited: wait and try again instead of calling the server offline
                    await asyncio.sleep(1 + attempt)
                    continue
                if response.status != 200:
                    return None
                data = await response.json()
        except Exception:
            return None

        try:
            return parse_api_status(ip, data, edition)
        except Exception as e:
            log.warning("Unexpected mcstatus.io response for %s: %s", ip, e)
            return None
    return None

def parse_api_status(ip, data, edition='java'):
    """
    Turns an mcstatus.io response into a result, or None if the server is offline.
    Tolerates missing or oddly typed fields so one strange server can't break a scan.
    """
    if not isinstance(data, dict) or not data.get('online', False):
        return None

    players = data.get('players')
    if not isinstance(players, dict):
        players = {}
    names = []
    for p in players.get('list') or []:
        if isinstance(p, dict):
            p = p.get('name_clean') or p.get('name')
        if isinstance(p, str) and p:
            names.append(p)

    version = data.get('version')
    if isinstance(version, dict):
        version = version.get('name_clean') or version.get('name_raw') or version.get('name')  # Bedrock only has "name"
    motd = data.get('motd')
    if isinstance(motd, dict):
        motd = motd.get('clean') or motd.get('raw')

    return make_result(ip, data.get('ip_address'), players.get('online'), players.get('max'), names,
                       version if isinstance(version, str) else None,
                       motd if isinstance(motd, str) else None, edition)

geo_db = None  # Offline country database, opened by load_geo_db() at startup

def geo_db_age_days():
    """Days since the database at GEO_DB_PATH was built, or None if it's missing or unreadable."""
    try:
        with maxminddb.open_database(GEO_DB_PATH) as db:
            return (time.time() - db.metadata().build_epoch) / 86400
    except Exception:
        return None

async def update_geo_db():
    """
    Downloads this month's DB-IP country database to GEO_DB_PATH, or last month's
    if this month's isn't published yet. Returns True if it saved one.
    """
    today = datetime.date.today()
    last_month = today.replace(day=1) - datetime.timedelta(days=1)
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(resolver=Quad9Resolver()),
                                     headers={'User-Agent': USER_AGENT}) as session:
        for month in (today.strftime('%Y-%m'), last_month.strftime('%Y-%m')):
            try:
                async with session.get(GEO_DB_URL.format(month=month), timeout=aiohttp.ClientTimeout(total=120)) as resp:
                    if resp.status != 200:
                        continue
                    data = gzip.decompress(await resp.read())
            except Exception as e:
                log.warning("Country database download failed: %s", e)
                continue
            # Write next to the old file, then swap, so a failed write never leaves half a database
            tmp_path = GEO_DB_PATH + '.tmp'
            with open(tmp_path, 'wb') as f:
                f.write(data)
            os.replace(tmp_path, GEO_DB_PATH)
            log.info("Downloaded the DB-IP country database for %s", month)
            return True
    return False

async def load_geo_db():
    """Opens the offline country database, downloading or refreshing it first if needed."""
    global geo_db
    age = geo_db_age_days()
    if age is None or age > GEO_DB_MAX_AGE_DAYS:
        try:
            await update_geo_db()
        except OSError as e:
            # e.g. a read-only install folder; keep using the old database if there is one
            log.warning("Could not save the country database to %s: %s", GEO_DB_PATH, e)
    geo_db = None
    try:
        geo_db = maxminddb.open_database(GEO_DB_PATH)
        built = datetime.datetime.fromtimestamp(geo_db.metadata().build_epoch, datetime.timezone.utc)
        log.info("Country database loaded (DB-IP, built %s)", built.strftime('%Y-%m-%d'))
    except Exception as e:
        log.warning("No country database (%s); flags come from ip-api.com only.", e)

def lookup_countries(ips):
    """
    Looks up countries in the offline database. Returns {ip: country code} for the IPs it knows.
    """
    found = {}
    if geo_db is None:
        return found
    for ip in ips:
        try:
            record = geo_db.get(ip)
        except ValueError:  # Not an IP address
            continue
        code = ((record or {}).get('country') or {}).get('iso_code')
        if code:
            found[ip] = code
    return found

async def batch_get_locations(session, ips, stop=None):
    """
    Uses ip-api.com batch endpoint to get locations for a list of IPs.
    Max 100 IPs per request.
    """
    stop = stop or asyncio.Event()
    locations = {}
    if not ips:
        return locations

    # Split into chunks of 100
    chunks = [ips[i:i + 100] for i in range(0, len(ips), 100)]

    for chunk in chunks:
        # Other scans may be using ip-api.com too, so wait for our turn
        await geo_pacer.wait(GEO_DELAY)
        if stop.is_set():
            break
        try:
            payload = [{"query": ip, "fields": "query,countryCode"} for ip in chunk]

            async with session.post(GEO_BATCH_URL, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    for entry in await resp.json():
                        # Entry looks like: {"query": "1.2.3.4", "countryCode": "US"}
                        if entry.get('query'):
                            locations[entry['query']] = entry.get('countryCode')
                else:
                    log.warning("Geolocation returned HTTP %s", resp.status)
        except Exception as e:
            log.warning("Geolocation failed: %s", e)

    return locations

def parse_ips(text):
    """
    Returns (unique valid addresses in file order, invalid line count, duplicate count,
    private or local address count). Blank lines and lines starting with # are ignored.
    """
    lines = [line.strip() for line in text.splitlines()]
    lines = [line for line in lines if line and not line.startswith('#')]
    valid = [line for line in lines if ADDRESS_RE.match(line)]
    public = [line for line in valid if is_public_entry(line)]
    unique = list(dict.fromkeys(public))
    return unique, len(lines) - len(valid), len(public) - len(unique), len(valid) - len(public)

async def set_status(text):
    try:
        await bot.change_presence(activity=discord.Game(name=text))
    except Exception as e:
        log.warning("Could not update presence: %s", e)

def vpn_down():
    """True when the bot uses the VPN and the last check found pings through it failing."""
    return bool(PINGER_URL) and bot.direct_ok is not None and not (bot.direct_ok or bot.direct_ok_bedrock)

async def update_presence():
    """
    Shows how many scans are running and queued, and whether the VPN is down.
    Called when a scan starts or ends, and when the VPN goes down or comes back.
    """
    running, waiting = running_count(), len(queue)
    if not running:
        text = "Idle | Waiting for IPs"
    else:
        text = f"Scanning · {running} running"
        if waiting:
            text += f", {waiting} queued"
    if vpn_down():
        text += " · VPN down"
    await set_status(text)

def progress_text(state):
    owner = f"{state['owner']} · " if state.get('owner') else ""
    return f"🔎 {owner}**{state['phase']}:** {state['done']}/{state['total']} · **Found:** {state['found']}"

async def report_progress(message, state):
    """Edits the progress message every few seconds while a scan runs."""
    last = None
    while True:
        await asyncio.sleep(PROGRESS_INTERVAL)
        text = progress_text(state)
        if text != last:
            try:
                await message.edit(content=text)
            except discord.HTTPException as e:
                log.warning("Could not update progress message: %s", e)
            last = text

async def run_direct(ips, results, state, edition='java', stop=None):
    """
    Pings every server directly, DIRECT_CONCURRENCY at a time.
    Returns the IPs that didn't answer, in file order.
    """
    stop = stop or asyncio.Event()
    state.update(phase="Pinging servers", done=0, total=len(ips))
    semaphore = asyncio.Semaphore(DIRECT_CONCURRENCY)
    pinged = set()
    check_server = check_direct if edition == 'java' else check_direct_bedrock

    async def ping(ip):
        async with semaphore:
            if stop.is_set():
                return
            try:
                result = await check_server(ip)
            except BlockedAddress:
                # Not pinged, and not passed on to the API either
                state['done'] += 1
                state['blocked'] += 1
                return
        state['done'] += 1
        pinged.add(ip)
        if result:
            results[ip] = result
            state['found'] += 1

    await asyncio.gather(*(ping(ip) for ip in ips))
    return [ip for ip in ips if ip in pinged and ip not in results]

async def run_api(session, ips, results, state, retrying, edition='java', stop=None, vpn_down=False):
    """
    Checks servers through mcstatus.io, starting one request every API_DELAY seconds.
    Scans running at the same time take turns, so together they stay within the limit.
    """
    stop = stop or asyncio.Event()
    phase = "Retrying unreachable servers via API" if retrying else "Checking servers via API"
    if vpn_down:
        phase += " (VPN down)"
    state.update(phase=phase, done=0, total=len(ips))

    async def check(ip):
        result = await check_api(session, ip, edition)
        state['done'] += 1
        if result:
            results[ip] = result
            state['found'] += 1

    tasks = []
    for ip in ips:
        await api_pacer.wait(API_DELAY)
        if stop.is_set():
            break
        tasks.append(asyncio.create_task(check(ip)))

    if stop.is_set():
        # Cancel checks still in flight before the session closes
        for t in tasks:
            t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)

def format_entry(r, locations, markdown=True):
    bold = (lambda s: f"**{s}**") if markdown else (lambda s: s)
    motd = discord.utils.escape_markdown(r['motd']) if markdown else r['motd']
    names = ", ".join(r['names'])
    if markdown:
        names = discord.utils.escape_markdown(names)

    text = f"{get_flag_emoji(locations.get(r['address']))} {bold(r['ip'])} | Players: {r['players']}/{r['max']} | Ver: {r['version']}"
    if motd: text += f"\n   └ 📝 {motd}"
    if names: text += f"\n   └ 👤 {bold('Users:')} {names}"
    return text

def csv_cell(value):
    """
    Spreadsheets run cells that start with = + - @ as formulas, and MOTDs, versions and player names
    come from strangers' servers. A leading ' makes the cell plain text.
    """
    if isinstance(value, str) and value.startswith(('=', '+', '-', '@', '\t', '\r')):
        return "'" + value
    return value

def build_files(populated, empty, locations):
    """Returns a readable .txt report and a .csv of every online server."""
    lines = []
    if populated:
        lines.append(f"Servers with Players ({len(populated)}):")
        lines += [format_entry(r, locations, markdown=False) for r in populated]
        lines.append("")
    if empty:
        lines.append(f"Online (Empty) Servers ({len(empty)}):")
        lines += [format_entry(r, locations, markdown=False) for r in empty]
    txt = discord.File(io.BytesIO("\n".join(lines).encode('utf-8')), filename="scan_results.txt")

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["ip", "resolved_ip", "country", "players_online", "players_max", "version", "motd", "players", "edition"])
    for r in populated + empty:
        writer.writerow([csv_cell(v) for v in (r['ip'], r['address'] or '', locations.get(r['address']) or '',
                                                r['players'], r['max'], r['version'], r['motd'],
                                                "; ".join(r['names']), r['edition'])])
    table = discord.File(io.BytesIO(buffer.getvalue().encode('utf-8')), filename="scan_results.csv")
    return [txt, table]

async def send_channel(ctx, *args, **kwargs):
    """
    Posts to the channel itself instead of replying to the interaction. A slash command's reply
    stops working 15 minutes after it was used, and a big scan can take longer than that.
    Falls back to replying if the bot isn't allowed to post in the channel.
    """
    try:
        return await ctx.channel.send(*args, **kwargs)
    except discord.Forbidden:
        for file in kwargs.get('files', []):
            file.reset()  # The failed upload already read them to the end
        return await ctx.send(*args, **kwargs)

async def send_results(ctx, results, locations, stopped, total_ips, duration, blocked=0, edition='java', owner=None,
                       vpn_down=False):
    minutes = int(duration // 60)
    seconds = int(duration % 60)

    populated = sorted((r for r in results if r['players'] > 0), key=lambda r: r['players'], reverse=True)
    empty = [r for r in results if r['players'] == 0]

    title = "🛑 **Scan stopped** — partial results" if stopped else "📊 **Scan Complete!**"
    if edition != 'java':
        title += f" ({EDITION_LABELS[edition]})"
    if owner:
        title += f" · {owner}"  # Several people may be scanning in the same channel
    summary = (f"{title}\n🟢 {len(populated)} with players · ⚪ {len(empty)} empty · 🔎 {total_ips} IPs\n"
               f"⏱️ **Time:** {minutes}m {seconds}s")
    if not stopped and duration > 0:
        summary += f"\n⚡ **Speed:** {total_ips / duration:.2f} IPs/sec"
    if blocked:
        summary += f"\n🚫 {blocked} name(s) pointed at private or local addresses and were skipped"
    if vpn_down:
        summary += "\n⚠️ The VPN was down: every server was checked through the API only"

    if not results:
        await send_channel(ctx, f"❌ No working servers found.\n{summary}")
        return

    lines = []
    if populated:
        lines.append(f"**🟢 Servers with Players ({len(populated)}):**")
        lines += [format_entry(r, locations) for r in populated]
        lines.append("")
    if empty:
        lines.append(f"**⚪ Online (Empty) Servers ({len(empty)}):**")
        lines += [format_entry(r, locations) for r in empty]
    body = "\n".join(lines)

    if len(summary) + len(body) + 2 <= INLINE_LIMIT:
        await send_channel(ctx, f"{summary}\n\n{body}")
        return

    # Too long for one message: summary + top servers in chat, everything in files
    top = [f"{get_flag_emoji(locations.get(r['address']))} **{r['ip']}** | {r['players']}/{r['max']}"
           for r in populated[:10]]
    message = summary
    if top:
        message += "\n\n**Top servers:**\n" + "\n".join(top)
    message += "\n\n📎 Full results are in the attached files."
    await send_channel(ctx, message[:2000], files=build_files(populated, empty, locations))

async def probe_direct(edition='java'):
    """True if a direct ping to any of the probe servers gets an answer."""
    servers, check_server = (PROBE_SERVERS, check_direct) if edition == 'java' else (BEDROCK_PROBE_SERVERS, check_direct_bedrock)

    async def probe(server):
        try:
            return await check_server(server) is not None
        except BlockedAddress:
            return False

    return any(await asyncio.gather(*(probe(server) for server in servers)))

async def check_vpn():
    """
    With the VPN: probes both editions through the pinger, and logs and shows it when
    the VPN goes down or comes back. The first check always logs.
    """
    first, was_down = bot.direct_ok is None, vpn_down()
    bot.direct_ok, bot.direct_ok_bedrock = await asyncio.gather(probe_direct('java'), probe_direct('bedrock'))
    bot.probed_at = time.monotonic()
    if vpn_down() and (first or not was_down):
        log.warning("Pings through the VPN fail (VPN down or still connecting): scans check every server through "
                    "the mcstatus.io API only until it's back. Checking again every %d s.", VPN_CHECK_DOWN)
    elif not vpn_down() and (first or was_down):
        log.info("Pings through the VPN work: scans ping servers through it, with API fallback.")
    if not first and vpn_down() != was_down:
        await update_presence()

async def fetch_mullvad_relays():
    """Mullvad's WireGuard server list, over this machine's own connection like the other APIs."""
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(resolver=Quad9Resolver()),
                                     headers={'User-Agent': USER_AGENT}) as session:
        async with session.get(vpn_switch.RELAYS_URL, timeout=aiohttp.ClientTimeout(total=15)) as response:
            response.raise_for_status()
            return await response.json()

async def set_vpn_server(hostname):
    """
    Tells gluetun to use one Mullvad server, through its control server (only the bot can reach it).
    gluetun ignores it when nothing changed, so repeating it costs nothing. Returns False if gluetun
    doesn't know that server.
    """
    body = {"provider": {"server_selection": {"hostnames": [hostname], "cities": [], "countries": []}}}
    async with get_pinger_session().put(f"{GLUETUN_URL}/v1/vpn/settings", json=body,
                                        headers={"X-API-Key": GLUETUN_API_KEY},
                                        timeout=aiohttp.ClientTimeout(total=60)) as response:
        if response.status == 400:
            log.debug("gluetun refused %s: %s", hostname, (await response.text()).strip())
            return False
        if response.status in (401, 403):
            raise PermissionError("gluetun refused the API key (GLUETUN_API_KEY and vpn/auth/config.toml must match)")
        response.raise_for_status()
        return True

def make_vpn_switcher():
    """The Mullvad server switcher, or None when the VPN isn't Mullvad or can't be controlled."""
    if not PINGER_URL or VPN_PROVIDER.lower() != 'mullvad':
        return None
    if not (GLUETUN_URL and GLUETUN_API_KEY):
        log.info("Mullvad server switching is off: there's no GLUETUN_API_KEY. Run the installer again to turn it on.")
        return None
    cities = [c.strip() for c in VPN_FALLBACK_CITIES.split(',') if c.strip()]
    if not cities and VPN_LOCATION:
        cities = [VPN_LOCATION.split(',')[0].strip()]
        log.info("No VPN_FALLBACK_CITIES: only servers in %s are used. Run the installer again for more cities.", cities[0])
    if not cities:
        log.info("Mullvad server switching is off: no city to pick servers from (VPN_FALLBACK_CITIES).")
        return None
    log.info("Mullvad server switching is on. Cities, best first: %s", ", ".join(cities))
    return vpn_switch.MullvadSwitcher(cities, fetch_mullvad_relays, set_vpn_server)

async def switch_vpn_server():
    """
    With Mullvad: moves the VPN to another server if the current one is listed as offline or doesn't
    let pings through. Returns how many seconds to wait before the next VPN check.
    """
    wait = VPN_CHECK_DOWN if vpn_down() else VPN_CHECK_UP
    if bot.vpn_switcher is not None:
        try:
            if await bot.vpn_switcher.tick(vpn_ok=not vpn_down()):
                wait = VPN_SWITCH_SETTLE  # Check again soon, so scans use the new server quickly
        except Exception:
            log.exception("Mullvad server switching failed")
    return wait

async def watch_vpn():
    """Checks the VPN in the background, more often while it's down, and switches Mullvad servers."""
    while True:
        await asyncio.sleep(await switch_vpn_server())
        try:
            await check_vpn()
        except Exception:
            log.exception("VPN check failed")

async def direct_pings_work(edition):
    """
    Whether a scan starting now can ping servers directly. With the VPN, a failed check is
    tried again right away (unless it just ran), in case the VPN has come back since.
    """
    if PINGER_URL and vpn_down() and time.monotonic() - bot.probed_at > 10:
        await check_vpn()
    return bot.direct_ok if edition == 'java' else bot.direct_ok_bedrock

@bot.event
async def on_ready():
    # on_ready runs again after reconnects; only probe once
    if bot.direct_ok is None and PINGER_URL:
        log.info("Pings go through the VPN (pinger at %s); Discord, the APIs and DNS use this machine's connection.",
                 PINGER_URL)
        await check_vpn()
        bot.vpn_switcher = make_vpn_switcher()
        bot.vpn_watcher = asyncio.create_task(watch_vpn())
    elif bot.direct_ok is None:
        bot.direct_ok, bot.direct_ok_bedrock = await asyncio.gather(probe_direct('java'), probe_direct('bedrock'))
        if bot.direct_ok:
            log.info("Direct pings work; scans use direct pings with API fallback.")
        else:
            log.warning("Direct pings to %s all failed; scans use the mcstatus.io API only (5 checks/second).",
                        ", ".join(PROBE_SERVERS))
        if bot.direct_ok_bedrock:
            log.info("Direct Bedrock (UDP) pings work; Bedrock scans use direct pings with API fallback.")
        else:
            log.warning("Direct Bedrock pings to %s all failed; Bedrock scans use the mcstatus.io API only (5 checks/second).",
                        ", ".join(BEDROCK_PROBE_SERVERS))
    log.info("Logged in as %s", bot.user.name)
    await update_presence()

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.MissingRequiredAttachment):
        await ctx.send("❌ Please attach a `.txt` file.")
    elif isinstance(error, commands.BadLiteralArgument):
        await ctx.send("❌ The edition must be `java` or `bedrock`, for example `!scan bedrock`.")
    elif not isinstance(error, commands.CommandNotFound):
        log.error("Command %s failed", ctx.command, exc_info=error)
        # A slash command that was deferred would otherwise sit on "thinking..." with no explanation
        try:
            await ctx.send("❌ Something went wrong. Please try again.")
        except discord.HTTPException:
            pass

@bot.hybrid_command(name="help", description="Show the commands and how to use them")
async def help(ctx):
    """Displays a list of available commands."""
    embed = discord.Embed(
        title="📖 Scanbot Help",
        description="Here are all available commands:",
        color=discord.Color.blue()
    )
    embed.add_field(
        name="/scan file:<.txt> [edition]  (or !scan [edition])",
        value="Scans a list of Minecraft server IPs from a `.txt` file. `edition` is `java` (default) or `bedrock`.\n"
              f"Up to {MAX_CONCURRENT_SCANS} people can scan at once, one scan each; more wait in a queue.",
        inline=False
    )
    embed.add_field(
        name="/stop  (or !stop)",
        value="Stops your scan and posts what it found so far, or cancels it if it's still queued.\n"
              "Moderators (**Manage Messages**) can stop someone else's scan with `/stop user:@name` "
              "(`!stop @name`), or every scan in the server with `/stop all:all` (`!stop all`).",
        inline=False
    )
    embed.add_field(
        name="/help  (or !help)",
        value="Displays this help message.",
        inline=False
    )
    embed.set_footer(text=f"Attach a .txt file with IPs (one per line, max {MAX_IPS_PER_SCAN}) to use /scan.\n"
                          "Country flags: IP Geolocation by DB-IP (db-ip.com)")
    await ctx.send(embed=embed)

def is_moderator(ctx):
    """Manage Messages in a server lets someone stop other people's scans there."""
    return ctx.guild is not None and ctx.permissions.manage_messages

@bot.hybrid_command(name="stop", description="Stop your scan. Moderators can stop someone else's, or all of them")
@app_commands.describe(scope="Moderators: stop every scan in this server",
                       user="Moderators: stop this person's scan")
@app_commands.rename(scope="all")
async def stop(ctx, scope: Optional[Literal['all']] = None, user: Optional[discord.User] = None):
    """Stops your own scan. With Manage Messages: someone else's, or every scan in this server."""
    if scope or (user and user.id != ctx.author.id):
        if not is_moderator(ctx):
            await ctx.send("❌ Only moderators (**Manage Messages** permission) can stop other people's scans.")
            return
        in_server = [s for s in scans.values() if s.guild_id == ctx.guild.id]
        targets = in_server if scope else [s for s in in_server if s.owner.id == user.id]
        if not targets:
            await ctx.send(f"⚠️ **{user.mention} has no scan running here.**" if not scope
                           else "⚠️ **No scans are running in this server.**")
            return
        for scan in targets:
            request_stop(scan, by=ctx.author)
        owners = ", ".join(s.owner.mention for s in targets)
        await ctx.send(f"🛑 **Stopping {len(targets)} scan(s)** ({owners}). "
                       "Running scans stop shortly and post what they found so far.")
        return

    scan = scans.get(ctx.author.id)
    if scan is None:
        await ctx.send("⚠️ **You don't have a scan running.**")
        return
    started = scan.running
    request_stop(scan, by=ctx.author)
    if started:
        await ctx.send("🛑 **Stop requested.** Your scan will stop shortly and post what it found so far...")
    else:
        await ctx.send("🛑 **Scan cancelled** before it started.")

@bot.hybrid_command(name="scan", aliases=['check'], description="Check a list of Minecraft servers from a .txt file")
@app_commands.describe(file="A .txt file with one IP or hostname per line",
                       edition="Minecraft edition of the servers in the file (default: java)")
async def check(ctx, file: discord.Attachment, edition: Literal['java', 'bedrock'] = 'java'):
    await ctx.defer()  # A slash command must be answered within 3 seconds

    # No await between this check and registering the scan, so nobody can start two at once
    if ctx.author.id in scans:
        await ctx.send("⏳ **You already have a scan running or queued.** Use `/stop` to stop it first.")
        return
    scan = Scan(ctx.author, ctx.guild.id if ctx.guild else None)
    scans[ctx.author.id] = scan
    try:
        await run_scan(ctx, scan, file, edition)
    finally:
        release(scan)
        await update_presence()

async def run_scan(ctx, scan, file, edition):
    # --- File Input --- (checked before queueing, so a bad file is rejected right away)
    if not file.filename.lower().endswith('.txt'):
        await ctx.send("❌ Must be a `.txt` file.")
        return
    if file.size > MAX_FILE_BYTES:
        await ctx.send(f"❌ That file is too big ({file.size // 1000} KB). "
                       f"A list of {MAX_IPS_PER_SCAN} servers is about 100 KB.")
        return

    try:
        content = await file.read()
        ips, invalid, duplicates, blocked = parse_ips(content.decode('utf-8-sig'))  # -sig: some editors add a BOM
    except Exception as e:
        await ctx.send(f"❌ Error reading file: {e}")
        return

    if not ips:
        note = f" ({blocked} private or local address(es) are never scanned)" if blocked else ""
        await ctx.send(f"⚠️ No valid IPs in the file.{note}")
        return

    total_ips = len(ips)
    if total_ips > MAX_IPS_PER_SCAN:
        await ctx.send(f"❌ Too many IPs. Maximum allowed per scan is {MAX_IPS_PER_SCAN}.")
        return

    notes = []
    if invalid: notes.append(f"skipped {invalid} invalid line(s)")
    if blocked: notes.append(f"skipped {blocked} private or local address(es)")
    if duplicates: notes.append(f"removed {duplicates} duplicate(s)")
    extra = f" ({', '.join(notes)})" if notes else ""

    # --- Wait for a free slot --- (unless /stop came while the file was being read)
    place = 0 if scan.stop.is_set() else claim_slot(scan)
    if place:
        await ctx.send(f"🕒 **Queued** (#{place}). All {MAX_CONCURRENT_SCANS} scan slots are busy; your scan of "
                       f"{total_ips} IPs starts automatically when one frees up. `/stop` cancels it.")
        await update_presence()
        await scan.turn.wait()
    if scan.stop.is_set():
        # Stopped before it started. The owner's own /stop already said so; a moderator's reply
        # may be in another channel, so tell the owner here.
        stopped_by = scan.stopped_by
        if not place:
            await ctx.send("🛑 **Scan cancelled.**")
        elif stopped_by is not None and stopped_by.id != ctx.author.id:
            await send_channel(ctx, f"🛑 {ctx.author.mention}, your queued scan was cancelled by {stopped_by.mention}.")
        return

    start_time = time.time()
    # Direct pings, unless the startup probe failed. With the VPN, pings only ever go through it:
    # while it's down, every server is checked through the API instead.
    direct_ok = await direct_pings_work(edition)
    no_vpn = bool(PINGER_URL) and not direct_ok
    owner = ctx.author.mention  # Renders as a name without pinging (mentions are switched off)
    label = "Bedrock " if edition == 'bedrock' else ""
    started = f"🚀 **Scan started** by {owner} on {total_ips} {label}IPs{extra}..."
    if no_vpn:
        started += "\n⚠️ **The VPN is down:** checking every server through the API only, so this is slower."
    if place:
        # The slash command's reply stops working after 15 minutes, which the queue may have taken
        await send_channel(ctx, started)
    else:
        await ctx.send(started)
    await update_presence()

    state = {"phase": "Starting", "done": 0, "total": total_ips, "found": 0, "blocked": 0, "owner": owner}
    progress = await send_channel(ctx, progress_text(state))
    updater = asyncio.create_task(report_progress(progress, state))
    results = {}
    locations = {}

    try:
        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(resolver=Quad9Resolver())) as session:
            # 1. Direct pings, many at once
            if direct_ok:
                retry = await run_direct(ips, results, state, edition, stop=scan.stop)
            else:
                retry = ips

            # 2. mcstatus.io API for everything that didn't answer, 5 per second shared by all scans
            if retry and not scan.stop.is_set():
                await run_api(session, retry, results, state, retrying=bool(direct_ok), edition=edition,
                              stop=scan.stop, vpn_down=no_vpn)

            # 3. Geolocation: offline database first (instant), ip-api.com for the rest
            addresses = sorted({r['address'] for r in results.values() if r['address']})
            locations = lookup_countries(addresses)
            unknown = [a for a in addresses if a not in locations]
            if unknown and not scan.stop.is_set():
                state.update(phase="Resolving locations", done=len(locations), total=len(addresses))
                locations.update(await batch_get_locations(session, unknown, stop=scan.stop))
                state['done'] = len(addresses)
    finally:
        updater.cancel()

    stopped = scan.stop.is_set()
    try:
        await progress.edit(content=("🛑 **Stopped.**" if stopped else "✅ **Done.**") +
                            f" {owner} found {state['found']} online servers.")
    except discord.HTTPException:
        pass

    await send_results(ctx, list(results.values()), locations, stopped, total_ips, time.time() - start_time,
                       blocked=state['blocked'], edition=edition, owner=owner, vpn_down=no_vpn)

async def main():
    discord.utils.setup_logging(root=True)
    try:
        await choose_dns_transport(DNS_TRANSPORT)
    except DnsUnavailable as e:
        print(f"❌ Error: {e}")
        sys.exit(1)
    await load_geo_db()
    # discord.py only builds its own connector if none is set, so Discord traffic uses Quad9 too
    bot.http.connector = aiohttp.TCPConnector(limit=0, resolver=Quad9Resolver())
    try:
        async with bot:
            await bot.start(TOKEN)
    finally:
        if pinger_session is not None:
            await pinger_session.close()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
