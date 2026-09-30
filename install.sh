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
  # Drop the override from .env first, so "up --remove-orphans" below removes gluetun
  env_unset COMPOSE_FILE; env_unset VPN_PROVIDER; env_unset VPN_LOCATION
  ok "No VPN: the bot uses this machine's connection"
}

enable_vpn() {  # enable_vpn "<provider label>" "<location label>"
  if [ ! -f docker-compose.vpn.yml ]; then
    curl -fsSL "${RAW}/docker-compose.vpn.yml" -o docker-compose.vpn.yml \
      || die "Couldn't download docker-compose.vpn.yml from ${RAW}"
  fi
  env_set COMPOSE_FILE "${VPN_COMPOSE_FILE}"
  env_set VPN_PROVIDER "\"$1\""
  env_set VPN_LOCATION "\"$2\""
}

valid_key() { [[ "$1" =~ ^[A-Za-z0-9+/]{43}=$ ]]; }

pick_city() {  # pick_city <provider> <on|off free only> <label>; sets PICK_COUNTRY and PICK_CITY
  local rows choice line count
  info "Finding the fastest $3 location from here..."
  mkdir -p vpn
  # gluetun writes its own server list into vpn/, so the city names match what it expects
  docker run --rm -v "$(pwd)/vpn:/gluetun" "${GLUETUN_IMAGE}" format-servers "-$1" >/dev/null 2>&1 \
    || warn "Couldn't read gluetun's server list; using the online copy"
  docker pull -q "${IMAGE}" >/dev/null 2>&1 || true
  local args=(--provider "$1")
  if [ "$2" = on ]; then args+=(--free); fi
  rows="$(docker run --rm -v "$(pwd)/vpn:/gluetun:ro" "${IMAGE}" python /app/vpn_select.py "${args[@]}")" \
    || die "Couldn't test the $3 locations (see above)."
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
}

setup_warp() {
  local arch sha profile endpoint host port endpoint_ip address
  if grep -q "^WIREGUARD_ENDPOINT_PORT=2408$" vpn.env 2>/dev/null; then
    ok "Keeping your existing Cloudflare WARP keys"
    enable_vpn "Cloudflare WARP" "nearest (automatic)"
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
  ok "Keeping your VPN: $(env_get VPN_PROVIDER | tr -d '"'), $(env_get VPN_LOCATION | tr -d '"')"
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

if vpn_enabled; then info "Starting the VPN and the bot (the bot waits until the VPN is connected)..."; fi
if ! docker compose up -d --remove-orphans; then
  if vpn_enabled; then
    warn "The VPN didn't connect. Last gluetun log lines:"
    docker compose logs --tail 15 gluetun 2>&1 || true
    die "Check the key in $(pwd)/vpn.env, or run again with --vpn. If your network blocks the VPN's UDP port, add WIREGUARD_ENDPOINT_PORT to vpn.env (Mullvad also accepts 53 or 123). Then: docker compose up -d"
  fi
  die "docker compose up failed."
fi

if vpn_enabled; then
  vpn_ip=""
  for _ in $(seq 1 20); do
    vpn_ip="$(docker compose logs gluetun 2>&1 | grep -oE "Public IP address is .*" | tail -n 1 || true)"
    if [ -n "${vpn_ip}" ]; then break; fi
    sleep 1
  done
  ok "VPN connected ($(env_get VPN_PROVIDER | tr -d '"'), $(env_get VPN_LOCATION | tr -d '"'))${vpn_ip:+. ${vpn_ip}}"
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
    # The startup probe's verdict(s): direct pings or API only, for Java (and Bedrock, if the bot has it)
    printf '%s' "${logs}" | grep -oE "Direct (Bedrock (\(UDP\) )?)?pings? .*" | tail -n 2 | sed 's/^/    /' || true
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
  curl -fsSL ${RAW}/install.sh | bash -s -- --vpn
                                                   set up, change or remove the VPN
EOF
