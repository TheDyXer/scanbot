"""
Tests for /rescan (check again the servers your last scan found online) and /diff (compare your last two scans).

Run from the repository root:  python -m unittest discover -s tests
"""
import asyncio
import csv
import io
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

# bot.py exits at import time without a token
os.environ.setdefault('DISCORD_TOKEN', 'test-token')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import bot as scanbot  # noqa: E402
import jobs  # noqa: E402


def server(entry, players, source='direct', address=None):
    return scanbot.make_result(entry, address or entry, players, 20, [], '1.21', 'hello', source=source)


def make_ctx(user_id=1):
    progress = mock.MagicMock(edit=mock.AsyncMock(), id=555)
    ctx = mock.MagicMock()
    ctx.author = types.SimpleNamespace(id=user_id, mention=f'<@{user_id}>')
    ctx.guild = types.SimpleNamespace(id=10, filesize_limit=10 * 1024 * 1024)
    ctx.defer = mock.AsyncMock()
    ctx.channel = mock.MagicMock()
    ctx.channel.id = 100
    ctx.channel.send = mock.AsyncMock(return_value=progress)
    ctx.send = mock.AsyncMock(return_value=progress)
    return ctx


def texts(mock_send):
    return [call.args[0] for call in mock_send.await_args_list if call.args]


