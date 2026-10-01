"""
Tests for resuming scans after a restart: a running scan is saved as it goes, a restart (SIGTERM) saves it instead of
posting results, and when the bot is back it carries on from where it got to.

Run from the repository root:  python -m unittest discover -s tests
"""
import asyncio
import os
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

import discord

# bot.py exits at import time without a token
os.environ.setdefault('DISCORD_TOKEN', 'test-token')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import bot as scanbot  # noqa: E402
import jobs  # noqa: E402

IPS = ['1.0.0.1', '1.0.0.2', '1.0.0.3', '1.0.0.4', '1.0.0.5', '1.0.0.6']


def person(user_id):
    return types.SimpleNamespace(id=user_id, mention=f'<@{user_id}>')


def online(ip, source):
    return scanbot.make_result(ip, ip, 3, 20, ['Steve'], '1.21', 'hello', source=source)


def make_channel(message_id=555):
    """A channel the bot posts in. channel.progress is the progress message it edits."""
    progress = mock.MagicMock(edit=mock.AsyncMock(), id=message_id)
    channel = mock.MagicMock()
    channel.id = 100
    channel.guild = None
    channel.send = mock.AsyncMock(return_value=progress)
    channel.old_progress = mock.MagicMock(edit=mock.AsyncMock())
    channel.get_partial_message = mock.MagicMock(return_value=channel.old_progress)
    channel.progress = progress
    return channel


def make_ctx(user_id, guild_id=10):
    ctx = mock.MagicMock()
    ctx.author = person(user_id)
    ctx.guild = types.SimpleNamespace(id=guild_id)
    ctx.permissions = types.SimpleNamespace(manage_messages=False)
    ctx.defer = mock.AsyncMock()
    ctx.channel = make_channel(500 + user_id)
    ctx.send = mock.AsyncMock(return_value=ctx.channel.progress)
    ctx.progress = ctx.channel.progress
    return ctx


def attachment(entries=IPS):
    data = ''.join(f'{e}\n' for e in entries).encode()
    return types.SimpleNamespace(filename='ips.txt', size=len(data), read=mock.AsyncMock(return_value=data))


def texts(mock_send):
    return [call.args[0] for call in mock_send.await_args_list if call.args]


def edits(message):
    return [call.kwargs.get('content', '') for call in message.edit.await_args_list]


def http_error(cls, status):
    return cls(mock.MagicMock(status=status, reason='x'), 'x')


async def until(condition, timeout=3):
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("condition never became true")
        await asyncio.sleep(0.01)


