"""
Tests for install.sh's docker-compose.yml refresh: a rerun updates the file unless the user changed it.

The installer's functions are sourced into bash (nothing else in install.sh runs then), with curl
replaced by a function that "downloads" a local file. Needs bash; skipped where there is none.

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
COMPOSE = REPO / 'docker-compose.yml'


def find_bash():
    path = shutil.which('bash')
    if path and os.name == 'nt' and 'system32' in path.lower():
        return None  # WSL's launcher: it can't see Windows paths the same way
    return path


BASH = find_bash()

# Stands in for curl: "downloads" $UPSTREAM to the -o target, or fails like curl -f when it's empty
CURL_STUB = """
curl() {
  local out=""
  while [ $# -gt 0 ]; do
    case "$1" in
      -o) out="$2"; shift 2 ;;
      *) shift ;;
    esac
  done
  [ -n "${UPSTREAM}" ] || return 22
  cp "${UPSTREAM}" "${out}"
}
"""


def write(path, text):
    Path(path).write_bytes(text.encode('utf-8'))  # Bytes: no \r\n on Windows


def read(path):
    return Path(path).read_bytes().decode('utf-8')


CURRENT = read(COMPOSE).replace('\r\n', '\n')  # As downloaded: a Windows checkout may have \r\n
# A made-up newer version, as a later release might publish it
NEWER = CURRENT.replace('  scanbot:\n', '  scanbot:\n    stop_grace_period: 45s\n', 1)


def without_watchtower(text):
    """The compose file after following the README's "delete the watchtower: block"."""
    return text[:text.index('  # Automatic updates:')].rstrip('\n') + '\n'


def without_watchtower_service(text):
    """Only the watchtower: service deleted; its comment lines left behind."""
    return text[:text.index('  watchtower:')]


@unittest.skipIf(BASH is None, "bash isn't installed")
class InstallerTestCase(unittest.TestCase):
    def setUp(self):
        self.assertNotEqual(NEWER, CURRENT)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.upstream = self.dir / 'upstream.yml'
        self.install_dir = self.dir / 'scanbot'
        self.install_dir.mkdir()
        write(self.install_dir / '.env', "# Scanbot settings (read by docker compose)\nTZ=UTC\n")

    def bash(self, script, upstream=True):
        env = dict(os.environ, INSTALLER=INSTALLER.as_posix(),
                   UPSTREAM=self.upstream.as_posix() if upstream else '')
        return subprocess.run([BASH, '-c', 'source "${INSTALLER}"\n' + CURL_STUB + script],
                              cwd=self.install_dir, env=env, capture_output=True, text=True,
                              encoding='utf-8', timeout=60)

    def digest(self, text):
        path = self.dir / 'digest-me.yml'
        write(path, text)
        done = self.bash(f'compose_digest "{path.as_posix()}"')
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout.strip()

    def fetch(self, upstream_text, upstream=True):
        write(self.upstream, upstream_text)
        return self.bash('fetch_compose', upstream=upstream)

    def local(self):
        return read(self.install_dir / 'docker-compose.yml')

    def env(self):
        return read(self.install_dir / '.env')

    def backup_exists(self):
        return (self.install_dir / 'docker-compose.yml.bak').exists()


