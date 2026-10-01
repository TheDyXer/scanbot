"""
Whole runs of install.sh, against stand-ins for docker, curl and sleep: what it writes, what it asks docker to do
and what it prints, for a first install, reruns, a new token, sudo, the VPN setups and their failures.

The stand-ins are scripts on PATH: docker logs every call and answers like a working machine (STUB_* variables
change that), curl copies the repository's own files. The installer runs in a new session, so it has no terminal
and asks nothing. Needs bash and a Unix system; skipped on Windows (CI runs them on Linux).

Run from the repository root:  python -m unittest discover -s tests
"""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
INSTALLER = REPO / 'install.sh'
BASH = shutil.which('bash')
KEY = 'a' * 43 + '='  # A well-formed WireGuard private key
WARP_MARKER = "# Cloudflare WARP keys made by install.sh"

DOCKER = r'''#!/usr/bin/env bash
# Stand-in for docker: logs every call and answers like a machine where everything works
args="$*"; printf '%s\n' "docker ${args//$'\n'/ }" >> "${STUB_LOG}"  # One line per call
mount_source() {  # The host side of the first -v option
  while [ $# -gt 0 ]; do
    if [ "$1" = -v ]; then printf '%s' "${2%%:*}"; return; fi
    shift
  done
}
case "$1" in
  info)
    if [ "${2:-}" = --format ]; then echo x86_64; fi ;;
  inspect) echo healthy ;;
  run)
    case "$*" in
      *"--device /dev/net/tun"*)
        if [ -n "${STUB_NO_TUN:-}" ]; then
          echo 'docker: Error response from daemon: error gathering device information while adding custom device "/dev/net/tun": no such file or directory.' >&2
          exit 125
        fi ;;
      *"vpn_select.py"*"--cities"*)
        printf 'Serbia\tBelgrade\nFrance\tParis\nUSA\tParis\nAustria\tVienna\n' ;;
      *"vpn_select.py"*)
        if [ -n "${STUB_RANK_FAIL:-}" ]; then
          echo "No VPN server answered a ping. Is ICMP blocked on this network?" >&2
          exit 2
        fi
        printf '1\tAustria\tVienna\t12\n2\tSerbia\tBelgrade\t20\n' ;;
      *wgcf*)
        cat > "$(mount_source "$@")/wgcf-profile.conf" <<'EOF'
[Interface]
PrivateKey = WARPprivateKeyFromWgcfAAAAAAAAAAAAAAAAAAAAA=
Address = 172.16.0.2/32, 2606:4700:110:8a36::1/128
[Peer]
PublicKey = bmXOC+F1FxEMF9dyiK2H5/1SUtzH0JuVo51h2wPfgyo=
Endpoint = engage.cloudflareclient.com:2408
EOF
        ;;
      *getent*) echo "162.159.192.7 engage.cloudflareclient.com" ;;
    esac ;;
  compose)
    shift
    case "$1" in
      ps)
        case "$*" in
          *scanbot*) echo "${STUB_BOT_ID:-}" ;;
          *gluetun*) echo "g1" ;;
        esac ;;
      logs)
        case "$*" in
          *scanbot*)
            if [[ "$*" == *--since* ]]; then printf '%b\n' "${STUB_BOT_LOG_NEW:-${STUB_BOT_LOG}}"
            else printf '%b\n' "${STUB_BOT_LOG}"; fi ;;
          *gluetun*) echo "Public IP address is 1.2.3.4 (Austria)" ;;
          *pinger*) echo "Pinger listening on port 8765" ;;
        esac ;;
    esac ;;
esac
exit 0
'''

CURL = r'''#!/usr/bin/env bash
# Stand-in for curl: "downloads" the repository's own copy of the file
out=""; url=""
while [ $# -gt 0 ]; do
  case "$1" in
    -o) out="$2"; shift 2 ;;
    -*) shift ;;
    *) url="$1"; shift ;;
  esac
done
printf '%s\n' "curl ${url}" >> "${STUB_LOG}"
tr -d '\r' < "${STUB_REPO}/$(basename "${url}")" > "${out}"
'''

