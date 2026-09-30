# Scanbot

Scanbot is a Discord bot that scans a list of Minecraft Java server IPs, identifies online servers, and reports player/version details in Discord.

## Features

- Reads IPs from an attached `.txt` file, skipping blank lines, `#` comments, invalid entries and duplicates
- Pings servers directly, 50 at a time, and retries the ones that don't answer through `api.mcstatus.io`
- Resolves server country codes in batches via `ip-api.com`
- Shows live progress in one message that updates itself
- `!stop` works at any point and posts what was found so far
- Groups results into:
  - Servers with players online
  - Online but empty servers
- Long results are sent as `scan_results.txt` and `scan_results.csv`, with a summary in chat
- All DNS lookups (Discord, the APIs, Minecraft servers) go to Quad9 over DNS-over-TLS
- Prevents concurrent scans with a lock

## How scanning works

1. **Direct pings.** At startup the bot pings `mc.hypixel.net`. If that works, scans ping each server directly, 50 at a time, waiting up to 3 seconds each. That covers 5,000 IPs in at most about 5 minutes.
2. **API fallback.** Servers that don't answer a direct ping are retried through `api.mcstatus.io`, which allows 5 requests per second, so lists with many dead IPs still take a while. If the startup ping failed (for example, a router blocks port 25565), every server goes through the API, and 5,000 IPs take about 17 minutes.
3. **Geolocation.** Online servers are looked up in batches of 100 on `ip-api.com`, 4 seconds apart to stay under its limit of 15 requests per minute.

Speed and limits are set at the top of `bot.py` (`DIRECT_CONCURRENCY`, `DIRECT_TIMEOUT`, `API_DELAY`, `MAX_IPS_PER_SCAN`).

## Requirements

- Python 3.10+
- A Discord bot token
- Outbound TCP port 853 (DNS-over-TLS to Quad9)

## Setup

1. Install dependencies:

   ```bash
   pip install -r requirements.txt
   ```

2. Give the bot your Discord token, either:
   - set the `DISCORD_TOKEN` environment variable, or
   - create a `token.txt` file next to `bot.py` containing only the token.

3. Run the bot:

   ```bash
   python bot.py
   ```

   The log says at startup whether direct pings work from your network.

## Usage

1. In Discord, run `!check` (or `!scan`) and attach a `.txt` file.
2. Put one IP or host per line (`1.2.3.4`, `play.example.com`, `1.2.3.4:25570`), up to 5,000 per scan.
3. Watch the progress message, then read the results.

| Command | What it does |
| --- | --- |
| `!check` / `!scan` | Scans the IPs in the attached `.txt` file |
| `!stop` | Stops the scan and posts what it found so far |
| `!help` | Lists the commands |

## Notes

- The bot reads only the first attachment in the command message.
- IPv6 addresses aren't supported in the list.
- To use Quad9 over DNS-over-HTTPS instead, `pip install httpx` and replace the two `DoTNameserver` entries in `bot.py` with `dns.nameserver.DoHNameserver('https://dns.quad9.net/dns-query', bootstrap_address='9.9.9.9')`.