class ComposeDigestTests(InstallerTestCase):
    def test_the_current_compose_file_is_known(self):
        digest = self.digest(CURRENT)
        done = self.bash(f'known_compose_digest {digest}')
        self.assertEqual(done.returncode, 0,
                         f"docker-compose.yml changed: add its digest {digest} to KNOWN_COMPOSE_DIGESTS in install.sh, "
                         "so installs with the old one are still updated")

    def test_every_published_compose_file_is_known(self):
        git = shutil.which('git')
        if git is None:
            self.skipTest("git isn't installed")
        shallow = subprocess.run([git, 'rev-parse', '--is-shallow-repository'], cwd=REPO,
                                 capture_output=True, text=True)
        if shallow.returncode != 0 or shallow.stdout.strip() != 'false':
            self.skipTest("needs the whole git history")
        commits = subprocess.run([git, 'log', '--format=%H', '--', 'docker-compose.yml'], cwd=REPO,
                                 capture_output=True, text=True, check=True).stdout.split()
        self.assertTrue(commits)
        for commit in commits:
            text = subprocess.run([git, 'show', f'{commit}:docker-compose.yml'], cwd=REPO, capture_output=True,
                                  check=True).stdout.decode('utf-8')
            with self.subTest(commit=commit[:7]):
                self.assertEqual(self.bash(f'known_compose_digest {self.digest(text)}').returncode, 0)

    def test_blank_lines_trailing_spaces_and_windows_line_endings_are_not_changes(self):
        messy = '\n' + CURRENT.replace('services:\n', 'services:\n\n\n').replace('\n', '  \r\n') + '\n\n'
        self.assertEqual(self.digest(messy), self.digest(CURRENT))

    def test_deleting_the_watchtower_block_is_not_a_change(self):
        self.assertEqual(self.digest(without_watchtower(CURRENT)), self.digest(CURRENT))
        self.assertEqual(self.digest(without_watchtower_service(CURRENT)), self.digest(CURRENT))

    def test_any_other_edit_is_a_change(self):
        self.assertNotEqual(self.digest(CURRENT.replace('max-size: 10m', 'max-size: 50m', 1)), self.digest(CURRENT))
        self.assertNotEqual(self.digest(NEWER), self.digest(CURRENT))

    def test_the_watchtower_block_is_the_last_thing_in_the_file(self):
        # drop_watchtower would also drop comments written after the block, so nothing may follow it
        after = CURRENT[CURRENT.index('  watchtower:'):].splitlines()[1:]
        self.assertTrue(all(line == '' or line.startswith('    ') for line in after), after)


class FetchComposeTests(InstallerTestCase):
    def test_a_new_install_downloads_the_file_and_remembers_its_digest(self):
        done = self.fetch(CURRENT)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("Downloaded docker-compose.yml", done.stdout)
        self.assertEqual(self.local(), CURRENT)
        self.assertIn(f"SCANBOT_COMPOSE_DIGEST={self.digest(CURRENT)}\n", self.env())
        self.assertIn("TZ=UTC\n", self.env())  # The rest of .env is kept
        self.assertFalse(self.backup_exists())

    def test_an_unchanged_file_is_left_alone(self):
        write(self.install_dir / 'docker-compose.yml', CURRENT)
        done = self.fetch(CURRENT)
        self.assertIn("docker-compose.yml is up to date", done.stdout)
        self.assertEqual(self.local(), CURRENT)
        self.assertFalse(self.backup_exists())

    def test_a_file_the_installer_wrote_is_updated_and_the_old_one_backed_up(self):
        self.fetch(CURRENT)
        done = self.fetch(NEWER)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("Updated docker-compose.yml (your old one is docker-compose.yml.bak)", done.stdout)
        self.assertEqual(self.local(), NEWER)
        self.assertEqual(read(self.install_dir / 'docker-compose.yml.bak'), CURRENT)
        self.assertIn(f"SCANBOT_COMPOSE_DIGEST={self.digest(NEWER)}\n", self.env())
        self.assertEqual(self.env().count("SCANBOT_COMPOSE_DIGEST="), 1)

    def test_a_published_file_from_before_the_digest_was_kept_is_updated(self):
        write(self.install_dir / 'docker-compose.yml', CURRENT)  # An older install: no digest in .env
        done = self.fetch(NEWER)
        self.assertIn("Updated docker-compose.yml", done.stdout)
        self.assertEqual(self.local(), NEWER)

    def test_a_file_without_the_watchtower_block_stays_without_it(self):
        for old in (without_watchtower(CURRENT), without_watchtower_service(CURRENT)):
            with self.subTest(old=old[-60:]):
                write(self.install_dir / 'docker-compose.yml', old)
                done = self.fetch(NEWER)
                self.assertIn("still without the watchtower block", done.stdout)
                self.assertEqual(self.local(), without_watchtower(NEWER))
                self.assertNotIn('watchtower:', self.local())
                self.assertIn('stop_grace_period: 45s', self.local())
                self.assertEqual(read(self.install_dir / 'docker-compose.yml.bak'), old)

    def test_the_next_update_after_that_still_works(self):
        write(self.install_dir / 'docker-compose.yml', without_watchtower(CURRENT))
        self.fetch(NEWER)
        done = self.fetch(NEWER.replace('max-size: 10m', 'max-size: 20m'))
        self.assertIn("still without the watchtower block", done.stdout)
        self.assertIn('max-size: 20m', self.local())

    def test_a_changed_file_is_kept_and_the_user_is_told(self):
        self.fetch(CURRENT)
        digest_line = f"SCANBOT_COMPOSE_DIGEST={self.digest(CURRENT)}\n"
        edited = CURRENT.replace('max-size: 10m', 'max-size: 50m', 1)
        write(self.install_dir / 'docker-compose.yml', edited)
        done = self.fetch(NEWER)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("docker-compose.yml was changed by hand, so it's kept as it is", done.stdout)
        self.assertIn("mv docker-compose.yml docker-compose.yml.bak", done.stdout)
        self.assertEqual(self.local(), edited)
        self.assertFalse(self.backup_exists())
        self.assertIn(digest_line, self.env())  # Still the digest of what the installer wrote

    def test_a_failed_download_stops_the_installer_and_changes_nothing(self):
        write(self.install_dir / 'docker-compose.yml', CURRENT)
        done = self.fetch(NEWER, upstream=False)
        self.assertEqual(done.returncode, 1)
        self.assertIn("Couldn't download docker-compose.yml", done.stderr)
        self.assertEqual(self.local(), CURRENT)
        self.assertNotIn("SCANBOT_COMPOSE_DIGEST", self.env())


