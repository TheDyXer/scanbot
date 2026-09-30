"""
Tests for the "⚡ Speed" line in a scan's results: it's timed over the direct-ping and API phases only,
so geolocation, the VPN check and sending messages don't drag it down, and it shows each phase's own
rate when both ran.

Run from the repository root:  python -m unittest discover -s tests
"""
import os
import sys
import time
import types
import unittest
from unittest import mock

# bot.py exits at import time without a token
os.environ.setdefault('DISCORD_TOKEN', 'test-token')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import bot as scanbot  # noqa: E402


class SpeedTextTests(unittest.TestCase):
    """speed_text(direct, api): each phase is (servers checked, seconds), or None if it didn't run."""

    def test_both_phases_show_the_overall_speed_and_each_phases_own(self):
        # 5,000 servers pinged in 300 s, 4,900 of them retried through the API in 980 s
        self.assertEqual(scanbot.speed_text((5000, 300.0), (4900, 980.0)),
                         "⚡ **Speed:** 3.91 IPs/sec (direct 16.67/s · API 5.00/s)")

    def test_direct_only_is_just_the_number(self):
        self.assertEqual(scanbot.speed_text((50, 2.0), None), "⚡ **Speed:** 25.00 IPs/sec")

    def test_api_only_says_so(self):
        self.assertEqual(scanbot.speed_text(None, (100, 20.0)), "⚡ **Speed:** 5.00 IPs/sec (API)")

    def test_nothing_ran_shows_nothing(self):
        self.assertEqual(scanbot.speed_text(None, None), '')

    def test_a_phase_that_took_no_time_is_ignored(self):
        # The clock can read the same twice for a very fast phase; never divide by zero
        self.assertEqual(scanbot.speed_text((10, 0.0), None), '')
        self.assertEqual(scanbot.speed_text((10, 0.0), (5, 1.0)), "⚡ **Speed:** 5.00 IPs/sec (API)")


class Clock:
    """A clock the fake scan phases move forward, so nothing here depends on real time."""

    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class SendResultsTests(unittest.IsolatedAsyncioTestCase):
    async def results(self, stopped, **timings):
        ctx = mock.MagicMock()
        ctx.channel.send = mock.AsyncMock()
        await scanbot.send_results(ctx, [], {}, stopped, 100, 65.0, **timings)
        return ctx.channel.send.await_args.args[0]

    async def test_the_speed_line_uses_the_phase_timings_not_the_total_time(self):
        text = await self.results(False, direct=(100, 5.0), api=(40, 20.0))
        self.assertIn("⏱️ **Time:** 1m 5s", text)
        self.assertIn("⚡ **Speed:** 4.00 IPs/sec (direct 20.00/s · API 2.00/s)", text)

    async def test_a_stopped_scan_shows_no_speed(self):
        self.assertNotIn("Speed", await self.results(True, direct=(100, 5.0)))

    async def test_no_timings_no_speed_line(self):
        text = await self.results(False)
        self.assertIn("⏱️ **Time:** 1m 5s", text)
        self.assertNotIn("Speed", text)


class ScanSpeedTests(unittest.IsolatedAsyncioTestCase):
    """A whole scan: the phases are faked and move a fake clock, so the expected text is exact."""

    IPS = b'8.8.8.8\n1.1.1.1\n9.9.9.9\n4.4.4.4\n'

    async def asyncSetUp(self):
        self.clock = Clock()
        self.answered = 3        # Servers that answer the direct ping (the rest go through the API)
        self.direct_seconds = 2
        self.api_seconds = 4
        self.geo_seconds = 100
        self.progress = mock.MagicMock(edit=mock.AsyncMock())
        self.ctx = mock.MagicMock()
        self.ctx.author = types.SimpleNamespace(id=1, mention='<@1>')
        self.ctx.guild = types.SimpleNamespace(id=10)
        self.ctx.defer = mock.AsyncMock()
        self.ctx.send = mock.AsyncMock(return_value=self.progress)
        self.ctx.channel.send = mock.AsyncMock(return_value=self.progress)

        def online(ip):
            return scanbot.make_result(ip, ip, 3, 20, [], '1.21', 'hello')

        async def fake_run_direct(ips, results, state, edition='java', stop=None):
            self.clock.advance(self.direct_seconds)
            results.update({ip: online(ip) for ip in ips[:self.answered]})
            return [ip for ip in ips if ip not in results]

        async def fake_run_api(session, ips, results, state, retrying, edition='java', stop=None, vpn_down=False):
            self.clock.advance(self.api_seconds)
            results.update({ip: online(ip) for ip in ips})

        async def fake_locations(session, ips, stop=None):
            self.clock.advance(self.geo_seconds)
            return {}

        self.fake_time = types.SimpleNamespace(monotonic=self.clock.monotonic, time=time.time)
        for p in (mock.patch.object(scanbot, 'time', self.fake_time),
                  mock.patch.object(scanbot, 'run_direct', fake_run_direct),
                  mock.patch.object(scanbot, 'run_api', fake_run_api),
                  mock.patch.object(scanbot, 'batch_get_locations', fake_locations),
                  mock.patch.object(scanbot, 'geo_db', None),
                  mock.patch.object(scanbot, 'PINGER_URL', ''),
                  mock.patch.object(scanbot, 'set_status', mock.AsyncMock()),
                  mock.patch.dict(scanbot.scans, clear=True),
                  mock.patch.object(scanbot, 'queue', [])):
            p.start()
            self.addCleanup(p.stop)

    async def scan(self, direct_ok):
        attachment = types.SimpleNamespace(filename='ips.txt', size=len(self.IPS),
                                           read=mock.AsyncMock(return_value=self.IPS))
        with mock.patch.object(scanbot.bot, 'direct_ok', direct_ok), \
                mock.patch.object(scanbot.bot, 'direct_ok_bedrock', direct_ok):
            await scanbot.bot.get_command('scan').callback(self.ctx, attachment)
        calls = self.ctx.send.await_args_list + self.ctx.channel.send.await_args_list
        return [call.args[0] for call in calls if call.args and 'Scan Complete' in call.args[0]][0]

    async def test_direct_and_api_phases_each_get_their_own_rate(self):
        text = await self.scan(direct_ok=True)
        # 4 servers pinged in 2 s, the 1 that didn't answer checked via the API in 4 s: 4 / 6 s overall
        self.assertIn("⚡ **Speed:** 0.67 IPs/sec (direct 2.00/s · API 0.25/s)", text)

    async def test_geolocation_time_counts_in_the_total_but_not_the_speed(self):
        text = await self.scan(direct_ok=True)
        self.assertIn("⏱️ **Time:** 1m 46s", text)  # 2 + 4 + 100 s
        self.assertNotIn("0.04 IPs/sec", text)       # 4 servers / 106 s, which is what it used to show

    async def test_everything_answering_directly_shows_one_rate(self):
        self.answered = 4
        text = await self.scan(direct_ok=True)
        self.assertIn("⚡ **Speed:** 2.00 IPs/sec\n", text)
        self.assertNotIn("API", text)

    async def test_api_only_when_direct_pings_are_blocked(self):
        text = await self.scan(direct_ok=False)
        self.assertIn("⚡ **Speed:** 1.00 IPs/sec (API)", text)  # 4 servers in 4 s


if __name__ == '__main__':
    unittest.main()
