"""
Tests for Bedrock Edition support.

Run from the repository root:  python -m unittest discover -s tests
"""
import asyncio
import csv
import io
import os
import sys
import types
import unittest
from unittest import mock

# bot.py exits at import time without a token
os.environ.setdefault('DISCORD_TOKEN', 'test-token')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import discord  # noqa: E402
from discord.ext import commands  # noqa: E402
from mcstatus import BedrockServer  # noqa: E402

import bot as scanbot  # noqa: E402

# What api.mcstatus.io/v2/status/bedrock/demo.mcstatus.io returned
BEDROCK_API_RESPONSE = {
    'online': True, 'host': 'demo.mcstatus.io', 'port': 19132, 'ip_address': '144.172.67.4',
    'version': {'name': '1.19.70', 'protocol': 575},
    'players': {'online': 64, 'max': 100, 'list': []},
    'motd': {'raw': 'A Bedrock server', 'clean': 'A Bedrock server', 'html': '<span>A Bedrock server</span>'},
    'gamemode': 'Survival', 'edition': 'MCPE',
}
JAVA_API_RESPONSE = {
    'online': True, 'ip_address': '93.184.216.34',
    'version': {'name_clean': '1.21', 'name_raw': '1.21', 'protocol': 767},
    'players': {'online': 3, 'max': 20, 'list': [{'name_clean': 'Steve'}]},
    'motd': {'clean': 'hello'},
}


def fake_answer(*addresses):
    return [types.SimpleNamespace(address=a) for a in addresses]


def bedrock_status():
    return types.SimpleNamespace(
        players=types.SimpleNamespace(online=64, max=100),
        version=types.SimpleNamespace(name='1.19.70'),
        motd=types.SimpleNamespace(to_plain=lambda: 'A Bedrock server'),
    )


class SplitEntryTests(unittest.TestCase):
    def test_default_and_explicit_ports(self):
        self.assertEqual(scanbot.split_entry('1.2.3.4', 19132), ('1.2.3.4', 19132))
        self.assertEqual(scanbot.split_entry('play.example.com:19133', 19132), ('play.example.com', 19133))


class BedrockDirectTests(unittest.IsolatedAsyncioTestCase):
    async def check(self, entry, resolved='93.184.216.34', status=None):
        connected_to = []

        async def fake_status(server, **kwargs):
            connected_to.append((server.address.host, server.address.port))
            if isinstance(status, Exception):
                raise status
            return status or bedrock_status()

        with mock.patch.object(scanbot.QUAD9, 'resolve', mock.AsyncMock(return_value=fake_answer(resolved))), \
                mock.patch.object(scanbot.BedrockServer, 'async_status', fake_status):
            try:
                result = await scanbot.check_direct_bedrock(entry)
            except scanbot.BlockedAddress:
                result = 'blocked'
        return result, connected_to

    async def test_public_server_is_pinged_on_the_default_port_at_the_checked_address(self):
        result, connected_to = await self.check('play.example.com')
        self.assertEqual(connected_to, [('93.184.216.34', 19132)])
        self.assertEqual((result['ip'], result['address'], result['edition']),
                         ('play.example.com', '93.184.216.34', 'Bedrock'))
        self.assertEqual((result['players'], result['max'], result['version'], result['motd'], result['names']),
                         (64, 100, '1.19.70', 'A Bedrock server', []))

    async def test_explicit_port_is_used(self):
        _, connected_to = await self.check('play.example.com:19140')
        self.assertEqual(connected_to, [('93.184.216.34', 19140)])

    async def test_private_addresses_are_never_contacted(self):
        for entry in ('127.0.0.1', '192.168.11.220:19132', 'localhost', 'nas.lan'):
            with self.subTest(entry=entry):
                result, connected_to = await self.check(entry)
                self.assertEqual((result, connected_to), ('blocked', []))

    async def test_name_pointing_at_a_private_address_is_blocked(self):
        result, connected_to = await self.check('evil.example.com', resolved='10.0.0.7')
        self.assertEqual((result, connected_to), ('blocked', []))

    async def test_no_answer_is_none(self):
        result, _ = await self.check('down.example.com', status=OSError('timed out'))
        self.assertIsNone(result)

    async def test_bad_port_is_none(self):
        result, _ = await self.check('play.example.com:99999')
        self.assertIsNone(result)

    async def test_unexpected_resolver_error_does_not_kill_the_scan(self):
        with mock.patch.object(scanbot.QUAD9, 'resolve', mock.AsyncMock(side_effect=RuntimeError('boom'))):
            self.assertIsNone(await scanbot.check_direct_bedrock('play.example.com'))