class StateDirTests(InstallerTestCase):
    """make_state_dir: the folder scans are saved in, writable by the user the bot runs as."""

    # Stand-ins for id and chown: the tests don't run as root
    AS_ROOT = """
id() { echo 0; }
chown() { echo "chown $*" >> calls; }
"""

    def test_it_is_created(self):
        done = self.bash('make_state_dir')
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertTrue((self.install_dir / 'state').is_dir())
        self.assertEqual(done.stdout, '')

    def test_under_sudo_it_is_given_to_the_bots_user(self):
        write(self.install_dir / '.env', "SCANBOT_UID=1000\nSCANBOT_GID=1001\n")
        done = self.bash(self.AS_ROOT + 'make_state_dir')
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(read(self.install_dir / 'calls'), "chown 1000:1001 state\n")

    def test_under_sudo_without_a_user_in_env_nothing_is_changed(self):
        done = self.bash(self.AS_ROOT + 'make_state_dir')
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertFalse((self.install_dir / 'calls').exists())

    @unittest.skipIf(os.name == 'nt' or (hasattr(os, 'geteuid') and os.geteuid() == 0),
                     "needs Unix permissions and a user that isn't root")
    def test_a_folder_the_user_cant_write_is_reported_with_the_fix(self):
        state = self.install_dir / 'state'
        state.mkdir()
        state.chmod(0o555)
        self.addCleanup(state.chmod, 0o755)
        done = self.bash('make_state_dir')
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("The bot can't save scans in", done.stdout)
        self.assertIn(f"sudo chown {os.getuid()}:{os.getgid()}", done.stdout)