# As root under sudo: id says 0, chown only logs
ID_ROOT = '''#!/usr/bin/env bash
case "$1" in -u|-g) echo 0 ;; *) PATH=/usr/bin:/bin exec id "$@" ;; esac
'''
CHOWN = '''#!/usr/bin/env bash
printf '%s\\n' "chown $*" >> "${STUB_LOG}"
'''


@unittest.skipIf(os.name == 'nt' or BASH is None, "needs bash on a Unix system")
class InstallerRunTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.stubs = self.dir / 'stubs'
        self.stubs.mkdir()
        self.log = self.dir / 'calls.log'
        self.log.touch()
        self.stub('docker', DOCKER)
        self.stub('curl', CURL)
        self.stub('sleep', '#!/bin/sh\nexit 0\n')
        self.install = self.dir / 'scanbot'

    def stub(self, name, text):
        path = self.stubs / name
        path.write_text(text, encoding='utf-8')
        path.chmod(0o755)

    def as_root(self, sudo=True):
        """Run as root: under sudo by alice (uid 1234) unless sudo is False."""
        self.stub('id', ID_ROOT)
        self.stub('chown', CHOWN)
        return {'SUDO_UID': '1234', 'SUDO_GID': '1235', 'SUDO_USER': 'alice'} if sudo else {}

    def run_installer(self, *args, **env):
        base = {k: v for k, v in os.environ.items()
                if not k.startswith(('SCANBOT_', 'WIREGUARD_', 'SUDO_')) and k != 'DISCORD_TOKEN'}
        base.update(PATH=f"{self.stubs}:{os.environ['PATH']}", STUB_LOG=str(self.log), STUB_REPO=str(REPO),
                    STUB_BOT_LOG='Logged in as Scanbot#0001\\nPings through the VPN work (Vienna)')
        base.update(env)
        return subprocess.run([BASH, str(INSTALLER), *args], cwd=self.dir, env=base, capture_output=True,
                              text=True, encoding='utf-8', timeout=120, start_new_session=True)

    def ok(self, *args, **env):
        done = self.run_installer(*args, **env)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        return done

    def fails(self, *args, **env):
        done = self.run_installer(*args, **env)
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        return done

    def calls(self):
        return self.log.read_text(encoding='utf-8').splitlines()

    def called(self, text):
        return [c for c in self.calls() if text in c]

    def read(self, name):
        return (self.install / name).read_text(encoding='utf-8')

    def env_file(self):
        return self.read('.env')


class FirstInstallTests(InstallerRunTestCase):
    def test_a_first_install_writes_its_files_and_starts_the_bot(self):
        done = self.ok(DISCORD_TOKEN='first-token')
        self.assertIn(f"SCANBOT_UID={os.getuid()}\n", self.env_file())
        self.assertIn("SCANBOT_COMPOSE_DIGEST=", self.env_file())
        self.assertEqual(self.read('data/token.txt'), "first-token\n")
        self.assertEqual((self.install / 'data/token.txt').stat().st_mode & 0o777, 0o600)
        self.assertTrue((self.install / 'docker-compose.yml').exists())
        self.assertTrue((self.install / 'state').is_dir())
        self.assertTrue(self.called("docker compose up -d --remove-orphans"))
        self.assertIn("Scanbot is running.", done.stdout)
        self.assertIn("  docker compose logs -f scanbot", done.stdout)
        self.assertIn("| bash -s -- --token", done.stdout)
        self.assertNotIn("sudo", done.stdout)

    def test_without_a_token_or_a_terminal_it_says_how_to_start_later(self):
        done = self.ok()
        self.assertIn("No token yet", done.stdout)
        self.assertIn("&& docker compose up -d", done.stdout)
        self.assertFalse(self.called("compose up"))

    def test_a_rerun_keeps_the_token_and_the_compose_file(self):
        self.ok(DISCORD_TOKEN='first-token')
        done = self.ok()
        self.assertIn("Keeping your existing data/token.txt", done.stdout)
        self.assertIn("docker-compose.yml is up to date", done.stdout)
        self.assertFalse(self.called("restart scanbot"))


