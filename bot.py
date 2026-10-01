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
import html
import io
import ipaddress
import json
import logging
import maxminddb
import os
import re
import signal
import socket
import sys
import time
from typing import Literal, Optional

import pinger
import vpn_switch
from pinger import is_public_ip

# --- Settings from the environment (docker-compose.yml passes them on from .env) ---
def env_flag(name):
    return os.environ.get(name, '').strip().lower() in ('1', 'true', 'yes', 'on')

def show_number(n):
    return f"{n:,}" if float(n).is_integer() else f"{n:g}"

def env_number(name, default, minimum, maximum, whole, why=''):
    """
    A number from the environment, or `default` when the variable is unset or empty (compose passes unset ones on
    as empty). Raises ValueError naming the variable and the allowed range for anything else.
    """
    raw = os.environ.get(name, '').strip()
    if not raw:
        return default
    try:
        value = int(raw) if whole else float(raw)
    except ValueError:
        value = None
    if value is None or not minimum <= value <= maximum:  # NaN fails the comparison too
        kind = "a whole number" if whole else "a number"
        raise ValueError(f"{name} must be {kind} from {show_number(minimum)} to {show_number(maximum)}, "
                         f"not {raw!r}{why}")
    return value

def env_int(name, default, minimum, maximum):
    return env_number(name, default, minimum, maximum, whole=True)

def env_float(name, default, minimum, maximum, why=''):
    return env_number(name, default, minimum, maximum, whole=False, why=why)

def env_switch(name, default):
    """On or off from the environment, or `default` when unset or empty. Raises ValueError for anything else."""
    raw = os.environ.get(name, '').strip()
    if not raw:
        return default
    if raw.lower() in ('1', 'true', 'yes', 'on'):
        return True
    if raw.lower() in ('0', 'false', 'no', 'off'):
        return False
    raise ValueError(f"{name} must be on or off, not {raw!r}")

# --- CONFIGURATION ---
MC_API_URLS = {
    'java': 'https://api.mcstatus.io/v2/status/java/',
    'bedrock': 'https://api.mcstatus.io/v2/status/bedrock/',
}
# Second opinion for Java servers mcstatus.io can't check. (Its Bedrock checks miss servers that are online, so
# Bedrock servers have no second opinion.) It rejects requests without a User-Agent.
MCSRVSTAT_URL = 'https://api.mcsrvstat.us/3/'
API_SERVER_TIMEOUT = 3     # Seconds mcstatus.io waits for a server before calling it offline (its default is 5)
API_FAILURE_THRESHOLD = 5  # mcstatus.io failures in a row (rate limits, server errors, timeouts) before ...
API_PAUSE_SECONDS = 60     # ... Java servers are checked through mcsrvstat.us for this long
API_SLOWDOWN_RESET = 60    # Seconds without a rate limit before a slowed-down pacer goes back to full speed
API_MAX_SLOWDOWN = 8       # A pacer slows down to at most this many times its normal spacing
EDITION_LABELS = {'java': 'Java', 'bedrock': 'Bedrock'}
JAVA_PORT = 25565         # Default port for Java servers given as an IP (hostnames go through mcstatus's SRV lookup)
BEDROCK_PORT = 19132      # Default port for Bedrock servers
GEO_BATCH_URL = 'http://ip-api.com/batch' # Fallback for IPs the offline database doesn't know
# Offline country database (DB-IP Lite, CC BY 4.0), next to bot.py unless GEO_DB_PATH is set
GEO_DB_PATH = os.environ.get('GEO_DB_PATH') or os.path.join(os.path.dirname(os.path.abspath(__file__)), 'dbip-country-lite.mmdb')
GEO_DB_URL = 'https://download.db-ip.com/free/dbip-country-lite-{month}.mmdb.gz'
# Offline network database (which AS / ISP an IP belongs to), from DB-IP too. The Docker image has it at /app.
GEO_ASN_DB_PATH = (os.environ.get('GEO_ASN_DB_PATH')
                   or os.path.join(os.path.dirname(os.path.abspath(__file__)), 'dbip-asn-lite.mmdb'))
GEO_ASN_DB_URL = 'https://download.db-ip.com/free/dbip-asn-lite-{month}.mmdb.gz'
USER_AGENT = 'scanbot (+https://github.com/TheDyXer/scanbot)'  # DB-IP rejects Python's default one
# These can be changed in .env (see README, "Configuration"); a value out of range stops the bot at startup
try:
    GEO_DB_MAX_AGE_DAYS = env_int('GEO_DB_MAX_AGE_DAYS', 40, 1, 3650)  # DB-IP publishes a new database every month
    MAX_IPS_PER_SCAN = env_int('MAX_IPS_PER_SCAN', 30000, 1, 1_000_000)
    # Scans running at the same time (one per user); more wait in a queue
    MAX_CONCURRENT_SCANS = env_int('MAX_CONCURRENT_SCANS', 5, 1, 50)
    # Direct pings in flight at the same time, per scan, and for all scans together. Pings to addresses that
    # don't answer wait out DIRECT_TIMEOUT, so on mostly dead ranges a scan checks about this many per timeout:
    # 300 / 3 s = 100 a second. (Until October 2026 it was 50: about 17 a second.)
    DIRECT_CONCURRENCY = env_int('DIRECT_CONCURRENCY', 300, 1, 2000)
    DIRECT_CONCURRENCY_TOTAL = env_int('DIRECT_CONCURRENCY_TOTAL', 2 * DIRECT_CONCURRENCY, 1, 20000)
    # Seconds to wait for a server to answer a direct ping
    DIRECT_TIMEOUT = env_float('DIRECT_TIMEOUT', 3, 0.5, pinger.MAX_TIMEOUT,
                               why=f" (the pinger waits at most {pinger.MAX_TIMEOUT} seconds)")
    API_DELAY = env_float('API_DELAY', 0.2, 0.05, 60)  # mcstatus.io allows 5 requests/second per IP, shared by all scans
    # Whether mcstatus.io also asks Java servers over the query protocol: software, plugins and full player lists
    API_QUERY = env_switch('API_QUERY', True)
    # mcsrvstat.us publishes no limit, so the bot asks it at most twice a second, shared by all scans
    MCSRVSTAT_DELAY = env_float('MCSRVSTAT_DELAY', 0.5, 0.1, 60)
    GEO_DELAY = env_float('GEO_DELAY', 4, 0, 600)      # ip-api.com batch allows 15 requests/minute, shared by all scans
    PROGRESS_INTERVAL = env_int('PROGRESS_INTERVAL', 3, 2, 600)  # Seconds between progress message updates
    # Without the VPN: seconds between new tries of direct pings while they don't work (the startup probe failed)
    DIRECT_RECHECK = env_int('DIRECT_RECHECK', 300, 10, 86400)
except ValueError as e:
    print(f"❌ Error: {e}")
    sys.exit(1)
# Largest list file the bot reads: MAX_IPS_PER_SCAN lines of up to 64 bytes, and never less than 2 MB.
# Ordinary IP lists are about 0.6 MB per 30,000 lines.
MAX_FILE_BYTES = max(2_000_000, MAX_IPS_PER_SCAN * 64)
UPLOAD_LIMIT = 10 * 1024 * 1024  # What Discord accepts per file, unless the server is boosted
UPLOAD_HEADROOM = 0.9            # The bot stays this far under it
# Pinged at startup to see if direct pings work from this network: one answer is enough. Direct pings
# connect to the server's IP, so these must answer that way (Hypixel, for one, routes by hostname).
PROBE_SERVERS = ('demo.mcstatus.io', 'play.cubecraft.net', 'play.wynncraft.com')
BEDROCK_PROBE_SERVERS = ('demo.mcstatus.io', 'play.cubecraft.net', 'geo.hivebedrock.network')  # Same, over UDP
INLINE_LIMIT = 1900       # Results longer than this are sent as files
GEO_DB_CHECK = 86400      # Seconds between checks of the country database's age while the bot runs
SHUTDOWN_GRACE = 35       # Seconds running scans get to post what they found when the bot is stopped (Docker allows 45)
# With the VPN, pings are sent by the pinger inside the VPN container; everything else uses this
# machine's connection. Set by docker-compose.vpn.yml; empty means the bot pings servers itself.
PINGER_URL = os.environ.get('PINGER_URL', '').strip().rstrip('/')
VPN_CHECK_DOWN = 60       # With the VPN: seconds between checks while pings through it fail
VPN_CHECK_UP = 300        # ... and while they work
VPN_SWITCH_SETTLE = 30    # Seconds to let the VPN connect after a reconnect or server change, before checking again
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

