import discord
from discord.ext import commands
import aiohttp
import asyncio
import time

# --- CONFIGURATION ---
MC_API_URL = 'https://api.mcstatus.io/v2/status/java/'
GEO_API_URL = 'http://ip-api.com/json/'
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
    result = None
    try:
        async with session.get(f"{MC_API_URL}{ip}", timeout=10) as response:
            if response.status == 200:
                data = await response.json()
                
                if data.get('online', False):
                    players_data = data.get('players', {})
                    players_online = players_data.get('online', 0)
                    players_max = players_data.get('max', 0)
                    version = data.get('version', {}).get('name_clean', 'Unknown')
                    
                    # 1. Get MOTD (Description)
                    motd = data.get('motd', {}).get('clean', '').strip()
                    motd = motd.replace('\n', '  ') 
                    
                    # 2. Get Player Names
                    player_names = []
                    player_list = players_data.get('list', [])
                    if player_list:
                        for p in player_list:
                            p_name = p.get('name_clean') or p.get('name')
                            if p_name:
                                player_names.append(p_name)
                    players_str = ", ".join(player_names)

                    # 3. Get Geolocation (Flag)
                    country_code = None
                    try:
                        async with session.get(f"{GEO_API_URL}{ip}?fields=countryCode", timeout=2) as geo_resp:
                            if geo_resp.status == 200:
                                geo_data = await geo_resp.json()
                                country_code = geo_data.get('countryCode')
                    except:
                        pass 

                    flag = get_flag_emoji(country_code)
                    
                    result = {
                        "ip": ip,
                        "text": f"{flag} **{ip}** | Players: {players_online}/{players_max} | Ver: {version}",
                        "players": players_online,
                        "players_names": players_str,
                        "motd": motd
                    }
    except:
        pass
    
    return result

@bot.event
async def on_ready():
    print(f'Logged in as {bot.user.name}')
    await bot.change_presence(activity=discord.Game(name="Idle | Waiting for IPs"))

# Supports both !check and !scan
@bot.command(aliases=['scan'])
async def check(ctx):
    if scan_lock.locked():
        await ctx.send("⏳ **Bot is busy.** Another scan is currently in progress. Please wait.")
        return

    async with scan_lock:
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
        
        tasks = []
        
        async with aiohttp.ClientSession() as session:
            for index, ip in enumerate(ips):
                # Status update
                if index % 20 == 0:
                    await bot.change_presence(activity=discord.Game(name=f"Scanning {index}/{total_ips} ({ip})..."))
                
                task = asyncio.create_task(check_server(session, ip))
                tasks.append(task)
                
                await asyncio.sleep(0.22) # Pipeline delay

            await bot.change_presence(activity=discord.Game(name=f"Finalizing results..."))
            scan_results = await asyncio.gather(*tasks)
        
        results = [r for r in scan_results if r]

        end_time = time.time()
        duration = end_time - start_time
        
        minutes = int(duration // 60)
        seconds = int(duration % 60)
        time_str = f"{minutes}m {seconds}s"

        if duration > 0:
            ips_per_sec = total_ips / duration
        else:
            ips_per_sec = 0
        
        populated = [r for r in results if r['players'] > 0]
        empty = [r for r in results if r['players'] == 0]
        
        # Sort by player count
        populated.sort(key=lambda x: x['players'], reverse=True)

        final_lines = []
        
        def format_entry(server):
            text = server['text']
            if server['motd']:
                text += f"\n   └ 📝 {server['motd']}"
            if server.get('players_names'):
                text += f"\n   └ 👤 **Users:** {server['players_names']}"
            return text

        if populated:
            final_lines.append(f"**🟢 Servers with Players ({len(populated)}):**")
            for s in populated:
                final_lines.append(format_entry(s))
            final_lines.append("")
        
        if empty:
            final_lines.append(f"**⚪ Online (Empty) Servers ({len(empty)}):**")
            for s in empty:
                final_lines.append(format_entry(s))

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