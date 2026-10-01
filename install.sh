#!/usr/bin/env bash
# Scanbot installer. Run it in the folder where you want the bot to live:
#
#   curl -fsSL https://raw.githubusercontent.com/TheDyXer/scanbot/main/install.sh | bash
#
# Creates ./scanbot with docker-compose.yml and data/token.txt, then starts the bot.
# Running it again is safe: it keeps your files and pulls the latest image. It also updates
# docker-compose.yml, unless you changed it (the old one is kept as docker-compose.yml.bak).
# Run with sudo ("| sudo bash") when your user can't use Docker: the files still belong to you, and
# the bot runs as you, not as root.
#
# Options (after "bash -s --" when piping from curl):
#   --vpn           set up, change or remove the VPN (asked automatically on the first install)
#   --token         replace the saved Discord token (asked for, hidden)
#
# Optional environment variables:
#   DISCORD_TOKEN   use this token instead of asking for it; it replaces a saved one
#   SCANBOT_DIR     install folder (default: scanbot)
#   SCANBOT_REF     git branch or tag to download the compose files from (default: main)
#   SCANBOT_IMAGE   image to run instead of ghcr.io/thedyxer/scanbot:latest
#   SCANBOT_VPN     none, mullvad, protonvpn or warp: choose the VPN without being asked
#   WIREGUARD_PRIVATE_KEY, WIREGUARD_ADDRESSES   VPN keys, instead of being asked
#   SCANBOT_VPN_FREE   yes or no: whether a Proton account is on the free plan
#   SCANBOT_VPN_CITY   the VPN city, like Belgrade (or "Paris, France"): skips the ping test
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

# --- docker-compose.yml ---
# Every run brings docker-compose.yml up to date, but only if you haven't changed it. .env keeps the
# digest of the copy the installer last wrote (SCANBOT_COMPOSE_DIGEST); for installs made before that,
# these are the digests of every docker-compose.yml ever published. Deleting the watchtower block (see
# the README, "Automatic updates") isn't a change, and neither are blank lines or trailing spaces.
# Changing docker-compose.yml means adding its digest here: tests/test_installer.py checks that.
KNOWN_COMPOSE_DIGESTS="
6f16e74d995836bad8f870db1f873966bec5d86503f6d1b74a7bb87ae662e3ec
e5015f8c2b2928c0a4e708d45520a4f178692785ffdd874afd3a9b054ec1bead
3812215fcebc935031097339a51d82cdf908a2ba30e0deb521c9e7ece791a3a7
6bd7892bcb419d4b4819ee314e4fd6824196d6024d84e11849df3f4719fe833c
0b22fe255c0cbea678237e24bc995ee6751fb2a2df1494829a1fa165c1239eee
b0180b6f8fd45dea4d20daddc100718404fc964cec9f3c73af37b04cfdf2204c
f42480fb21fe2c911e7be3ded48ee8b8055e86b00349e1157daebafbb1b9de43
70af29717480df28f808ad0d334b3fc9a65148a83e629d8d9988628a52586922
4ffb6680069590e1c3230488897e4394f6f2d757344f9f400e9cbaf1da939f89
94ec221385070a8d2df980847e90f6850270df375c0c198f5add090071ddd2a7
d4fcc8bd425edeccd95198b550b3b9aad9debeef08e83d1edd57bde411dfee0c
"

# The compose file without the watchtower block (from its comment to the next service or top-level
# key, or to the end) and without blank lines at the end
drop_watchtower() {
  awk '/^  # Automatic updates:/ || /^  watchtower:/ { skip = 1; next }
       skip && (/^[A-Za-z]/ || /^  [A-Za-z]/) { skip = 0 }
       !skip { line[++n] = $0 }
       END { while (n > 0 && line[n] ~ /^[ \t\r]*$/) n--; for (i = 1; i <= n; i++) print line[i] }' "$1"
}

sha256_of_stdin() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum; else shasum -a 256; fi | cut -d' ' -f1
}

# What decides whether docker-compose.yml was changed: everything but the watchtower block, blank lines,
# trailing spaces and Windows line endings
compose_digest() {
  drop_watchtower "$1" | tr -d '\r' | awk 'NF { sub(/[ \t]+$/, ""); print }' | sha256_of_stdin
}

