"""
Tests for the settings that can be changed in .env: defaults, the allowed ranges, the error a bad value
gives at startup, and what the bot logs about them.

Run from the repository root:  python -m unittest discover -s tests
"""
import os
import subprocess
import sys
import unittest
from unittest import mock

# bot.py exits at import time without a token
os.environ.setdefault('DISCORD_TOKEN', 'test-token')
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
sys.path.insert(0, ROOT)

import bot as scanbot  # noqa: E402
import pinger  # noqa: E402

SETTINGS = ('MAX_IPS_PER_SCAN', 'MAX_CONCURRENT_SCANS', 'DIRECT_CONCURRENCY', 'DIRECT_CONCURRENCY_TOTAL',
            'DIRECT_TIMEOUT', 'API_DELAY', 'GEO_DELAY', 'PROGRESS_INTERVAL', 'GEO_DB_MAX_AGE_DAYS')


def import_bot(env, show):
    """Imports bot.py in a fresh Python with these settings and prints `show` (an expression). Returns the result."""
    clean = {k: v for k, v in os.environ.items() if k not in SETTINGS + ('PINGER_URL', 'DNS_TRANSPORT')}
    clean.update(DISCORD_TOKEN='test-token', PYTHONIOENCODING='utf-8', **env)
    return subprocess.run([sys.executable, '-c', f'import bot; print({show})'], cwd=ROOT, env=clean,
                          capture_output=True, text=True, encoding='utf-8', timeout=60)


class EnvNumberTests(unittest.TestCase):
    def setting(self, value, parse=None, *args):
        parse = parse or scanbot.env_int
        args = args or ('DIRECT_CONCURRENCY', 50, 1, 2000)
        with mock.patch.dict(os.environ, {args[0]: value}):
            return parse(*args)

    def test_unset_and_blank_mean_the_default(self):
        with mock.patch.dict(os.environ):
            os.environ.pop('DIRECT_CONCURRENCY', None)
            self.assertEqual(scanbot.env_int('DIRECT_CONCURRENCY', 50, 1, 2000), 50)
        # Compose passes a variable that isn't in .env on as an empty string
        self.assertEqual(self.setting(''), 50)
        self.assertEqual(self.setting('   '), 50)

    def test_values_in_range_are_used(self):
        self.assertEqual(self.setting('300'), 300)
        self.assertEqual(self.setting(' 1 '), 1)
        self.assertEqual(self.setting('2000'), 2000)
        self.assertEqual(self.setting('2.5', scanbot.env_float, 'DIRECT_TIMEOUT', 3, 0.5, 10), 2.5)
        self.assertEqual(self.setting('10', scanbot.env_float, 'DIRECT_TIMEOUT', 3, 0.5, 10), 10)

    def test_bad_values_name_the_variable_the_range_and_the_value(self):
        for value in ('abc', '0', '2001', '1.5', '-3'):
            with self.subTest(value=value):
                with self.assertRaises(ValueError) as caught:
                    self.setting(value)
                message = str(caught.exception)
                self.assertIn('DIRECT_CONCURRENCY must be a whole number from 1 to 2,000', message)
                self.assertIn(repr(value), message)

    def test_numbers_that_arent_numbers_are_refused(self):
        for value in ('nan', 'inf', '-inf', '0.1', 'three'):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.setting(value, scanbot.env_float, 'DIRECT_TIMEOUT', 3, 0.5, 10)

    def test_the_defaults(self):
        self.assertEqual((scanbot.MAX_IPS_PER_SCAN, scanbot.MAX_CONCURRENT_SCANS, scanbot.MAX_FILE_BYTES),
                         (30000, 5, 2_000_000))
        self.assertEqual(scanbot.DIRECT_CONCURRENCY_TOTAL, 2 * scanbot.DIRECT_CONCURRENCY)
        self.assertEqual((scanbot.DIRECT_TIMEOUT, scanbot.API_DELAY, scanbot.GEO_DELAY), (3, 0.2, 4))
        self.assertEqual((scanbot.PROGRESS_INTERVAL, scanbot.GEO_DB_MAX_AGE_DAYS), (3, 40))


