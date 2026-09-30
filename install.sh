#!/usr/bin/env bash
# Scanbot installer. Run it in the folder where you want the bot to live:
#
#   curl -fsSL https://raw.githubusercontent.com/TheDyXer/scanbot/main/install.sh | bash
#
# Creates ./scanbot with docker-compose.yml and data/token.txt, then starts the bot.
# Running it again is safe: it keeps your files and pulls the latest image.
#
# Optional environment variables:
#   DISCORD_TOKEN   use this token instead of asking for it
#   SCANBOT_DIR     install folder (default: scanbot)
#   SCANBOT_REF     git branch or tag to download docker-compose.yml from (default: main)
#   SCANBOT_IMAGE   image to run instead of ghcr.io/thedyxer/scanbot:latest
set -euo pipefail

REPO="TheDyXer/scanbot"
REF="${SCANBOT_REF:-main}"
DIR="${SCANBOT_DIR:-scanbot}"
RAW="https://raw.githubusercontent.com/${REPO}/${REF}"
PACKAGE_SETTINGS="https://github.com/users/TheDyXer/packages/container/scanbot/settings"

info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m✔\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m✘\033[0m %s\n' "$*" >&2; exit 1; }

# --- Preflight ---
command -v docker >/dev/null 2>&1 || die "Docker isn't installed. Install it first: https://docs.docker.com/engine/install/"
docker compose version >/dev/null 2>&1 \
  || die "Docker Compose v2 ('docker compose') is missing. Install the compose plugin: https://docs.docker.com/compose/install/"
command -v curl >/dev/null 2>&1 || die "curl is missing. Install it and run this again."
if ! docker_error=$(docker info 2>&1 >/dev/null); then
  if printf '%s' "$docker_error" | grep -qi "permission denied"; then
    die "Your user can't talk to Docker. Run this with sudo, or add yourself to the docker group: sudo usermod -aG docker \$USER (then log out and back in)."
  fi
  die "Docker isn't running or can't be reached: ${docker_error}"
fi

# --- Folder and files ---
mkdir -p "${DIR}/data"
cd "${DIR}"
info "Installing into $(pwd)"

if [ -f docker-compose.yml ]; then
  ok "Keeping your existing docker-compose.yml"
else
  curl -fsSL "${RAW}/docker-compose.yml" -o docker-compose.yml \
    || die "Couldn't download docker-compose.yml from ${RAW}"
  ok "Downloaded docker-compose.yml"
fi

if [ -f .env ]; then
  ok "Keeping your existing .env"
else
  {
    echo "# Scanbot settings (read by docker compose)"
    echo "SCANBOT_UID=$(id -u)"
    echo "SCANBOT_GID=$(id -g)"
    echo "# Time zone for the daily 4 AM update check, e.g. Europe/Budapest"
    echo "TZ=UTC"
    if [ -n "${SCANBOT_IMAGE:-}" ]; then echo "SCANBOT_IMAGE=${SCANBOT_IMAGE}"; fi
  } > .env
  ok "Created .env"
fi

# --- Token ---
TOKEN_FILE="data/token.txt"
if [ -s "${TOKEN_FILE}" ] && [ -n "$(tr -d '[:space:]' < "${TOKEN_FILE}")" ]; then
  ok "Keeping your existing ${TOKEN_FILE}"
else
  token="${DISCORD_TOKEN:-}"
  # stdin is this script when piped from curl, so ask on the terminal instead
  if [ -z "${token}" ] && { : </dev/tty; } 2>/dev/null; then
    printf 'Paste your Discord bot token (hidden, Enter to skip): ' >/dev/tty
    IFS= read -rs token </dev/tty || token=""
    printf '\n' >/dev/tty
  fi
  token="$(printf '%s' "${token}" | tr -d '[:space:]')"
  if [ -z "${token}" ]; then
    warn "No token yet. Put it in $(pwd)/${TOKEN_FILE}, then start the bot with:"
    echo "    cd $(pwd) && docker compose up -d"
    exit 0
  fi
  (umask 077; printf '%s\n' "${token}" > "${TOKEN_FILE}")
  ok "Saved the token to ${TOKEN_FILE}"
fi

# --- Start ---
info "Pulling images..."
if ! pull_output=$(docker compose pull 2>&1); then
  if printf '%s' "${pull_output}" | grep -qiE "denied|unauthorized"; then
    die "The scanbot image isn't public yet. The repo owner needs to set it to Public: ${PACKAGE_SETTINGS}"
  fi
  printf '%s\n' "${pull_output}" >&2
  die "docker compose pull failed."
fi

docker compose up -d --remove-orphans
info "Waiting for the bot to log in..."
status="unknown"
for _ in $(seq 1 30); do
  logs="$(docker compose logs scanbot 2>&1 || true)"
  if printf '%s' "${logs}" | grep -q "Logged in as"; then status="ok"; break; fi
  if printf '%s' "${logs}" | grep -qE "Improper token|LoginFailure"; then status="bad-token"; break; fi
  if printf '%s' "${logs}" | grep -q "Error: "; then status="error"; break; fi
  sleep 1
done

echo
docker compose logs --tail 5 scanbot 2>&1 || true
echo

case "${status}" in
  ok)
    ok "Scanbot is running."
    printf '%s' "${logs}" | grep -oE "(Direct pings work|Direct ping to).*" | tail -n 1 | sed 's/^/    /' || true
    ;;
  bad-token)
    # Stop it so it doesn't keep retrying a bad login (Discord blocks IPs that do that a lot)
    docker compose stop scanbot >/dev/null 2>&1 || true
    die "Discord rejected the token. Put the right one in $(pwd)/${TOKEN_FILE}, then: docker compose up -d"
    ;;
  error)
    docker compose stop scanbot >/dev/null 2>&1 || true
    die "The bot stopped with an error (see above). Fix it, then: docker compose up -d"
    ;;
  *)
    warn "The bot hasn't logged in yet. Check the log with: docker compose logs -f scanbot"
    ;;
esac

cat <<EOF

Useful commands (run them in $(pwd)):
  docker compose logs -f scanbot                   follow the log
  docker compose restart scanbot                   restart the bot
  docker compose pull && docker compose up -d      update now (Watchtower also does it daily at 4 AM)
  docker compose down                              stop the bot
EOF
