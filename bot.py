import discord
from discord.ext import commands
from mcstatus import JavaServer
import aiohttp
from aiohttp.abc import AbstractResolver
import asyncio
import csv
import dns.asyncresolver
import dns.exception
import dns.nameserver
import dns.resolver
import io
import ipaddress
import logging
import os
import re
import socket
import sys
import time

# --- CONFIGURATION ---
MC_API_URL = 'https://api.mcstatus.io/v2/status/java/'
GEO_BATCH_URL = 'http://ip-api.com/batch' # Using batch endpoint
MAX_IPS_PER_SCAN = 5000
DIRECT_CONCURRENCY = 50   # Direct pings running at the same time
DIRECT_TIMEOUT = 3        # Seconds to wait for a server to answer a direct ping
API_DELAY = 0.2           # mcstatus.io allows 5 requests/second per client IP
GEO_DELAY = 4             # ip-api.com batch allows 15 requests/minute
PROBE_SERVER = 'mc.hypixel.net'  # Pinged at startup to see if direct pings work from this network
PROGRESS_INTERVAL = 3     # Seconds between progress message updates
INLINE_LIMIT = 1900       # Results longer than this are sent as files
# ---------------------

log = logging.getLogger('scanbot')

# --- DNS: every lookup goes to Quad9 over DNS-over-TLS (port 853) ---
QUAD9 = dns.asyncresolver.Resolver(configure=False)
QUAD9.nameservers = [
    dns.nameserver.DoTNameserver('9.9.9.9', hostname='dns.quad9.net'),
    dns.nameserver.DoTNameserver('149.112.112.112', hostname='dns.quad9.net'),
]
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

def get_flag_emoji(country_code):
    if not country_code:
        return "🏳️"
    return "".join([chr(ord(c.upper()) + 127397) for c in country_code])

intents = discord.Intents.default()
intents.message_content = True
intents.dm_messages = True

# MOTDs and player names come from strangers' servers, so never let them ping anyone
bot = commands.Bot(command_prefix='!', intents=intents, help_command=None,
                   allowed_mentions=discord.AllowedMentions.none())
scan_lock = asyncio.Lock()
stop_scan_event = asyncio.Event()
bot.direct_ok = None  # Set by the startup probe in on_ready

def make_result(ip, address, players, players_max, names, version, motd):
    return {
        "ip": ip,
        "address": address,  # Resolved IP, used for geolocation
        "players": players or 0,
        "max": players_max or 0,
        "names": names,
        "version": version or 'Unknown',
        "motd": (motd or '').strip().replace('\n', '  '),
    }

async def check_direct(ip):
    """
    Pings the server itself. Returns a result if it answers, None otherwise.
    """
    try:
        server = await JavaServer.async_lookup(ip, timeout=DIRECT_TIMEOUT)
        status = await server.async_status(tries=1)
    except Exception:
        return None

    try:
        address = str(await server.address.async_resolve_ip())
    except Exception:
        address = None

    names = [p.name for p in (status.players.sample or []) if p.name]
    return make_result(ip, address, status.players.online, status.players.max,
                       names, status.version.name, status.motd.to_plain())

async def check_api(session, ip):
    """
    Asks mcstatus.io about the server. Returns a result if it's online, None otherwise.
    """
    for attempt in range(3):
        try:
            async with session.get(f"{MC_API_URL}{ip}", timeout=aiohttp.ClientTimeout(total=10)) as response:
                if response.status == 429:
                    # Rate limited: wait and try again instead of calling the server offline
                    await asyncio.sleep(1 + attempt)
                    continue
                if response.status != 200:
                    return None
                data = await response.json()
        except Exception:
            return None

        if not data.get('online', False):
            return None

        players = data.get('players') or {}
        names = [p.get('name_clean') or p.get('name') for p in players.get('list') or []]
        return make_result(ip, data.get('ip_address'), players.get('online'), players.get('max'),
                           [n for n in names if n], (data.get('version') or {}).get('name_clean'),
                           (data.get('motd') or {}).get('clean'))
    return None

async def batch_get_locations(session, ips):
    """
    Uses ip-api.com batch endpoint to get locations for a list of IPs.
    Max 100 IPs per request.
    """
    locations = {}
    if not ips:
        return locations

    # Split into chunks of 100
    chunks = [ips[i:i + 100] for i in range(0, len(ips), 100)]

    for index, chunk in enumerate(chunks):
        if stop_scan_event.is_set():
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

        if index < len(chunks) - 1:
            await asyncio.sleep(GEO_DELAY)

    return locations

