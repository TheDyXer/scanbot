#!/usr/bin/env bash
# Scanbot installer. Run it in the folder where you want the bot to live:
#
#   curl -fsSL https://raw.githubusercontent.com/TheDyXer/scanbot/main/install.sh | bash
#
# Creates ./scanbot with docker-compose.yml and data/token.txt, then starts the bot.
# Running it again is safe: it keeps your files and pulls the latest image.
#
# Options (after "bash -s --" when piping from curl):
#   --vpn           set up, change or remove the VPN (asked automatically on the first install)
#
# Optional environment variables:
#   DISCORD_TOKEN   use this token instead of asking for it
#   SCANBOT_DIR     install folder (default: scanbot)
#   SCANBOT_REF     git branch or tag to download the compose files from (default: main)
#   SCANBOT_IMAGE   image to run instead of ghcr.io/thedyxer/scanbot:latest
#   SCANBOT_VPN     none, mullvad, protonvpn or warp: choose the VPN without being asked
#   WIREGUARD_PRIVATE_KEY, WIREGUARD_ADDRESSES   VPN keys, instead of being asked
#   SCANBOT_VPN_FREE   yes or no: whether a Proton account is on the free plan
set -euo pipefail

REPO="TheDyXer/scanbot"
REF="${SCANBOT_REF:-main}"
DIR="${SCANBOT_DIR:-scanbot}"
RAW="https://raw.githubusercontent.com/${REPO}/${REF}"
PACKAGE_SETTINGS="https://github.com/users/TheDyXer/packages/container/scanbot/settings"
VPN_COMPOSE_FILE="docker-compose.yml:docker-compose.vpn.yml"
# Cloudflare WARP keys are made with wgcf (https://github.com/ViRb3/wgcf), pinned and checksum-verified
WGCF_VERSION="2.3.0"
WGCF_SHA256_AMD64="01614e38c0eb5f3405232e71cfaf02d64d4809e4988ad8f5a8071af16d193405"
WGCF_SHA256_ARM64="dcadadc42bcc410a4032a6d1c0490ea510e199f0aaaee397dc1aa0fbd27038e8"
WARP_ENDPOINT_FALLBACK="162.159.192.1"

info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m✔\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m✘\033[0m %s\n' "$*" >&2; exit 1; }

# stdin is this script when piped from curl, so questions go to the terminal instead
has_tty() { { : </dev/tty; } 2>/dev/null; }
ask() {
  local answer=""
  if has_tty; then printf '%s' "$1" >/dev/tty; IFS= read -r answer </dev/tty || answer=""; fi
  printf '%s' "${answer}"
}
ask_secret() {
  local answer=""
  if has_tty; then printf '%s' "$1" >/dev/tty; IFS= read -rs answer </dev/tty || answer=""; printf '\n' >/dev/tty; fi
  printf '%s' "${answer}"
}

# Set or remove KEY=value lines in .env, keeping the file's permissions
env_set() {
  local tmp; tmp="$(mktemp)"
  grep -v "^$1=" .env > "${tmp}" || true
  printf '%s=%s\n' "$1" "$2" >> "${tmp}"
  cat "${tmp}" > .env; rm -f "${tmp}"
}
env_unset() {
  local tmp; tmp="$(mktemp)"
  grep -v "^$1=" .env > "${tmp}" || true
  cat "${tmp}" > .env; rm -f "${tmp}"
}
env_get() { grep -m1 "^$1=" .env 2>/dev/null | cut -d= -f2- || true; }
vpn_enabled() { grep -q "^COMPOSE_FILE=.*docker-compose.vpn.yml" .env 2>/dev/null; }

VPN_SETUP=""
for arg in "$@"; do
  case "${arg}" in
    --vpn) VPN_SETUP="yes" ;;
    -h|--help)
      echo "Usage: curl -fsSL ${RAW}/install.sh | bash [-s -- --vpn]"
      echo "  --vpn   set up, change or remove the VPN (asked automatically on the first install)"
      exit 0 ;;
    *) die "Unknown option: ${arg}" ;;
  esac
done

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
  VPN_SETUP="${VPN_SETUP:-yes}"  # First install: ask about the VPN too
fi
IMAGE="$(env_get SCANBOT_IMAGE)"; IMAGE="${IMAGE:-ghcr.io/thedyxer/scanbot:latest}"
GLUETUN_IMAGE="$(env_get GLUETUN_IMAGE)"; GLUETUN_IMAGE="${GLUETUN_IMAGE:-qmcgaw/gluetun:v3}"