class TokenTests(InstallerRunTestCase):
    def setUp(self):
        super().setUp()
        self.ok(DISCORD_TOKEN='old-token')

    def test_a_new_token_replaces_the_saved_one_and_restarts_the_bot(self):
        # The running bot's log still says the old token was rejected: only what it logs after the restart counts
        done = self.ok(DISCORD_TOKEN='new-token', STUB_BOT_ID='b1', STUB_BOT_LOG='Improper token has been passed.',
                       STUB_BOT_LOG_NEW='Logged in as Scanbot#0001')
        self.assertEqual(self.read('data/token.txt'), "new-token\n")
        self.assertEqual((self.install / 'data/token.txt').stat().st_mode & 0o777, 0o600)
        self.assertIn("Replaced the token", done.stdout)
        self.assertTrue(self.called("docker compose restart scanbot"))
        self.assertTrue(self.called("logs --since"))
        self.assertIn("Scanbot is running.", done.stdout)

    def test_the_same_token_again_changes_nothing(self):
        done = self.ok(DISCORD_TOKEN='old-token', STUB_BOT_ID='b1')
        self.assertIn("Keeping your existing data/token.txt", done.stdout)
        self.assertFalse(self.called("restart scanbot"))

    def test_the_token_option_without_a_terminal_keeps_the_saved_one(self):
        done = self.ok('--token', STUB_BOT_ID='b1')
        self.assertEqual(self.read('data/token.txt'), "old-token\n")
        self.assertIn("Keeping your existing data/token.txt", done.stdout)

    def test_a_rejected_token_points_to_the_token_option(self):
        done = self.fails(STUB_BOT_LOG='Improper token has been passed.')
        self.assertIn("bash -s -- --token", done.stderr)
        self.assertTrue(self.called("docker compose stop scanbot"))


class SudoTests(InstallerRunTestCase):
    def test_under_sudo_the_files_belong_to_the_user_and_the_hints_say_sudo(self):
        done = self.ok(DISCORD_TOKEN='a-token', **self.as_root())
        self.assertIn("SCANBOT_UID=1234\nSCANBOT_GID=1235\n", self.env_file())
        chowns = self.called("chown")
        for expected in ("chown 1234:1235 .", "chown 1234:1235 .env", "chown 1234:1235 docker-compose.yml",
                         "chown -R 1234:1235 data", "chown -R 1234:1235 state"):
            self.assertIn(expected, chowns)
        self.assertIn("  sudo docker compose logs -f scanbot", done.stdout)
        self.assertIn("| sudo bash -s -- --vpn", done.stdout)

    def test_a_folder_that_was_there_already_keeps_its_owner(self):
        self.install.mkdir()
        self.ok(DISCORD_TOKEN='a-token', **self.as_root())
        self.assertNotIn("chown 1234:1235 .", self.called("chown"))

    def test_a_sudo_run_that_stops_early_still_gives_the_files_to_the_user(self):
        done = self.ok(**self.as_root())  # No token: it stops after writing .env and the compose file
        self.assertIn("&& sudo docker compose up -d", done.stdout)
        self.assertIn("chown 1234:1235 .env", self.called("chown"))

    def test_a_sudo_run_that_fails_still_gives_the_files_to_the_user(self):
        self.fails(DISCORD_TOKEN='a-token', SCANBOT_VPN='warp', STUB_NO_TUN='1', **self.as_root())
        self.assertIn("chown 1234:1235 .env", self.called("chown"))

    def test_an_install_that_ran_the_bot_as_root_moves_to_the_user(self):
        self.install.joinpath('data').mkdir(parents=True)
        self.install.joinpath('.env').write_text("SCANBOT_UID=0\nSCANBOT_GID=0\nTZ=UTC\n", encoding='utf-8')
        self.install.joinpath('data/token.txt').write_text("a-token\n", encoding='utf-8')
        done = self.ok(**self.as_root())
        self.assertIn("SCANBOT_UID=1234\n", self.env_file())
        self.assertIn("The bot now runs as alice instead of root", done.stdout)
        self.assertIn("chown -R 1234:1235 data", self.called("chown"))

    def test_plain_root_runs_the_bot_as_root_and_is_told(self):
        done = self.ok(DISCORD_TOKEN='a-token', **self.as_root(sudo=False))
        self.assertIn("SCANBOT_UID=0\n", self.env_file())
        self.assertIn("You're installing as root", done.stdout)
        self.assertFalse([c for c in self.called("chown") if "-R" in c or c.endswith(" .env")])
        self.assertNotIn("sudo docker compose", done.stdout)