def is_ip_literal(host):
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False

def valid_port(entry):
    """False if the entry has a port outside 1-65535 (the file format allows up to 5 digits)."""
    _, sep, port = entry.partition(':')
    return not sep or 1 <= int(port) <= 65535

def dedupe_key(entry, edition='java'):
    """
    What makes two lines of a list the same server. Case and a trailing dot don't matter, and the edition's default
    port is the same as no port, except for a Java hostname: without a port, mcstatus looks up the name's SRV record,
    which can point somewhere else than port 25565 of the same name. Bedrock has no SRV records.
    """
    host, sep, port = entry.partition(':')
    host = host.lower().rstrip('.')
    if not sep:
        return host
    port = int(port)
    default = BEDROCK_PORT if edition == 'bedrock' else JAVA_PORT
    if port == default and (edition == 'bedrock' or is_ip_literal(host)):
        return host
    return f"{host}:{port}"

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
bot.direct_watcher = None  # Without the VPN: tries direct pings again while they don't work (set in on_ready)
bot.geo_watcher = None  # Keeps the country database up to date (set in on_ready)

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
        self.stop_reason = None       # 'user' for /stop, 'restart' when the bot is shutting down
        self.done = asyncio.Event()   # Set once it has finished, results posted, and been forgotten

scans = {}  # User ID -> that user's scan, queued or running
queue = []  # Scans waiting for a free slot, oldest first
shutting_down = False  # Set by shutdown(): no new scans, and queued ones don't start
shutdown_tasks = set()  # shutdown() runs started by a signal, kept so they aren't garbage collected

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
    """Hands free slots to queued scans, oldest first. Not while shutting down: they're being cancelled."""
    if shutting_down:
        return
    while queue and running_count() < MAX_CONCURRENT_SCANS:
        scan = queue.pop(0)
        scan.running = True
        scan.turn.set()

def request_stop(scan, by=None, reason='user'):
    scan.stopped_by = by
    if scan.stop_reason != 'restart':  # Once the bot is shutting down, that's what the owner needs to hear
        scan.stop_reason = reason
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
    scan.done.set()

class Pacer:
    """
    Spaces out requests to a rate-limited service. mcstatus.io and ip-api.com count requests
    per client IP, so all scans running at the same time share one pacer each. Waiters are
    served in order, which makes concurrent scans take turns.

    If the service rate-limits the bot anyway, slow_down() doubles the spacing (up to API_MAX_SLOWDOWN times), and
    it goes back to normal after API_SLOWDOWN_RESET seconds without another rate limit.
    """
    def __init__(self, name='The service'):
        self._lock = asyncio.Lock()
        self._next = 0.0
        self.name = name
        self.factor = 1
        self.penalised_at = None  # When the spacing was last made longer

    def spacing(self, interval):
        """The gap to leave after a request: `interval`, or longer for a while after a rate limit."""
        if self.factor > 1 and time.monotonic() - self.penalised_at >= API_SLOWDOWN_RESET:
            self.factor = 1
            log.info("%s hasn't rate-limited the bot for %d s; back to one request every %s s",
                     self.name, API_SLOWDOWN_RESET, show_number(interval))
        return interval * self.factor

    def slow_down(self, interval, sent_at):
        """
        The service refused a request sent at `sent_at` as one too many. Requests sent before the last slowdown went
        out at the old speed, so their refusals don't slow it down again: one burst of refusals counts once.
        """
        if self.penalised_at is not None and sent_at < self.penalised_at:
            return
        now = time.monotonic()
        self.penalised_at = now
        if self.factor < API_MAX_SLOWDOWN:
            self.factor *= 2
            log.warning("%s is rate-limiting the bot; spacing requests %s s apart until it stops",
                        self.name, show_number(interval * self.factor))
        self._next = max(self._next, now + interval * self.factor)

    async def wait(self, interval):
        async with self._lock:
            delay = self._next - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            self._next = time.monotonic() + self.spacing(interval)

class ApiHealth:
    """
    Whether mcstatus.io is answering. After API_FAILURE_THRESHOLD failures in a row (rate limits it kept up, server
    errors, timeouts), Java servers are checked through mcsrvstat.us for API_PAUSE_SECONDS; then mcstatus.io gets
    the next one again. Bedrock servers have nowhere else to go, so they keep asking mcstatus.io, and an answer to
    one of them ends the pause too.
    """
    def __init__(self):
        self.failures = 0
        self.paused_until = 0.0

    def paused(self):
        return time.monotonic() < self.paused_until

    def failed(self):
        self.failures += 1
        if self.failures >= API_FAILURE_THRESHOLD and not self.paused():
            if self.failures == API_FAILURE_THRESHOLD:  # Later pauses in the same outage aren't logged again
                log.warning("mcstatus.io failed %d checks in a row; Java servers are checked through mcsrvstat.us "
                            "for the next %d s", self.failures, API_PAUSE_SECONDS)
            self.paused_until = time.monotonic() + API_PAUSE_SECONDS

    def answered(self):
        if self.failures >= API_FAILURE_THRESHOLD:
            log.info("mcstatus.io answers again")
        self.failures = 0
        self.paused_until = 0.0

class Unchecked:
    """check_api's answer when no service could check a server: it's neither online nor offline."""
    def __bool__(self):
        return False

    def __repr__(self):
        return 'UNCHECKED'

UNCHECKED = Unchecked()

api_pacer = Pacer('mcstatus.io')
geo_pacer = Pacer('ip-api.com')
mcsrvstat_pacer = Pacer('mcsrvstat.us')
api_health = ApiHealth()  # mcstatus.io's, shared by all scans

def to_int(value):
    """
    A player count as a whole number, 0 if it's missing or not a number. Some servers send -1 to hide their counts;
    that's shown as 0 too, so the server is listed with the empty ones instead of in no list at all.
    """
    try:
        return max(int(value), 0)
    except (TypeError, ValueError, OverflowError):
        return 0

def make_result(ip, address, players, players_max, names, version, motd, edition='java', *, latency=None,
                protocol=None, secure_chat=None, modded=None, mod_count=None, software=None, plugins=None,
                eula_blocked=None, gamemode=None, map_name=None, brand=None, source=None):
    """
    One online server. The keyword fields are whatever the source reported, None when it didn't: a direct ping
    knows latency, secure chat and Forge mods; mcstatus.io knows software, plugins and whether Mojang blocks the
    server (eula_blocked); Bedrock servers report a game mode, and to a direct ping a map name and brand.
    `source` is where the answer came from: 'direct', 'mcstatus.io' or 'mcsrvstat.us'.
    """
    return {
        "ip": ip,
        "edition": EDITION_LABELS[edition],
        "address": address if isinstance(address, str) else None,  # Resolved IP, used for geolocation
        "players": to_int(players),
        "max": to_int(players_max),
        "names": names,
        "version": version or 'Unknown',
        "motd": (motd or '').strip().replace('\n', '  '),
        "latency": latency,           # Milliseconds
        "protocol": protocol,         # Protocol number of the server's version
        "secure_chat": secure_chat,   # Java: whether the server enforces signed chat
        "modded": modded,
        "mod_count": mod_count,
        "software": software,         # Paper, Velocity, ... as mcstatus.io reports it
        "plugins": plugins,           # Plugin names
        "eula_blocked": eula_blocked, # Blocked by Mojang for breaking the EULA
        "gamemode": gamemode,
        "map": map_name,
        "brand": brand,               # Bedrock: MCPE or MCEE (Education Edition)
        "source": source,
    }

# Optional fields a direct ping may report, and the type each must have. An older pinger sends none of them and
# a newer one may send more, so missing keys are None and unknown ones are dropped.
STATUS_FIELDS = {'latency': (int, float), 'protocol': int, 'mod_count': int, 'secure_chat': bool, 'modded': bool,
                 'gamemode': str, 'map': str, 'brand': str}

