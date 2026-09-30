# Scanbot

Scanbot is a Discord bot that scans a list of Minecraft Java server IPs, identifies online servers, and reports player/version details in Discord.

## Features

- Reads IPs from an attached `.txt` file
- Checks Minecraft Java server status via `api.mcstatus.io`
- Resolves server country codes in batches via `ip-api.com`
- Groups results into:
  - Servers with players online
  - Online but empty servers
- Shows scan duration and IPs/second throughput
- Prevents concurrent scans with a lock

## Requirements

- Python 3.9+
- A Discord bot token
- Python packages:
  - `discord.py`
  - `aiohttp`

## Setup

1. Install dependencies:

   ```bash
   pip install discord.py aiohttp
   ```

2. Create a `token.txt` file next to `bot.py` containing only your Discord bot token.

3. Run the bot:

   ```bash
   python bot.py
   ```

## Usage

1. In Discord, run `!check` (or `!scan`) and attach a `.txt` file.
2. Put one IP (or host) per line in the file, up to 5,000 per scan (`MAX_IPS_PER_SCAN` in `bot.py`).
3. Wait for the bot to post grouped scan results.

| Command | What it does |
| --- | --- |
| `!check` / `!scan` | Scans the IPs in the attached `.txt` file |
| `!stop` | Stops the scan that is running |
| `!help` | Lists the commands |

## Notes

- The bot currently reads only the first attachment in the command message.
- Geolocation requests use the `ip-api.com` batch endpoint.
- If `token.txt` is missing, the bot exits immediately.
