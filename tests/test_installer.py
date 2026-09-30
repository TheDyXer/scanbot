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


if __name__ == '__main__':
    unittest.main()