def status_extras(status):
    """The optional fields of a direct ping's answer, as make_result's keyword arguments."""
    extras = {key: status.get(key) for key in STATUS_FIELDS}
    extras['map_name'] = extras.pop('map')
    return extras

pinger_session = None

def get_pinger_session():
    """
    HTTP session to the pinger in the VPN container. Unlike every other session here it doesn't use
    Quad9: the pinger's name ("gluetun") only exists in Docker's own DNS.
    """
    global pinger_session
    if pinger_session is None or pinger_session.closed:
        pinger_session = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(limit=0),  # Up to DIRECT_CONCURRENCY_TOTAL pings are in flight at once
            timeout=aiohttp.ClientTimeout(total=DIRECT_TIMEOUT + 2))
    return pinger_session

def clean_status(data):
    """The pinger's answer, with every field forced to the type a result needs."""
    names = data.get('names')
    status = {"players": data.get('players'), "max": data.get('max'),
              "names": [n for n in names if isinstance(n, str)] if isinstance(names, list) else [],
              "version": data.get('version') if isinstance(data.get('version'), str) else None,
              "motd": data.get('motd') if isinstance(data.get('motd'), str) else ''}
    for key, kind in STATUS_FIELDS.items():
        value = data.get(key)
        # True is an int to Python, but not a latency or a protocol number
        typed = isinstance(value, kind) and (kind is bool or not isinstance(value, bool))
        status[key] = value if typed else None
    return status

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
    host, _, port = ip.partition(':')
    try:
        ipaddress.ip_address(host)
    except ValueError:
        # A hostname: the lookup follows SRV records, so check where the server really is below
        try:
            server = await JavaServer.async_lookup(ip, timeout=DIRECT_TIMEOUT)
        except Exception:
            return None
        host, port = server.address.host, server.address.port
    else:
        # An IP address: no SRV lookup, which would only ask Quad9 about a name that can't exist
        port = int(port) if port else JAVA_PORT
        if not 1 <= port <= 65535:
            return None

    # Connect to the checked address: pinging by name would resolve it a second time, outside Quad9 and unchecked
    try:
        address = await resolve_public_address(host)
        if address is None:
            return None
        status = await ping_server(str(address), port, 'java')
    except BlockedAddress:
        raise
    except Exception:
        return None
    if status is None:
        return None

    return make_result(ip, str(address), status['players'], status['max'],
                       status['names'], status['version'], status['motd'], **status_extras(status), source='direct')

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
                       [], status['version'], status['motd'], edition='bedrock', **status_extras(status),
                       source='direct')

async def check_api(session, ip, edition='java'):
    """
    Asks mcstatus.io about the server, and for a Java server mcsrvstat.us when mcstatus.io can't answer (or has
    failed so often lately that it's paused). Returns a result if the server is online, None if it's offline, and
    UNCHECKED if no service could check it.
    """
    if edition == 'java' and api_health.paused():
        return await check_mcsrvstat(session, ip)
    outcome = await check_mcstatus(session, ip, edition)
    if outcome is UNCHECKED and edition == 'java':
        return await check_mcsrvstat(session, ip)
    return outcome

async def check_mcstatus(session, ip, edition='java'):
    """
    Asks mcstatus.io about the server. Returns a result, None if it says the server is offline, or UNCHECKED if
    it didn't answer: it kept rate-limiting the bot, had a server error, or timed out.
    """
    # mcstatus.io waits up to its own timeout for the server; 3 s instead of its default 5 frees dead hosts sooner
    params = {'timeout': show_number(API_SERVER_TIMEOUT)}
    if edition == 'java' and not API_QUERY:
        params['query'] = 'false'
    for attempt in range(3):
        sent = time.monotonic()
        try:
            async with session.get(f"{MC_API_URLS[edition]}{ip}", params=params,
                                   timeout=aiohttp.ClientTimeout(total=10)) as response:
                status = response.status
                data = await response.json() if status == 200 else None
        except Exception:
            api_health.failed()  # Timed out, couldn't connect, or answered something that isn't JSON
            return UNCHECKED
        if status == 429:
            # Rate limited: slow every scan's requests down, and try again in turn instead of calling it offline
            api_pacer.slow_down(API_DELAY, sent)
            if attempt < 2:
                await api_pacer.wait(API_DELAY)
                continue
            api_health.failed()
            return UNCHECKED
        if status >= 500:
            api_health.failed()
            return UNCHECKED
        api_health.answered()
        if status != 200:
            return None  # mcstatus.io answered but refused this address
        try:
            return parse_api_status(ip, data, edition)
        except Exception as e:
            log.warning("Unexpected mcstatus.io response for %s: %s", ip, e)
            return None
    return UNCHECKED

async def check_mcsrvstat(session, ip):
    """
    Asks mcsrvstat.us about a Java server. Returns a result, None if it says the server is offline, or UNCHECKED if
    it doesn't answer either. It keeps answers for 5 minutes, so a server that just came online can look offline.
    """
    for attempt in range(2):
        await mcsrvstat_pacer.wait(MCSRVSTAT_DELAY)
        sent = time.monotonic()
        try:
            async with session.get(f"{MCSRVSTAT_URL}{ip}", timeout=aiohttp.ClientTimeout(total=10)) as response:
                status = response.status
                data = await response.json(content_type=None) if status == 200 else None
        except Exception:
            return UNCHECKED
        if status == 429 and attempt == 0:
            mcsrvstat_pacer.slow_down(MCSRVSTAT_DELAY, sent)
            continue
        if status != 200:
            return UNCHECKED
        try:
            return parse_mcsrvstat_status(ip, data)
        except Exception as e:
            log.warning("Unexpected mcsrvstat.us response for %s: %s", ip, e)
            return UNCHECKED
    return UNCHECKED

def parse_mcsrvstat_status(ip, data):
    """
    Turns an mcsrvstat.us (API v3) response for a Java server into a result, or None if the server is offline.
    Its offline answers still carry an `ip` (sometimes 127.0.0.1), so nothing is read from them.
    """
    if not isinstance(data, dict) or data.get('online') is not True:
        return None

    players = data.get('players')
    if not isinstance(players, dict):
        players = {}
    names = [p['name'] for p in players.get('list') or [] if isinstance(p, dict) and isinstance(p.get('name'), str)
             and p['name']]

    # The MOTD comes as a list of lines, with HTML entities (&gt;) in them
    motd = data.get('motd')
    lines = motd.get('clean') if isinstance(motd, dict) else None
    motd = "  ".join(html.unescape(line).strip() for line in lines if isinstance(line, str)) \
        if isinstance(lines, list) else None

    version = data.get('version')  # Free text, as the server sends it ("Paper 1.21.4", "We support: 1.20-1.21")
    version = re.sub('§.', '', version).strip() if isinstance(version, str) else None
    protocol = data.get('protocol')
    protocol = protocol.get('version') if isinstance(protocol, dict) else None
    mods = data.get('mods')  # Only there when the server reports mods
    plugins = data.get('plugins')
    plugins = [p['name'] for p in plugins if isinstance(p, dict) and isinstance(p.get('name'), str)] \
        if isinstance(plugins, list) else None
    software, eula_blocked = data.get('software'), data.get('eula_blocked')

    return make_result(ip, data.get('ip'), players.get('online'), players.get('max'), names, version or None, motd,
                       'java',
                       protocol=protocol if isinstance(protocol, int) and not isinstance(protocol, bool) else None,
                       modded=bool(mods) if isinstance(mods, list) else None,
                       mod_count=len(mods) if isinstance(mods, list) and mods else None,
                       software=software if isinstance(software, str) and software else None,
                       plugins=plugins,
                       eula_blocked=eula_blocked if isinstance(eula_blocked, bool) else None,
                       source='mcsrvstat.us')

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
    protocol = None
    if isinstance(version, dict):
        protocol = version.get('protocol')
        version = version.get('name_clean') or version.get('name_raw') or version.get('name')  # Bedrock only has "name"
    motd = data.get('motd')
    if isinstance(motd, dict):
        motd = motd.get('clean') or motd.get('raw')

    mods = data.get('mods')  # Java only; an empty list for a server without Forge mods
    plugins = data.get('plugins')
    plugins = [p['name'] for p in plugins if isinstance(p, dict) and isinstance(p.get('name'), str)] \
        if isinstance(plugins, list) else None
    software, eula_blocked, gamemode = data.get('software'), data.get('eula_blocked'), data.get('gamemode')
    brand = data.get('edition') if edition == 'bedrock' else None  # MCPE or MCEE

    return make_result(ip, data.get('ip_address'), players.get('online'), players.get('max'), names,
                       version if isinstance(version, str) else None,
                       motd if isinstance(motd, str) else None, edition,
                       protocol=protocol if isinstance(protocol, int) and not isinstance(protocol, bool) else None,
                       modded=bool(mods) if isinstance(mods, list) else None,
                       mod_count=len(mods) if isinstance(mods, list) and mods else None,
                       software=software if isinstance(software, str) and software else None,
                       plugins=plugins,
                       eula_blocked=eula_blocked if isinstance(eula_blocked, bool) else None,
                       gamemode=gamemode if isinstance(gamemode, str) and gamemode else None,
                       brand=brand if isinstance(brand, str) and brand else None,
                       source='mcstatus.io')