class ApiTests(unittest.IsolatedAsyncioTestCase):
    def test_bedrock_response_is_parsed(self):
        result = scanbot.parse_api_status('demo.mcstatus.io', BEDROCK_API_RESPONSE, 'bedrock')
        self.assertEqual((result['players'], result['max'], result['version'], result['motd']),
                         (64, 100, '1.19.70', 'A Bedrock server'))
        self.assertEqual((result['address'], result['names'], result['edition']),
                         ('144.172.67.4', [], 'Bedrock'))

    def test_java_response_is_unchanged(self):
        result = scanbot.parse_api_status('1.2.3.4', JAVA_API_RESPONSE)
        self.assertEqual((result['version'], result['names'], result['edition']), ('1.21', ['Steve'], 'Java'))

    async def test_each_edition_asks_its_own_endpoint(self):
        urls = []

        class FakeResponse:
            status = 200

            async def json(self):
                return BEDROCK_API_RESPONSE

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        session = mock.MagicMock()
        session.get = lambda url, **kwargs: (urls.append(url), FakeResponse())[1]

        await scanbot.check_api(session, 'demo.mcstatus.io', 'bedrock')
        await scanbot.check_api(session, 'demo.mcstatus.io')
        self.assertEqual(urls, ['https://api.mcstatus.io/v2/status/bedrock/demo.mcstatus.io',
                                'https://api.mcstatus.io/v2/status/java/demo.mcstatus.io'])


class RunTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.state = {'phase': '', 'done': 0, 'total': 0, 'found': 0, 'blocked': 0}

    async def test_run_direct_uses_the_bedrock_check(self):
        java, bedrock = mock.AsyncMock(return_value=None), mock.AsyncMock(return_value=None)
        with mock.patch.object(scanbot, 'check_direct', java), mock.patch.object(scanbot, 'check_direct_bedrock', bedrock):
            await scanbot.run_direct(['1.2.3.4'], {}, self.state, 'bedrock')
        bedrock.assert_awaited_once_with('1.2.3.4')
        java.assert_not_awaited()

    async def test_run_direct_defaults_to_java(self):
        java, bedrock = mock.AsyncMock(return_value=None), mock.AsyncMock(return_value=None)
        with mock.patch.object(scanbot, 'check_direct', java), mock.patch.object(scanbot, 'check_direct_bedrock', bedrock):
            await scanbot.run_direct(['1.2.3.4'], {}, self.state)
        java.assert_awaited_once_with('1.2.3.4')
        bedrock.assert_not_awaited()

    async def test_run_api_passes_the_edition(self):
        seen = []

        async def fake_api(session, ip, edition='java'):
            seen.append(edition)

        with mock.patch.object(scanbot, 'check_api', fake_api), mock.patch.object(scanbot, 'API_DELAY', 0), \
                mock.patch.object(scanbot, 'set_status', mock.AsyncMock()):
            await scanbot.run_api(None, ['1.2.3.4'], {}, self.state, retrying=False, edition='bedrock')
        self.assertEqual(seen, ['bedrock'])


class ProbeTests(unittest.IsolatedAsyncioTestCase):
    async def test_bedrock_probe_uses_udp_check_and_its_own_servers(self):
        bedrock = mock.AsyncMock(return_value={'ip': 'x'})
        java = mock.AsyncMock(return_value=None)
        with mock.patch.object(scanbot, 'check_direct_bedrock', bedrock), mock.patch.object(scanbot, 'check_direct', java):
            self.assertTrue(await scanbot.probe_direct('bedrock'))
        self.assertEqual(sorted(c.args[0] for c in bedrock.await_args_list), sorted(scanbot.BEDROCK_PROBE_SERVERS))
        java.assert_not_awaited()

    async def test_bedrock_probe_fails_when_nothing_answers(self):
        with mock.patch.object(scanbot, 'check_direct_bedrock', mock.AsyncMock(return_value=None)):
            self.assertFalse(await scanbot.probe_direct('bedrock'))

    def test_probe_servers_are_public(self):
        for server in scanbot.BEDROCK_PROBE_SERVERS:
            with self.subTest(server=server):
                self.assertTrue(scanbot.is_public_entry(server))


class OutputTests(unittest.TestCase):
    def test_csv_has_an_edition_column_at_the_end(self):
        java = scanbot.make_result('1.2.3.4', '1.2.3.4', 1, 20, [], '1.21', 'hi')
        bedrock = scanbot.make_result('5.6.7.8', '5.6.7.8', 2, 50, [], '1.19.70', 'hey', edition='bedrock')
        files = scanbot.build_files([java, bedrock], [], {})
        table = next(f for f in files if f.filename == 'scan_results.csv')
        table.fp.seek(0)
        rows = list(csv.reader(io.StringIO(table.fp.read().decode('utf-8'))))
        self.assertEqual(rows[0][-1], 'edition')
        self.assertEqual([r[-1] for r in rows[1:]], ['Java', 'Bedrock'])


