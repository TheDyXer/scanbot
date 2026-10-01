"""
Tests for /scan's target option (asn:, country:, cidr:) and range lines, through the scan command: how ! commands
read it next to the edition and API options, the refusals, and what gets scanned.

Run from the repository root:  python -m unittest discover -s tests
"""
import ipaddress
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

import bot as scanbot  # noqa: E402

Target = scanbot.TargetSpec


async def parse(content, attach=True):
    """The scan command's arguments for a ! message, after ctx: [file, target, edition, api], or 'bad'."""
    att = discord.Attachment(data={'id': 1, 'filename': 'ips.txt', 'size': 10, 'url': 'https://x/y',
                                   'proxy_url': 'https://x/y'}, state=mock.MagicMock())
    msg = mock.MagicMock(attachments=[att] if attach else [], content=content)
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
    return ['file' if isinstance(a, discord.Attachment) else a for a in ctx.args[1:]]


class PrefixParsingTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_file_and_no_words_is_still_the_plain_scan(self):
        self.assertEqual(await parse('!scan'), ['file', None, 'java', 'on'])

    async def test_the_edition_and_api_still_come_without_a_target(self):
        self.assertEqual(await parse('!scan bedrock off'), ['file', None, 'bedrock', 'off'])
        self.assertEqual(await parse('!scan bedrock'), ['file', None, 'bedrock', 'on'])
        self.assertEqual(await parse('!scan pocket'), 'bad')

    async def test_a_target_comes_first(self):
        self.assertEqual(await parse('!scan asn:AS8400 bedrock off', attach=False),
                         [None, Target('asn', 'AS8400'), 'bedrock', 'off'])
        self.assertEqual(await parse('!scan COUNTRY:rs', attach=False), [None, Target('country', 'rs'), 'java', 'on'])

    async def test_a_bare_network_range_or_wildcard_is_a_cidr_target(self):
        for word in ('1.2.3.0/24', '1.2.3.1-1.2.3.9', '1.2.3.*', '1.2.3.0/24:25570'):
            with self.subTest(word=word):
                self.assertEqual(await parse(f'!scan {word}', attach=False), [None, Target('cidr', word), 'java', 'on'])


class ConverterTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_slash_command_passes_anything_on_for_the_scan_to_explain(self):
        ctx = mock.MagicMock(interaction=mock.MagicMock())
        self.assertEqual(await scanbot.Target().convert(ctx, ' hello '), Target('invalid', 'hello'))
        self.assertEqual(await scanbot.Target().convert(ctx, 'asn:foo'), Target('asn', 'foo'))

    async def test_a_command_word_that_isnt_a_target_is_left_for_the_edition(self):
        ctx = mock.MagicMock(interaction=None)
        with self.assertRaises(commands.BadArgument):
            await scanbot.Target().convert(ctx, 'bedrock')


class TargetScanTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.progress = mock.MagicMock(edit=mock.AsyncMock())
        self.ctx = mock.MagicMock()
        self.ctx.send = mock.AsyncMock(return_value=self.progress)
        self.ctx.defer = mock.AsyncMock()
        self.ctx.channel.send = mock.AsyncMock(return_value=self.progress)
        self.checked = []

        async def fake_api(session, ip, edition='java'):
            self.checked.append(ip)
            return None

        self.prefixes_for_asn = mock.AsyncMock(return_value=[ipaddress.IPv4Network('1.2.3.0/30')])
        for p in (mock.patch.object(scanbot.bot, 'direct_ok', False),  # API only, so no network is needed
                  mock.patch.object(scanbot, 'check_api', fake_api),
                  mock.patch.object(scanbot, 'prefixes_for_asn', self.prefixes_for_asn),
                  mock.patch.object(scanbot, 'batch_get_locations', mock.AsyncMock(return_value={})),
                  mock.patch.object(scanbot, 'API_DELAY', 0),
                  mock.patch.object(scanbot, 'api_pacer', scanbot.Pacer()),
                  mock.patch.object(scanbot, 'set_status', mock.AsyncMock()),
                  mock.patch.dict(scanbot.scans, clear=True),
                  mock.patch.object(scanbot, 'queue', [])):
            p.start()
            self.addCleanup(p.stop)

    async def scan(self, file=None, target=None, **options):
        await scanbot.bot.get_command('scan').callback(self.ctx, file, target, **options)

    def replies(self):
        return [c.args[0] for c in self.ctx.send.await_args_list if c.args]

    @staticmethod
    def attachment(data):
        return types.SimpleNamespace(filename='ips.txt', size=len(data), read=mock.AsyncMock(return_value=data))

    async def test_without_a_file_or_a_target_the_reply_says_what_to_give(self):
        await self.scan()
        self.assertIn("Attach a `.txt` file, or give a target: `asn:AS8400`", self.replies()[-1])
        self.assertEqual((self.checked, scanbot.scans), ([], {}))

    async def test_a_file_and_a_target_together_are_refused(self):
        await self.scan(self.attachment(b'1.2.3.4\n'), Target('cidr', '1.2.3.0/30'))
        self.assertIn("Give a file or a target, not both", self.replies()[-1])
        self.assertEqual(self.checked, [])

    async def test_a_cidr_target_scans_its_hosts(self):
        await self.scan(target=Target('cidr', '1.2.3.0/30'))
        self.assertEqual(sorted(self.checked), ['1.2.3.1', '1.2.3.2'])
        self.assertIn("on 2 IPs from `cidr:1.2.3.0/30`", self.replies()[0])

    async def test_an_asn_target_scans_its_announced_networks(self):
        await self.scan(target=Target('asn', 'as8400'))
        self.assertEqual(sorted(self.checked), ['1.2.3.1', '1.2.3.2'])
        self.assertIn("on 2 IPs from AS8400 (1 prefix)", self.replies()[0])

    async def test_an_asn_bigger_than_a_scan_is_refused_with_its_size_and_nothing_starts(self):
        self.prefixes_for_asn.return_value = [ipaddress.IPv4Network('11.0.0.0/16'), ipaddress.IPv4Network('12.0.0.0/16')]
        await self.scan(target=Target('asn', 'AS8400'))
        self.assertEqual(self.replies()[-1],
                         "❌ **Too many IPs:** `asn:AS8400 (2 prefixes)` has 131,068 addresses; a scan takes at most "
                         f"{scanbot.MAX_IPS_PER_SCAN}. Scan part of it with `cidr:`, one prefix at a time.")
        self.assertEqual((self.checked, scanbot.scans), ([], {}))

    async def test_bad_targets_get_helpful_replies(self):
        cases = [(Target('asn', 'foo'), "`foo` isn't an AS number"),
                 (Target('country', 'XYZ'), "`XYZ` isn't a two-letter country code"),
                 (Target('cidr', '10.0.0.0/8'), "`10.0.0.0/8` is a private or local range"),
                 (Target('cidr', 'nonsense'), "`nonsense` isn't a network, range or wildcard"),
                 (Target('cidr', '11.0.0.0/8'), "`cidr:11.0.0.0/8` has 16,777,214 addresses"),
                 (Target('invalid', 'hello `there`'), "`hello 'there'` isn't a scan target. Use `asn:AS8400`")]
        for target, reply in cases:
            with self.subTest(target=target):
                await self.scan(target=target)
                self.assertIn(reply, self.replies()[-1])
        self.assertEqual(self.checked, [])

    async def test_a_ripestat_failure_is_reported_not_a_crash(self):
        self.prefixes_for_asn.side_effect = scanbot.LookupFailed("RIPEstat answered HTTP 500 for AS8400. Try again later.")
        await self.scan(target=Target('asn', 'AS8400'))
        self.assertEqual(self.replies()[-1], "❌ RIPEstat answered HTTP 500 for AS8400. Try again later.")

    async def test_a_file_with_range_lines_says_what_it_expanded(self):
        await self.scan(self.attachment(b'5.6.7.8\n1.2.3.0/30\n1.2.4.1-1.2.4.3\n10.0.0.0/8\n'))
        self.assertEqual(sorted(self.checked), ['1.2.3.1', '1.2.3.2', '1.2.4.1', '1.2.4.2', '1.2.4.3', '5.6.7.8'])
        self.assertIn("(expanded 2 range line(s) into 5 addresses, skipped 1 private or local address(es))",
                      self.replies()[0])

    async def test_a_file_past_the_limit_names_the_line(self):
        with mock.patch.object(scanbot, 'MAX_IPS_PER_SCAN', 2):
            await self.scan(self.attachment(b'1.2.3.4\n1.2.3.0/30\n'))
        self.assertEqual(self.replies()[-1], "❌ **Too many IPs:** line 2 (`1.2.3.0/30`) takes the list past 2 "
                                             "addresses, the most a scan takes.")
        self.assertEqual(self.checked, [])


if __name__ == '__main__':
    unittest.main()