geo_db = None  # Offline country database, opened by load_geo_db() at startup
asn_db = None  # Offline network (AS) database, the same way

# DB-IP's two Lite databases: the global holding the open one, what it's for, the setting with its path, its URL
DATABASES = (('geo_db', 'country', 'GEO_DB_PATH', GEO_DB_URL),
             ('asn_db', 'network', 'GEO_ASN_DB_PATH', GEO_ASN_DB_URL))
NO_DATABASE = {'country': "No country database (%s); flags come from ip-api.com only.",
               'network': "No network database (%s); networks come from ip-api.com, for the IPs it's asked about."}

def database_paths():
    return [globals()[setting] for _, _, setting, _ in DATABASES]  # Looked up now: tests change the settings

def api_session():
    """
    An HTTP session for the outside services (mcstatus.io, ip-api.com, DB-IP, Mullvad): lookups go to Quad9 like
    everything else, and requests say what they come from (DB-IP rejects Python's default User-Agent).
    """
    return aiohttp.ClientSession(connector=aiohttp.TCPConnector(resolver=Quad9Resolver()),
                                 headers={'User-Agent': USER_AGENT})

def mmdb_age_days(path):
    """Days since the database at `path` was built, or None if it's missing or unreadable."""
    try:
        with maxminddb.open_database(path) as db:
            return (time.time() - db.metadata().build_epoch) / 86400
    except Exception:
        return None

def is_old(age):
    return age is None or age > GEO_DB_MAX_AGE_DAYS

async def update_mmdb(path, url, label):
    """
    Downloads this month's DB-IP database (`label`: country or network) to `path`, or last month's
    if this month's isn't published yet. Returns True if it saved one.
    """
    today = datetime.date.today()
    last_month = today.replace(day=1) - datetime.timedelta(days=1)
    async with api_session() as session:
        for month in (today.strftime('%Y-%m'), last_month.strftime('%Y-%m')):
            try:
                async with session.get(url.format(month=month), timeout=aiohttp.ClientTimeout(total=120)) as resp:
                    if resp.status != 200:
                        continue
                    data = gzip.decompress(await resp.read())
            except Exception as e:
                log.warning("The %s database download failed: %s", label, e)
                continue
            # Write next to the old file, then swap, so a failed write never leaves half a database
            tmp_path = path + '.tmp'
            with open(tmp_path, 'wb') as f:
                f.write(data)
            os.replace(tmp_path, path)
            log.info("Downloaded the DB-IP %s database for %s", label, month)
            return True
    return False

async def load_mmdb(name, label, path, url):
    """Opens one database into the global `name`, downloading or refreshing it first if needed."""
    if is_old(mmdb_age_days(path)):
        try:
            await update_mmdb(path, url, label)
        except OSError as e:
            # e.g. a read-only install folder; keep using the old database if there is one
            log.warning("Could not save the %s database to %s: %s", label, path, e)
    try:
        new = maxminddb.open_database(path)
        built = datetime.datetime.fromtimestamp(new.metadata().build_epoch, datetime.timezone.utc)
    except Exception as e:
        if globals()[name] is None:
            log.warning(NO_DATABASE[label], e)
        else:
            log.warning("Couldn't open the new %s database (%s); still using the old one.", label, e)
        return
    # Swap first, then close the old one, so scans looking something up right now never find no database
    old = globals()[name]
    globals()[name] = new
    if old is not None:
        old.close()
    log.info("%s database loaded (DB-IP, built %s)", label.capitalize(), built.strftime('%Y-%m-%d'))

async def load_geo_db():
    """Opens the offline country and network databases, downloading or refreshing them first if needed."""
    for (name, label, _, url), path in zip(DATABASES, database_paths()):
        await load_mmdb(name, label, path, url)

async def refresh_geo_db_if_old():
    """Loads new databases if one on disk is older than GEO_DB_MAX_AGE_DAYS or missing. True if it tried."""
    old = [path for path in database_paths() if is_old(mmdb_age_days(path))]
    if not old:
        return False
    if not any(os.access(os.path.dirname(path) or '.', os.W_OK) for path in old):
        return False  # Docker: the databases are part of the image, which the weekly rebuild keeps fresh
    await load_geo_db()
    return True

async def watch_geo_db():
    """Checks the country database once a day, so a bot that runs for months still gets DB-IP's monthly updates."""
    while True:
        await asyncio.sleep(GEO_DB_CHECK)
        try:
            await refresh_geo_db_if_old()
        except Exception:
            log.exception("Checking the country database failed")

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

def lookup_networks(ips):
    """
    Looks up networks in the offline database. Returns {ip: (AS number, organisation)} for the IPs it knows.
    """
    found = {}
    if asn_db is None:
        return found
    for ip in ips:
        try:
            record = asn_db.get(ip)
        except ValueError:  # Not an IP address
            continue
        number = (record or {}).get('autonomous_system_number')
        if isinstance(number, int):
            name = record.get('autonomous_system_organization')
            found[ip] = (number, name if isinstance(name, str) else '')
    return found

AS_FIELD = re.compile(r'^AS(\d+)\s*(.*)$')

def parse_as(value, isp=None):
    """ip-api.com's "as" field, like "AS8400 Telekom Srbija a.d.", as (8400, 'Telekom Srbija a.d.'), or None."""
    match = AS_FIELD.match(value.strip()) if isinstance(value, str) else None
    if not match:
        return None
    name = match.group(2).strip() or (isp.strip() if isinstance(isp, str) else '')
    return int(match.group(1)), name