class ResumeTests(unittest.IsolatedAsyncioTestCase):
    """Real scans with fake pings: entries in self.slow wait at a gate (and give up after self.slow_timeout)."""

    async def asyncSetUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)  # Registered first, so it runs last
        self.store = jobs.JobStore.open(tmp.name)
        self.gate = asyncio.Event()
        self.slow, self.online, self.api_online, self.blocked = set(), {'1.0.0.1', '1.0.0.3'}, set(), set()
        self.api_slow = set()
        self.slow_timeout = 5
        self.waiting = 0
        self.checked, self.api_checked = [], []
        self.close = mock.AsyncMock()
        self.scan_command = scanbot.bot.get_command('scan').callback
        self.stop_command = scanbot.bot.get_command('stop').callback

        async def fake_direct(ip):
            self.checked.append(ip)
            if ip in self.slow:
                self.waiting += 1
                try:
                    await asyncio.wait_for(self.gate.wait(), self.slow_timeout)
                except asyncio.TimeoutError:
                    return None  # Like a server that never answers
            if ip in self.blocked:
                raise scanbot.BlockedAddress(ip)
            return online(ip, 'direct') if ip in self.online else None

        async def fake_api(session, ip, edition='java'):
            self.api_checked.append(ip)
            if ip in self.api_slow:
                await self.gate.wait()
            return online(ip, 'mcstatus.io') if ip in self.api_online else None

        for p in (mock.patch.object(scanbot, 'store', self.store),
                  mock.patch.object(scanbot, 'check_direct', fake_direct),
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
                  mock.patch.object(scanbot.bot, 'direct_ok_bedrock', True),
                  mock.patch.object(scanbot.bot, 'resumed', False)):
            p.start()
            self.addCleanup(p.stop)

    def saved(self, owner=None):
        found = [j for j in self.store.all() if owner is None or j.owner_id == owner]
        return found[0] if len(found) == 1 else found

    def start(self, ctx, entries=IPS, **options):
        return asyncio.create_task(self.scan_command(ctx, attachment(entries), **options))

    async def restart(self):
        """SIGTERM: the bot saves its scans and closes."""
        await asyncio.wait_for(scanbot.shutdown('SIGTERM', grace=5, close=self.close), 5)
        scanbot.shutting_down = False  # The next process starts fresh (the patch restores it after the test)

    async def resume(self, channel):
        """The bot is back: what on_ready does, with `channel` as the one every saved scan was started in."""
        with mock.patch.object(scanbot.bot, 'get_channel', mock.MagicMock(return_value=channel)):
            scanbot.start_resumed(scanbot.register_resumable(self.store))
            await asyncio.wait_for(asyncio.gather(*scanbot.resume_tasks), 5)

    def new_job(self, owner=1, entries=IPS, **changes):
        job = jobs.new_job(owner, f'<@{owner}>', 10, 100, 'java', True, {'kind': 'file', 'description': 'ips.txt'},
                           len(entries), ' (from ips.txt)')
        for key, value in changes.items():
            setattr(job, key, value)
        self.store.write_list(job, entries)
        self.store.save(job)
        return job

    # --- Saving ---

    async def test_a_running_scan_is_saved_as_it_goes_and_kept_with_its_results_when_done(self):
        self.slow = {'1.0.0.4'}
        task = self.start(make_ctx(1))
        await until(lambda: self.waiting == 1 and len(self.checked) == 6)

        # Every entry but the slow one is checked; the cursor waits at it
        await until(lambda: self.saved().cursor == {'phase': 'direct', 'index': 3})
        job = self.saved()
        self.assertEqual(job.status, 'running')
        self.assertEqual(set(job.results), {'1.0.0.1', '1.0.0.3'})
        self.assertEqual((job.channel_id, job.guild_id, job.progress_message_id), (100, 10, 501))
        self.assertEqual(self.store.read_list(job), IPS)

        self.gate.set()
        await asyncio.wait_for(task, 5)
        job = self.saved()
        self.assertEqual(job.status, 'done')
        self.assertEqual(set(job.results), {'1.0.0.1', '1.0.0.3'})
        self.assertIsNotNone(job.finished_at)
        self.assertIsNone(self.store.read_list(job))  # Only needed while it runs
        self.assertEqual(scanbot.scans, {})

    async def test_a_phases_speed_figures_are_saved_while_it_runs(self):
        # A crash mid-phase must not lose the time the phase ran, or the speed line comes out wrong after a resume
        self.api_slow = {'1.0.0.4'}
        with mock.patch.object(scanbot.bot, 'direct_ok', False):  # API only
            task = self.start(make_ctx(1))
            await until(lambda: [(j.timings.get('api') or [0])[0] for j in self.store.all()] == [3])
            checked, seconds = self.saved().timings['api']
            self.assertGreater(seconds, 0)
            self.gate.set()
            await asyncio.wait_for(task, 5)
        self.assertEqual(self.saved().timings['api'][0], 6)

    async def test_the_cursor_waits_for_a_server_that_hasnt_answered_yet(self):
        entries = [f'2.0.{i // 250}.{i % 250 + 1}' for i in range(200)]
        self.slow, self.online = {entries[2]}, set()
        state = {'phase': '', 'done': 0, 'total': 0, 'found': 0, 'blocked': 0}
        task = asyncio.create_task(scanbot.run_direct(entries, {}, state))
        await until(lambda: state['done'] == 199)
        self.assertEqual(state['cursor'], 2)
        self.gate.set()
        await asyncio.wait_for(task, 5)
        self.assertEqual(state['cursor'], 200)

    async def test_a_resumed_phase_counts_from_where_it_got_to(self):
        state = {'phase': '', 'done': 0, 'total': 0, 'found': 1, 'blocked': 0}
        await scanbot.run_direct(IPS[4:], {'1.0.0.1': online('1.0.0.1', 'direct')}, state, offset=4)
        self.assertEqual((state['done'], state['total'], state['cursor']), (6, 6, 6))
        self.assertEqual(self.checked, IPS[4:])

    async def test_a_server_found_before_the_restart_isnt_counted_twice(self):
        results = {'1.0.0.1': online('1.0.0.1', 'direct')}
        state = {'phase': '', 'done': 0, 'total': 0, 'found': 1, 'blocked': 0}
        await scanbot.run_direct(IPS[:2], results, state)  # The cursor was at 0: 1.0.0.1 is checked again
        self.assertEqual(state['found'], 1)
        self.api_online = {'1.0.0.1'}  # Found again through the API
        await scanbot.run_api(None, IPS[:2], results, state, retrying=True)
        self.assertEqual(state['found'], 1)

    async def test_the_api_list_comes_out_the_same_after_a_restart(self):
        self.blocked, self.online = {'1.0.0.2'}, {'1.0.0.1', '1.0.0.5'}
        results, state = {}, {'phase': '', 'done': 0, 'total': 0, 'found': 0, 'blocked': 0}
        unanswered = await scanbot.run_direct(IPS, results, state)
        self.assertEqual(scanbot.retry_list(IPS, results, state['blocked_entries']), unanswered)
        self.assertEqual(unanswered, ['1.0.0.3', '1.0.0.4', '1.0.0.6'])
        # What the API phase finds doesn't move its own list along
        results['1.0.0.3'] = online('1.0.0.3', 'mcstatus.io')
        self.assertEqual(scanbot.retry_list(IPS, results, state['blocked_entries']), unanswered)

    # --- Restarting ---

    async def test_a_restart_saves_a_running_scan_instead_of_posting_its_results(self):
        self.slow, self.slow_timeout = {'1.0.0.4'}, 0.3
        ctx = make_ctx(1)
        with mock.patch.object(scanbot, 'DIRECT_CONCURRENCY', 1):  # One at a time, so the stop comes at 1.0.0.4
            task = self.start(ctx)
            await until(lambda: self.waiting == 1)
            await self.restart()
        await asyncio.wait_for(task, 5)

        job = self.saved()
        self.assertEqual(job.status, 'interrupted')
        self.assertEqual(job.cursor, {'phase': 'direct', 'index': 4})  # 1.0.0.4 finished, 1.0.0.5 never started
        self.assertEqual(set(job.results), {'1.0.0.1', '1.0.0.3'})
        self.assertEqual(self.store.read_list(job), IPS)
        self.assertFalse([t for t in texts(ctx.channel.send) if 'Scan stopped' in t or 'Scan Complete' in t])
        self.assertIn('Paused', edits(ctx.progress)[-1])
        self.assertIn('carries on', edits(ctx.progress)[-1])
        self.assertEqual(scanbot.scans, {})
        self.close.assert_awaited_once()

    async def test_a_queued_scan_is_kept_for_after_the_restart(self):
        self.slow, self.slow_timeout = {'1.0.0.4'}, 0.3
        a, b = make_ctx(1), make_ctx(2)
        with mock.patch.object(scanbot, 'MAX_CONCURRENT_SCANS', 1):
            tasks = [self.start(a), self.start(b)]
            await until(lambda: self.waiting == 1 and len(scanbot.queue) == 1)
            await self.restart()
        await asyncio.wait_for(asyncio.gather(*tasks), 5)

        self.assertEqual(self.saved(owner=2).status, 'queued')
        self.assertTrue(any('starts once' in t for t in texts(b.channel.send)), texts(b.channel.send))
        self.assertEqual(self.saved(owner=1).status, 'interrupted')

    async def test_a_scan_cancelled_before_it_starts_leaves_nothing_behind(self):
        self.slow = {'1.0.0.4'}
        a, b = make_ctx(1), make_ctx(2)
        with mock.patch.object(scanbot, 'MAX_CONCURRENT_SCANS', 1):
            first, second = self.start(a), self.start(b)
            await until(lambda: len(scanbot.queue) == 1)
            await self.stop_command(b)
            await asyncio.wait_for(second, 5)
            self.assertEqual(self.saved(owner=2), [])
            self.gate.set()
            await asyncio.wait_for(first, 5)

    # --- Resuming ---

    async def test_a_restarted_scan_carries_on_and_posts_everything_it_found(self):
        self.slow, self.slow_timeout = {'1.0.0.4'}, 0.3
        ctx = make_ctx(1)
        with mock.patch.object(scanbot, 'DIRECT_CONCURRENCY', 1):
            task = self.start(ctx)
            await until(lambda: self.waiting == 1)
            await self.restart()
        await asyncio.wait_for(task, 5)
        self.checked.clear()
        self.online.add('1.0.0.6')

        await self.resume(ctx.channel)

        self.assertEqual(self.checked, ['1.0.0.5', '1.0.0.6'])  # Only what it hadn't got to
        posted = texts(ctx.channel.send)
        resumed = [t for t in posted if 'Scan resumed' in t]
        self.assertEqual(len(resumed), 1)
        self.assertIn('4 of 6 were pinged before the restart, 2 found so far', resumed[0])
        [results] = [t for t in posted if 'Scan Complete' in t]
        for ip in ('1.0.0.1', '1.0.0.3', '1.0.0.6'):
            self.assertIn(ip, results)
        self.assertIn('6 IPs', results)
        self.assertIn('resumed once after the bot restarted', results)
        self.assertIn('progress continues below', edits(ctx.channel.old_progress)[-1])
        ctx.channel.get_partial_message.assert_called_with(501)  # The first run's progress message
        job = self.saved()
        self.assertEqual((job.status, job.resumed), ('done', 1))
        self.assertEqual(scanbot.scans, {})

    async def test_a_scan_restarted_in_the_api_phase_checks_only_the_rest(self):
        job = self.new_job(cursor={'phase': 'api', 'index': 2}, status='interrupted',
                           results={'1.0.0.1': online('1.0.0.1', 'direct')}, timings={'direct': [6, 2.0], 'api': None})
        channel = make_channel()
        self.api_online = {'1.0.0.6'}

        await self.resume(channel)

        self.assertEqual(self.checked, [])  # The direct pings were done
        self.assertEqual(self.api_checked, ['1.0.0.4', '1.0.0.5', '1.0.0.6'])  # The API list without its first 2
        [results] = [t for t in texts(channel.send) if 'Scan Complete' in t]
        self.assertIn('1.0.0.6', results)
        self.assertIn('1.0.0.1', results)
        self.assertEqual(self.store.all()[0].id, job.id)

    async def test_resumed_scans_keep_their_order_and_come_before_new_ones(self):
        for owner in range(1, 8):
            self.new_job(owner=owner, status='running', created_at=f'2026-10-01T00:00:0{owner}Z')
        with mock.patch.object(scanbot, 'MAX_CONCURRENT_SCANS', 5):
            pending = scanbot.register_resumable(self.store)
        self.assertEqual([place for _, _, place in pending], [0, 0, 0, 0, 0, 1, 2])
        self.assertEqual([s.owner.id for s in scanbot.queue], [6, 7])

        # Their owners can't start a second scan meanwhile
        ctx = make_ctx(3)
        await self.scan_command(ctx, attachment())
        self.assertIn('already have a scan', texts(ctx.send)[-1])

    async def test_only_the_oldest_unfinished_scan_of_one_person_resumes(self):
        old = self.new_job(created_at='2026-10-01T00:00:01Z', status='running')
        new = self.new_job(created_at='2026-10-01T00:00:02Z', status='queued')
        with self.assertLogs('scanbot', 'WARNING'):
            pending = scanbot.register_resumable(self.store)
        self.assertEqual([job.id for _, job, _ in pending], [old.id])
        self.assertEqual({j.id: j.status for j in self.store.all()}, {old.id: 'running', new.id: 'abandoned'})

    async def test_when_the_channel_is_gone_the_scan_carries_on_in_a_dm(self):
        self.new_job(status='running')
        dm = make_channel()
        user = mock.MagicMock(create_dm=mock.AsyncMock(return_value=dm))
        with mock.patch.object(scanbot.bot, 'fetch_channel', mock.AsyncMock(side_effect=http_error(discord.NotFound, 404))), \
                mock.patch.object(scanbot.bot, 'fetch_user', mock.AsyncMock(return_value=user)):
            await self.resume(None)
        posted = texts(dm.send)
        self.assertIn('gone', posted[0])
        self.assertTrue(any('Scan Complete' in t for t in posted))
        self.assertEqual(self.saved().status, 'done')

    async def test_a_scan_with_nowhere_to_post_is_abandoned_and_frees_its_slot(self):
        self.new_job(status='running')
        with mock.patch.object(scanbot.bot, 'fetch_channel', mock.AsyncMock(side_effect=http_error(discord.Forbidden, 403))), \
                mock.patch.object(scanbot.bot, 'fetch_user', mock.AsyncMock(side_effect=http_error(discord.NotFound, 404))), \
                self.assertLogs('scanbot', 'WARNING'):
            await self.resume(None)
        job = self.saved()
        self.assertEqual(job.status, 'abandoned')
        self.assertIsNone(self.store.read_list(job))
        self.assertEqual((scanbot.scans, scanbot.queue), ({}, []))

    async def test_a_scan_whose_list_is_gone_is_abandoned(self):
        job = self.new_job(status='running')
        self.store.remove_list(job)
        with self.assertLogs('scanbot', 'WARNING'):
            await self.resume(make_channel())
        self.assertEqual(self.saved().status, 'abandoned')
        self.assertEqual(scanbot.scans, {})

    async def test_stopping_a_resumed_scan_posts_what_it_found_before_and_after(self):
        self.new_job(status='interrupted', cursor={'phase': 'direct', 'index': 2},
                     results={'1.0.0.1': online('1.0.0.1', 'direct')})
        self.slow = {'1.0.0.4'}
        channel = make_channel()
        with mock.patch.object(scanbot.bot, 'get_channel', mock.MagicMock(return_value=channel)):
            scanbot.start_resumed(scanbot.register_resumable(self.store))
            await until(lambda: self.waiting == 1)
            owner = make_ctx(1)
            await self.stop_command(owner)
            self.assertIn('Stop requested', texts(owner.send)[-1])
            self.gate.set()
            await asyncio.wait_for(asyncio.gather(*scanbot.resume_tasks), 5)
        [results] = [t for t in texts(channel.send) if 'Scan stopped' in t]
        self.assertIn('1.0.0.1', results)  # Found before the restart
        self.assertIn('1.0.0.3', results)  # ... and after
        self.assertEqual(self.saved().status, 'stopped')

    async def test_on_ready_puts_saved_scans_back_before_anything_else(self):
        self.new_job(status='running')
        seen = []

        def start_resumed(pending):
            seen.append([(s.owner.id, place) for s, _, place in pending])

        with mock.patch.object(scanbot, 'start_resumed', start_resumed), \
                mock.patch.object(scanbot.bot, 'geo_watcher', object()), \
                mock.patch.object(type(scanbot.bot), 'user', types.SimpleNamespace(name='Scanbot')):
            await scanbot.on_ready()
            await scanbot.on_ready()  # After a reconnect: nothing twice
        self.assertEqual(seen, [[(1, 0)]])
        self.assertIn(1, scanbot.scans)
        self.assertEqual(self.saved().status, 'running')  # Not taken for a second scan of the same person