def parse_ips(text):
    """
    Returns (unique valid addresses in file order, invalid line count, duplicate count).
    Blank lines and lines starting with # are ignored.
    """
    lines = [line.strip() for line in text.splitlines()]
    lines = [line for line in lines if line and not line.startswith('#')]
    valid = [line for line in lines if ADDRESS_RE.match(line)]
    unique = list(dict.fromkeys(valid))
    return unique, len(lines) - len(valid), len(valid) - len(unique)

async def set_status(text):
    try:
        await bot.change_presence(activity=discord.Game(name=text))
    except Exception as e:
        log.warning("Could not update presence: %s", e)

def progress_text(state):
    return f"🔎 **{state['phase']}:** {state['done']}/{state['total']} · **Found:** {state['found']}"

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

async def run_direct(ips, results, state):
    """
    Pings every server directly, DIRECT_CONCURRENCY at a time.
    Returns the IPs that didn't answer, in file order.
    """
    state.update(phase="Pinging servers", done=0, total=len(ips))
    semaphore = asyncio.Semaphore(DIRECT_CONCURRENCY)
    pinged = set()

    async def ping(ip):
        async with semaphore:
            if stop_scan_event.is_set():
                return
            result = await check_direct(ip)
        state['done'] += 1
        pinged.add(ip)
        if result:
            results[ip] = result
            state['found'] += 1

    await asyncio.gather(*(ping(ip) for ip in ips))
    return [ip for ip in ips if ip in pinged and ip not in results]

async def run_api(session, ips, results, state, retrying):
    """
    Checks servers through mcstatus.io, starting one request every API_DELAY seconds.
    """
    phase = "Retrying unreachable servers via API" if retrying else "Checking servers via API"
    state.update(phase=phase, done=0, total=len(ips))

    async def check(ip):
        result = await check_api(session, ip)
        state['done'] += 1
        if result:
            results[ip] = result
            state['found'] += 1

    tasks = []
    for index, ip in enumerate(ips):
        if stop_scan_event.is_set():
            break
        if index % 50 == 0:
            await set_status(f"API {index}/{len(ips)}...")
        tasks.append(asyncio.create_task(check(ip)))
        await asyncio.sleep(API_DELAY)

    if stop_scan_event.is_set():
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
    writer.writerow(["ip", "resolved_ip", "country", "players_online", "players_max", "version", "motd", "players"])
    for r in populated + empty:
        writer.writerow([r['ip'], r['address'] or '', locations.get(r['address']) or '', r['players'],
                         r['max'], r['version'], r['motd'], "; ".join(r['names'])])
    table = discord.File(io.BytesIO(buffer.getvalue().encode('utf-8')), filename="scan_results.csv")
    return [txt, table]

