"""
Tests for the startup probe that decides between direct pings and the API.

Run from the repository root:  python -m unittest discover -s tests
"""
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


if __name__ == '__main__':
    unittest.main()
