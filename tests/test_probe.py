"""
Tests for the startup probe that decides between direct pings and the API.

Run from the repository root:  python -m unittest discover -s tests
"""
import asyncio
import os
import sys
import unittest
from unittest import mock

# bot.py exits at import time without a token
os.environ.setdefault('DISCORD_TOKEN', 'test-token')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import bot  # noqa: E402


class ProbeTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_answering_server_is_enough(self):
        async def fake_check(server):
            return {'ip': server} if server == bot.PROBE_SERVERS[-1] else None

        with mock.patch.object(bot, 'check_direct', fake_check):
            self.assertTrue(await bot.probe_direct())

    async def test_no_answer_means_direct_pings_are_blocked(self):
        with mock.patch.object(bot, 'check_direct', mock.AsyncMock(return_value=None)):
            self.assertFalse(await bot.probe_direct())

    async def test_a_blocked_probe_server_does_not_crash_startup(self):
        with mock.patch.object(bot, 'check_direct', mock.AsyncMock(side_effect=bot.BlockedAddress('x'))):
            self.assertFalse(await bot.probe_direct())

    async def test_probe_servers_are_public_names(self):
        # A probe that the address filter refuses would silently force API-only mode
        for server in bot.PROBE_SERVERS:
            with self.subTest(server=server):
                self.assertTrue(bot.is_public_entry(server))



class RecheckTests(unittest.IsolatedAsyncioTestCase):
    """Without the VPN, direct pings that failed at startup are tried again every DIRECT_RECHECK seconds."""

    def setUp(self):
        self.probed = []
        self.answers = {'java': False, 'bedrock': False}

        async def fake_probe(edition='java'):
            self.probed.append(edition)
            return self.answers[edition]

        for p in (mock.patch.object(bot, 'probe_direct', fake_probe),
                  mock.patch.object(bot.bot, 'direct_ok', True),
                  mock.patch.object(bot.bot, 'direct_ok_bedrock', False),
                  mock.patch.object(bot.bot, 'probed_at', 0.0),
                  mock.patch.object(bot, 'DIRECT_RECHECK', 0.01)):
            p.start()
            self.addCleanup(p.stop)

    async def test_only_the_edition_that_failed_is_probed_again(self):
        await bot.recheck_direct()
        self.assertEqual(self.probed, ['bedrock'])

    async def test_an_edition_that_still_fails_stays_off(self):
        self.assertEqual(await bot.recheck_direct(), [])
        self.assertFalse(bot.bot.direct_ok_bedrock)
        self.assertGreater(bot.bot.probed_at, 0)

    async def test_an_edition_that_answers_is_switched_back_on(self):
        self.answers['bedrock'] = True
        with self.assertLogs('scanbot', level='INFO') as logs:
            self.assertEqual(await bot.recheck_direct(), ['bedrock'])
        self.assertTrue(bot.bot.direct_ok_bedrock)
        self.assertIn('work now', logs.output[0])

    async def test_the_watcher_stops_once_everything_works(self):
        self.answers['bedrock'] = True
        with self.assertLogs('scanbot', level='INFO'):
            await asyncio.wait_for(bot.watch_direct(), 2)
        self.assertEqual(self.probed, ['bedrock'])

    async def test_the_watcher_keeps_trying_while_pings_fail(self):
        watcher = asyncio.create_task(bot.watch_direct())
        await asyncio.sleep(0.1)
        self.assertFalse(watcher.done())
        self.assertGreater(len(self.probed), 1)
        watcher.cancel()

    async def test_nothing_is_probed_when_everything_works(self):
        with mock.patch.object(bot.bot, 'direct_ok_bedrock', True):
            await asyncio.wait_for(bot.watch_direct(), 2)
        self.assertEqual(self.probed, [])

    def test_the_interval_reads_well_in_the_log(self):
        self.assertEqual([bot.every(s) for s in (300, 60, 90)], ['every 5 minutes', 'every minute', 'every 90 seconds'])


if __name__ == '__main__':
    unittest.main()