known_compose_digest() { printf '%s\n' "${KNOWN_COMPOSE_DIGESTS}" | grep -qx "$1"; }

fetch_compose() {
  local tmp new old
  tmp="$(mktemp)"
  curl -fsSL "${RAW}/docker-compose.yml" -o "${tmp}" \
    || { rm -f "${tmp}"; die "Couldn't download docker-compose.yml from ${RAW}"; }
  new="$(compose_digest "${tmp}")"
  if [ ! -f docker-compose.yml ]; then
    cat "${tmp}" > docker-compose.yml
    ok "Downloaded docker-compose.yml"
  else
    old="$(compose_digest docker-compose.yml)"
    if [ "${old}" = "${new}" ]; then
      ok "docker-compose.yml is up to date"
    elif [ "${old}" = "$(env_get SCANBOT_COMPOSE_DIGEST)" ] || known_compose_digest "${old}"; then
      cp docker-compose.yml docker-compose.yml.bak
      if grep -q '^  watchtower:' docker-compose.yml; then
        cat "${tmp}" > docker-compose.yml
        ok "Updated docker-compose.yml (your old one is docker-compose.yml.bak)"
      else
        drop_watchtower "${tmp}" > docker-compose.yml
        ok "Updated docker-compose.yml, still without the watchtower block (your old one is docker-compose.yml.bak)"
      fi
    else
      rm -f "${tmp}"
      warn "docker-compose.yml was changed by hand, so it's kept as it is. There's a newer one at ${RAW}/docker-compose.yml"
      warn "To use it: mv docker-compose.yml docker-compose.yml.bak, run this installer again, then redo your changes."
      return 0
    fi
  fi
  rm -f "${tmp}"
  env_set SCANBOT_COMPOSE_DIGEST "${new}"
}

# Running scans are saved in ./state, so a restart resumes them. Docker would create a missing folder as root,
# which the bot (SCANBOT_UID) can't write to, so it's made here
make_state_dir() {
  local uid gid
  mkdir -p state
  uid="$(env_get SCANBOT_UID)"; gid="$(env_get SCANBOT_GID)"
  if [ "$(id -u)" = 0 ] && [ -n "${uid}" ] && [ -n "${gid}" ]; then
    chown "${uid}:${gid}" state
  elif [ ! -w state ]; then
    warn "The bot can't save scans in $(pwd)/state, so a restart ends them. Fix: sudo chown $(id -u):$(id -g) $(pwd)/state"
  fi
}

# --- sudo ---
# Under sudo, the files belong to and the bot runs as the user who ran sudo (SUDO_UID), not root. Plain root
# stays root. DC is how the commands this installer prints start: with sudo when it was run with sudo.
detect_sudo() {
  RUN_UID="$(id -u)"; RUN_GID="$(id -g)"; SUDO_PREFIX=""
  if [ "${RUN_UID}" = 0 ] && [ -n "${SUDO_UID:-}" ] && [ "${SUDO_UID}" != 0 ]; then
    RUN_UID="${SUDO_UID}"; RUN_GID="${SUDO_GID:-${SUDO_UID}}"; SUDO_PREFIX="sudo "
  fi
  DC="${SUDO_PREFIX}docker compose"
}

# Under sudo, what the installer made is given to the user who ran it, even when the run stops early (it runs
# on exit). A list, never "chown -R .": SCANBOT_DIR=. installs into a folder with other things in it. gluetun
# writes root-owned files into vpn/, so a chown that fails is skipped.
own_files() {
  [ -n "${SUDO_PREFIX:-}" ] || return 0
  local f
  if [ -n "${CREATED_DIR:-}" ]; then chown "${RUN_UID}:${RUN_GID}" . 2>/dev/null || true; fi
  for f in .env docker-compose.yml docker-compose.yml.bak docker-compose.vpn.yml docker-compose.vpn.yml.bak vpn.env; do
    if [ -e "${f}" ]; then chown "${RUN_UID}:${RUN_GID}" "${f}" 2>/dev/null || true; fi
  done
  for f in data vpn state; do
    if [ -e "${f}" ]; then chown -R "${RUN_UID}:${RUN_GID}" "${f}" 2>/dev/null || true; fi
  done
}