# --- Token ---
TOKEN_FILE="data/token.txt"
if [ -s "${TOKEN_FILE}" ] && [ -n "$(tr -d '[:space:]' < "${TOKEN_FILE}")" ]; then
  ok "Keeping your existing ${TOKEN_FILE}"
else
  token="${DISCORD_TOKEN:-}"
  if [ -z "${token}" ]; then
    token="$(ask_secret 'Paste your Discord bot token (hidden, Enter to skip): ')"
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

# --- VPN ---
disable_vpn() {
  # Drop the override from .env first, so "up --remove-orphans" below removes gluetun and the pinger
  env_unset COMPOSE_FILE; env_unset VPN_PROVIDER; env_unset VPN_LOCATION
  clear_mullvad_switch
  ok "No VPN: the bot uses this machine's connection"
}

# docker-compose.vpn.yml belongs to the installer, so it's replaced on every run: older versions sent
# all of the bot's traffic through the VPN. A changed copy is kept as docker-compose.vpn.yml.bak.
fetch_vpn_compose() {
  local tmp; tmp="$(mktemp)"
  curl -fsSL "${RAW}/docker-compose.vpn.yml" -o "${tmp}" \
    || { rm -f "${tmp}"; die "Couldn't download docker-compose.vpn.yml from ${RAW}"; }
  if [ -f docker-compose.vpn.yml ] && ! cmp -s "${tmp}" docker-compose.vpn.yml; then
    cp docker-compose.vpn.yml docker-compose.vpn.yml.bak
    ok "Updated docker-compose.vpn.yml (your old one is docker-compose.vpn.yml.bak)"
  fi
  cat "${tmp}" > docker-compose.vpn.yml; rm -f "${tmp}"
}

enable_vpn() {  # enable_vpn "<provider label>" "<location label>"
  fetch_vpn_compose
  env_set COMPOSE_FILE "${VPN_COMPOSE_FILE}"
  env_set VPN_PROVIDER "\"$1\""
  env_set VPN_LOCATION "\"$2\""
}

valid_key() { [[ "$1" =~ ^[A-Za-z0-9+/]{43}=$ ]]; }

rank_cities() {  # rank_cities <provider> <on|off free only> <label>; sets RANKED: the 10 fastest cities, tab-separated rows
  info "Finding the fastest $3 locations from here..."
  mkdir -p vpn
  # gluetun writes its own server list into vpn/, so the city names match what it expects
  docker run --rm -v "$(pwd)/vpn:/gluetun" "${GLUETUN_IMAGE}" format-servers "-$1" >/dev/null 2>&1 \
    || warn "Couldn't read gluetun's server list; using the online copy"
  docker pull -q "${IMAGE}" >/dev/null 2>&1 || true
  local args=(--provider "$1" --top 10)
  if [ "$2" = on ]; then args+=(--free); fi
  RANKED="$(docker run --rm -v "$(pwd)/vpn:/gluetun:ro" "${IMAGE}" python /app/vpn_select.py "${args[@]}")" \
    || die "Couldn't test the $3 locations (see above)."
}

pick_city() {  # pick_city <provider> <on|off free only> <label>; sets PICK_COUNTRY, PICK_CITY and RANKED
  local rows choice line count
  rank_cities "$1" "$2" "$3"
  rows="$(printf '%s\n' "${RANKED}" | head -n 5)"
  count="$(printf '%s\n' "${rows}" | wc -l | tr -d ' ')"
  echo "Fastest $3 locations from here:"
  while IFS=$'\t' read -r rank country city ms; do
    printf '  %s) %-34s %s ms\n' "${rank}" "${city}, ${country}" "${ms}"
  done <<< "${rows}"
  choice="$(ask "Choose [1-${count}] (Enter = 1): ")"
  line="$(printf '%s\n' "${rows}" | awk -F'\t' -v n="${choice:-1}" '$1 == n')"
  if [ -z "${line}" ]; then line="$(printf '%s\n' "${rows}" | head -n 1)"; fi
  PICK_COUNTRY="$(printf '%s' "${line}" | cut -f2)"
  PICK_CITY="$(printf '%s' "${line}" | cut -f3)"
  ok "Using ${PICK_CITY}, ${PICK_COUNTRY}"
}

