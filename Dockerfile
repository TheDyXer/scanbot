FROM python:3.13-slim

LABEL org.opencontainers.image.source="https://github.com/TheDyXer/scanbot" \
      org.opencontainers.image.description="Discord bot that scans lists of Minecraft Java servers"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY bot.py .

# The bot reads token.txt from its working directory, so mount the folder
# holding token.txt at /data
WORKDIR /data
USER 1000:1000

CMD ["python", "/app/bot.py"]