async def batch_get_locations(session, ips, stop=None, networks=None):
    """
    Uses ip-api.com batch endpoint to get locations for a list of IPs.
    Max 100 IPs per request. With `networks` (a dict), also asks for each IP's network and adds the
    ones `networks` doesn't have yet as {ip: (AS number, name)}.
    """
    stop = stop or asyncio.Event()
    locations = {}
    if not ips:
        return locations

    # Split into chunks of 100
    chunks = [ips[i:i + 100] for i in range(0, len(ips), 100)]

    fields = "query,countryCode,as,isp" if networks is not None else "query,countryCode"
    payloads = [[{"query": ip, "fields": fields} for ip in chunk] for chunk in chunks]
    for payload in payloads:
        # A rate limit, a server error or a network hiccup gets one more try; after that, those IPs get no flag
        for attempt in range(2):
            # Other scans may be using ip-api.com too, so wait for our turn
            await geo_pacer.wait(GEO_DELAY)
            if stop.is_set():
                return locations
            try:
                async with session.post(GEO_BATCH_URL, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        for entry in await resp.json():
                            # Entry looks like: {"query": "1.2.3.4", "countryCode": "US", "as": "AS3320 ...", ...}
                            if entry.get('query'):
                                locations[entry['query']] = entry.get('countryCode')
                                network = parse_as(entry.get('as'), entry.get('isp'))
                                if networks is not None and network:
                                    networks.setdefault(entry['query'], network)
                        break
                    problem = f"HTTP {resp.status}"
                    retry = resp.status == 429 or resp.status >= 500
            except Exception as e:
                problem, retry = str(e) or type(e).__name__, True
            if retry and attempt == 0:
                log.info("Geolocation failed (%s); retrying once", problem)
                continue
            log.warning("Geolocation failed: %s", problem)
            break

    return locations

def parse_ips(text, edition='java'):
    """
    Returns (unique valid addresses in file order, invalid line count, duplicate count,
    private or local address count). Blank lines and lines starting with # are ignored, ports must be 1-65535,
    and of two lines for the same server (see dedupe_key) the first one is kept as it was written.
    """
    lines = [line.strip() for line in text.splitlines()]
    lines = [line for line in lines if line and not line.startswith('#')]
    valid = [line for line in lines if ADDRESS_RE.match(line) and valid_port(line)]
    public = [line for line in valid if is_public_entry(line)]
    unique = {}
    for line in public:
        unique.setdefault(dedupe_key(line, edition), line)
    return list(unique.values()), len(lines) - len(valid), len(public) - len(unique), len(valid) - len(public)

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
    if shutting_down:
        await set_status("Restarting · back in a minute")
        return
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
    if message is None:  # It couldn't be posted
        return
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

direct_slots_state = None  # ((event loop, size), semaphore) behind direct_slots()

def direct_slots():
    """
    The bot-wide limit on direct pings in flight, DIRECT_CONCURRENCY_TOTAL, shared by all scans. Every ping to an
    address that doesn't answer leaves a connection-tracking entry behind for a while (in Docker's NAT and in the
    VPN container), and too many of those make the machine drop packets, Discord's included.
    """
    global direct_slots_state
    key = (asyncio.get_running_loop(), DIRECT_CONCURRENCY_TOTAL)  # Tests run each in a loop of its own
    if direct_slots_state is None or direct_slots_state[0] != key:
        direct_slots_state = (key, asyncio.Semaphore(DIRECT_CONCURRENCY_TOTAL))
    return direct_slots_state[1]

async def run_direct(ips, results, state, edition='java', stop=None):
    """
    Pings every server directly: DIRECT_CONCURRENCY workers, each taking the next server from the list, and at most
    DIRECT_CONCURRENCY_TOTAL pings in flight across all scans. Returns the IPs that didn't answer, in file order.
    """
    stop = stop or asyncio.Event()
    state.update(phase="Pinging servers", done=0, total=len(ips))
    slots = direct_slots()
    pinged = set()
    check_server = check_direct if edition == 'java' else check_direct_bedrock
    remaining = iter(ips)  # Shared by the workers. next() can't be interrupted, so each server is taken once

    async def worker():
        for ip in remaining:
            if stop.is_set():
                return
            async with slots:
                if stop.is_set():
                    return
                try:
                    result = await check_server(ip)
                except BlockedAddress:
                    # Not pinged, and not passed on to the API either
                    state['done'] += 1
                    state['blocked'] += 1
                    continue
            state['done'] += 1
            pinged.add(ip)
            if result:
                results[ip] = result
                state['found'] += 1

    await asyncio.gather(*(worker() for _ in range(min(DIRECT_CONCURRENCY, len(ips)))))
    return [ip for ip in ips if ip in pinged and ip not in results]

async def run_api(session, ips, results, state, retrying, edition='java', stop=None, vpn_down=False):
    """
    Checks servers through mcstatus.io (and Java servers it can't check through mcsrvstat.us), starting one request
    every API_DELAY seconds. Scans running at the same time take turns, so together they stay within the limit.
    Servers no service could check are counted in state['unchecked'], not as offline.
    """
    stop = stop or asyncio.Event()
    phase = "Retrying unreachable servers via API" if retrying else "Checking servers via API"
    if vpn_down:
        phase += " (VPN down)"
    state.update(phase=phase, done=0, total=len(ips))

    async def check(ip):
        result = await check_api(session, ip, edition)
        state['done'] += 1
        if result is UNCHECKED:
            state['unchecked'] = state.get('unchecked', 0) + 1
        elif result:
            results[ip] = result
            state['found'] += 1

    tasks = []
    for ip in ips:
        await api_pacer.wait(API_DELAY)
        if stop.is_set():
            break
        tasks.append(asyncio.create_task(check(ip)))

    # Wait for the last checks, but not after a stop: one check can take half a minute (timeouts and retries), and
    # a stop for a restart has to post its results within SHUTDOWN_GRACE
    finished = asyncio.gather(*tasks, return_exceptions=True)
    stopped = asyncio.create_task(stop.wait())
    await asyncio.wait((finished, stopped), return_when=asyncio.FIRST_COMPLETED)
    stopped.cancel()
    if stop.is_set():
        # Cancel checks still in flight before the session closes
        for t in tasks:
            t.cancel()
    await finished

def details_line(r, networks=None):
    """The .txt report's extra line: network, protocol, software, mods and the like, whatever is known."""
    parts = []
    asn, as_name = (networks or {}).get(r['address']) or (None, None)
    if asn is not None:
        parts.append(f"AS{asn} {as_name}".strip())
    if r.get('protocol') is not None:
        parts.append(f"protocol {r['protocol']}")
    for key in ('software', 'brand', 'gamemode'):
        if r.get(key):
            parts.append(r[key])
    if r.get('map'):
        parts.append(f"map {r['map']}")
    if r.get('secure_chat'):
        parts.append("secure chat")
    if r.get('mod_count'):
        parts.append(f"{r['mod_count']} mods")
    elif r.get('modded'):
        parts.append("modded")
    if r.get('plugins'):
        parts.append(f"{len(r['plugins'])} plugins")
    if r.get('eula_blocked'):
        parts.append("blocked by Mojang")
    return " · ".join(parts)

def format_entry(r, locations, markdown=True, networks=None):
    bold = (lambda s: f"**{s}**") if markdown else (lambda s: s)
    motd = discord.utils.escape_markdown(r['motd']) if markdown else r['motd']
    names = ", ".join(r['names'])
    if markdown:
        names = discord.utils.escape_markdown(names)

    ip, version = r['ip'], r['version']
    if markdown:  # Both come from the list or the server, and names like play_server_1 would turn italic
        ip, version = discord.utils.escape_markdown(ip), discord.utils.escape_markdown(version)
    text = f"{get_flag_emoji(locations.get(r['address']))} {bold(ip)} | Players: {r['players']}/{r['max']} | Ver: {version}"
    if isinstance(r.get('latency'), (int, float)):
        text += f" · {round(r['latency'])} ms"
    if motd: text += f"\n   └ 📝 {motd}"
    if names: text += f"\n   └ 👤 {bold('Users:')} {names}"
    details = "" if markdown else details_line(r, networks)  # Chat stays short; the file has room
    if details: text += f"\n   └ 🌐 {details}"
    return text

# The results files' columns, in order. The first nine were the only ones until October 2026, so new ones go at the
# end. The CSV and the JSON file use the same names; `players` holds the names, `players_online` the count.
COLUMNS = ('ip', 'resolved_ip', 'country', 'players_online', 'players_max', 'version', 'motd', 'players', 'edition',
           'latency_ms', 'protocol', 'secure_chat', 'modded', 'mod_count', 'software', 'plugins', 'eula_blocked',
           'gamemode', 'map', 'brand', 'asn', 'as_name', 'source')

def result_row(r, locations, networks=None):
    """One result under the files' column names. Lists stay lists, and anything unknown is None."""
    asn, as_name = (networks or {}).get(r['address']) or (None, None)
    return {
        'ip': r['ip'], 'resolved_ip': r['address'], 'country': locations.get(r['address']) or None,
        'players_online': r['players'], 'players_max': r['max'], 'version': r['version'], 'motd': r['motd'],
        'players': r['names'], 'edition': r['edition'], 'latency_ms': r.get('latency'), 'protocol': r.get('protocol'),
        'secure_chat': r.get('secure_chat'), 'modded': r.get('modded'), 'mod_count': r.get('mod_count'),
        'software': r.get('software'), 'plugins': r.get('plugins'), 'eula_blocked': r.get('eula_blocked'),
        'gamemode': r.get('gamemode'), 'map': r.get('map'), 'brand': r.get('brand'), 'asn': asn,
        'as_name': as_name or None, 'source': r.get('source'),
    }

def csv_value(value):
    """A CSV cell: empty for unknown, true/false, lists joined with "; ", text defused (see csv_cell)."""
    if value is None:
        return ''
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, list):
        value = "; ".join(value)
    return csv_cell(value)

