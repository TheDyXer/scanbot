"""
Tests for the results files.

Run from the repository root:  python -m unittest discover -s tests
"""
import csv
import io
import os
import sys
import unittest
from unittest import mock

# bot.py exits at import time without a token
os.environ.setdefault('DISCORD_TOKEN', 'test-token')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import bot  # noqa: E402


def read_csv(files):
    table = next(f for f in files if f.filename == 'scan_results.csv')
    table.fp.seek(0)
    return list(csv.reader(io.StringIO(table.fp.read().decode('utf-8'))))


class CsvSafetyTests(unittest.TestCase):
    """MOTDs, versions and player names come from strangers' servers; a spreadsheet runs cells that start with = + - @."""

    def test_formulas_from_servers_are_defused(self):
        evil = bot.make_result('-1.example.com', '1.2.3.4', 1, 20, ['@steve', 'ok'], '+1.21',
                               '=HYPERLINK("http://evil.example","click")')
        rows = read_csv(bot.build_files([evil], [], {}))
        ip, _, _, _, _, version, motd, players, _ = rows[1]
        self.assertEqual(ip, "'-1.example.com")
        self.assertEqual(version, "'+1.21")
        self.assertEqual(motd, "'=HYPERLINK(\"http://evil.example\",\"click\")")
        self.assertEqual(players, "'@steve; ok")

    def test_tab_and_carriage_return_prefixes_are_defused_too(self):
        rows = read_csv(bot.build_files([bot.make_result('1.2.3.4', '1.2.3.4', 1, 2, [], '1.21', '\t=1+1')], [], {}))
        self.assertTrue(rows[1][6].startswith("'"))

    def test_normal_values_are_untouched(self):
        ok = bot.make_result('play.example.com:25566', '93.184.216.34', 3, 20, ['Steve', 'Alex'], '1.21.4', 'A Minecraft server')
        row = read_csv(bot.build_files([ok], [], {'93.184.216.34': 'US'}))[1]
        self.assertEqual(row, ['play.example.com:25566', '93.184.216.34', 'US', '3', '20', '1.21.4',
                               'A Minecraft server', 'Steve; Alex', 'Java'])

    def test_numbers_and_empty_cells_are_untouched(self):
        row = read_csv(bot.build_files([bot.make_result('1.2.3.4', None, 0, 0, [], None, '')], [], {}))[1]
        self.assertEqual(row[1:], ['', '', '0', '0', 'Unknown', '', '', 'Java'])



class NegativeCountTests(unittest.TestCase):
    def test_hidden_counts_are_shown_as_zero(self):
        # Some servers send -1 to hide their player counts
        result = bot.make_result('1.2.3.4', '1.2.3.4', -1, -1, [], '1.21', '')
        self.assertEqual((result['players'], result['max']), (0, 0))

    def test_numbers_that_are_not_numbers_are_zero(self):
        for value in (None, 'lots', float('inf')):
            with self.subTest(value=value):
                self.assertEqual(bot.to_int(value), 0)


def results_ctx():
    ctx = mock.MagicMock()
    ctx.channel.send = mock.AsyncMock()
    return ctx


def posted(ctx):
    return [call.args[0] for call in ctx.channel.send.await_args_list if call.args]


class ChatFormattingTests(unittest.IsolatedAsyncioTestCase):
    SERVER = bot.make_result('play_server_1.example.com', '1.2.3.4', 5, 20, [], '1.21_*beta*', '')

    async def test_a_server_with_a_hidden_count_is_listed_with_the_empty_ones(self):
        ctx = results_ctx()
        hidden = bot.make_result('1.2.3.4', '1.2.3.4', -1, -1, [], '1.21', '')
        await bot.send_results(ctx, [hidden], {}, False, 1, 1.0)
        self.assertIn('Online (Empty) Servers (1)', posted(ctx)[0])
        self.assertIn('1.2.3.4', posted(ctx)[0])

    def test_ip_and_version_are_escaped_in_chat_and_raw_in_files(self):
        chat = bot.format_entry(self.SERVER, {})
        self.assertIn('play\\_server\\_1.example.com', chat)
        self.assertIn('1.21\\_\\*beta\\*', chat)
        plain = bot.format_entry(self.SERVER, {}, markdown=False)
        self.assertIn('play_server_1.example.com | Players: 5/20 | Ver: 1.21_*beta*', plain)

    async def test_the_top_list_escapes_the_ip_too(self):
        ctx = results_ctx()
        with mock.patch.object(bot, 'INLINE_LIMIT', 0):  # Summary + top servers + files
            await bot.send_results(ctx, [self.SERVER], {}, False, 1, 1.0)
        self.assertIn('**play\\_server\\_1.example.com**', posted(ctx)[0])


if __name__ == '__main__':
    unittest.main()