# --- Mullvad server switching ---
# The bot moves the VPN to another Mullvad server when the current one is down: first the other
# servers in the chosen city, then the next-fastest cities (VPN_FALLBACK_CITIES). It tells gluetun
# through gluetun's control server, with a key that allows exactly that (vpn/auth/config.toml).
# gluetun mustn't also restart the VPN by itself then: in v3.41.3, a server change during its connection
# check makes it restart in a loop (qdm12/gluetun#3485). So HEALTH_RESTART_VPN=off, and the bot reconnects.
GLUETUN_RESTART=""
set_self_restart() {  # set_self_restart on|off: whether gluetun restarts the VPN by itself (vpn.env)
  [ -f vpn.env ] || return 0
  local tmp; tmp="$(mktemp)"
  grep -v -e '^HEALTH_RESTART_VPN=' -e '^# The bot reconnects the VPN itself' vpn.env > "${tmp}" || true
  if [ "$1" = off ]; then
    { echo "# The bot reconnects the VPN itself (see docker-compose.vpn.yml)"; echo "HEALTH_RESTART_VPN=off"; } >> "${tmp}"
  fi
  cat "${tmp}" > vpn.env; rm -f "${tmp}"  # cat keeps vpn.env's permissions (600)
}

set_fallback_cities() {  # set_fallback_cities <chosen city>; uses RANKED
  local cities
  cities="$( { printf '%s\n' "$1"; printf '%s\n' "${RANKED}" | cut -f3; } | awk 'NF && !seen[$0]++' | head -n 10 | paste -sd, -)"
  env_set VPN_FALLBACK_CITIES "\"${cities}\""
  ok "If a Mullvad server goes down, the bot switches to another one in: ${cities//,/, }"
}

setup_mullvad_switch() {
  local key tmp
  key="$(env_get GLUETUN_API_KEY)"
  if [ -z "${key}" ]; then
    key="$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')"
    env_set GLUETUN_API_KEY "${key}"
  fi
  mkdir -p vpn/auth
  tmp="$(mktemp)"
  {
    echo "# Written by install.sh: lets the scanbot bot switch Mullvad servers, and nothing else"
    echo "[[roles]]"
    echo 'name = "scanbot"'
    echo 'routes = ["PUT /v1/vpn/settings", "PUT /v1/vpn/status", "GET /v1/vpn/status"]'
    echo 'auth = "apikey"'
    echo "apikey = \"${key}\""
  } > "${tmp}"
  if ! cmp -s "${tmp}" vpn/auth/config.toml 2>/dev/null; then
    rm -f vpn/auth/config.toml
    (umask 077; cat "${tmp}" > vpn/auth/config.toml)
    GLUETUN_RESTART="yes"  # gluetun reads it only when it starts
  fi
  rm -f "${tmp}"
  set_self_restart off
}

clear_mullvad_switch() {
  env_unset VPN_FALLBACK_CITIES; env_unset GLUETUN_API_KEY
  if [ -f vpn/auth/config.toml ]; then rm -f vpn/auth/config.toml; GLUETUN_RESTART="yes"; fi
  set_self_restart on
}

