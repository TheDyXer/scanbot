"""
Tests for the 30,000-IP limit: the file and list caps, and getting the results of a huge scan
back to Discord within its upload limit.

Run from the repository root:  python -m unittest discover -s tests
"""
import csv
import io
import json
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

MIB = 1024 * 1024


def public_ips(count):
    """count distinct public IPs (11.0.0.0/8 is public address space)."""
    return [f"11.{i // 65536}.{(i // 256) % 256}.{i % 256}:25565" for i in range(count)]


def online(count, motd_length=30, names=0):
    return [scanbot.make_result(ip, ip.split(':')[0], 3, 20, [f"Player{n}" for n in range(names)], '1.21.4',
                                'A' * motd_length)
            for ip in public_ips(count)]


class LimitTests(unittest.TestCase):
    def test_limits(self):
        self.assertEqual(scanbot.MAX_IPS_PER_SCAN, 30000)

    def test_the_file_cap_fits_the_largest_list(self):
        # 64 bytes is a long hostname with a port and a line break
        self.assertGreaterEqual(scanbot.MAX_FILE_BYTES, scanbot.MAX_IPS_PER_SCAN * 64)

    def test_a_full_list_of_ordinary_lines_is_far_below_the_file_cap(self):
        text = "\n".join(public_ips(scanbot.MAX_IPS_PER_SCAN))
        self.assertLess(len(text.encode()), scanbot.MAX_FILE_BYTES)

    def test_parsing_thirty_thousand_lines_is_fast(self):
        text = "\n".join(public_ips(30000))
        started = time.monotonic()
        ips, invalid, duplicates, blocked = scanbot.parse_ips(text)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual((len(ips), invalid, duplicates, blocked), (30000, 0, 0, 0))


class ScanLimitTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        scanbot.bot.direct_ok = False  # API-only, so no network is needed
        self.progress = mock.MagicMock(edit=mock.AsyncMock())
        self.ctx = mock.MagicMock()
        self.ctx.send = mock.AsyncMock(return_value=self.progress)
        self.ctx.defer = mock.AsyncMock()
        self.ctx.channel.send = mock.AsyncMock(return_value=self.progress)
        self.scan = scanbot.bot.get_command('scan').callback

        async def fake_api(session, ip, edition='java'):
            return None

        for p in (mock.patch.object(scanbot, 'check_api', fake_api),
                  mock.patch.object(scanbot, 'batch_get_locations', mock.AsyncMock(return_value={})),
                  mock.patch.object(scanbot, 'API_DELAY', 0),
                  mock.patch.object(scanbot, 'set_status', mock.AsyncMock())):
            p.start()
            self.addCleanup(p.stop)

    def attachment(self, count):
        data = "\n".join(public_ips(count)).encode()
        return types.SimpleNamespace(filename='ips.txt', size=len(data), read=mock.AsyncMock(return_value=data))

    def texts(self, mock_send):
        return [call.args[0] for call in mock_send.await_args_list if call.args]

    async def test_a_list_of_exactly_thirty_thousand_is_scanned(self):
        await self.scan(self.ctx, self.attachment(30000))
        self.assertIn('30000 IPs', self.texts(self.ctx.send)[0])
        self.assertTrue(any('No working servers' in t and '30000 IPs' in t for t in self.texts(self.ctx.channel.send)))

    async def test_one_more_is_a_campaign_that_waits_for_confirmation(self):
        # Direct pings on for the preview, but never sent: a broken preview must fail here, not ping 30,001 addresses
        with mock.patch.object(scanbot.bot, 'direct_ok', True), mock.patch.object(scanbot, 'check_direct', mock.AsyncMock(side_effect=AssertionError('a preview pings nothing'))):
            await self.scan(self.ctx, self.attachment(30001))
        reply = self.texts(self.ctx.send)[-1]
        self.assertIn('Campaign preview:** ips.txt has 30,001 addresses: 2 parts of up to 30,000', reply)
        self.assertIn('confirm:yes', reply)
        self.ctx.channel.send.assert_not_awaited()

    async def test_with_campaigns_off_one_more_is_refused_with_the_limit_in_the_message(self):
        with mock.patch.object(scanbot, 'MAX_CAMPAIGN_ADDRESSES', 1):
            await self.scan(self.ctx, self.attachment(30001))
        self.assertIn('Too many IPs', self.texts(self.ctx.send)[-1])
        self.assertIn('30000', self.texts(self.ctx.send)[-1])
        self.ctx.channel.send.assert_not_awaited()

    async def test_the_too_big_message_does_not_quote_the_old_size(self):
        data = b'x' * (scanbot.MAX_FILE_BYTES + 1)
        attachment = types.SimpleNamespace(filename='ips.txt', size=len(data), read=mock.AsyncMock(return_value=data))
        await self.scan(self.ctx, attachment)
        message = self.texts(self.ctx.send)[-1]
        self.assertIn('too big', message)
        self.assertNotIn('100 KB', message)
        self.assertIn('20 MB', message)


class RunDirectScaleTests(unittest.IsolatedAsyncioTestCase):
    async def test_thirty_thousand_direct_pings_complete(self):
        ips = public_ips(30000)
        results, state = {}, {'phase': '', 'done': 0, 'total': 0, 'found': 0, 'blocked': 0}

        async def fake_check(ip):
            return None

        with mock.patch.object(scanbot, 'check_direct', fake_check):
            started = time.monotonic()
            retry = await scanbot.run_direct(ips, results, state)
        self.assertEqual(len(retry), 30000)
        self.assertEqual(state['done'], 30000)
        self.assertLess(time.monotonic() - started, 30)


