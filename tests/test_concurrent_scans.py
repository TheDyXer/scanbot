"""
Tests for several people scanning at the same time: one scan each, a queue when all slots
are busy, /stop rights, and the shared mcstatus.io and ip-api.com rate limits.

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

import discord  # noqa: E402
from discord.ext import commands  # noqa: E402

import bot as scanbot  # noqa: E402


def person(user_id):
    return types.SimpleNamespace(id=user_id, mention=f'<@{user_id}>')


def make_ctx(user_id, guild_id=10, moderator=False, log=None):
    """A command context for one person. `log` collects (user ID, text) for every channel message."""
    progress = mock.MagicMock(edit=mock.AsyncMock())
    ctx = mock.MagicMock()
    ctx.author = person(user_id)
    ctx.guild = types.SimpleNamespace(id=guild_id) if guild_id else None
    ctx.permissions = types.SimpleNamespace(manage_messages=moderator)
    ctx.defer = mock.AsyncMock()
    ctx.send = mock.AsyncMock(return_value=progress)

    def channel_send(*args, **kwargs):
        if log is not None:
            log.append((user_id, args[0] if args else ''))
        return progress
    ctx.channel.send = mock.AsyncMock(side_effect=channel_send)
    return ctx


def attachment():
    return types.SimpleNamespace(filename='ips.txt', read=mock.AsyncMock(return_value=b'8.8.8.8\n1.1.1.1\n'))


def texts(mock_send):
    return [call.args[0] for call in mock_send.await_args_list if call.args]


async def until(condition, timeout=2):
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("condition never became true")
        await asyncio.sleep(0.01)


class ScanFlowTests(unittest.IsolatedAsyncioTestCase):
    """Runs real scans (API-only, no network): each check waits at a gate the test opens."""

    async def asyncSetUp(self):
        scanbot.bot.direct_ok = False
        self.gate = asyncio.Event()
        self.scan = scanbot.bot.get_command('scan').callback
        self.stop = scanbot.bot.get_command('stop').callback

        async def fake_api(session, ip, edition='java'):
            await self.gate.wait()
            return scanbot.make_result(ip, ip, 3, 20, ['Steve'], '1.21', 'hello')

        for p in (mock.patch.object(scanbot, 'check_api', fake_api),
                  mock.patch.object(scanbot, 'batch_get_locations', mock.AsyncMock(return_value={})),
                  mock.patch.object(scanbot, 'API_DELAY', 0),
                  mock.patch.object(scanbot, 'api_pacer', scanbot.Pacer()),
                  mock.patch.object(scanbot, 'set_status', mock.AsyncMock()),
                  mock.patch.dict(scanbot.scans, clear=True),
                  mock.patch.object(scanbot, 'queue', [])):
            p.start()
            self.addCleanup(p.stop)

    async def test_two_people_scan_at_the_same_time(self):
        a, b = make_ctx(1), make_ctx(2)
        tasks = [asyncio.create_task(self.scan(ctx, attachment())) for ctx in (a, b)]
        await until(lambda: scanbot.running_count() == 2)
        self.assertEqual(scanbot.queue, [])

        self.gate.set()
        await asyncio.gather(*tasks)
        for ctx, user_id in ((a, 1), (b, 2)):
            with self.subTest(user=user_id):
                done = [t for t in texts(ctx.channel.send) if 'Scan Complete' in t]
                self.assertEqual(len(done), 1)
                self.assertIn(f'<@{user_id}>', done[0])  # Results say whose scan it was
                self.assertIn(f'<@{user_id}>', texts(ctx.send)[0])
        self.assertEqual(scanbot.scans, {})

    async def test_the_same_person_cannot_start_a_second_scan(self):
        first = asyncio.create_task(self.scan(make_ctx(1), attachment()))
        await until(lambda: scanbot.running_count() == 1)
        again = make_ctx(1)
        await self.scan(again, attachment())
        self.assertIn('already have a scan', texts(again.send)[0])
        self.gate.set()
        await first

    async def test_extra_scans_wait_in_a_queue_and_start_in_order(self):
        log = []
        a, b, c = make_ctx(1, log=log), make_ctx(2, log=log), make_ctx(3, log=log)
        with mock.patch.object(scanbot, 'MAX_CONCURRENT_SCANS', 1):
            first = asyncio.create_task(self.scan(a, attachment()))
            await until(lambda: scanbot.running_count() == 1)
            waiting = [asyncio.create_task(self.scan(ctx, attachment())) for ctx in (b, c)]
            await until(lambda: len(scanbot.queue) == 2)

            self.assertIn('Queued** (#1)', texts(b.send)[0])
            self.assertIn('Queued** (#2)', texts(c.send)[0])
            self.assertEqual(scanbot.running_count(), 1)
            self.assertFalse(any('Scan started' in text for _, text in log))

            self.gate.set()
            await asyncio.gather(first, *waiting)

        started = [user for user, text in log if 'Scan started' in text]
        finished = [user for user, text in log if 'Scan Complete' in text]
        # Queued scans announce their start in the channel: the slash command's reply may have expired
        self.assertEqual(started, [2, 3])
        self.assertEqual(finished, [1, 2, 3])
        # Each queued scan started only after the one before it had finished
        order = [(user, 'start' if 'Scan started' in text else 'done') for user, text in log
                 if 'Scan started' in text or 'Scan Complete' in text]
        self.assertEqual(order, [(1, 'done'), (2, 'start'), (2, 'done'), (3, 'start'), (3, 'done')])
        self.assertEqual(scanbot.scans, {})

    async def test_stop_cancels_a_queued_scan_without_touching_the_running_one(self):
        a, b = make_ctx(1), make_ctx(2)
        with mock.patch.object(scanbot, 'MAX_CONCURRENT_SCANS', 1):
            first = asyncio.create_task(self.scan(a, attachment()))
            await until(lambda: scanbot.running_count() == 1)
            queued = asyncio.create_task(self.scan(b, attachment()))
            await until(lambda: len(scanbot.queue) == 1)

            await self.stop(b)
            await asyncio.wait_for(queued, 1)  # Returns without waiting for a slot
            self.assertIn('cancelled', texts(b.send)[-1])
            self.assertFalse(any('Scan started' in t for t in texts(b.channel.send)))
            self.assertEqual(scanbot.queue, [])
            self.assertFalse(scanbot.scans[1].stop.is_set())

            self.gate.set()
            await first
        self.assertTrue(any('Scan Complete' in t for t in texts(a.channel.send)))

    async def test_a_moderator_cancelling_a_queued_scan_tells_its_owner(self):
        a, b, moderator = make_ctx(1), make_ctx(2), make_ctx(9, moderator=True)
        with mock.patch.object(scanbot, 'MAX_CONCURRENT_SCANS', 1):
            first = asyncio.create_task(self.scan(a, attachment()))
            await until(lambda: scanbot.running_count() == 1)
            queued = asyncio.create_task(self.scan(b, attachment()))
            await until(lambda: len(scanbot.queue) == 1)

            await self.stop(moderator, None, person(2))
            await asyncio.wait_for(queued, 1)
            # The moderator's reply may be in another channel, so the owner hears it where they're waiting
            self.assertTrue(any('<@2>' in t and 'cancelled by <@9>' in t for t in texts(b.channel.send)))

            self.gate.set()
            await first

    async def test_stop_only_stops_your_own_scan(self):
        a, b = make_ctx(1), make_ctx(2)
        tasks = [asyncio.create_task(self.scan(ctx, attachment())) for ctx in (a, b)]
        await until(lambda: scanbot.running_count() == 2)

        await self.stop(a)
        self.assertTrue(scanbot.scans[1].stop.is_set())
        self.assertFalse(scanbot.scans[2].stop.is_set())

        self.gate.set()
        await asyncio.gather(*tasks)
        self.assertTrue(any('Scan stopped' in t for t in texts(a.channel.send)))
        self.assertTrue(any('Scan Complete' in t for t in texts(b.channel.send)))

    async def test_a_freed_slot_is_not_taken_by_a_newcomer_ahead_of_the_queue(self):
        with mock.patch.object(scanbot, 'MAX_CONCURRENT_SCANS', 1):
            running = scanbot.Scan(person(1), 10)
            waiting = scanbot.Scan(person(2), 10)
            newcomer = scanbot.Scan(person(3), 10)
            scanbot.scans.update({1: running, 2: waiting, 3: newcomer})
            self.assertEqual(scanbot.claim_slot(running), 0)
            self.assertEqual(scanbot.claim_slot(waiting), 1)

            scanbot.release(running)
            self.assertTrue(waiting.running and waiting.turn.is_set())
            self.assertEqual(scanbot.claim_slot(newcomer), 1)  # Slot is taken again, so it queues
            self.assertFalse(newcomer.running)


class ModeratorStopTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.stop = scanbot.bot.get_command('stop').callback
        self.scans = {}
        for user_id, guild_id in ((1, 10), (2, 10), (3, 20)):  # Person 3 is scanning in another server
            scan = scanbot.Scan(person(user_id), guild_id)
            scan.running = True
            self.scans[user_id] = scan
        for p in (mock.patch.dict(scanbot.scans, self.scans, clear=True), mock.patch.object(scanbot, 'queue', [])):
            p.start()
            self.addCleanup(p.stop)

    def stopped(self):
        return sorted(user_id for user_id, scan in self.scans.items() if scan.stop.is_set())

    async def test_people_without_manage_messages_cannot_stop_others(self):
        ctx = make_ctx(9, moderator=False)
        await self.stop(ctx, 'all')
        await self.stop(ctx, None, person(1))
        self.assertEqual(self.stopped(), [])
        self.assertTrue(all('Only moderators' in t for t in texts(ctx.send)))

    async def test_moderators_stop_one_person_or_every_scan_in_their_server(self):
        ctx = make_ctx(9, moderator=True)
        await self.stop(ctx, None, person(1))
        self.assertEqual(self.stopped(), [1])
        self.assertIn('<@1>', texts(ctx.send)[-1])

        await self.stop(ctx, 'all')
        self.assertEqual(self.stopped(), [1, 2])  # Not person 3's: that's another server

        await self.stop(ctx, None, person(3))
        self.assertEqual(self.stopped(), [1, 2])
        self.assertIn('no scan running here', texts(ctx.send)[-1])

    async def test_nobody_is_a_moderator_in_dms(self):
        ctx = make_ctx(9, guild_id=None, moderator=True)
        await self.stop(ctx, 'all')
        self.assertEqual(self.stopped(), [])

    async def test_naming_yourself_stops_your_own_scan(self):
        ctx = make_ctx(1, moderator=False)
        await self.stop(ctx, None, person(1))
        self.assertEqual(self.stopped(), [1])


class StopArgumentTests(unittest.IsolatedAsyncioTestCase):
    """!stop takes "all" or a mention; /stop has an `all` choice and a `user` picker."""

    async def parse(self, content, mentions=()):
        msg = mock.MagicMock(attachments=[], mentions=list(mentions), content=content, guild=None)
        view = commands.view.StringView(content)
        ctx = commands.Context(message=msg, bot=scanbot.bot, view=view, prefix='!')
        view.skip_string('!')
        name = view.get_word()
        view.skip_ws()
        ctx.invoked_with, ctx.command = name, scanbot.bot.get_command(name)
        await ctx.command._parse_arguments(ctx)
        return ctx.args[1:]  # after ctx

    async def test_prefix_stop_takes_all_or_a_mention(self):
        user = discord.Object(id=123456789012345678)  # Real IDs have 15-20 digits; shorter ones aren't recognised
        self.assertEqual(await self.parse('!stop'), [None, None])
        self.assertEqual(await self.parse('!stop all'), ['all', None])
        self.assertEqual(await self.parse('!stop <@123456789012345678>', [user]), [None, user])

    def test_slash_stop_has_an_all_choice_and_a_user_option(self):
        params = {p.display_name: p for p in scanbot.bot.tree.get_command('stop').parameters}
        self.assertEqual(set(params), {'all', 'user'})
        self.assertEqual([c.value for c in params['all'].choices], ['all'])
        self.assertEqual(params['user'].type, discord.AppCommandOptionType.user)
        self.assertFalse(any(p.required for p in params.values()))


class SharedLimitTests(unittest.IsolatedAsyncioTestCase):
    async def test_pacer_spaces_requests_and_concurrent_scans_take_turns(self):
        pacer = scanbot.Pacer()
        order, times = [], []
        # A request just went out, so both scans find the service busy. (An idle pacer lets
        # the first caller through without waiting, which gives it one extra turn.)
        await pacer.wait(0.1)

        async def scan(name):
            for _ in range(3):
                await pacer.wait(0.1)
                order.append(name)
                times.append(time.monotonic())

        await asyncio.gather(scan('a'), scan('b'))
        self.assertEqual(order, ['a', 'b', 'a', 'b', 'a', 'b'])
        gaps = [later - earlier for earlier, later in zip(times, times[1:])]
        self.assertTrue(all(gap >= 0.08 for gap in gaps), gaps)  # Timer resolution on Windows is ~15 ms

    async def test_every_api_request_waits_on_the_shared_pacer(self):
        pacer = mock.MagicMock(wait=mock.AsyncMock())
        state = {'phase': '', 'done': 0, 'total': 0, 'found': 0, 'blocked': 0}
        with mock.patch.object(scanbot, 'api_pacer', pacer), \
                mock.patch.object(scanbot, 'check_api', mock.AsyncMock(return_value=None)):
            await scanbot.run_api(None, ['a', 'b', 'c'], {}, state, retrying=False)
        self.assertEqual(pacer.wait.await_count, 3)
        pacer.wait.assert_awaited_with(scanbot.API_DELAY)

    async def test_geolocation_waits_on_its_pacer_and_honours_stop(self):
        pacer = mock.MagicMock(wait=mock.AsyncMock())
        session = mock.MagicMock()
        stop = asyncio.Event()
        stop.set()
        with mock.patch.object(scanbot, 'geo_pacer', pacer):
            self.assertEqual(await scanbot.batch_get_locations(session, ['1.2.3.4'], stop=stop), {})
        pacer.wait.assert_awaited_once_with(scanbot.GEO_DELAY)
        session.post.assert_not_called()

    async def test_a_stopped_scan_pings_nothing(self):
        stop = asyncio.Event()
        stop.set()
        check = mock.AsyncMock(return_value=None)
        state = {'phase': '', 'done': 0, 'total': 0, 'found': 0, 'blocked': 0}
        with mock.patch.object(scanbot, 'check_direct', check):
            await scanbot.run_direct(['1.2.3.4', '5.6.7.8'], {}, state, stop=stop)
        check.assert_not_awaited()


class PresenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_presence_counts_running_and_queued_scans(self):
        set_status = mock.AsyncMock()
        running = scanbot.Scan(person(1), 10)
        running.running = True
        waiting = scanbot.Scan(person(2), 10)
        with mock.patch.object(scanbot, 'set_status', set_status), \
                mock.patch.dict(scanbot.scans, clear=True), mock.patch.object(scanbot, 'queue', []):
            await scanbot.update_presence()
            self.assertIn('Idle', set_status.await_args.args[0])

            scanbot.scans.update({1: running, 2: waiting})
            scanbot.queue.append(waiting)
            await scanbot.update_presence()
            self.assertEqual(set_status.await_args.args[0], 'Scanning · 1 running, 1 queued')

    def test_progress_line_names_the_owner(self):
        state = {'phase': 'Pinging servers', 'done': 1, 'total': 2, 'found': 1, 'owner': '<@1>'}
        self.assertIn('<@1>', scanbot.progress_text(state))
        del state['owner']
        self.assertTrue(scanbot.progress_text(state).startswith('🔎 **Pinging servers:**'))


if __name__ == '__main__':
    unittest.main()