setup_provider() {  # setup_provider mullvad|protonvpn
  local provider="$1" label key address="" free="off"
  if [ "${provider}" = mullvad ]; then label="Mullvad"; else label="Proton VPN"; fi
  if [ "${provider}" = protonvpn ]; then
    warn "Proton's terms forbid 'attempting to access, probe, or connect to computing devices without proper"
    warn "authorization'. Big scans of servers you don't run may count; Mullvad's terms have no such rule."
    case "${SCANBOT_VPN_FREE:-$(ask 'Is your Proton account on the free plan? [Y/n]: ')}" in
      [nN]*) free="off" ;;
      *) free="on" ;;
    esac
  fi
  echo "Get the key from a WireGuard config file made in your ${label} account:"
  if [ "${provider}" = mullvad ]; then
    echo "  https://mullvad.net/en/account/wireguard-config"
  else
    echo "  https://account.proton.me/u/0/vpn/WireGuard"
  fi
  # Re-running with the same provider: Enter keeps the saved key and address
  local old_key="" old_address="" keep=""
  if grep -q "^VPN_SERVICE_PROVIDER=${provider}$" vpn.env 2>/dev/null; then
    old_key="$(grep -m1 '^WIREGUARD_PRIVATE_KEY=' vpn.env | cut -d= -f2- || true)"
    old_address="$(grep -m1 '^WIREGUARD_ADDRESSES=' vpn.env | cut -d= -f2- || true)"
    keep=", Enter = keep the current one"
  fi
  key="${WIREGUARD_PRIVATE_KEY:-$(ask_secret "PrivateKey from the [Interface] section (hidden${keep}): ")}"
  key="$(printf '%s' "${key:-${old_key}}" | tr -d '[:space:]')"
  valid_key "${key}" || die "That doesn't look like a WireGuard private key (44 characters ending in '='). Run again with --vpn."
  if [ "${provider}" = mullvad ]; then
    address="${WIREGUARD_ADDRESSES:-$(ask "Address from the [Interface] section (like 10.64.12.34/32${keep}): ")}"
    address="$(printf '%s' "${address:-${old_address}}" | tr ',' '\n' | tr -d ' ' | grep -m1 -F . || true)"
    [[ "${address}" =~ ^[0-9.]+/[0-9]+$ ]] || die "That doesn't look like an IPv4 address like 10.64.12.34/32. Run again with --vpn."
  fi
  pick_city "${provider}" "${free}" "${label}"
  (umask 077; {
    echo "VPN_SERVICE_PROVIDER=${provider}"
    echo "VPN_TYPE=wireguard"
    echo "WIREGUARD_PRIVATE_KEY=${key}"
    if [ -n "${address}" ]; then echo "WIREGUARD_ADDRESSES=${address}"; fi
    echo "SERVER_COUNTRIES=${PICK_COUNTRY}"
    echo "SERVER_CITIES=${PICK_CITY}"
    if [ "${provider}" = protonvpn ]; then echo "FREE_ONLY=${free}"; fi
    echo "# Refresh gluetun's server list every 20 days"
    echo "UPDATER_PERIOD=480h"
  } > vpn.env)
  enable_vpn "${label}" "${PICK_CITY}, ${PICK_COUNTRY}"
  if [ "${provider}" = mullvad ]; then
    set_fallback_cities "${PICK_CITY}"
    setup_mullvad_switch
  else
    clear_mullvad_switch
  fi
}

setup_warp() {
  local arch sha profile endpoint host port endpoint_ip address
  if grep -q "^WIREGUARD_ENDPOINT_PORT=2408$" vpn.env 2>/dev/null; then
    ok "Keeping your existing Cloudflare WARP keys"
    enable_vpn "Cloudflare WARP" "nearest (automatic)"
    clear_mullvad_switch
    return
  fi
  case "$(docker info --format '{{.Architecture}}' 2>/dev/null)" in
    x86_64|amd64) arch="amd64"; sha="${WGCF_SHA256_AMD64}" ;;
    aarch64|arm64) arch="arm64"; sha="${WGCF_SHA256_ARM64}" ;;
    *) die "Cloudflare WARP setup supports amd64 and arm64 only." ;;
  esac
  info "Registering a free Cloudflare WARP device (wgcf ${WGCF_VERSION})..."
  mkdir -p vpn/warp
  docker run --rm -v "$(pwd)/vpn/warp:/work" -w /work alpine:3 sh -c "
    wget -q -O /tmp/wgcf https://github.com/ViRb3/wgcf/releases/download/v${WGCF_VERSION}/wgcf_${WGCF_VERSION}_linux_${arch} &&
    echo '${sha}  /tmp/wgcf' | sha256sum -c - >/dev/null &&
    chmod +x /tmp/wgcf &&
    /tmp/wgcf register --accept-tos >/dev/null 2>&1 &&
    /tmp/wgcf generate >/dev/null 2>&1 &&
    chmod 644 wgcf-profile.conf" || die "Couldn't create Cloudflare WARP keys."
  profile="vpn/warp/wgcf-profile.conf"
  field() { grep -m1 "^$1 = " "${profile}" | cut -d' ' -f3-; }
  endpoint="$(field Endpoint)"; host="${endpoint%:*}"; port="${endpoint##*:}"
  # gluetun needs the endpoint as an IP address
  endpoint_ip="$(docker run --rm alpine:3 getent hosts "${host}" 2>/dev/null | awk '{print $1}' | grep -m1 -F . || true)"
  address="$(field Address | tr ',' '\n' | tr -d ' ' | grep -m1 -F . || true)"
  (umask 077; {
    echo "VPN_SERVICE_PROVIDER=custom"
    echo "VPN_TYPE=wireguard"
    echo "WIREGUARD_ENDPOINT_IP=${endpoint_ip:-${WARP_ENDPOINT_FALLBACK}}"
    echo "WIREGUARD_ENDPOINT_PORT=${port:-2408}"
    echo "WIREGUARD_PUBLIC_KEY=$(field PublicKey)"
    echo "WIREGUARD_PRIVATE_KEY=$(field PrivateKey)"
    echo "WIREGUARD_ADDRESSES=${address}"
    echo "WIREGUARD_MTU=1280"
  } > vpn.env)
  rm -rf vpn/warp  # The account file isn't needed once the keys are in vpn.env
  ok "Cloudflare WARP keys saved to vpn.env"
  enable_vpn "Cloudflare WARP" "nearest (automatic)"
  clear_mullvad_switch
}