class UploadLimitTests(unittest.TestCase):
    def test_a_server_uses_its_boost_level_with_headroom(self):
        ctx = types.SimpleNamespace(guild=types.SimpleNamespace(filesize_limit=50 * MIB))
        self.assertEqual(scanbot.upload_limit(ctx), int(50 * MIB * 0.9))

    def test_dms_use_the_default_limit(self):
        self.assertEqual(scanbot.upload_limit(types.SimpleNamespace(guild=None)), int(10 * MIB * 0.9))

    def test_anything_unreadable_falls_back_to_the_default(self):
        self.assertEqual(scanbot.upload_limit(mock.MagicMock()), int(10 * MIB * 0.9))


class SplitAndBatchTests(unittest.TestCase):
    def test_small_files_stay_whole(self):
        parts = scanbot.split_rows('scan_results.csv', 'h\r\n', ['a\r\n', 'b\r\n'], limit=1000)
        self.assertEqual(parts, [('scan_results.csv', b'h\r\na\r\nb\r\n')])

    def test_a_big_file_is_split_between_rows_with_the_header_repeated(self):
        rows = [f"row{i:03d}\r\n" for i in range(100)]
        parts = scanbot.split_rows('scan_results.csv', 'header\r\n', rows, limit=200)
        self.assertGreater(len(parts), 1)
        self.assertEqual([name for name, _ in parts], [f'scan_results_{n}.csv' for n in range(1, len(parts) + 1)])
        for _, data in parts:
            self.assertLessEqual(len(data), 200)
            self.assertTrue(data.startswith(b'header\r\n'))
        rebuilt = "".join(data.decode().removeprefix('header\r\n') for _, data in parts)
        self.assertEqual(rebuilt, "".join(rows))  # every row kept, in order, none cut

    def test_multi_line_entries_are_never_cut_in_half(self):
        rows = [f"entry {i}\n   └ motd {i}\n" for i in range(50)]
        parts = scanbot.split_rows('scan_results.txt', '', rows, limit=120)
        for _, data in parts:
            for chunk in data.decode().split('entry ')[1:]:
                self.assertIn('└ motd', chunk)

    def test_multibyte_text_is_measured_in_bytes(self):
        rows = ["é" * 50 + "\n"] * 10  # 101 bytes each
        parts = scanbot.split_rows('x.txt', '', rows, limit=250)
        for _, data in parts:
            self.assertLessEqual(len(data), 250)

    def test_files_are_grouped_into_messages_under_the_limit(self):
        files = [('a', b'12345'), ('b', b'12345'), ('c', b'12345')]
        self.assertEqual(scanbot.batch_files(files, limit=10), [[files[0], files[1]], [files[2]]])
        self.assertEqual(scanbot.batch_files(files, limit=100), [files])
        self.assertEqual(scanbot.batch_files(files, limit=5), [[f] for f in files])


class ResultsTests(unittest.IsolatedAsyncioTestCase):
    def ctx(self, guild=None):
        ctx = mock.MagicMock()
        ctx.guild = guild
        ctx.channel.send = mock.AsyncMock()
        ctx.send = mock.AsyncMock()
        return ctx

    def sent(self, ctx):
        return [(c.args[0] if c.args else '', c.kwargs.get('files', [])) for c in ctx.channel.send.await_args_list]

    def sizes(self, files):
        total = 0
        for f in files:
            f.fp.seek(0, io.SEEK_END)
            total += f.fp.tell()
        return total

    async def test_small_results_are_one_message_with_both_files(self):
        ctx = self.ctx()
        await scanbot.send_results(ctx, online(100), {}, False, 100, 5)
        messages = self.sent(ctx)
        self.assertEqual(len(messages), 1)
        self.assertEqual([f.filename for f in messages[0][1]],
                         ['scan_results.txt', 'scan_results.csv', 'scan_results.json'])

    async def test_a_huge_scan_is_delivered_in_messages_that_each_fit(self):
        # 30,000 online servers with long MOTDs and player lists: well over Discord's 10 MiB together
        results = online(30000, motd_length=150, names=8)
        ctx = self.ctx()
        await scanbot.send_results(ctx, results, {}, False, 30000, 6000)

        messages = self.sent(ctx)
        limit = scanbot.upload_limit(ctx)
        self.assertGreater(len(messages), 1)
        for _, files in messages:
            self.assertLessEqual(self.sizes(files), limit)
        self.assertIn('Scan Complete', messages[0][0])
        self.assertIn('continued', messages[1][0])

        # Every server is in the CSV parts and in the JSON parts, exactly once, and every part is valid
        rows, servers = 0, []
        for _, files in messages:
            for f in files:
                f.fp.seek(0)
                if f.filename.endswith('.csv'):
                    parsed = list(csv.reader(io.StringIO(f.fp.read().decode('utf-8'))))
                    self.assertEqual(parsed[0][0], 'ip')
                    rows += len(parsed) - 1
                elif f.filename.endswith('.json'):
                    servers += [row['ip'] for row in json.loads(f.fp.read().decode('utf-8'))]
        self.assertEqual(rows, 30000)
        self.assertEqual(sorted(servers), sorted(r['ip'] for r in results))

    async def test_a_boosted_server_gets_fewer_messages(self):
        results = online(30000, motd_length=150, names=8)
        normal, boosted = self.ctx(), self.ctx(types.SimpleNamespace(filesize_limit=50 * MIB))
        await scanbot.send_results(normal, results, {}, False, 30000, 6000)
        await scanbot.send_results(boosted, results, {}, False, 30000, 6000)
        self.assertLess(len(self.sent(boosted)), len(self.sent(normal)))


if __name__ == '__main__':
    unittest.main()