class EditionErrorTests(unittest.IsolatedAsyncioTestCase):
    async def test_mistyped_edition_gets_a_helpful_reply(self):
        ctx = mock.MagicMock(send=mock.AsyncMock())
        error = commands.BadLiteralArgument(mock.MagicMock(), ('java', 'bedrock'), [], 'bedorck')
        with self.assertNoLogs('scanbot', level='ERROR'):
            await scanbot.bot.on_command_error(ctx, error)
        message = ctx.send.await_args.args[0]
        self.assertIn('bedrock', message)
        self.assertIn('java', message)


class EditionOptionTests(unittest.IsolatedAsyncioTestCase):
    def test_slash_command_offers_the_two_editions(self):
        params = {p.name: p for p in scanbot.bot.tree.get_command('scan').parameters}
        self.assertEqual(list(params), ['file', 'edition'])
        self.assertFalse(params['edition'].required)
        self.assertEqual([c.value for c in params['edition'].choices], ['java', 'bedrock'])

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
        except commands.BadLiteralArgument:
            return 'bad'
        return ctx.args[2:]  # after self-less (ctx, file)

    async def test_prefix_command_takes_the_edition_as_text(self):
        self.assertEqual(await self.parse('!scan bedrock'), ['bedrock'])
        self.assertEqual(await self.parse('!scan java'), ['java'])
        self.assertEqual(await self.parse('!scan'), ['java'])
        self.assertEqual(await self.parse('!scan pocket'), 'bad')


class ScanCommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        scanbot.bot.direct_ok = False
        scanbot.bot.direct_ok_bedrock = False
        self.progress = mock.MagicMock(edit=mock.AsyncMock())
        self.ctx = mock.MagicMock()
        self.ctx.send = mock.AsyncMock(return_value=self.progress)
        self.ctx.defer = mock.AsyncMock()
        self.ctx.channel.send = mock.AsyncMock(return_value=self.progress)
        self.editions_asked = []

        async def fake_api(session, ip, edition='java'):
            self.editions_asked.append(edition)
            return scanbot.make_result(ip, ip, 3, 20, [], '1.21', 'hello', edition=edition)

        for p in (mock.patch.object(scanbot, 'check_api', fake_api),
                  mock.patch.object(scanbot, 'batch_get_locations', mock.AsyncMock(return_value={})),
                  mock.patch.object(scanbot, 'API_DELAY', 0),
                  mock.patch.object(scanbot, 'set_status', mock.AsyncMock())):
            p.start()
            self.addCleanup(p.stop)

    def attachment(self):
        return types.SimpleNamespace(filename='ips.txt', read=mock.AsyncMock(return_value=b'8.8.8.8\n1.1.1.1\n'))

    async def test_default_scan_is_java(self):
        await scanbot.bot.get_command('scan').callback(self.ctx, self.attachment())
        self.assertEqual(set(self.editions_asked), {'java'})
        self.assertNotIn('Bedrock', self.ctx.send.await_args_list[0].args[0])

    async def test_bedrock_scan_uses_the_bedrock_path_and_says_so(self):
        await scanbot.bot.get_command('scan').callback(self.ctx, self.attachment(), 'bedrock')
        self.assertEqual(set(self.editions_asked), {'bedrock'})
        self.assertIn('Bedrock', self.ctx.send.await_args_list[0].args[0])
        channel_texts = [c.args[0] for c in self.ctx.channel.send.await_args_list if c.args]
        self.assertTrue(any('Scan Complete' in t and 'Bedrock' in t for t in channel_texts), channel_texts)

    async def test_direct_pings_are_chosen_by_the_matching_startup_probe(self):
        # UDP can work while TCP 25565 is blocked (and the other way round)
        scanbot.bot.direct_ok, scanbot.bot.direct_ok_bedrock = False, True
        direct = mock.AsyncMock(return_value=[])
        with mock.patch.object(scanbot, 'run_direct', direct):
            await scanbot.bot.get_command('scan').callback(self.ctx, self.attachment(), 'java')
            direct.assert_not_awaited()
            await scanbot.bot.get_command('scan').callback(self.ctx, self.attachment(), 'bedrock')
            direct.assert_awaited_once()
            self.assertEqual(direct.await_args.args[-1], 'bedrock')


if __name__ == '__main__':
    unittest.main()
