# syntax=docker/dockerfile:1
FROM python:3.13-slim

LABEL org.opencontainers.image.source="https://github.com/TheDyXer/scanbot" \
      org.opencontainers.image.description="Discord bot that scans lists of Minecraft Java and Bedrock servers" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

# Offline country database (DB-IP Lite, CC BY 4.0) for the flags. CI passes this month
# as DBIP_MONTH so the download layer isn't reused from cache once a new month is out.
# This month's file isn't published on day one, so fall back to last month's.
ARG DBIP_MONTH=
RUN <<'EOF' python
import datetime, gzip, os, urllib.error, urllib.request
first = datetime.date.today().replace(day=1)
if os.environ.get("DBIP_MONTH"):
    first = datetime.date.fromisoformat(os.environ["DBIP_MONTH"] + "-01")
last_month = (first - datetime.timedelta(days=1)).replace(day=1)
for month in (first, last_month):
    url = f"https://download.db-ip.com/free/dbip-country-lite-{month:%Y-%m}.mmdb.gz"
    # DB-IP rejects Python's default User-Agent with HTTP 403
    req = urllib.request.Request(url, headers={"User-Agent": "scanbot (+https://github.com/TheDyXer/scanbot)"})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = gzip.decompress(resp.read())
    except urllib.error.HTTPError as e:
        print(f"{url}: HTTP {e.code}")
        continue
    with open("/app/dbip-country-lite.mmdb", "wb") as f:
        f.write(data)
    print(f"Saved DB-IP country database for {month:%Y-%m} ({len(data)} bytes)")
    break
else:
    raise SystemExit("Could not download the DB-IP country database")
EOF

COPY bot.py .

# The bot reads token.txt from its working directory, so mount the folder
# holding token.txt at /data
WORKDIR /data
USER 1000:1000

CMD ["python", "/app/bot.py"]