class SudoTests(InstallerTestCase):
    """detect_sudo, own_files and migrate_from_root: under sudo the files and the bot belong to the user."""

    # Stand-ins for id and chown: the tests don't run as root
    ROOT = """
id() { case "$1" in -u|-g) echo 0 ;; esac; }
chown() { echo "chown $*" >> calls; }
"""

    def run_as_root(self, script, sudo_uid='1234', sudo_gid='1235', sudo_user='alice'):
        setup = (f'export SUDO_UID={sudo_uid} SUDO_GID={sudo_gid} SUDO_USER={sudo_user}\n' if sudo_uid
                 else 'unset SUDO_UID SUDO_GID SUDO_USER\n')
        done = self.bash(self.ROOT + setup + 'detect_sudo\n' + script)
        self.assertEqual(done.returncode, 0, done.stderr)
        return done

    def calls(self):
        path = self.install_dir / 'calls'
        return read(path).splitlines() if path.exists() else []

    def test_under_sudo_the_user_who_ran_it_runs_the_bot(self):
        done = self.run_as_root('echo "$RUN_UID:$RUN_GID|$SUDO_PREFIX|$DC"')
        self.assertEqual(done.stdout.strip(), "1234:1235|sudo |sudo docker compose")

    def test_plain_root_stays_root(self):
        done = self.run_as_root('echo "$RUN_UID:$RUN_GID|$SUDO_PREFIX|$DC"', sudo_uid=None)
        self.assertEqual(done.stdout.strip(), "0:0||docker compose")

    def test_sudo_to_another_user_runs_as_that_user(self):
        # sudo -u bob: SUDO_UID is the one who ran sudo, but the installer runs as bob
        script = 'id() { case "$1" in -u|-g) echo 1001 ;; esac; }\ndetect_sudo\necho "$RUN_UID|$SUDO_PREFIX"'
        done = self.run_as_root(script)
        self.assertEqual(done.stdout.strip(), "1001|")

    def test_the_installers_files_are_given_to_the_user_and_nothing_else(self):
        for name in ('.env', 'docker-compose.yml', 'vpn.env', 'notes.txt'):
            write(self.install_dir / name, 'x\n')
        for name in ('data', 'state', 'vpn'):
            (self.install_dir / name).mkdir()
        self.run_as_root('own_files')
        self.assertEqual(self.calls(), ["chown 1234:1235 .env", "chown 1234:1235 docker-compose.yml",
                                        "chown 1234:1235 vpn.env", "chown -R 1234:1235 data",
                                        "chown -R 1234:1235 vpn", "chown -R 1234:1235 state"])

    def test_a_folder_the_installer_made_is_given_to_the_user_too(self):
        self.run_as_root('CREATED_DIR=yes\nown_files')
        self.assertEqual(self.calls()[0], "chown 1234:1235 .")

    def test_without_sudo_nothing_changes_hands(self):
        write(self.install_dir / 'vpn.env', 'x\n')
        self.run_as_root('own_files', sudo_uid=None)
        self.assertEqual(self.calls(), [])

    def test_an_install_running_as_root_moves_to_the_user(self):
        write(self.install_dir / '.env', "TZ=UTC\nSCANBOT_UID=0\nSCANBOT_GID=0\n")
        done = self.run_as_root('migrate_from_root')
        self.assertIn("SCANBOT_UID=1234\n", self.env())
        self.assertIn("SCANBOT_GID=1235\n", self.env())
        self.assertNotIn("SCANBOT_UID=0", self.env())
        self.assertIn("The bot now runs as alice instead of root", done.stdout)

    def test_plain_root_keeps_running_as_root(self):
        write(self.install_dir / '.env', "SCANBOT_UID=0\nSCANBOT_GID=0\n")
        done = self.run_as_root('migrate_from_root', sudo_uid=None)
        self.assertEqual(self.env(), "SCANBOT_UID=0\nSCANBOT_GID=0\n")
        self.assertEqual(done.stdout, '')