# Installs made with sudo by older installers run the bot as root: move them to the user who ran sudo
migrate_from_root() {
  if [ -n "${SUDO_PREFIX:-}" ] && [ "$(env_get SCANBOT_UID)" = 0 ]; then
    env_set SCANBOT_UID "${RUN_UID}"; env_set SCANBOT_GID "${RUN_GID}"
    ok "The bot now runs as ${SUDO_USER:-user ${RUN_UID}} instead of root"
  fi
}

# --- vpn.env ---
# Written into WARP's vpn.env, so a later run knows the keys are the installer's (a vpn.env written by hand can
# use VPN_SERVICE_PROVIDER=custom too)
WARP_MARKER="# Cloudflare WARP keys made by install.sh"

vpn_env_kind() {  # The VPN in vpn.env: its VPN_SERVICE_PROVIDER, or warp for WARP keys made by this installer
  [ -f vpn.env ] || return 0
  if grep -qxF "${WARP_MARKER}" vpn.env \
     || { grep -q '^VPN_SERVICE_PROVIDER=custom$' vpn.env && grep -q '^WIREGUARD_ENDPOINT_PORT=2408$' vpn.env; }; then
    echo warp  # The second test: WARP keys from before the marker existed
  else
    grep -m1 '^VPN_SERVICE_PROVIDER=' vpn.env | cut -d= -f2- || true
  fi
}

# write_vpn_env <kind> < new settings: writes vpn.env (readable only by you). For the same VPN as before, the
# lines you added yourself (WIREGUARD_ENDPOINT_PORT=53, for example) are kept: every old KEY=value whose key
# the new settings don't have. HEALTH_RESTART_VPN isn't one: the installer sets it itself.
write_vpn_env() {
  local tmp kept="" old_kind line key
  tmp="$(mktemp)"
  cat > "${tmp}"
  old_kind="$(vpn_env_kind)"
  if [ -n "${old_kind}" ] && [ "${old_kind}" = "$1" ]; then
    while IFS= read -r line || [ -n "${line}" ]; do
      [[ "${line}" =~ ^([A-Za-z_][A-Za-z0-9_]*)= ]] || continue
      key="${BASH_REMATCH[1]}"
      if [ "${key}" = HEALTH_RESTART_VPN ] || grep -q "^${key}=" "${tmp}"; then continue; fi
      if [ -z "${kept}" ]; then echo "# Kept from your previous vpn.env" >> "${tmp}"; fi
      printf '%s\n' "${line}" >> "${tmp}"
      kept="${kept:+${kept}, }${key}"
    done < vpn.env
  elif [ -n "${old_kind}" ]; then
    info "vpn.env was for ${old_kind}, so it now has only the new settings"
  fi
  rm -f vpn.env
  (umask 077; cat "${tmp}" > vpn.env)
  rm -f "${tmp}"
  if [ -n "${kept}" ]; then ok "Kept your own vpn.env settings: ${kept}"; fi
}

# find_city "<country TAB city lines>" "<City or City, Country>": sets PICK_COUNTRY and PICK_CITY, ignoring case.
# Returns 1 when no city matches, and 2 when the name is in more than one country (FOUND lists them).
find_city() {
  local matches count
  matches="$(printf '%s\n' "$1" | awk -F'\t' -v want="$2" '
    function norm(s) { gsub(/^[ \t]+|[ \t]+$/, "", s); return tolower(s) }
    BEGIN { n = index(want, ","); if (n) { city = norm(substr(want, 1, n - 1)); country = norm(substr(want, n + 1)) }
            else city = norm(want) }
    NF >= 2 && city != "" && norm($2) == city && (country == "" || norm($1) == country)')"
  count="$(printf '%s' "${matches}" | grep -c . || true)"
  FOUND="$(printf '%s\n' "${matches}" | awk -F'\t' 'NF >= 2 { printf "%s%s, %s", (NR > 1 ? "; " : ""), $2, $1 }')"
  if [ "${count}" = 1 ]; then
    PICK_COUNTRY="$(printf '%s' "${matches}" | cut -f1)"; PICK_CITY="$(printf '%s' "${matches}" | cut -f2)"
    return 0
  fi
  if [ "${count}" = 0 ]; then return 1; fi
  return 2
}