class StartupTests(unittest.TestCase):
    """bot.py imported with settings in the environment, the way the container starts it."""

    def test_settings_from_the_environment_are_used(self):
        done = import_bot({'DIRECT_CONCURRENCY': '300', 'MAX_IPS_PER_SCAN': '100000', 'API_DELAY': '0.5'},
                          'bot.DIRECT_CONCURRENCY, bot.DIRECT_CONCURRENCY_TOTAL, bot.MAX_FILE_BYTES, bot.API_DELAY')
        self.assertEqual(done.returncode, 0, done.stderr)
        # The bot-wide limit follows the per-scan one, and the file size limit follows the list limit
        self.assertEqual(done.stdout.strip(), '300 600 6400000 0.5')

    def test_the_file_size_limit_never_drops_below_2_mb(self):
        done = import_bot({'MAX_IPS_PER_SCAN': '100'}, 'bot.MAX_FILE_BYTES')
        self.assertEqual(done.stdout.strip(), '2000000', done.stderr)

    def test_a_bad_value_stops_the_bot_with_a_clear_message(self):
        done = import_bot({'DIRECT_CONCURRENCY': 'lots'}, '"started"')
        self.assertEqual(done.returncode, 1)
        self.assertNotIn('started', done.stdout)
        self.assertIn("Error: DIRECT_CONCURRENCY must be a whole number from 1 to 2,000, not 'lots'", done.stdout)

    def test_a_timeout_longer_than_the_pinger_allows_is_refused(self):
        done = import_bot({'DIRECT_TIMEOUT': '11'}, '"started"')
        self.assertEqual(done.returncode, 1)
        self.assertIn(f"DIRECT_TIMEOUT must be a number from 0.5 to {pinger.MAX_TIMEOUT}, not '11'", done.stdout)
        self.assertIn(f"the pinger waits at most {pinger.MAX_TIMEOUT} seconds", done.stdout)


class CheckSettingsTests(unittest.TestCase):
    def test_the_log_shows_the_direct_ping_settings(self):
        with mock.patch.object(pinger, 'raise_file_limit', return_value=None), \
                self.assertLogs('scanbot', 'INFO') as logs:
            scanbot.check_settings()
        self.assertIn(f"Direct pings: up to {scanbot.DIRECT_CONCURRENCY} per scan "
                      f"({scanbot.DIRECT_CONCURRENCY_TOTAL} for all scans together), 3 s timeout", logs.output[0])
        self.assertEqual(len(logs.output), 1)

    def test_a_short_api_delay_is_warned_about(self):
        with mock.patch.object(scanbot, 'API_DELAY', 0.1), \
                mock.patch.object(pinger, 'raise_file_limit', return_value=None), \
                self.assertLogs('scanbot', 'WARNING') as logs:
            scanbot.check_settings()
        self.assertIn('API_DELAY is 0.1', logs.output[0])

    def test_a_low_open_file_limit_is_warned_about(self):
        with mock.patch.object(pinger, 'raise_file_limit', return_value=(256, 256)) as raise_limit, \
                self.assertLogs('scanbot', 'WARNING') as logs:
            scanbot.check_settings()
        wanted = raise_limit.call_args.args[0]
        self.assertEqual(wanted, 2 * min(scanbot.DIRECT_CONCURRENCY_TOTAL,
                                         scanbot.MAX_CONCURRENT_SCANS * scanbot.DIRECT_CONCURRENCY) + 1024)
        self.assertIn('Only 256 files can be open at once', logs.output[0])
        self.assertIn('DIRECT_CONCURRENCY', logs.output[0])

    @unittest.skipIf(os.name == 'nt', "Windows has no open-file limit to raise")
    def test_the_open_file_limit_is_raised_as_far_as_allowed(self):
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        wanted = soft + 16 if hard == resource.RLIM_INFINITY else min(soft + 16, hard)
        now, _ = pinger.raise_file_limit(wanted)
        self.assertGreaterEqual(now, wanted)
        self.assertEqual(resource.getrlimit(resource.RLIMIT_NOFILE)[0], now)

    @unittest.skipUnless(os.name == 'nt', "Only Windows has no open-file limit")
    def test_windows_has_no_open_file_limit(self):
        self.assertIsNone(pinger.raise_file_limit(10_000))


if __name__ == '__main__':
    unittest.main()