class VpnEnvTests(InstallerTestCase):
    """write_vpn_env and vpn_env_kind: rewriting vpn.env for the same VPN keeps the lines you added."""

    MULLVAD = ("VPN_SERVICE_PROVIDER=mullvad\nWIREGUARD_PRIVATE_KEY=new\nSERVER_COUNTRIES=Serbia\n"
               "SERVER_CITIES=Belgrade\n")

    def vpn_env(self):
        return read(self.install_dir / 'vpn.env')

    def write_new(self, kind, new):
        write(self.dir / 'new.env', new)
        done = self.bash(f'write_vpn_env {kind} < "{(self.dir / "new.env").as_posix()}"')
        self.assertEqual(done.returncode, 0, done.stderr)
        return done

    def kind(self):
        done = self.bash('vpn_env_kind')
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout.strip()

    def test_the_same_provider_keeps_your_own_lines(self):
        write(self.install_dir / 'vpn.env', "VPN_SERVICE_PROVIDER=mullvad\nWIREGUARD_PRIVATE_KEY=old\n"
                                            "SERVER_CITIES=Vienna\n# my port\nWIREGUARD_ENDPOINT_PORT=53\n"
                                            "HEALTH_RESTART_VPN=off\nWIREGUARD_MTU=1300")  # No newline at the end
        done = self.write_new('mullvad', self.MULLVAD)
        self.assertEqual(self.vpn_env(), self.MULLVAD + "# Kept from your previous vpn.env\n"
                                                        "WIREGUARD_ENDPOINT_PORT=53\nWIREGUARD_MTU=1300\n")
        self.assertIn("Kept your own vpn.env settings: WIREGUARD_ENDPOINT_PORT, WIREGUARD_MTU", done.stdout)

    def test_nothing_of_your_own_means_no_kept_section(self):
        write(self.install_dir / 'vpn.env', "VPN_SERVICE_PROVIDER=mullvad\nWIREGUARD_PRIVATE_KEY=old\n"
                                            "HEALTH_RESTART_VPN=off\n")
        done = self.write_new('mullvad', self.MULLVAD)
        self.assertEqual(self.vpn_env(), self.MULLVAD)
        self.assertNotIn("Kept", done.stdout)

    def test_another_provider_starts_fresh_and_says_so(self):
        write(self.install_dir / 'vpn.env', "VPN_SERVICE_PROVIDER=protonvpn\nWIREGUARD_ENDPOINT_PORT=53\n")
        done = self.write_new('mullvad', self.MULLVAD)
        self.assertEqual(self.vpn_env(), self.MULLVAD)
        self.assertIn("vpn.env was for protonvpn", done.stdout)

    def test_a_first_vpn_env_is_just_the_new_settings(self):
        done = self.write_new('mullvad', self.MULLVAD)
        self.assertEqual((self.vpn_env(), done.stdout), (self.MULLVAD, ''))

    @unittest.skipIf(os.name == 'nt', "needs Unix permissions")
    def test_only_you_can_read_it(self):
        self.write_new('mullvad', self.MULLVAD)
        self.assertEqual((self.install_dir / 'vpn.env').stat().st_mode & 0o777, 0o600)

    def test_warp_keys_are_recognised_by_the_marker_whatever_the_port(self):
        write(self.install_dir / 'vpn.env', "# Cloudflare WARP keys made by install.sh\nVPN_SERVICE_PROVIDER=custom\n"
                                            "WIREGUARD_ENDPOINT_PORT=500\n")
        self.assertEqual(self.kind(), 'warp')

    def test_warp_keys_from_before_the_marker_are_recognised_by_their_port(self):
        write(self.install_dir / 'vpn.env', "VPN_SERVICE_PROVIDER=custom\nWIREGUARD_ENDPOINT_PORT=2408\n")
        self.assertEqual(self.kind(), 'warp')

    def test_a_custom_vpn_env_you_wrote_is_not_warp(self):
        write(self.install_dir / 'vpn.env', "VPN_SERVICE_PROVIDER=custom\nWIREGUARD_ENDPOINT_PORT=51820\n"
                                            "WIREGUARD_PRESHARED_KEY=mine\n")
        self.assertEqual(self.kind(), 'custom')
        self.write_new('warp', "# Cloudflare WARP keys made by install.sh\nVPN_SERVICE_PROVIDER=custom\n")
        self.assertNotIn("PRESHARED", self.vpn_env())

    def test_no_vpn_env_has_no_kind(self):
        self.assertEqual(self.kind(), '')


class FindCityTests(InstallerTestCase):
    """find_city: a VPN city by name, for when the ping test is skipped or doesn't work."""

    CITIES = "Serbia\\tBelgrade\\nFrance\\tParis\\nUSA\\tParis\\nAustria\\tVienna\\n"

    def find(self, name):
        done = self.bash(f'rc=0\nfind_city "$(printf \'{self.CITIES}\')" "{name}" || rc=$?\n'
                         'echo "$rc|${PICK_COUNTRY:-}|${PICK_CITY:-}|${FOUND:-}"')
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout.strip()

    def test_a_city_by_name_in_any_case(self):
        self.assertEqual(self.find('belgrade'), "0|Serbia|Belgrade|Belgrade, Serbia")

    def test_a_city_and_its_country_with_spaces_around(self):
        self.assertEqual(self.find(' Vienna , austria '), "0|Austria|Vienna|Vienna, Austria")

    def test_a_name_in_two_countries_needs_the_country(self):
        self.assertEqual(self.find('Paris'), "2|||Paris, France; Paris, USA")
        self.assertEqual(self.find('paris, usa'), "0|USA|Paris|Paris, USA")

    def test_an_unknown_or_empty_name_matches_nothing(self):
        self.assertEqual(self.find('Atlantis'), "1|||")
        self.assertEqual(self.find(''), "1|||")


if __name__ == '__main__':
    unittest.main()