async def send_results(ctx, results, locations, stopped, total_ips, duration):
    minutes = int(duration // 60)
    seconds = int(duration % 60)

    populated = sorted((r for r in results if r['players'] > 0), key=lambda r: r['players'], reverse=True)
    empty = [r for r in results if r['players'] == 0]

    title = "🛑 **Scan stopped** — partial results" if stopped else "📊 **Scan Complete!**"
    summary = (f"{title}\n🟢 {len(populated)} with players · ⚪ {len(empty)} empty · 🔎 {total_ips} IPs\n"
               f"⏱️ **Time:** {minutes}m {seconds}s")
    if not stopped and duration > 0:
        summary += f"\n⚡ **Speed:** {total_ips / duration:.2f} IPs/sec"

    if not results:
        await ctx.send(f"❌ No working servers found.\n{summary}")
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
        await ctx.send(f"{summary}\n\n{body}")
        return

    # Too long for one message: summary + top servers in chat, everything in files
    top = [f"{get_flag_emoji(locations.get(r['address']))} **{r['ip']}** | {r['players']}/{r['max']}"
           for r in populated[:10]]
    message = summary
    if top:
        message += "\n\n**Top servers:**\n" + "\n".join(top)
    message += "\n\n📎 Full results are in the attached files."
    await ctx.send(message[:2000], files=build_files(populated, empty, locations))

@bot.event
async def on_ready():
    # on_ready runs again after reconnects; only probe once
    if bot.direct_ok is None:
        bot.direct_ok = await check_direct(PROBE_SERVER) is not None
        if bot.direct_ok:
            log.info("Direct pings work; scans use direct pings with API fallback.")
        else:
            log.warning("Direct ping to %s failed; scans use the mcstatus.io API only (5 checks/second).", PROBE_SERVER)
    log.info("Logged in as %s", bot.user.name)
    await set_status("Idle | Waiting for IPs")

@bot.command()
async def help(ctx):
    """Displays a list of available commands."""
    embed = discord.Embed(
        title="📖 Scanbot Help",
        description="Here are all available commands:",
        color=discord.Color.blue()
    )
    embed.add_field(
        name="!check / !scan",
        value="Scans a list of Minecraft server IPs from an attached `.txt` file.",
        inline=False
    )
    embed.add_field(
        name="!stop",
        value="Stops the currently running scan and posts what it found so far.",
        inline=False
    )
    embed.add_field(
        name="!help",
        value="Displays this help message.",
        inline=False
    )
    embed.set_footer(text=f"Attach a .txt file with IPs (one per line, max {MAX_IPS_PER_SCAN}) to use !scan.")
    await ctx.send(embed=embed)

@bot.command()
async def stop(ctx):
    """Stops the currently running scan."""
    if scan_lock.locked():
        stop_scan_event.set()
        await ctx.send("🛑 **Stop requested.** The scan will stop shortly and post what it found so far...")
    else:
        await ctx.send("⚠️ **No scan is currently running.**")

@bot.command(aliases=['scan'])
async def check(ctx):
    # No await between this check and acquiring the lock, so two commands can't both get past it
    if scan_lock.locked():
        await ctx.send("⏳ **Bot is busy.** Another scan is currently in progress.")
        return

    async with scan_lock:
        stop_scan_event.clear()  # Reset the stop event at the start of scan

        # --- File Input ---
        if not ctx.message.attachments:
            await ctx.send("❌ Please attach a `.txt` file.")
            return

        attachment = ctx.message.attachments[0]
        if not attachment.filename.endswith('.txt'):
            await ctx.send("❌ Must be a `.txt` file.")
            return

        try:
            content = await attachment.read()
            ips, invalid, duplicates = parse_ips(content.decode('utf-8'))
        except Exception as e:
            await ctx.send(f"❌ Error reading file: {e}")
            return

        if not ips:
            await ctx.send("⚠️ No valid IPs in the file.")
            return

        total_ips = len(ips)
        if total_ips > MAX_IPS_PER_SCAN:
            await ctx.send(f"❌ Too many IPs. Maximum allowed per scan is {MAX_IPS_PER_SCAN}.")
            return

        notes = []
        if invalid: notes.append(f"skipped {invalid} invalid line(s)")
        if duplicates: notes.append(f"removed {duplicates} duplicate(s)")
        extra = f" ({', '.join(notes)})" if notes else ""

        start_time = time.time()
        await ctx.send(f"🚀 **Scan started** on {total_ips} IPs{extra}...")

        state = {"phase": "Starting", "done": 0, "total": total_ips, "found": 0}
        progress = await ctx.send(progress_text(state))
        updater = asyncio.create_task(report_progress(progress, state))
        results = {}
        locations = {}

        try:
            async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(resolver=Quad9Resolver())) as session:
                # 1. Direct pings, many at once (skipped if the startup probe failed)
                if bot.direct_ok:
                    await set_status(f"Pinging {total_ips} servers...")
                    retry = await run_direct(ips, results, state)
                else:
                    retry = ips

                # 2. mcstatus.io API for everything that didn't answer, 5 per second
                if retry and not stop_scan_event.is_set():
                    await run_api(session, retry, results, state, retrying=bool(bot.direct_ok))

                # 3. Batch geolocation
                addresses = sorted({r['address'] for r in results.values() if r['address']})
                if addresses and not stop_scan_event.is_set():
                    state.update(phase="Resolving locations", done=0, total=len(addresses))
                    await set_status("Resolving locations...")
                    locations = await batch_get_locations(session, addresses)
                    state['done'] = len(addresses)
        finally:
            updater.cancel()
            await set_status("Idle | Waiting for IPs")

        stopped = stop_scan_event.is_set()
        try:
            await progress.edit(content=("🛑 **Stopped.**" if stopped else "✅ **Done.**") + f" Found {state['found']} online servers.")
        except discord.HTTPException:
            pass

        await send_results(ctx, list(results.values()), locations, stopped, total_ips, time.time() - start_time)

async def main():
    discord.utils.setup_logging(root=True)
    # discord.py only builds its own connector if none is set, so Discord traffic uses Quad9 too
    bot.http.connector = aiohttp.TCPConnector(limit=0, resolver=Quad9Resolver())
    async with bot:
        await bot.start(TOKEN)

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