if [ -n "${VPN_SETUP}" ]; then
  choice="${SCANBOT_VPN:-}"
  if [ -z "${choice}" ] && has_tty; then
    cat >/dev/tty <<'EOF'

Send the bot's traffic through a VPN?
  0) No VPN (default)
  1) Mullvad           paid; key from your Mullvad account
  2) Proton VPN        free or paid; key from your Proton account
  3) Cloudflare WARP   free; set up automatically, keeps your country
EOF
    case "$(ask 'Choose [0-3]: ')" in
      1) choice="mullvad" ;;
      2) choice="protonvpn" ;;
      3) choice="warp" ;;
      *) choice="none" ;;
    esac
  fi
  case "${choice:-none}" in
    none) disable_vpn ;;
    mullvad|protonvpn) setup_provider "${choice}" ;;
    warp) setup_warp ;;
    *) die "Unknown SCANBOT_VPN value: ${choice} (use none, mullvad, protonvpn or warp)" ;;
  esac
elif vpn_enabled; then
  fetch_vpn_compose
  ok "Keeping your VPN: $(env_get VPN_PROVIDER | tr -d '"'), $(env_get VPN_LOCATION | tr -d '"')"
  # Installs from before Mullvad server switching get it now
  if grep -q '^VPN_SERVICE_PROVIDER=mullvad$' vpn.env 2>/dev/null; then
    if [ -z "$(env_get VPN_FALLBACK_CITIES)" ]; then
      rank_cities mullvad off Mullvad
      set_fallback_cities "$(grep -m1 '^SERVER_CITIES=' vpn.env | cut -d= -f2- | cut -d, -f1)"
    fi
    setup_mullvad_switch
  fi
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

# --- DNS ---
# The bot sends every lookup to Quad9 over TLS (port 853) and falls back to HTTPS (port 443) by itself.
# Some networks block 853, so check from a container, the same way the bot goes out, and pin HTTPS
# if so: that skips a failed try on every start. A DNS_TRANSPORT you set yourself is left alone.
if [ -z "$(env_get DNS_TRANSPORT)" ]; then
  dns_check='
import socket, sys
def reachable(port):
    try:
        socket.create_connection(("9.9.9.9", port), 5).close()
        return True
    except OSError:
        return False
sys.exit(0 if reachable(853) else 1 if reachable(443) else 2)
'
  dns_rc=0
  docker run --rm --entrypoint python "${IMAGE}" -c "${dns_check}" >/dev/null 2>&1 || dns_rc=$?
  case "${dns_rc}" in
    0) ok "DNS-over-TLS (port 853) works from here" ;;
    1)
      env_set DNS_TRANSPORT doh
      warn "Port 853 (DNS-over-TLS) is blocked here, so the bot sends its DNS to Quad9 over HTTPS (port 443) instead."
      ;;
    2) warn "A container can't reach Quad9 (9.9.9.9) on port 853 or 443. The bot will need Docker's internet access to work." ;;
  esac
fi