def csv_cell(value):
    """
    Spreadsheets run cells that start with = + - @ as formulas, and MOTDs, versions and player names
    come from strangers' servers. A leading ' makes the cell plain text.
    """
    if isinstance(value, str) and value.startswith(('=', '+', '-', '@', '\t', '\r')):
        return "'" + value
    return value

def csv_line(values):
    buffer = io.StringIO()
    csv.writer(buffer).writerow(values)
    return buffer.getvalue()

def report_rows(populated, empty, locations, networks=None):
    """
    Returns (text rows, CSV header, CSV rows, JSON rows) of the results. Each row is a whole entry, so a report
    that is too big for one upload can be split between rows.
    """
    txt_rows = []
    if populated:
        txt_rows.append(f"Servers with Players ({len(populated)}):\n")
        txt_rows += [format_entry(r, locations, markdown=False, networks=networks) + "\n" for r in populated]
        txt_rows.append("\n")
    if empty:
        txt_rows.append(f"Online (Empty) Servers ({len(empty)}):\n")
        txt_rows += [format_entry(r, locations, markdown=False, networks=networks) + "\n" for r in empty]

    rows = [result_row(r, locations, networks) for r in populated + empty]
    header = csv_line(COLUMNS)
    csv_rows = [csv_line([csv_value(row[column]) for column in COLUMNS]) for row in rows]
    json_rows = [json.dumps(row, ensure_ascii=False) for row in rows]
    return txt_rows, header, csv_rows, json_rows

def split_rows(filename, header, rows, limit, joiner='', footer=''):
    """
    Returns [(filename, bytes), ...]: the rows as one file, or as several when they wouldn't fit in `limit`
    bytes. Only splits between rows, and every part is header + rows joined by `joiner` + footer (so each part of
    a JSON array is a valid array too). Parts are named name_1.ext, name_2.ext, ...
    """
    header_bytes, joiner_bytes, footer_bytes = header.encode('utf-8'), joiner.encode('utf-8'), footer.encode('utf-8')
    fixed = len(header_bytes) + len(footer_bytes)
    parts, current, size = [], [], fixed
    for row in rows:
        data = row.encode('utf-8')
        added = len(data) + (len(joiner_bytes) if current else 0)
        if current and size + added > limit:
            parts.append(current)
            current, size, added = [], fixed, len(data)
        current.append(data)
        size += added
    parts.append(current)
    files = [header_bytes + joiner_bytes.join(part) + footer_bytes for part in parts]
    if len(files) == 1:
        return [(filename, files[0])]
    stem, extension = os.path.splitext(filename)
    return [(f"{stem}_{number}{extension}", data) for number, data in enumerate(files, 1)]

def batch_files(files, limit):
    """Groups (filename, bytes) files, in order, into messages that each stay within `limit` bytes in total."""
    batches, size = [], 0
    for file in files:
        if batches and size + len(file[1]) <= limit:
            batches[-1].append(file)
            size += len(file[1])
        else:
            batches.append([file])
            size = len(file[1])
    return batches

def upload_limit(ctx):
    """How many bytes the bot may upload in one message here: the server's limit (higher if boosted), minus headroom."""
    limit = getattr(ctx.guild, 'filesize_limit', None)  # No guild in DMs
    if not isinstance(limit, int):
        limit = UPLOAD_LIMIT
    return int(limit * UPLOAD_HEADROOM)

def build_file_batches(populated, empty, locations, limit, networks=None):
    """Returns the results files as batches of discord.File, one batch per message, each within `limit` bytes."""
    txt_rows, header, csv_rows, json_rows = report_rows(populated, empty, locations, networks)
    files = (split_rows("scan_results.txt", "", txt_rows, limit)
             + split_rows("scan_results.csv", header, csv_rows, limit)
             + split_rows("scan_results.json", "[\n", json_rows, limit, joiner=",\n", footer="\n]\n"))
    return [[discord.File(io.BytesIO(data), filename=name) for name, data in batch]
            for batch in batch_files(files, limit)]

def build_files(populated, empty, locations, networks=None):
    """Returns a readable .txt report, a .csv and a .json of every online server."""
    return build_file_batches(populated, empty, locations, float('inf'), networks)[0]

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
        try:
            return await ctx.send(*args, **kwargs)
        except discord.HTTPException as e:
            log.warning("Couldn't post in %s, and the command's reply no longer works either (%s); message lost",
                        getattr(ctx.channel, 'name', None) or 'a DM', e)
            return None

def speed_text(direct, api):
    """
    The results' speed line: servers checked per second, timed only while pinging and asking the API.
    Geolocation, the VPN check and sending messages aren't part of it, and when both phases ran, each one's
    own rate is shown too (the API phase is capped at 5 a second, shared by all scans, so it sets the pace).
    `direct` and `api` are (servers checked, seconds) for a phase that ran, None for one that didn't.
    Returns '' when there's nothing to show.
    """
    ran = {name: phase for name, phase in (('direct', direct), ('API', api)) if phase and phase[1] > 0}
    if not ran:
        return ''
    checked = next(iter(ran.values()))[0]  # Every server goes through the first phase that ran
    text = f"⚡ **Speed:** {checked / sum(seconds for _, seconds in ran.values()):.2f} IPs/sec"
    if len(ran) == 2:
        text += " (" + " · ".join(f"{name} {count / seconds:.2f}/s" for name, (count, seconds) in ran.items()) + ")"
    elif 'API' in ran:
        text += " (API)"
    return text