class StoreSetupTests(unittest.TestCase):

    def test_an_unwritable_state_folder_leaves_scans_unsaved_and_says_how_to_fix_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            blocker = os.path.join(tmp, 'file')
            open(blocker, 'w').close()
            with mock.patch.object(scanbot, 'STATE_DIR', os.path.join(blocker, 'state')), \
                    mock.patch.object(scanbot, 'store', 'unset'), \
                    self.assertLogs('scanbot', 'WARNING') as logs:
                scanbot.open_store()
                self.assertIsNone(scanbot.store)
        self.assertIn("instead of resuming them", logs.output[0])

    def test_a_writable_state_folder_is_cleaned_up_and_used(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = jobs.JobStore.open(tmp)
            for i in range(4):
                job = jobs.new_job(1, '<@1>', None, None, 'java', True, {}, 1)
                job.status, job.finished_at = 'done', f'2026-10-01T00:00:0{i}Z'
                store.save(job)
            with mock.patch.object(scanbot, 'STATE_DIR', tmp), mock.patch.object(scanbot, 'store', None), \
                    mock.patch.object(scanbot, 'KEEP_FINISHED_PER_USER', 2), \
                    self.assertLogs('scanbot', 'INFO') as logs:
                scanbot.open_store()
                self.assertIsInstance(scanbot.store, jobs.JobStore)
            self.assertEqual(len(store.all()), 2)
        self.assertIn('Scans are saved in', logs.output[0])


if __name__ == '__main__':
    unittest.main()