class VpnTests(InstallerRunTestCase):
    MULLVAD = dict(SCANBOT_VPN='mullvad', WIREGUARD_PRIVATE_KEY=KEY, WIREGUARD_ADDRESSES='10.64.1.2/32')

    def setUp(self):
        super().setUp()
        self.install.joinpath('data').mkdir(parents=True)
        self.install.joinpath('data/token.txt').write_text("a-token\n", encoding='utf-8')

    def vpn_env(self):
        return self.read('vpn.env')

    def add_to_vpn_env(self, text):
        with open(self.install / 'vpn.env', 'a', encoding='utf-8') as f:
            f.write(text)

    def test_mullvad_with_the_fastest_city(self):
        done = self.ok(**self.MULLVAD)
        env = self.vpn_env()
        for line in ("VPN_SERVICE_PROVIDER=mullvad", f"WIREGUARD_PRIVATE_KEY={KEY}", "WIREGUARD_ADDRESSES=10.64.1.2/32",
                     "SERVER_COUNTRIES=Austria", "SERVER_CITIES=Vienna", "HEALTH_RESTART_VPN=off"):
            self.assertIn(line + "\n", env)
        self.assertIn('VPN_FALLBACK_CITIES="Vienna,Belgrade"', self.env_file())
        self.assertIn("COMPOSE_FILE=docker-compose.yml:docker-compose.vpn.yml", self.env_file())
        self.assertIn("VPN connected", done.stdout)

    def test_a_vpn_rerun_with_the_same_provider_keeps_your_own_lines(self):
        self.ok(**self.MULLVAD)
        self.add_to_vpn_env("WIREGUARD_ENDPOINT_PORT=53\n")
        done = self.ok('--vpn', **self.MULLVAD)
        env = self.vpn_env()
        self.assertIn("# Kept from your previous vpn.env\nWIREGUARD_ENDPOINT_PORT=53\n", env)
        self.assertEqual(env.count("HEALTH_RESTART_VPN=off"), 1)
        self.assertIn("Kept your own vpn.env settings: WIREGUARD_ENDPOINT_PORT", done.stdout)

    def test_another_provider_starts_a_fresh_vpn_env(self):
        self.ok(**self.MULLVAD)
        self.add_to_vpn_env("WIREGUARD_ENDPOINT_PORT=53\n")
        done = self.ok('--vpn', SCANBOT_VPN='protonvpn', SCANBOT_VPN_FREE='yes', WIREGUARD_PRIVATE_KEY=KEY)
        env = self.vpn_env()
        self.assertIn("VPN_SERVICE_PROVIDER=protonvpn\n", env)
        self.assertIn("FREE_ONLY=on\n", env)
        self.assertNotIn("ENDPOINT_PORT", env)
        self.assertIn("vpn.env was for mullvad", done.stdout)

    def test_no_tun_device_stops_it_before_the_keys_are_needed(self):
        done = self.fails(SCANBOT_VPN='mullvad', STUB_NO_TUN='1')
        self.assertIn("sudo modprobe tun", done.stderr)
        self.assertFalse(self.called("vpn_select.py"))
        self.assertFalse((self.install / 'vpn.env').exists())

    def test_a_named_city_skips_the_ping_test(self):
        done = self.ok(SCANBOT_VPN_CITY='belgrade', **self.MULLVAD)
        self.assertIn("SERVER_COUNTRIES=Serbia\nSERVER_CITIES=Belgrade\n", self.vpn_env())
        self.assertFalse([c for c in self.called("vpn_select.py") if "--cities" not in c])
        self.assertIn('VPN_FALLBACK_CITIES="Belgrade"', self.env_file())
        self.assertIn("Without the ping test it stays in that city", done.stdout)

    def test_a_city_in_two_countries_needs_its_country(self):
        done = self.fails(SCANBOT_VPN_CITY='Paris', **self.MULLVAD)
        self.assertIn("Paris, France; Paris, USA", done.stderr)
        self.ok('--vpn', SCANBOT_VPN_CITY='Paris, USA', **self.MULLVAD)
        self.assertIn("SERVER_COUNTRIES=USA\nSERVER_CITIES=Paris\n", self.vpn_env())

    def test_a_failed_ping_test_without_a_terminal_asks_for_a_named_city(self):
        done = self.fails(STUB_RANK_FAIL='1', **self.MULLVAD)
        self.assertIn("Couldn't test the Mullvad locations", done.stdout)
        self.assertIn("Set SCANBOT_VPN_CITY", done.stderr)

    def test_warp_keys_survive_a_changed_port_and_mtu(self):
        self.ok(SCANBOT_VPN='warp')
        env = self.vpn_env()
        self.assertTrue(env.startswith(WARP_MARKER + "\n"))
        self.assertIn("WIREGUARD_ENDPOINT_PORT=2408\n", env)
        (self.install / 'vpn.env').write_text(env.replace("PORT=2408", "PORT=500").replace("MTU=1280", "MTU=1200"),
                                              encoding='utf-8')
        done = self.ok('--vpn', SCANBOT_VPN='warp')
        self.assertIn("Keeping your existing Cloudflare WARP keys", done.stdout)
        self.assertIn("WIREGUARD_ENDPOINT_PORT=500\n", self.vpn_env())
        self.assertIn("WIREGUARD_MTU=1200\n", self.vpn_env())
        self.assertEqual(len(self.called("wgcf")), 1)

    def test_older_warp_keys_get_the_marker_and_are_kept(self):
        (self.install / 'vpn.env').write_text("VPN_SERVICE_PROVIDER=custom\nWIREGUARD_ENDPOINT_PORT=2408\n"
                                              f"WIREGUARD_PRIVATE_KEY={KEY}\n", encoding='utf-8')
        self.ok('--vpn', SCANBOT_VPN='warp')
        self.assertTrue(self.vpn_env().startswith(WARP_MARKER + "\n"))
        self.assertFalse(self.called("wgcf"))

    def test_a_custom_vpn_env_you_wrote_gets_new_warp_keys(self):
        (self.install / 'vpn.env').write_text("VPN_SERVICE_PROVIDER=custom\nWIREGUARD_ENDPOINT_PORT=51820\n"
                                              f"WIREGUARD_PRIVATE_KEY={KEY}\n", encoding='utf-8')
        self.ok('--vpn', SCANBOT_VPN='warp')
        self.assertEqual(len(self.called("wgcf")), 1)
        self.assertNotIn(KEY, self.vpn_env())

    def test_no_vpn_without_a_terminal_keeps_the_files(self):
        self.ok(**self.MULLVAD)
        done = self.ok('--vpn', SCANBOT_VPN='none')
        self.assertIn("Kept the VPN files", done.stdout)
        self.assertTrue((self.install / 'vpn.env').exists())
        self.assertNotIn("COMPOSE_FILE=", self.env_file())


if __name__ == '__main__':
    unittest.main()