async def send_results(ctx, results, locations, stopped, total_ips, duration, blocked=0, edition='java', owner=None,
                       vpn_down=False, direct=None, api=None, not_retried=0, stop_reason=None, error=False,
                       networks=None, unchecked=0):
    minutes = int(duration // 60)
    seconds = int(duration % 60)

    populated = sorted((r for r in results if r['players'] > 0), key=lambda r: r['players'], reverse=True)
    empty = [r for r in results if r['players'] == 0]

    if error:
        title = "⚠️ **Scan failed** — partial results"
    else:
        title = "🛑 **Scan stopped** — partial results" if stopped else "📊 **Scan Complete!**"
    if edition != 'java':
        title += f" ({EDITION_LABELS[edition]})"
    if owner:
        title += f" · {owner}"  # Several people may be scanning in the same channel
    summary = (f"{title}\n🟢 {len(populated)} with players · ⚪ {len(empty)} empty · 🔎 {total_ips} IPs\n"
               f"⏱️ **Time:** {minutes}m {seconds}s")
    speed = speed_text(direct, api)
    if speed and not stopped:
        summary += f"\n{speed}"
    if not_retried:
        was = "wasn't" if not_retried == 1 else "weren't"
        summary += f"\nℹ️ {not_retried} didn't answer a direct ping and {was} retried through the API"
    if unchecked:
        services = "mcstatus.io and mcsrvstat.us" if edition == 'java' else "mcstatus.io"
        summary += f"\n❔ {unchecked} couldn't be checked: {services} didn't answer, so they aren't counted as offline"
    if blocked:
        summary += f"\n🚫 {blocked} name(s) pointed at private or local addresses and were skipped"
    if vpn_down:
        summary += "\n⚠️ The VPN was down: every server was checked through the API only"
    if error:
        summary += "\n⚠️ The bot hit an error and stopped the scan early (details are in its log)."
    if stopped and stop_reason == 'restart':
        summary += ("\n🔄 The bot is restarting (usually for an update), so the rest of the list wasn't checked. "
                    "Scan it again in a minute.")

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
    top = [f"{get_flag_emoji(locations.get(r['address']))} **{discord.utils.escape_markdown(r['ip'])}** | "
           f"{r['players']}/{r['max']}"
           for r in populated[:10]]
    message = summary
    if top:
        message += "\n\n**Top servers:**\n" + "\n".join(top)
    batches = build_file_batches(populated, empty, locations, upload_limit(ctx), networks)
    message += "\n\n📎 Full results are in the attached files."
    if len(batches) > 1:
        message += f" They're too big for one message, so they come in {len(batches)}."
    await send_channel(ctx, message[:2000], files=batches[0])
    for number, files in enumerate(batches[1:], 2):
        await send_channel(ctx, f"📎 Results, continued ({number}/{len(batches)})", files=files)

async def probe_direct(edition='java'):
    """True if a direct ping to any of the probe servers gets an answer."""
    servers, check_server = (PROBE_SERVERS, check_direct) if edition == 'java' else (BEDROCK_PROBE_SERVERS, check_direct_bedrock)

    async def probe(server):
        try:
            return await check_server(server) is not None
        except BlockedAddress:
            return False

    return any(await asyncio.gather(*(probe(server) for server in servers)))

def every(seconds):
    """'every 5 minutes', 'every 90 seconds'."""
    if seconds % 60 == 0:
        minutes = seconds // 60
        return "every minute" if minutes == 1 else f"every {minutes} minutes"
    return f"every {seconds} seconds"

async def recheck_direct():
    """
    Without the VPN: probes the editions whose direct pings don't work, and switches each one back on when it answers
    (the network was down when the bot started, a firewall rule changed). Returns the editions that work now.
    """
    failed = [edition for edition, ok in (('java', bot.direct_ok), ('bedrock', bot.direct_ok_bedrock)) if not ok]
    answered = await asyncio.gather(*(probe_direct(edition) for edition in failed))
    bot.probed_at = time.monotonic()
    working = [edition for edition, ok in zip(failed, answered) if ok]
    for edition in working:
        if edition == 'java':
            bot.direct_ok = True
            log.info("Direct pings work now; scans use direct pings with API fallback again.")
        else:
            bot.direct_ok_bedrock = True
            log.info("Direct Bedrock (UDP) pings work now; Bedrock scans use direct pings with API fallback again.")
    return working

async def watch_direct():
    """Without the VPN: tries direct pings again every DIRECT_RECHECK seconds until both editions work."""
    while not (bot.direct_ok and bot.direct_ok_bedrock):
        await asyncio.sleep(DIRECT_RECHECK)
        try:
            await recheck_direct()
        except Exception:
            log.exception("Checking direct pings again failed")

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
    async with api_session() as session:
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

async def reconnect_vpn():
    """
    Makes gluetun connect to its Mullvad server again: stops the VPN, then starts it. With server
    switching, gluetun doesn't do this by itself (HEALTH_RESTART_VPN=off, see vpn_switch.py).
    """
    for status in ("stopped", "running"):
        async with get_pinger_session().put(f"{GLUETUN_URL}/v1/vpn/status", json={"status": status},
                                            headers={"X-API-Key": GLUETUN_API_KEY},
                                            timeout=aiohttp.ClientTimeout(total=60)) as response:
            if response.status in (401, 403):
                raise PermissionError("gluetun doesn't let this key reconnect the VPN yet. Run the installer again")
            response.raise_for_status()

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
    return vpn_switch.MullvadSwitcher(cities, fetch_mullvad_relays, set_vpn_server, reconnect=reconnect_vpn)

async def switch_vpn_server():
    """
    With Mullvad: reconnects the VPN, or moves it to another server, if the current one is listed as
    offline or doesn't let pings through. Returns how many seconds to wait before the next VPN check.
    """
    wait = VPN_CHECK_DOWN if vpn_down() else VPN_CHECK_UP
    if bot.vpn_switcher is not None:
        try:
            if await bot.vpn_switcher.tick(vpn_ok=not vpn_down()):
                wait = VPN_SWITCH_SETTLE  # Check again soon, so scans use the VPN again quickly
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
            log.warning("Direct pings to %s all failed; scans use the mcstatus.io API only (5 checks/second), "
                        "trying again %s.", ", ".join(PROBE_SERVERS), every(DIRECT_RECHECK))
        if bot.direct_ok_bedrock:
            log.info("Direct Bedrock (UDP) pings work; Bedrock scans use direct pings with API fallback.")
        else:
            log.warning("Direct Bedrock pings to %s all failed; Bedrock scans use the mcstatus.io API only "
                        "(5 checks/second), trying again %s.", ", ".join(BEDROCK_PROBE_SERVERS), every(DIRECT_RECHECK))
        if not (bot.direct_ok and bot.direct_ok_bedrock):
            bot.direct_watcher = asyncio.create_task(watch_direct())
    if bot.geo_watcher is None:
        bot.geo_watcher = asyncio.create_task(watch_geo_db())
    log.info("Logged in as %s", bot.user.name)
    await update_presence()

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.MissingRequiredAttachment):
        await ctx.send("❌ Please attach a `.txt` file.")
    elif isinstance(error, commands.BadLiteralArgument):
        if error.param.name == 'api':
            await ctx.send("❌ The API option must be `on` or `off`, for example `!scan java off`.")
        else:
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
        name="/scan file:<.txt> [edition] [api]  (or !scan [edition] [api])",
        value="Scans a list of Minecraft server IPs from a `.txt` file. `edition` is `java` (default) or `bedrock`.\n"
              "`api` is `on` (default) or `off`: with `off`, a server that doesn't answer a direct ping counts as "
              "offline instead of being retried through mcstatus.io, which is much faster for long lists of "
              "mostly dead addresses.\n"
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
                          "Country flags and networks: IP Geolocation by DB-IP (db-ip.com)")
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
                       edition="Minecraft edition of the servers in the file (default: java)",
                       api="off: servers that don't answer a direct ping count as offline, no mcstatus.io retry (default: on)")
async def check(ctx, file: discord.Attachment, edition: Literal['java', 'bedrock'] = 'java',
                api: Literal['on', 'off'] = 'on'):
    await ctx.defer()  # A slash command must be answered within 3 seconds

    # No await between these checks and registering the scan, so nobody can start two at once, and shutdown()
    # sees every scan that got past the first one
    if shutting_down:
        await ctx.send("⏳ **The bot is restarting.** Try again in a minute.")
        return
    if ctx.author.id in scans:
        await ctx.send("⏳ **You already have a scan running or queued.** Use `/stop` to stop it first.")
        return
    scan = Scan(ctx.author, ctx.guild.id if ctx.guild else None)
    scans[ctx.author.id] = scan
    try:
        await run_scan(ctx, scan, file, edition, api_retry=(api == 'on'))
    finally:
        release(scan)
        await update_presence()

def no_direct_reply():
    """Why api:off can't run: it only checks servers with direct pings, and those don't work right now."""
    if PINGER_URL:
        return ("❌ `api:off` would check nothing: the VPN is down, so direct pings aren't working right now. "
                "Try again in a minute, or scan with the API on (slower).")
    return ("❌ `api:off` would check nothing: direct pings don't work from this network. "
            "Scan with the API on instead (5 servers per second).")