class StoredScans(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = jobs.JobStore.open(tmp.name)
        self.checked, self.api_checked = [], []
        self.online = set()

        async def fake_direct(entry):
            self.checked.append(entry)
            return server(entry, 5) if entry in self.online else None

        async def fake_api(session, entry, edition='java'):
            self.api_checked.append(entry)
            return None

        for p in (mock.patch.object(scanbot, 'store', self.store),
                  mock.patch.object(scanbot, 'check_direct', fake_direct),
                  mock.patch.object(scanbot, 'check_direct_bedrock', fake_direct),
                  mock.patch.object(scanbot, 'check_api', fake_api),
                  mock.patch.object(scanbot, 'batch_get_locations', mock.AsyncMock(return_value={})),
                  mock.patch.object(scanbot, 'geo_db', None),
                  mock.patch.object(scanbot, 'asn_db', None),
                  mock.patch.object(scanbot, 'PINGER_URL', ''),
                  mock.patch.object(scanbot, 'API_DELAY', 0),
                  mock.patch.object(scanbot, 'api_pacer', scanbot.Pacer()),
                  mock.patch.object(scanbot, 'CHECKPOINT_INTERVAL', 0.01),
                  mock.patch.object(scanbot, 'set_status', mock.AsyncMock()),
                  mock.patch.dict(scanbot.scans, clear=True),
                  mock.patch.object(scanbot, 'queue', []),
                  mock.patch.object(scanbot, 'shutting_down', False),
                  mock.patch.object(scanbot.bot, 'direct_ok', True),
                  mock.patch.object(scanbot.bot, 'direct_ok_bedrock', True)):
            p.start()
            self.addCleanup(p.stop)
        self.when = 0

    def finished(self, results, owner=1, edition='java', status='done', api_retry=True, total=None, **source):
        """A scan that ended earlier, saved the way run_job saves it. Each one finishes a minute after the last."""
        self.when += 1
        job = jobs.new_job(owner, f'<@{owner}>', 10, 100, edition, api_retry,
                           {'kind': 'file', 'description': 'ips.txt', **source}, total or len(results) + 10)
        job.results = {r['ip']: r for r in results}
        job.status = status
        job.finished_at = f'2026-10-01T10:{self.when:02d}:00Z'
        job.id = f'2026100{self.when}T100000Z-{owner}-abcd'
        self.store.save(job)
        return job

    def saved(self, kind):
        return [j for j in self.store.all() if j.source.get('kind') == kind]


class RescanTests(StoredScans):
    async def rescan(self, ctx, **options):
        await asyncio.wait_for(scanbot.bot.get_command('rescan').callback(ctx, **options), 5)

    async def test_it_checks_exactly_the_servers_the_last_scan_found_online(self):
        self.finished([server('old.example', 1)])
        parent = self.finished([server('1.2.3.4', 3), server('play.example.com:25570', 0, 'mcstatus.io')],
                               api_retry=False)
        self.online = {'1.2.3.4'}
        ctx = make_ctx()
        await self.rescan(ctx)
        self.assertEqual(sorted(self.checked), ['1.2.3.4', 'play.example.com:25570'])
        self.assertEqual(self.api_checked, [])  # api:off, like the scan it repeats
        self.assertIn("on 2 IPs (the servers your scan of <t:", texts(ctx.send)[0])
        [job] = self.saved('rescan')
        self.assertEqual((job.status, job.source['parent'], job.api_retry), ('done', parent.id, False))
        self.assertIn('1.2.3.4', job.results)

    async def test_the_api_option_overrides_the_last_scans(self):
        self.finished([server('1.2.3.4', 3)], api_retry=False)
        await self.rescan(make_ctx(), api='on')
        self.assertEqual(self.api_checked, ['1.2.3.4'])

    async def test_the_edition_picks_which_scan_to_repeat(self):
        self.finished([server('1.1.1.1', 3)], edition='bedrock')
        self.finished([server('2.2.2.2', 3)])
        await self.rescan(make_ctx(), edition='bedrock')
        self.assertEqual(self.checked, ['1.1.1.1'])
        [job] = self.saved('rescan')
        self.assertEqual(job.edition, 'bedrock')

    async def test_without_a_finished_scan_it_says_so(self):
        ctx = make_ctx()
        await self.rescan(ctx)
        self.assertIn("You have no finished scan to rescan", texts(ctx.send)[-1])
        await self.rescan(ctx, edition='bedrock')
        self.assertIn("You have no finished Bedrock scan to rescan", texts(ctx.send)[-1])
        self.assertEqual((self.checked, scanbot.scans), ([], {}))

    async def test_a_scan_that_found_nothing_has_nothing_to_rescan(self):
        self.finished([])
        ctx = make_ctx()
        await self.rescan(ctx)
        self.assertIn("found no online servers, so there's nothing to rescan", texts(ctx.send)[-1])
        self.assertEqual(self.checked, [])

    async def test_someone_elses_scans_dont_count(self):
        self.finished([server('1.2.3.4', 3)], owner=2)
        ctx = make_ctx(1)
        await self.rescan(ctx)
        self.assertIn("You have no finished scan", texts(ctx.send)[-1])

    async def test_without_a_state_folder_there_is_nothing_to_rescan(self):
        ctx = make_ctx()
        with mock.patch.object(scanbot, 'store', None):
            await self.rescan(ctx)
        self.assertIn("doesn't keep scan results", texts(ctx.send)[-1])

    async def test_one_scan_at_a_time_counts_rescans_too(self):
        self.finished([server('1.2.3.4', 3)])
        scanbot.scans[1] = object()
        ctx = make_ctx()
        await self.rescan(ctx)
        self.assertIn("You already have a scan running or queued", texts(ctx.send)[-1])
        self.assertEqual(self.checked, [])


class DiffTests(StoredScans):
    async def diff(self, ctx):
        await asyncio.wait_for(scanbot.bot.get_command('diff').callback(ctx), 5)

    def csv_rows(self, ctx):
        files = ctx.send.await_args_list[0].kwargs['files']
        [csv_file] = [f for f in files if f.filename == 'diff.csv']
        csv_file.fp.seek(0)
        return list(csv.reader(io.StringIO(csv_file.fp.read().decode())))

    async def test_it_counts_new_gone_changed_and_unchanged_servers(self):
        older = self.finished([server('1.0.0.1', 3), server('1.0.0.2', 7), server('1.0.0.3', 0)])
        self.finished([server('1.0.0.1', 3), server('1.0.0.2', 12), server('1.0.0.4', 30)], kind='rescan',
                      parent=older.id)
        ctx = make_ctx()
        await self.diff(ctx)
        text = texts(ctx.send)[0]
        self.assertIn("🆕 1 new · 💤 1 gone · 🔁 1 changed · ⚪ 1 unchanged", text)
        self.assertIn("**Newer:** <t:", text)
        self.assertIn("(rescan, 3 online of 13)", text)
        self.assertNotIn("different lists", text)  # A rescan of the older scan: "gone" really went offline
        changes = text.split("**Biggest player changes:**")[1].strip().splitlines()
        self.assertEqual(changes, ["🆕 **1.0.0.4** — → 30 (+30)", "🔁 **1.0.0.2** 7 → 12 (+5)",
                                   "💤 **1.0.0.3** 0 → — (+0)"])
        rows = self.csv_rows(ctx)
        self.assertEqual(rows[0], ['ip', 'change', 'players_before', 'players_after', 'max', 'version', 'country'])
        self.assertEqual(rows[1:], [['1.0.0.4', 'new', '', '30', '20', '1.21', ''],
                                    ['1.0.0.3', 'gone', '0', '', '20', '1.21', ''],
                                    ['1.0.0.2', 'changed', '7', '12', '20', '1.21', ''],
                                    ['1.0.0.1', 'unchanged', '3', '3', '20', '1.21', '']])

    async def test_only_the_ten_biggest_changes_are_listed(self):
        self.finished([server(f'1.0.0.{n}', 0) for n in range(1, 21)])
        self.finished([server(f'1.0.0.{n}', n) for n in range(1, 21)])
        ctx = make_ctx()
        await self.diff(ctx)
        changes = texts(ctx.send)[0].split("**Biggest player changes:**")[1].strip().splitlines()
        self.assertEqual(len(changes), 10)
        self.assertTrue(changes[0].startswith("🔁 **1.0.0.20** 0 → 20"))
        self.assertEqual(len(self.csv_rows(ctx)), 21)  # Every server is in the file

    async def test_scans_of_different_lists_say_what_gone_means(self):
        self.finished([server('1.0.0.1', 3)], description='a.txt')
        self.finished([server('1.0.0.2', 3)], description='b.txt')
        ctx = make_ctx()
        await self.diff(ctx)
        self.assertIn("different lists, so \"gone\" also counts servers the newer scan didn't check", texts(ctx.send)[0])

    async def test_a_stopped_scan_is_called_partial(self):
        self.finished([server('1.0.0.1', 3)])
        self.finished([server('1.0.0.1', 3)], status='stopped')
        ctx = make_ctx()
        await self.diff(ctx)
        text = texts(ctx.send)[0]
        self.assertIn("⚠️ The newer scan was stopped early, so the comparison is partial.", text)
        self.assertIn("stopped early)", text)

    async def test_it_compares_scans_of_the_same_edition(self):
        self.finished([server('1.0.0.1', 3)])
        self.finished([server('1.0.0.9', 3)], edition='bedrock')
        self.finished([server('1.0.0.1', 8)])
        ctx = make_ctx()
        await self.diff(ctx)
        self.assertIn("🔁 1 changed", texts(ctx.send)[0])
        self.assertNotIn("1.0.0.9", texts(ctx.send)[0])

    async def test_one_scan_has_nothing_to_compare_with(self):
        self.finished([server('1.0.0.1', 3)], edition='bedrock')
        self.finished([server('1.0.0.1', 3)])
        ctx = make_ctx()
        await self.diff(ctx)
        self.assertIn("your only finished Java scan", texts(ctx.send)[-1])
        ctx.send.reset_mock()
        with mock.patch.object(self.store, 'finished_for', return_value=[]):
            await self.diff(ctx)
        self.assertIn("no finished scans to compare", texts(ctx.send)[-1])

    async def test_without_a_state_folder_there_is_nothing_to_compare(self):
        ctx = make_ctx()
        with mock.patch.object(scanbot, 'store', None):
            await self.diff(ctx)
        self.assertIn("doesn't keep scan results", texts(ctx.send)[-1])

    async def test_a_big_diff_comes_in_several_files(self):
        self.finished([server(f'1.0.{n // 250}.{n % 250}', 1) for n in range(2000)])
        self.finished([server(f'1.0.{n // 250}.{n % 250}', 2) for n in range(2000)])
        ctx = make_ctx()
        with mock.patch.object(scanbot, 'upload_limit', return_value=20_000):
            await self.diff(ctx)
        sends = ctx.send.await_args_list
        self.assertGreater(len(sends), 1)
        names = [f.filename for call in sends for f in call.kwargs['files']]
        self.assertEqual(names[0], 'diff_1.csv')
        self.assertIn("Diff, continued (2/", sends[1].args[0])

    async def test_a_server_name_cant_break_the_message(self):
        self.finished([server('**bold**.example', 1)])
        self.finished([server('**bold**.example', 9)])
        ctx = make_ctx()
        await self.diff(ctx)
        self.assertIn(r"**\*\*bold\*\*.example**", texts(ctx.send)[0])


if __name__ == '__main__':
    unittest.main()
