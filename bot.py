import discord
from discord.ext import commands
import aiohttp
import asyncio
import time
import json

# --- CONFIGURATION ---
MC_API_URL = 'https://api.mcstatus.io/v2/status/java/'
GEO_BATCH_URL = 'http://ip-api.com/batch' # Using batch endpoint
# ---------------------

# Read token
try:
    with open('token.txt', 'r') as f:
        TOKEN = f.read().strip()
except FileNotFoundError:
    print("❌ Error: token.txt not found.")
    exit()

def get_flag_emoji(country_code):
    if not country_code:
        return "🏳️"
    return "".join([chr(ord(c.upper()) + 127397) for c in country_code])

intents = discord.Intents.default()
intents.message_content = True
intents.dm_messages = True

bot = commands.Bot(command_prefix='!', intents=intents)
scan_lock = asyncio.Lock()

async def check_server(session, ip):
    """
    Checks Minecraft status only. Returns data if online, None otherwise.
    Does NOT check geolocation yet.
    """
    try:
        async with session.get(f"{MC_API_URL}{ip}", timeout=10) as response:
            if response.status == 200:
                data = await response.json()
                
                if data.get('online', False):
                    return {
                        "ip": ip,
                        "data": data
                    }
    except:
        pass
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
    
    for chunk in chunks:
        try:
            # ip-api batch format: POST body is a JSON list of IPs or objects
            # We just send a list of strings (IPs) to get default fields + countryCode
            payload = [{"query": ip, "fields": "query,countryCode"} for ip in chunk]
            
            async with session.post(GEO_BATCH_URL, json=payload, timeout=5) as resp:
                if resp.status == 200:
                    results = await resp.json()
                    for entry in results:
                        # Map the IP back to its country code
                        # Entry looks like: {"query": "1.2.3.4", "countryCode": "US"}
                        ip_addr = entry.get('query')
                        country = entry.get('countryCode')
                        if ip_addr:
                            locations[ip_addr] = country
            
            # Respect batch rate limit (15 req/min = 1 req every 4 seconds)
            # But since we only do this rarely (once per scan usually), a small sleep is fine.
            # If you have >100 valid servers, this loop runs multiple times.
            if len(chunks) > 1:
                await asyncio.sleep(2) 
                
        except Exception as e:
            print(f"Geo error: {e}")
            pass
            
    return locations

@bot.event
async def on_ready():
    print(f'Logged in as {bot.user.name}')
    await bot.change_presence(activity=discord.Game(name="Idle | Waiting for IPs"))

@bot.command(aliases=['scan'])
async def check(ctx):
    if scan_lock.locked():
        await ctx.send("⏳ **Bot is busy.** Another scan is currently in progress.")
        return

    async with scan_lock:
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
            ips = [line.strip() for line in content.decode('utf-8').splitlines() if line.strip()]
        except Exception as e:
            await ctx.send(f"❌ Error reading file: {e}")
            return

        if not ips:
            await ctx.send("⚠️ File is empty.")
            return

        start_time = time.time()
        total_ips = len(ips)
        await ctx.send(f"🚀 **Scan started** on {total_ips} IPs...")
        
        # 1. PHASE ONE: High-Speed Minecraft Scan
        tasks = []
        mc_results = []
        
        async with aiohttp.ClientSession() as session:
            for index, ip in enumerate(ips):
                if index % 50 == 0:
                    await bot.change_presence(activity=discord.Game(name=f"Scanning {index}/{total_ips} ({ip})..."))
                
                task = asyncio.create_task(check_server(session, ip))
                tasks.append(task)
                
                await asyncio.sleep(0.2) # Fast scan

            await bot.change_presence(activity=discord.Game(name=f"Finalizing MC scan..."))
            raw_results = await asyncio.gather(*tasks)
            
            # Filter valid online servers
            valid_servers = [r for r in raw_results if r is not None]

            # 2. PHASE TWO: Batch Geolocation
            # Collect all valid IPs to resolve their location
            await bot.change_presence(activity=discord.Game(name=f"Resolving locations..."))
            
            valid_ips = [s['ip'] for s in valid_servers]
            location_map = await batch_get_locations(session, valid_ips)

            # 3. PHASE THREE: Processing & Formatting
            final_results = []
            for server in valid_servers:
                ip = server['ip']
                data = server['data']
                
                # Extract details
                players_data = data.get('players', {})
                players_online = players_data.get('online', 0)
                players_max = players_data.get('max', 0)
                version = data.get('version', {}).get('name_clean', 'Unknown')
                
                motd = data.get('motd', {}).get('clean', '').strip().replace('\n', '  ')
                
                player_names = []
                if players_data.get('list'):
                    for p in players_data['list']:
                        name = p.get('name_clean') or p.get('name')
                        if name: player_names.append(name)
                players_str = ", ".join(player_names)

                # Get cached location
                country_code = location_map.get(ip)
                flag = get_flag_emoji(country_code)

                # Build object
                final_results.append({
                    "ip": ip,
                    "text": f"{flag} **{ip}** | Players: {players_online}/{players_max} | Ver: {version}",
                    "players": players_online,
                    "players_names": players_str,
                    "motd": motd
                })

        # --- Stats & Output ---
        end_time = time.time()
        duration = end_time - start_time
        
        minutes = int(duration // 60)
        seconds = int(duration % 60)
        time_str = f"{minutes}m {seconds}s"
        ips_per_sec = total_ips / duration if duration > 0 else 0
        
        populated = [r for r in final_results if r['players'] > 0]
        empty = [r for r in final_results if r['players'] == 0]
        populated.sort(key=lambda x: x['players'], reverse=True)

        final_lines = []
        
        def format_entry(s):
            text = s['text']
            if s['motd']: text += f"\n   └ 📝 {s['motd']}"
            if s['players_names']: text += f"\n   └ 👤 **Users:** {s['players_names']}"
            return text

        if populated:
            final_lines.append(f"**🟢 Servers with Players ({len(populated)}):**")
            for s in populated: final_lines.append(format_entry(s))
            final_lines.append("")
        
        if empty:
            final_lines.append(f"**⚪ Online (Empty) Servers ({len(empty)}):**")
            for s in empty: final_lines.append(format_entry(s))

        footer = f"\n⏱️ **Time:** {time_str}\n⚡ **Speed:** {ips_per_sec:.2f} IPs/sec"

        if not final_lines:
            await ctx.send(f"❌ No working servers found.\n{footer}")
        else:
            header = "**📊 Scan Complete!**\n"
            chunk = header
            for line in final_lines:
                if len(chunk) + len(line) + 1 > 1900:
                    await ctx.send(chunk)
                    chunk = ""
                chunk += line + "\n"
            
            if len(chunk) + len(footer) + 1 > 1900:
                await ctx.send(chunk)
                await ctx.send(footer)
            else:
                await ctx.send(chunk + footer)

        await bot.change_presence(activity=discord.Game(name="Idle | Waiting for IPs"))

bot.run(TOKEN)