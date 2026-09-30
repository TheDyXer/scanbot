"""
Tests for the `api` option of /scan: `api:off` skips the mcstatus.io retry, so only servers that answer
a direct ping are reported. It needs working direct pings, so it's refused when they don't work.

Run from the repository root:  python -m unittest discover -s tests
"""
import os
import sys
import time
import types
import unittest
from unittest import mock

import discord
from discord.ext import commands

# bot.py exits at import time without a token
os.environ.setdefault('DISCORD_TOKEN', 'test-token')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import bot as scanbot  # noqa: E402


class ApiOptionCommandTests(unittest.IsolatedAsyncioTestCase):
    def test_slash_command_offers_on_and_off_and_defaults_to_on(self):
        params = {p.name: p for p in scanbot.bot.tree.get_command('scan').parameters}
        self.assertEqual(list(params), ['file', 'edition', 'api'])
        self.assertFalse(params['api'].required)
        self.assertEqual([c.value for c in params['api'].choices], ['on', 'off'])
        self.assertIn('mcstatus.io', params['api'].description)

    async def parse(self, content):
        att = discord.Attachment(data={'id': 1, 'filename': 'ips.txt', 'size': 10, 'url': 'https://x/y',
                                       'proxy_url': 'https://x/y'}, state=mock.MagicMock())
        msg = mock.MagicMock(attachments=[att], content=content)
        view = commands.view.StringView(content)
        ctx = commands.Context(message=msg, bot=scanbot.bot, view=view, prefix='!')
        view.skip_string('!')
        name = view.get_word()
        view.skip_ws()
        ctx.invoked_with, ctx.command = name, scanbot.bot.get_command(name)
        try:
            await ctx.command._parse_arguments(ctx)
        except commands.BadLiteralArgument as e:
            return 'bad', e.param.name
        return ctx.args[2:]  # after (self-less) ctx and file

    async def test_prefix_command_takes_the_api_option_after_the_edition(self):
        self.assertEqual(await self.parse('!scan bedrock off'), ['bedrock', 'off'])
        self.assertEqual(await self.parse('!scan java on'), ['java', 'on'])
        self.assertEqual(await self.parse('!scan java'), ['java', 'on'])
        self.assertEqual(await self.parse('!scan'), ['java', 'on'])

    async def test_a_bad_value_names_the_option_that_was_wrong(self):
        self.assertEqual(await self.parse('!scan java maybe'), ('bad', 'api'))
        self.assertEqual(await self.parse('!scan off'), ('bad', 'edition'))  # the edition always comes first

    async def test_mistyped_api_option_gets_a_helpful_reply(self):
        ctx = mock.MagicMock(send=mock.AsyncMock())
        param = mock.MagicMock()
        param.name = 'api'
        error = commands.BadLiteralArgument(param, ('on', 'off'), [], 'maybe')
        with self.assertNoLogs('scanbot', level='ERROR'):
            await scanbot.bot.on_command_error(ctx, error)
        message = ctx.send.await_args.args[0]
        self.assertIn('`on`', message)
        self.assertIn('`off`', message)
        self.assertNotIn('edition', message)

    async def test_help_mentions_the_option(self):
        ctx = mock.MagicMock(send=mock.AsyncMock())
        await scanbot.bot.get_command('help').callback(ctx)
        embed = ctx.send.await_args.kwargs['embed']
        text = ' '.join(f.name + f.value for f in embed.fields)
        self.assertIn('api', text)
        self.assertIn('off', text)


