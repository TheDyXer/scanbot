"""
Tests for scans that hit an error: what was found so far is still posted, and the slot is freed.

Run from the repository root:  python -m unittest discover -s tests
"""
import asyncio
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


class ScanErrorTests(unittest.IsolatedAsyncioTestCase):
    """Whole scans with the pinging faked: 4 servers, 3 answer a direct ping, and then the API phase fails."""

    IPS = b'8.8.8.8\n1.1.1.1\n9.9.9.9\n4.4.4.4\n'

    async def asyncSetUp(self):
        self.api_error = RuntimeError('mcstatus.io sent something odd')
        self.progress = mock.MagicMock(edit=mock.AsyncMock())
        self.ctx = mock.MagicMock()
        self.ctx.author = types.SimpleNamespace(id=1, mention='<@1>')
        self.ctx.guild = types.SimpleNamespace(id=10)
        self.ctx.defer = mock.AsyncMock()
        self.ctx.send = mock.AsyncMock(return_value=self.progress)
        self.ctx.channel.send = mock.AsyncMock(return_value=self.progress)

        async def fake_run_direct(ips, results, state, edition='java', stop=None):
            results.update({ip: scanbot.make_result(ip, ip, 3, 20, [], '1.21', 'hello') for ip in ips[:3]})
            state['found'] = 3
            return [ip for ip in ips if ip not in results]

        async def fake_run_api(session, ips, results, state, retrying, edition='java', stop=None, vpn_down=False):
            raise self.api_error

        for p in (mock.patch.object(scanbot, 'run_direct', fake_run_direct),
                  mock.patch.object(scanbot, 'run_api', fake_run_api),
                  mock.patch.object(scanbot, 'batch_get_locations', mock.AsyncMock(return_value={})),
                  mock.patch.object(scanbot, 'geo_db', None),
                  mock.patch.object(scanbot, 'PINGER_URL', ''),
                  mock.patch.object(scanbot, 'set_status', mock.AsyncMock()),
                  mock.patch.object(scanbot.bot, 'direct_ok', True),
                  mock.patch.object(scanbot.bot, 'direct_ok_bedrock', True),
                  mock.patch.object(scanbot.bot, 'probed_at', time.monotonic()),
                  mock.patch.dict(scanbot.scans, clear=True),
                  mock.patch.object(scanbot, 'queue', [])):
            p.start()
            self.addCleanup(p.stop)

    async def scan(self):
        attachment = types.SimpleNamespace(filename='ips.txt', size=len(self.IPS),
                                           read=mock.AsyncMock(return_value=self.IPS))
        await scanbot.bot.get_command('scan').callback(self.ctx, attachment)

    def texts(self):
        calls = self.ctx.send.await_args_list + self.ctx.channel.send.await_args_list
        return [call.args[0] for call in calls if call.args]

    async def test_partial_results_survive_an_error_in_a_phase(self):
        with self.assertLogs('scanbot', level='ERROR') as logs:
            await self.scan()
        results = [t for t in self.texts() if 'Scan failed' in t]
        self.assertEqual(len(results), 1, self.texts())
        self.assertIn('partial results', results[0])
        self.assertIn('3 with players', results[0])
        self.assertIn('stopped the scan early', results[0])
        self.assertNotIn('Speed', results[0])
        self.assertIn('Error', self.progress.edit.await_args.kwargs['content'])
        self.assertIn('mcstatus.io sent something odd', "\n".join(logs.output))
        self.assertEqual(scanbot.scans, {})  # The slot is free again

    async def test_cancelling_still_cancels(self):
        # asyncio.CancelledError isn't an Exception: a hard shutdown must not turn into "partial results"
        self.api_error = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.scan()
        self.assertFalse(any('Scan failed' in t for t in self.texts()))
        self.assertEqual(scanbot.scans, {})


if __name__ == '__main__':
    unittest.main()