# gluetun needs /dev/net/tun. Some VPS and LXC containers don't have it: say so before asking for keys.
check_tun() {
  local out
  out="$(docker run --rm --device /dev/net/tun:/dev/net/tun --entrypoint true "${IMAGE}" 2>&1)" && return 0
  if printf '%s' "${out}" | grep -q '/dev/net/tun'; then
    die "This machine has no /dev/net/tun, which the VPN needs. Try: sudo modprobe tun (to keep it after a reboot: echo tun | sudo tee /etc/modules-load.d/tun.conf). On a VPS or in an LXC container, ask the provider to turn on TUN/TAP. Or run this again with --vpn and choose 0 (no VPN)."
  fi
  die "Couldn't check for /dev/net/tun, which the VPN needs: ${out}"
}

# tests/test_installer.py sources this file for the functions above; nothing below runs then
if (return 0 2>/dev/null); then return 0; fi

VPN_SETUP=""
NEW_TOKEN=""
for arg in "$@"; do
  case "${arg}" in
    --vpn) VPN_SETUP="yes" ;;
    --token) NEW_TOKEN="yes" ;;
    -h|--help)
      echo "Usage: curl -fsSL ${RAW}/install.sh | bash [-s -- --vpn --token]"
      echo "  --vpn     set up, change or remove the VPN (asked automatically on the first install)"
      echo "  --token   replace the saved Discord token"
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
detect_sudo
CREATED_DIR=""
if [ ! -d "${DIR}" ]; then CREATED_DIR="yes"; fi
mkdir -p "${DIR}/data"
cd "${DIR}"
trap own_files EXIT
info "Installing into $(pwd)"

if [ -f .env ]; then
  ok "Keeping your existing .env"
else
  {
    echo "# Scanbot settings (read by docker compose)"
    echo "SCANBOT_UID=${RUN_UID}"
    echo "SCANBOT_GID=${RUN_GID}"
    echo "# Time zone for the bot's log and the daily 4 AM update check, e.g. Europe/Budapest"
    echo "TZ=UTC"
    if [ -n "${SCANBOT_IMAGE:-}" ]; then echo "SCANBOT_IMAGE=${SCANBOT_IMAGE}"; fi
  } > .env
  ok "Created .env"
  VPN_SETUP="${VPN_SETUP:-yes}"  # First install: ask about the VPN too
  if [ "${RUN_UID}" = 0 ]; then
    warn "You're installing as root, so the bot runs as root. To run it as your own user, run the installer as that user (with sudo if it can't use Docker)."
  fi
fi
migrate_from_root  # Before make_state_dir, which reads SCANBOT_UID
fetch_compose  # After .env, which keeps its digest
make_state_dir
IMAGE="$(env_get SCANBOT_IMAGE)"; IMAGE="${IMAGE:-ghcr.io/thedyxer/scanbot:latest}"
GLUETUN_IMAGE="$(env_get GLUETUN_IMAGE)"; GLUETUN_IMAGE="${GLUETUN_IMAGE:-qmcgaw/gluetun:v3}"

# --- Token ---
# A saved token is kept unless DISCORD_TOKEN or --token gives another one; the bot reads it when it starts,
# so a changed token restarts it below (TOKEN_CHANGED)
TOKEN_FILE="data/token.txt"
TOKEN_CHANGED=""
saved_token=""
if [ -s "${TOKEN_FILE}" ]; then saved_token="$(tr -d '[:space:]' < "${TOKEN_FILE}")"; fi
token="$(printf '%s' "${DISCORD_TOKEN:-}" | tr -d '[:space:]')"
if [ -z "${token}" ] && [ -z "${saved_token}" ]; then
  token="$(ask_secret 'Paste your Discord bot token (hidden, Enter to skip): ' | tr -d '[:space:]')"
elif [ -z "${token}" ] && [ -n "${NEW_TOKEN}" ]; then
  token="$(ask_secret 'Paste the new Discord bot token (hidden, Enter = keep the current one): ' | tr -d '[:space:]')"
fi
if [ -n "${token}" ] && [ "${token}" != "${saved_token}" ]; then
  rm -f "${TOKEN_FILE}"
  (umask 077; printf '%s\n' "${token}" > "${TOKEN_FILE}")
  if [ -n "${saved_token}" ]; then
    TOKEN_CHANGED="yes"
    ok "Replaced the token in ${TOKEN_FILE}"
  else
    ok "Saved the token to ${TOKEN_FILE}"
  fi