class ApiOptionScanTests(unittest.IsolatedAsyncioTestCase):
    """Whole scans with the pinging faked: 4 servers, 3 of which answer a direct ping."""

    IPS = b'8.8.8.8\n1.1.1.1\n9.9.9.9\n4.4.4.4\n'

    async def asyncSetUp(self):
        self.answered = 3
        self.direct_calls = []
        self.api_calls = []
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
            self.direct_calls.append(edition)
            results.update({ip: online(ip) for ip in ips[:self.answered]})
            return [ip for ip in ips if ip not in results]

        async def fake_run_api(session, ips, results, state, retrying, edition='java', stop=None, vpn_down=False):
            self.api_calls.append(list(ips))
            results.update({ip: online(ip) for ip in ips})

        for p in (mock.patch.object(scanbot, 'run_direct', fake_run_direct),
                  mock.patch.object(scanbot, 'run_api', fake_run_api),
                  mock.patch.object(scanbot, 'batch_get_locations', mock.AsyncMock(return_value={})),
                  mock.patch.object(scanbot, 'geo_db', None),
                  mock.patch.object(scanbot, 'PINGER_URL', ''),
                  mock.patch.object(scanbot, 'set_status', mock.AsyncMock()),
                  mock.patch.object(scanbot.bot, 'probed_at', time.monotonic()),
                  mock.patch.dict(scanbot.scans, clear=True),
                  mock.patch.object(scanbot, 'queue', [])):
            p.start()
            self.addCleanup(p.stop)

    def texts(self):
        calls = self.ctx.send.await_args_list + self.ctx.channel.send.await_args_list
        return [call.args[0] for call in calls if call.args]

    def text(self, needle):
        found = [t for t in self.texts() if needle in t]
        return found[0] if found else None

    async def scan(self, *options, direct_ok=True, bedrock_ok=True):
        attachment = types.SimpleNamespace(filename='ips.txt', size=len(self.IPS),
                                           read=mock.AsyncMock(return_value=self.IPS))
        with mock.patch.object(scanbot.bot, 'direct_ok', direct_ok), \
                mock.patch.object(scanbot.bot, 'direct_ok_bedrock', bedrock_ok):
            await scanbot.bot.get_command('scan').callback(self.ctx, attachment, *options)

    async def test_off_never_asks_the_api(self):
        await self.scan('java', 'off')
        self.assertEqual(self.direct_calls, ['java'])
        self.assertEqual(self.api_calls, [])

    async def test_off_reports_only_what_answered_and_counts_the_rest(self):
        await self.scan('java', 'off')
        done = self.text('Scan Complete')
        self.assertIn('🟢 3 with players', done)
        self.assertIn("1 didn't answer a direct ping and wasn't retried through the API", done)
        self.assertNotIn('4.4.4.4', done)  # the one that didn't answer isn't listed as online

    async def test_off_says_so_when_the_scan_starts(self):
        await self.scan('java', 'off')
        self.assertIn('API retry is off', self.text('Scan started'))

    async def test_off_shows_a_single_direct_speed(self):
        await self.scan('java', 'off')
        speed = [line for line in self.text('Scan Complete').splitlines() if 'Speed' in line][0]
        self.assertNotIn('API', speed)

    async def test_off_with_every_server_answering_has_no_note(self):
        self.answered = 4
        await self.scan('java', 'off')
        self.assertNotIn("didn't answer", self.text('Scan Complete'))

    async def test_off_works_for_bedrock_too(self):
        await self.scan('bedrock', 'off')
        self.assertEqual(self.direct_calls, ['bedrock'])
        self.assertEqual(self.api_calls, [])
        self.assertIn('Bedrock', self.text('Scan Complete'))

    async def test_on_is_the_default_and_still_retries_through_the_api(self):
        for options in ((), ('java',), ('java', 'on')):
            with self.subTest(options=options):
                self.api_calls.clear()
                await self.scan(*options)
                self.assertEqual(self.api_calls, [['4.4.4.4']])
                self.assertIsNone(self.text('API retry is off'))
                self.assertNotIn("didn't answer", self.text('Scan Complete'))
                self.ctx.send.reset_mock()
                self.ctx.channel.send.reset_mock()

    async def test_off_is_refused_when_direct_pings_are_blocked(self):
        await self.scan('java', 'off', direct_ok=False)
        reply = self.text('direct pings')
        self.assertIn('api:off', reply)
        self.assertIsNone(self.text('Scan started'))
        self.assertEqual((self.direct_calls, self.api_calls), ([], []))
        self.assertEqual(scanbot.scans, {})  # the scan isn't left registered

    async def test_the_same_scan_with_the_api_on_still_runs_when_direct_pings_are_blocked(self):
        await self.scan('java', 'on', direct_ok=False)
        self.assertIsNotNone(self.text('Scan started'))
        self.assertEqual(self.api_calls, [['8.8.8.8', '1.1.1.1', '9.9.9.9', '4.4.4.4']])

    async def test_off_checks_the_matching_editions_probe(self):
        # Java's TCP port can work while Bedrock's UDP is blocked, and the other way round
        await self.scan('bedrock', 'off', direct_ok=True, bedrock_ok=False)
        self.assertIsNone(self.text('Scan started'))
        self.assertEqual(self.direct_calls, [])
        await self.scan('java', 'off', direct_ok=True, bedrock_ok=False)
        self.assertIsNotNone(self.text('Scan started'))

    async def test_off_is_refused_when_the_vpn_is_down_and_says_so(self):
        with mock.patch.object(scanbot, 'PINGER_URL', 'http://gluetun:8765'):
            await self.scan('java', 'off', direct_ok=False, bedrock_ok=False)
        reply = self.text('VPN')
        self.assertIn('api:off', reply)
        self.assertIsNone(self.text('Scan started'))
        self.assertEqual((self.direct_calls, self.api_calls), ([], []))

    async def test_off_is_refused_if_direct_pings_stop_working_while_queued(self):
        # Fine when it was sent, broken by the time a slot frees up
        with mock.patch.object(scanbot, 'direct_pings_work', mock.AsyncMock(side_effect=[True, False])):
            await self.scan('java', 'off')
        self.assertIn('api:off', self.text('direct pings'))
        self.assertIsNone(self.text('Scan started'))
        self.assertEqual(self.direct_calls, [])
        self.assertEqual(scanbot.scans, {})


if __name__ == '__main__':
    unittest.main()