if vpn_enabled; then info "Starting the VPN, its pinger and the bot..."; fi
vpn_failed=""
# A gluetun that's already running must restart to read a new server-switching key; a new one reads it anyway
gluetun_running="$(docker compose ps -q --status running gluetun 2>/dev/null || true)"
docker compose up -d --remove-orphans || die "docker compose up failed."
if vpn_enabled; then
  if [ -n "${GLUETUN_RESTART}" ] && [ -n "${gluetun_running}" ] \
     && [ "$(docker compose ps -q gluetun 2>/dev/null || true)" = "${gluetun_running}" ]; then
    # Still the same gluetun, so it hasn't read vpn/auth/config.toml yet (the pinger restarts with it)
    info "Restarting the VPN so it picks up the server-switching key..."
    docker compose restart gluetun >/dev/null 2>&1 || warn "Couldn't restart gluetun. Run: docker compose restart gluetun"
  fi
  # The bot doesn't wait for the VPN: it runs anyway and checks servers through the API until the VPN is up.
  # gluetun checks its connection every 5 s, so a working VPN is healthy within about 15 s.
  vpn_health=""
  for _ in $(seq 1 60); do
    vpn_health="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{end}}' \
      "$(docker compose ps -q gluetun 2>/dev/null)" 2>/dev/null || true)"
    if [ "${vpn_health}" = healthy ] || [ "${vpn_health}" = unhealthy ]; then break; fi
    sleep 1
  done
  if [ "${vpn_health}" != healthy ]; then
    vpn_failed="yes"
    warn "The VPN didn't connect. Last gluetun log lines:"
    docker compose logs --tail 15 gluetun 2>&1 || true
    warn "Check the key in $(pwd)/vpn.env, or run again with --vpn. If your network blocks the VPN's UDP port, add WIREGUARD_ENDPOINT_PORT to vpn.env (Mullvad also accepts 53 or 123). Then: docker compose up -d"
    if [ -n "$(env_get GLUETUN_API_KEY)" ]; then
      warn "With Mullvad, the bot also keeps trying: it reconnects, then moves to other servers."
    fi
    warn "Until then the bot still runs, and checks every server through the API only."
  fi
fi

if vpn_enabled && [ -z "${vpn_failed}" ]; then
  vpn_ip=""
  for _ in $(seq 1 20); do
    vpn_ip="$(docker compose logs gluetun 2>&1 | grep -oE "Public IP address is .*" | tail -n 1 || true)"
    if [ -n "${vpn_ip}" ]; then break; fi
    sleep 1
  done
  ok "VPN connected ($(env_get VPN_PROVIDER | tr -d '"'), $(env_get VPN_LOCATION | tr -d '"'))${vpn_ip:+. ${vpn_ip}}"
  pinger_up=""
  for _ in $(seq 1 20); do
    if docker compose logs pinger 2>&1 | grep -q "Pinger listening"; then pinger_up="yes"; break; fi
    sleep 1
  done
  if [ -n "${pinger_up}" ]; then
    ok "Pinger ready: server pings go through the VPN; Discord, the APIs and DNS use this machine's connection"
  else
    warn "The pinger hasn't started yet. Check it with: docker compose logs pinger"
  fi
fi

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
    if vpn_enabled; then
      # The bot's first check can run before the VPN is up; with a working VPN, wait for the next one.
      # Only its latest verdict counts: an older run of the bot may be in the log too.
      vpn_verdict() { printf '%s' "${logs}" | grep -oE "Pings through the VPN .*" | tail -n 1 || true; }
      if [ -z "${vpn_failed}" ] && ! vpn_verdict | grep -q "^Pings through the VPN work"; then
        info "Waiting for the bot's first ping through the VPN..."
        for _ in $(seq 1 60); do
          logs="$(docker compose logs scanbot 2>&1 || true)"
          if vpn_verdict | grep -q "^Pings through the VPN work"; then break; fi
          sleep 1
        done
      fi
      vpn_verdict | sed 's/^/    /'
    else
      # The startup probe's verdict(s): direct pings or API only, for Java (and Bedrock, if the bot has it)
      printf '%s' "${logs}" | grep -oE "Direct (Bedrock (\(UDP\) )?)?pings? .*" | tail -n 2 | sed 's/^/    /' || true
    fi
    ;;
  bad-token)
    # Stop it so it doesn't keep retrying a bad login (Discord blocks IPs that do that a lot)
    docker compose stop scanbot >/dev/null 2>&1 || true
    die "Discord rejected the token. Put the right one in $(pwd)/${TOKEN_FILE}, then: docker compose up -d"
    ;;
  error)
    docker compose stop scanbot >/dev/null 2>&1 || true
    if printf '%s' "${logs}" | grep -q "DNS lookups failed"; then
      die "The bot can't look up any names over TLS or HTTPS (see above), so its container probably has no internet access. Fix that, then: docker compose up -d"
    fi
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
  curl -fsSL ${RAW}/install.sh | bash -s -- --vpn
                                                   set up, change or remove the VPN
EOF