elif [ -n "${saved_token}" ]; then
  ok "Keeping your existing ${TOKEN_FILE}"
else
  warn "No token yet. Put it in $(pwd)/${TOKEN_FILE}, then start the bot with:"
  echo "    cd $(pwd) && ${DC} up -d"
  exit 0
fi

# --- VPN ---
DELETE_VPN_FILES=""
disable_vpn() {
  # Drop the override from .env first, so "up --remove-orphans" below removes gluetun and the pinger
  env_unset COMPOSE_FILE; env_unset VPN_PROVIDER; env_unset VPN_LOCATION
  clear_mullvad_switch
  ok "No VPN: the bot uses this machine's connection"
  # The files go after "up" below: gluetun has vpn/ mounted until then
  if [ -e vpn.env ] || [ -e vpn ] || [ -e docker-compose.vpn.yml ] || [ -e docker-compose.vpn.yml.bak ]; then
    if has_tty; then
      case "$(ask 'Also delete the VPN files (vpn.env with your key, vpn/, docker-compose.vpn.yml)? [y/N]: ')" in
        [yY]*) DELETE_VPN_FILES="yes" ;;
      esac
    fi
    if [ -z "${DELETE_VPN_FILES}" ]; then
      ok "Kept the VPN files (vpn.env, vpn/, docker-compose.vpn.yml), so --vpn can use them again"
    fi
  fi
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

server_list() {  # server_list <provider>: gluetun writes its own server list into vpn/, so city names match it
  mkdir -p vpn
  docker run --rm -v "$(pwd)/vpn:/gluetun" "${GLUETUN_IMAGE}" format-servers "-$1" >/dev/null 2>&1 \
    || warn "Couldn't read gluetun's server list; using the online copy"
  docker pull -q "${IMAGE}" >/dev/null 2>&1 || true
}

# rank_cities <provider> <on|off free only> <label>: sets RANKED, the 10 fastest cities as tab-separated rows.
# Fails, with RANKED empty, when the ping test doesn't work (a network that blocks ping, for example).
rank_cities() {
  info "Finding the fastest $3 locations from here..."
  server_list "$1"
  local args=(--provider "$1" --top 10)
  if [ "$2" = on ]; then args+=(--free); fi
  RANKED="$(docker run --rm -v "$(pwd)/vpn:/gluetun:ro" "${IMAGE}" python /app/vpn_select.py "${args[@]}")" || {
    RANKED=""; warn "Couldn't test the $3 locations (see above)."; return 1; }
}

# city_by_name <provider> <on|off free only> <label>: the city named in SCANBOT_VPN_CITY, or asked for by name.
# Sets PICK_COUNTRY and PICK_CITY. For when the ping test is skipped or doesn't work.
city_by_name() {
  local cities answer rc
  local args=(--provider "$1" --cities)
  if [ "$2" = on ]; then args+=(--free); fi
  cities="$(docker run --rm -v "$(pwd)/vpn:/gluetun:ro" "${IMAGE}" python /app/vpn_select.py "${args[@]}" 2>/dev/null)" \
    || cities=""
  [ -n "${cities}" ] || die "Couldn't get the list of $3 cities from ${IMAGE}. Update it (docker pull ${IMAGE}) and run again with --vpn."
  if [ -n "${SCANBOT_VPN_CITY:-}" ]; then
    rc=0; find_city "${cities}" "${SCANBOT_VPN_CITY}" || rc=$?
    case "${rc}" in
      0) ok "Using ${PICK_CITY}, ${PICK_COUNTRY}"; return 0 ;;
      2) die "SCANBOT_VPN_CITY=${SCANBOT_VPN_CITY} is in more than one country (${FOUND}). Add the country, like SCANBOT_VPN_CITY=\"City, Country\"." ;;
      *) die "SCANBOT_VPN_CITY=${SCANBOT_VPN_CITY} isn't a $3 city. The cities: $(printf '%s\n' "${cities}" | cut -f2 | sort -u | paste -sd, - | sed 's/,/, /g')" ;;
    esac
  fi
  has_tty || die "No $3 city chosen, and there's no terminal to ask. Set SCANBOT_VPN_CITY (like SCANBOT_VPN_CITY=Belgrade) and run again with --vpn, or choose 0 (no VPN)."
  echo "$3 cities: $(printf '%s\n' "${cities}" | cut -f2 | sort -u | paste -sd, - | sed 's/,/, /g')"
  for _ in 1 2 3; do
    answer="$(ask 'City (like Belgrade; add the country when a name is in two, like "Paris, France"): ')"
    rc=0; find_city "${cities}" "${answer}" || rc=$?
    case "${rc}" in
      0) ok "Using ${PICK_CITY}, ${PICK_COUNTRY}"; return 0 ;;
      2) warn "There's a ${answer} in more than one country (${FOUND}). Add the country." ;;
      *) warn "There's no $3 city called '${answer}'." ;;
    esac
  done
  die "No $3 city chosen. Run again with --vpn."
}