async def run_scan(ctx, scan, file, edition, api_retry=True):
    # --- File Input --- (checked before queueing, so a bad file is rejected right away)
    if not file.filename.lower().endswith('.txt'):
        await ctx.send("❌ Must be a `.txt` file.")
        return
    if file.size > MAX_FILE_BYTES:
        await ctx.send(f"❌ That file is too big ({file.size // 1000:,} KB). The limit is {MAX_FILE_BYTES // 1_000_000} MB.")
        return

    try:
        content = await file.read()
    except (discord.HTTPException, aiohttp.ClientError, asyncio.TimeoutError) as e:
        log.warning("Couldn't download %s from Discord: %s", file.filename, e)
        await ctx.send("❌ Couldn't download the attachment from Discord. Please try again.")
        return
    try:
        text = content.decode('utf-8-sig')  # -sig: some editors add a BOM
    except UnicodeDecodeError:
        await ctx.send("❌ The file isn't UTF-8 text. Save it as plain UTF-8 text, one server per line.")
        return
    ips, invalid, duplicates, blocked = parse_ips(text, edition)

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

    # api:off only checks servers with direct pings, so there's nothing to do while they don't work. Checked before
    # queueing, so nobody waits in line for a refusal, and again once it's their turn.
    if not api_retry and not await direct_pings_work(edition):
        await ctx.send(no_direct_reply())
        return

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
        if scan.stop_reason == 'restart':
            note = (f"🛑 {ctx.author.mention}, your {'queued ' if place else ''}scan was cancelled because the bot "
                    "is restarting. Start it again in a minute.")
            # A queued scan's slash command reply may have stopped working (15 minutes)
            await (send_channel(ctx, note) if place else ctx.send(note))
        elif not place:
            await ctx.send("🛑 **Scan cancelled.**")
        elif stopped_by is not None and stopped_by.id != ctx.author.id:
            await send_channel(ctx, f"🛑 {ctx.author.mention}, your queued scan was cancelled by {stopped_by.mention}.")
        return

    start_time = time.monotonic()  # Not time.time(): a clock adjustment mid-scan must not change the duration
    # Direct pings, unless the startup probe failed. With the VPN, pings only ever go through it:
    # while it's down, every server is checked through the API instead.
    direct_ok = await direct_pings_work(edition)
    if not api_retry and not direct_ok:
        # They stopped working while this scan was queued
        if place:
            await send_channel(ctx, no_direct_reply())
        else:
            await ctx.send(no_direct_reply())
        return
    no_vpn = bool(PINGER_URL) and not direct_ok
    owner = ctx.author.mention  # Renders as a name without pinging (mentions are switched off)
    label = "Bedrock " if edition == 'bedrock' else ""
    started = f"🚀 **Scan started** by {owner} on {total_ips} {label}IPs{extra}..."
    if not api_retry:
        started += "\nℹ️ **API retry is off:** a server that doesn't answer a direct ping counts as offline."
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
    networks = {}
    direct = api = None  # (servers checked, seconds) for each phase that ran, for the speed line
    retry = []
    error = False

    try:
        async with api_session() as session:
            # 1. Direct pings, many at once
            if direct_ok:
                began = time.monotonic()
                retry = await run_direct(ips, results, state, edition, stop=scan.stop)
                direct = (len(ips) - state['blocked'], time.monotonic() - began)
            else:
                retry = ips

            # 2. mcstatus.io API for everything that didn't answer, 5 per second shared by all scans (Java servers it
            # can't check go to mcsrvstat.us)
            if retry and api_retry and not scan.stop.is_set():
                began = time.monotonic()
                await run_api(session, retry, results, state, retrying=bool(direct_ok), edition=edition,
                              stop=scan.stop, vpn_down=no_vpn)
                api = (len(retry), time.monotonic() - began)

            # 3. Countries and networks: offline databases first (instant), ip-api.com for what they don't know.
            # The network database only counts when it's open, so a failed download doesn't send every server
            # to ip-api.com.
            addresses = sorted({r['address'] for r in results.values() if r['address']})
            locations = lookup_countries(addresses)
            networks = lookup_networks(addresses)
            unknown = [a for a in addresses if a not in locations or (asn_db is not None and a not in networks)]
            if unknown and not scan.stop.is_set():
                state.update(phase="Resolving locations", done=len(addresses) - len(unknown), total=len(addresses))
                found = await batch_get_locations(session, unknown, stop=scan.stop, networks=networks)
                for address, code in found.items():
                    locations.setdefault(address, code)  # The offline database's answer wins
                state['done'] = len(addresses)
    except Exception:
        # Exception, not BaseException: cancelling the task (the bot shutting down hard) must still cancel it
        error = True
        log.exception("Scan by %s failed while %s (%d/%d); posting the %d server(s) found so far",
                      ctx.author, state['phase'].lower(), state['done'], state['total'], len(results))
    finally:
        updater.cancel()

    stopped = scan.stop.is_set() or error
    stop_reason = scan.stop_reason if scan.stop.is_set() else None
    if error:
        head = "⚠️ **Error:** the scan stopped early."
    elif stop_reason == 'restart':
        head = "🛑 **Stopped:** the bot is restarting."
    else:
        head = "🛑 **Stopped.**" if stopped else "✅ **Done.**"
    if progress is not None:
        try:
            await progress.edit(content=f"{head} {owner} found {state['found']} online servers.")
        except discord.HTTPException:
            pass

    # With api:off, the servers that didn't answer a direct ping are offline as far as this scan knows. (A stopped
    # scan doesn't know how many never got pinged, so it says nothing.)
    not_retried = len(retry) if not api_retry and not stopped else 0
    await send_results(ctx, list(results.values()), locations, stopped, total_ips, time.monotonic() - start_time,
                       blocked=state['blocked'], edition=edition, owner=owner, vpn_down=no_vpn,
                       direct=direct, api=api, not_retried=not_retried, stop_reason=stop_reason, error=error,
                       networks=networks, unchecked=state.get('unchecked', 0))

async def shutdown(reason, grace=SHUTDOWN_GRACE, close=None):
    """
    Stops the bot cleanly on SIGTERM (Docker, Watchtower, systemd) or SIGINT (Ctrl+C). New scans are refused,
    queued ones are cancelled and their owners told, and running ones stop the way /stop stops them: each posts
    what it found so far. After at most `grace` seconds of that, the bot disconnects from Discord and main()
    returns. A second signal disconnects at once. `close` is for tests; it defaults to bot.close.
    """
    global shutting_down
    close = close or bot.close
    if shutting_down:
        log.warning("%s again: closing now", reason)
        await close()
        return
    shutting_down = True
    pending = list(scans.values())
    log.warning("%s: shutting down. Stopping %d running and %d queued scan(s) first", reason,
                sum(1 for s in pending if s.running), len(queue))
    for scan in pending:
        if not scan.stop.is_set():  # One its owner or a moderator already stopped keeps that reason
            request_stop(scan, reason='restart')
    await update_presence()
    if pending:
        try:
            await asyncio.wait_for(asyncio.gather(*(s.done.wait() for s in pending)), timeout=grace)
        except asyncio.TimeoutError:
            late = ", ".join(str(s.owner.id) for s in pending if not s.done.is_set())
            log.warning("Scans of user(s) %s didn't finish posting within %s s; closing anyway", late, grace)
    await close()

def install_signal_handlers(loop):
    """
    Makes SIGTERM and SIGINT run shutdown(). Without this, Docker's stop signal ends the bot mid-scan with no results.
    Windows' event loop can't catch signals; there Ctrl+C stops the bot at once, as before.
    """
    def handle(sig):
        task = loop.create_task(shutdown(sig.name))
        shutdown_tasks.add(task)
        task.add_done_callback(shutdown_tasks.discard)

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, handle, sig)
        except (NotImplementedError, RuntimeError):
            log.debug("Can't catch %s on this platform", sig.name)

def check_settings():
    """Logs the direct ping settings at startup, and warns about the ones likely to cause trouble."""
    log.info("Direct pings: up to %d per scan (%d for all scans together), %s s timeout",
             DIRECT_CONCURRENCY, DIRECT_CONCURRENCY_TOTAL, show_number(DIRECT_TIMEOUT))
    log.info("API checks: mcstatus.io every %s s (query %s, %d s server timeout); Java servers it can't check go to "
             "mcsrvstat.us, every %s s", show_number(API_DELAY), "on" if API_QUERY else "off", API_SERVER_TIMEOUT,
             show_number(MCSRVSTAT_DELAY))
    if API_DELAY < 0.2:
        log.warning("API_DELAY is %s: below 0.2, mcstatus.io rate-limits the bot, which makes scans slower, not faster",
                    show_number(API_DELAY))
    # Each ping in flight holds a socket, and with the VPN a connection to the pinger as well
    wanted = 2 * min(DIRECT_CONCURRENCY_TOTAL, MAX_CONCURRENT_SCANS * DIRECT_CONCURRENCY) + 1024
    limits = pinger.raise_file_limit(wanted)
    if limits and limits[0] < wanted:
        log.warning("Only %d files can be open at once, and these settings need about %d: pings will fail and count "
                    "as offline. Raise the limit (ulimits in docker-compose.yml, LimitNOFILE in systemd) or lower "
                    "DIRECT_CONCURRENCY", limits[0], wanted)

async def main():
    discord.utils.setup_logging(root=True)
    check_settings()
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
            install_signal_handlers(asyncio.get_running_loop())
            await bot.start(TOKEN)
    finally:
        if pinger_session is not None:
            await pinger_session.close()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
