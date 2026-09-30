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

2. Create a `token.txt` file in the project root (`/home/runner/work/scanbot/scanbot`) containing only your Discord bot token.

3. Run the bot:

   ```bash
   python bot.py
   ```

## Usage

1. In Discord, run `!check` (or `!scan`) and attach a `.txt` file.
2. Put one IP (or host) per line in the file.
3. Wait for the bot to post grouped scan results.

## Notes

- The bot currently reads only the first attachment in the command message.
- Geolocation requests use the `ip-api.com` batch endpoint.
- If `token.txt` is missing, the bot exits immediately.