pick_city() {  # pick_city <provider> <on|off free only> <label>; sets PICK_COUNTRY, PICK_CITY and RANKED
  local rows choice line count
  RANKED=""
  if [ -n "${SCANBOT_VPN_CITY:-}" ]; then
    server_list "$1"  # A named city: no ping test
    city_by_name "$1" "$2" "$3"
    return 0
  fi
  if ! rank_cities "$1" "$2" "$3"; then
    warn "Choose the city by name instead."
    city_by_name "$1" "$2" "$3"
    return 0
  fi
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
  if [ -z "${RANKED}" ]; then
    ok "If a Mullvad server goes down, the bot switches to another one in ${cities}. Without the ping test it stays in that city."
  else
    ok "If a Mullvad server goes down, the bot switches to another one in: ${cities//,/, }"
  fi
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
  check_tun
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
  write_vpn_env "${provider}" < <(
    echo "VPN_SERVICE_PROVIDER=${provider}"
    echo "VPN_TYPE=wireguard"
    echo "WIREGUARD_PRIVATE_KEY=${key}"
    if [ -n "${address}" ]; then echo "WIREGUARD_ADDRESSES=${address}"; fi
    echo "SERVER_COUNTRIES=${PICK_COUNTRY}"
    echo "SERVER_CITIES=${PICK_CITY}"
    if [ "${provider}" = protonvpn ]; then echo "FREE_ONLY=${free}"; fi
    echo "# Refresh gluetun's server list every 20 days"
    echo "UPDATER_PERIOD=480h"
  )
  enable_vpn "${label}" "${PICK_CITY}, ${PICK_COUNTRY}"
  if [ "${provider}" = mullvad ]; then
    set_fallback_cities "${PICK_CITY}"
    setup_mullvad_switch
  else
    clear_mullvad_switch
  fi
}

setup_warp() {
  local arch sha profile endpoint host port endpoint_ip address tmp
  check_tun
  # The keys are kept even when you changed the port or the MTU in vpn.env
  if [ "$(vpn_env_kind)" = warp ] && grep -q '^WIREGUARD_PRIVATE_KEY=.' vpn.env; then
    if ! grep -qxF "${WARP_MARKER}" vpn.env; then  # Made before the marker existed: add it
      tmp="$(mktemp)"
      { echo "${WARP_MARKER}"; cat vpn.env; } > "${tmp}"
      cat "${tmp}" > vpn.env; rm -f "${tmp}"  # cat keeps vpn.env's permissions (600)
    fi
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
  write_vpn_env warp < <(
    echo "${WARP_MARKER}"
    echo "VPN_SERVICE_PROVIDER=custom"
    echo "VPN_TYPE=wireguard"
    echo "WIREGUARD_ENDPOINT_IP=${endpoint_ip:-${WARP_ENDPOINT_FALLBACK}}"
    echo "WIREGUARD_ENDPOINT_PORT=${port:-2408}"
    echo "WIREGUARD_PUBLIC_KEY=$(field PublicKey)"
    echo "WIREGUARD_PRIVATE_KEY=$(field PrivateKey)"
    echo "WIREGUARD_ADDRESSES=${address}"
    echo "WIREGUARD_MTU=1280"
  )
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
  check_tun
  ok "Keeping your VPN: $(env_get VPN_PROVIDER | tr -d '"'), $(env_get VPN_LOCATION | tr -d '"')"
  # Installs from before Mullvad server switching get it now
  if grep -q '^VPN_SERVICE_PROVIDER=mullvad$' vpn.env 2>/dev/null; then
    if [ -z "$(env_get VPN_FALLBACK_CITIES)" ]; then
      rank_cities mullvad off Mullvad || true
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
# The same goes for the bot and a new token. Its log may still hold an older run's verdict, so a bot this run
# starts or restarts is judged only by what it logs from now on.
bot_running="$(docker compose ps -q --status running scanbot 2>/dev/null || true)"
since="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
docker compose up -d --remove-orphans || die "docker compose up failed."
fresh_log="yes"
if [ -n "${bot_running}" ] && [ "$(docker compose ps -q scanbot 2>/dev/null || true)" = "${bot_running}" ]; then
  if [ -n "${TOKEN_CHANGED}" ]; then
    info "Restarting the bot with the new token..."
    docker compose restart scanbot >/dev/null 2>&1 || warn "Couldn't restart the bot. Run: ${DC} restart scanbot"
  else
    fresh_log=""  # Already running and left alone: its whole log counts
  fi
fi
bot_log() {
  if [ -n "${fresh_log}" ]; then docker compose logs --since "${since}" scanbot 2>&1 || true
  else docker compose logs scanbot 2>&1 || true; fi
}
if [ -n "${DELETE_VPN_FILES}" ]; then
  rm -f vpn.env docker-compose.vpn.yml docker-compose.vpn.yml.bak
  if rm -rf vpn 2>/dev/null; then
    ok "Deleted the VPN files"
  else
    warn "Deleted vpn.env, but some files in vpn/ belong to root. Delete them with: sudo rm -rf $(pwd)/vpn"
  fi
fi
if vpn_enabled; then
  if [ -n "${GLUETUN_RESTART}" ] && [ -n "${gluetun_running}" ] \
     && [ "$(docker compose ps -q gluetun 2>/dev/null || true)" = "${gluetun_running}" ]; then
    # Still the same gluetun, so it hasn't read vpn/auth/config.toml yet (the pinger restarts with it)
    info "Restarting the VPN so it picks up the server-switching key..."
    docker compose restart gluetun >/dev/null 2>&1 || warn "Couldn't restart gluetun. Run: ${DC} restart gluetun"
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
    warn "Check the key in $(pwd)/vpn.env, or run again with --vpn. If your network blocks the VPN's UDP port, add WIREGUARD_ENDPOINT_PORT to vpn.env (Mullvad also accepts 53 or 123). Then: ${DC} up -d"
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
    warn "The pinger hasn't started yet. Check it with: ${DC} logs pinger"
  fi
fi

info "Waiting for the bot to log in..."
status="unknown"
for _ in $(seq 1 30); do
  logs="$(bot_log)"
  if printf '%s' "${logs}" | grep -q "Logged in as"; then status="ok"; break; fi
  if printf '%s' "${logs}" | grep -qE "Improper token|LoginFailure"; then status="bad-token"; break; fi
  if printf '%s' "${logs}" | grep -q "Error: "; then status="error"; break; fi
  sleep 1
done

echo
bot_log | tail -n 5
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
          logs="$(bot_log)"
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
    die "Discord rejected the token. Give it the right one: curl -fsSL ${RAW}/install.sh | ${SUDO_PREFIX}bash -s -- --token"
    ;;
  error)
    docker compose stop scanbot >/dev/null 2>&1 || true
    if printf '%s' "${logs}" | grep -q "DNS lookups failed"; then
      die "The bot can't look up any names over TLS or HTTPS (see above), so its container probably has no internet access. Fix that, then: ${DC} up -d"
    fi
    die "The bot stopped with an error (see above). Fix it, then: ${DC} up -d"
    ;;
  *)
    warn "The bot hasn't logged in yet. Check the log with: ${DC} logs -f scanbot"
    ;;
esac

hint() { printf '  %-54s %s\n' "$1" "$2"; }
echo
echo "Useful commands (run them in $(pwd)):"
hint "${DC} logs -f scanbot" "follow the log"
hint "${DC} restart scanbot" "restart the bot"
hint "${DC} pull && ${DC} up -d" "update now (Watchtower also does it daily at 4 AM)"
hint "${DC} down" "stop the bot"
echo "  curl -fsSL ${RAW}/install.sh | ${SUDO_PREFIX}bash -s -- --vpn"
hint "" "set up, change or remove the VPN"
echo "  curl -fsSL ${RAW}/install.sh | ${SUDO_PREFIX}bash -s -- --token"
hint "" "replace the Discord token"
