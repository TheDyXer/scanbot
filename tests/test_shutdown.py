"""
Tests for stopping the bot cleanly: on SIGTERM (Docker, Watchtower, systemd) or Ctrl+C, running scans post what
they found so far, queued ones are cancelled and their owners told, and new scans are refused until it's back.

Run from the repository root:  python -m unittest discover -s tests
"""
import asyncio
import os
import signal
import sys
import time
import types
import unittest
from unittest import mock

# bot.py exits at import time without a token
os.environ.setdefault('DISCORD_TOKEN', 'test-token')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import bot as scanbot  # noqa: E402

ANSWERS = '8.8.8.8'   # Answers at once
SLOW = '1.1.1.1'      # Waits at the gate, which the tests keep closed


def person(user_id):
    return types.SimpleNamespace(id=user_id, mention=f'<@{user_id}>')


def make_ctx(user_id, guild_id=10):
    """A command context for one person. ctx.progress is the progress message the bot edits."""
    progress = mock.MagicMock(edit=mock.AsyncMock())
    ctx = mock.MagicMock()
    ctx.author = person(user_id)
    ctx.guild = types.SimpleNamespace(id=guild_id) if guild_id else None
    ctx.permissions = types.SimpleNamespace(manage_messages=False)
    ctx.defer = mock.AsyncMock()
    ctx.send = mock.AsyncMock(return_value=progress)
    ctx.channel.send = mock.AsyncMock(return_value=progress)
    ctx.progress = progress
    return ctx


def attachment(read=None):
    return types.SimpleNamespace(filename='ips.txt', size=20,
                                 read=read or mock.AsyncMock(return_value=f'{ANSWERS}\n{SLOW}\n'.encode()))


def texts(mock_send):
    return [call.args[0] for call in mock_send.await_args_list if call.args]


def edits(progress):
    return [call.kwargs.get('content', '') for call in progress.edit.await_args_list]


async def finish(*aws, timeout=5):
    """Awaits scans and shutdowns, failing instead of hanging if one never ends."""
    await asyncio.wait_for(asyncio.gather(*aws), timeout)


async def until(condition, timeout=2):
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("condition never became true")
        await asyncio.sleep(0.01)


class ShutdownTests(unittest.IsolatedAsyncioTestCase):
    """Real scans (API only, no network): SLOW's check waits at a gate that stays closed, as a slow server would."""

    async def asyncSetUp(self):
        self.gate = asyncio.Event()
        self.waiting = 0  # Checks waiting at the gate
        self.api_calls = 0
        self.scan = scanbot.bot.get_command('scan').callback
        self.close = mock.AsyncMock()

        async def fake_api(session, ip, edition='java'):
            self.api_calls += 1
            if ip == SLOW:
                self.waiting += 1
                await self.gate.wait()
            return scanbot.make_result(ip, ip, 3, 20, ['Steve'], '1.21', 'hello')

        for p in (mock.patch.object(scanbot.bot, 'direct_ok', False),
                  mock.patch.object(scanbot, 'PINGER_URL', ''),
                  mock.patch.object(scanbot, 'check_api', fake_api),
                  mock.patch.object(scanbot, 'batch_get_locations', mock.AsyncMock(return_value={})),
                  mock.patch.object(scanbot, 'API_DELAY', 0),
                  mock.patch.object(scanbot, 'api_pacer', scanbot.Pacer()),
                  mock.patch.object(scanbot, 'set_status', mock.AsyncMock()),
                  mock.patch.dict(scanbot.scans, clear=True),
                  mock.patch.object(scanbot, 'queue', []),
                  mock.patch.object(scanbot, 'shutting_down', False)):
            p.start()
            self.addCleanup(p.stop)

    def start(self, ctx, file=None):
        return asyncio.create_task(self.scan(ctx, file or attachment()))

    async def test_running_scans_post_what_they_found_and_say_the_bot_is_restarting(self):
        a, b = make_ctx(1), make_ctx(2)
        tasks = [self.start(a), self.start(b)]
        await until(lambda: self.waiting == 2)  # Both found ANSWERS and are waiting for SLOW

        # Returns as soon as both have posted, long before the grace period is over
        await finish(scanbot.shutdown('SIGTERM', grace=30, close=self.close), timeout=3)

        for ctx, user_id in ((a, 1), (b, 2)):
            with self.subTest(user=user_id):
                results = [t for t in texts(ctx.channel.send) if 'Scan stopped' in t]
                self.assertEqual(len(results), 1)
                self.assertIn('restarting', results[0])
                self.assertIn(ANSWERS, results[0])  # What it found before the stop
                self.assertIn(f'<@{user_id}>', results[0])
                self.assertIn('restarting', edits(ctx.progress)[-1])
        self.assertEqual(scanbot.scans, {})
        self.close.assert_awaited_once()
        await finish(*tasks)

    async def test_queued_scans_are_cancelled_and_their_owners_told(self):
        a, b = make_ctx(1), make_ctx(2)
        with mock.patch.object(scanbot, 'MAX_CONCURRENT_SCANS', 1):
            tasks = [self.start(a)]
            await until(lambda: self.waiting == 1)
            tasks.append(self.start(b))
            await until(lambda: len(scanbot.queue) == 1)

            await finish(scanbot.shutdown('SIGTERM', grace=30, close=self.close), timeout=3)

        # Posted in the channel: a queued scan's slash command reply may have stopped working
        notes = [t for t in texts(b.channel.send) if 'queued scan was cancelled' in t]
        self.assertEqual(len(notes), 1)
        self.assertIn('restarting', notes[0])
        self.assertIn('<@2>', notes[0])
        self.assertFalse(any('Scan started' in t for t in texts(b.send) + texts(b.channel.send)))
        self.assertEqual(scanbot.queue, [])
        self.assertEqual(scanbot.scans, {})
        self.assertEqual(self.api_calls, 2)  # Only a's two servers were ever checked
        await finish(*tasks)

    async def test_a_scan_still_reading_its_file_is_cancelled_instead_of_starting(self):
        file_gate = asyncio.Event()

        async def slow_read():
            await file_gate.wait()
            return f'{ANSWERS}\n{SLOW}\n'.encode()

        ctx = make_ctx(1)
        task = self.start(ctx, attachment(read=slow_read))
        await until(lambda: 1 in scanbot.scans)  # Registered, but neither running nor queued yet
        stopping = asyncio.create_task(scanbot.shutdown('SIGTERM', grace=30, close=self.close))
        await until(lambda: scanbot.shutting_down)
        file_gate.set()
        await finish(task, stopping, timeout=3)

        self.assertTrue(any('cancelled because the bot is restarting' in t for t in texts(ctx.send)))
        self.assertFalse(any('Scan started' in t for t in texts(ctx.send) + texts(ctx.channel.send)))
        self.assertEqual(self.api_calls, 0)
        self.close.assert_awaited_once()

    async def test_new_scans_are_refused_while_the_bot_is_restarting(self):
        ctx, file = make_ctx(1), attachment()
        with mock.patch.object(scanbot, 'shutting_down', True):
            await finish(self.scan(ctx, file), timeout=3)
        self.assertIn('restarting', texts(ctx.send)[0])
        self.assertEqual(scanbot.scans, {})
        file.read.assert_not_awaited()

    async def test_shutdown_closes_after_the_grace_period_when_a_scan_hangs(self):
        hang = asyncio.Event()

        async def stuck_results(*args, **kwargs):
            await hang.wait()

        with mock.patch.object(scanbot, 'send_results', stuck_results):
            task = self.start(make_ctx(1))
            await until(lambda: self.waiting == 1)
            with self.assertLogs('scanbot', 'WARNING') as logs:
                await scanbot.shutdown('SIGTERM', grace=0.2, close=self.close)
            self.close.assert_awaited_once()
            self.assertIn(1, scanbot.scans)  # Still posting when the bot closed
            self.assertTrue(any("didn't finish" in line and '1' in line for line in logs.output))
            hang.set()
            await finish(task)

    async def test_a_second_signal_closes_at_once(self):
        hang = asyncio.Event()

        async def stuck_results(*args, **kwargs):
            await hang.wait()

        first_close, second_close = mock.AsyncMock(), mock.AsyncMock()
        with mock.patch.object(scanbot, 'send_results', stuck_results):
            task = self.start(make_ctx(1))
            await until(lambda: self.waiting == 1)
            first = asyncio.create_task(scanbot.shutdown('SIGTERM', grace=10, close=first_close))
            await until(lambda: scanbot.shutting_down)

            await asyncio.wait_for(scanbot.shutdown('SIGINT', close=second_close), 1)
            second_close.assert_awaited_once()
            first_close.assert_not_awaited()  # The first one is still waiting for the scan

            hang.set()
            await finish(task, first)
        first_close.assert_awaited_once()

    async def test_stop_cancels_api_checks_that_already_started(self):
        # The last checks of a list are all in flight at once; /stop and shutdown mustn't wait for them
        stop, results = asyncio.Event(), {}
        state = {'phase': '', 'done': 0, 'total': 0, 'found': 0, 'blocked': 0}
        task = asyncio.create_task(scanbot.run_api(None, [ANSWERS, SLOW], results, state, retrying=False, stop=stop))
        await until(lambda: self.waiting == 1)
        stop.set()
        await asyncio.wait_for(task, 1)
        self.assertEqual(list(results), [ANSWERS])

    async def test_presence_says_the_bot_is_restarting(self):
        with mock.patch.object(scanbot, 'shutting_down', True):
            await scanbot.update_presence()
        self.assertIn('Restarting', scanbot.set_status.await_args.args[0])


class QueueTests(unittest.TestCase):
    def setUp(self):
        for p in (mock.patch.dict(scanbot.scans, clear=True),
                  mock.patch.object(scanbot, 'queue', []),
                  mock.patch.object(scanbot, 'MAX_CONCURRENT_SCANS', 1),
                  mock.patch.object(scanbot, 'shutting_down', False)):
            p.start()
            self.addCleanup(p.stop)

    def test_queued_scans_dont_start_while_shutting_down(self):
        first, second = scanbot.Scan(person(1), 10), scanbot.Scan(person(2), 10)
        scanbot.scans.update({1: first, 2: second})
        self.assertEqual(scanbot.claim_slot(first), 0)
        self.assertEqual(scanbot.claim_slot(second), 1)
        with mock.patch.object(scanbot, 'shutting_down', True):
            scanbot.release(first)
        self.assertFalse(second.running)
        self.assertFalse(second.turn.is_set())
        self.assertEqual(scanbot.queue, [second])

    def test_release_marks_the_scan_done(self):
        scan = scanbot.Scan(person(1), 10)
        scanbot.scans[1] = scan
        self.assertFalse(scan.done.is_set())
        scanbot.release(scan)
        self.assertTrue(scan.done.is_set())

    def test_the_restart_reason_sticks(self):
        scan = scanbot.Scan(person(1), 10)
        scanbot.request_stop(scan, reason='restart')
        scanbot.request_stop(scan, by=person(2))  # A moderator's /stop after SIGTERM
        self.assertEqual(scan.stop_reason, 'restart')

        own = scanbot.Scan(person(3), 10)
        scanbot.request_stop(own, by=person(3))
        self.assertEqual(own.stop_reason, 'user')


class ResultsTests(unittest.IsolatedAsyncioTestCase):
    async def results_text(self, stopped, stop_reason):
        ctx = make_ctx(1)
        result = scanbot.make_result(ANSWERS, ANSWERS, 3, 20, [], '1.21', 'hello')
        await scanbot.send_results(ctx, [result], {}, stopped, 2, 1.0, stop_reason=stop_reason)
        return texts(ctx.channel.send)[0]

    async def test_results_say_the_rest_wasnt_checked_because_of_a_restart(self):
        text = await self.results_text(True, 'restart')
        self.assertIn('Scan stopped', text)
        self.assertIn('restarting', text)
        self.assertIn("wasn't checked", text)

    async def test_results_of_a_scan_stopped_with_stop_dont_mention_a_restart(self):
        self.assertNotIn('restarting', await self.results_text(True, 'user'))
        self.assertNotIn('restarting', await self.results_text(False, None))


class SignalTests(unittest.IsolatedAsyncioTestCase):
    async def test_sigterm_and_sigint_start_shutdown(self):
        loop = asyncio.get_running_loop()
        registered = {}
        fake_loop = types.SimpleNamespace(
            add_signal_handler=lambda sig, callback, *args: registered.__setitem__(sig, (callback, args)),
            create_task=loop.create_task)
        scanbot.install_signal_handlers(fake_loop)
        self.assertEqual(set(registered), {signal.SIGTERM, signal.SIGINT})

        with mock.patch.object(scanbot, 'shutdown', mock.AsyncMock()) as shutdown:
            callback, args = registered[signal.SIGTERM]
            callback(*args)
            self.assertEqual(len(scanbot.shutdown_tasks), 1)  # Kept, so it isn't garbage collected mid-shutdown
            await asyncio.gather(*scanbot.shutdown_tasks)
            shutdown.assert_awaited_once_with('SIGTERM')
        await asyncio.sleep(0)
        self.assertEqual(scanbot.shutdown_tasks, set())

    async def test_platforms_without_signal_support_are_fine(self):
        fake_loop = mock.MagicMock()
        fake_loop.add_signal_handler.side_effect = NotImplementedError  # Windows
        scanbot.install_signal_handlers(fake_loop)

        # The real loop: on Windows this is the NotImplementedError path, elsewhere it really installs them
        loop = asyncio.get_running_loop()
        scanbot.install_signal_handlers(loop)
        if os.name != 'nt':
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.remove_signal_handler(sig)

    @unittest.skipIf(os.name == 'nt', "Windows' event loop can't catch signals")
    async def test_a_real_sigterm_starts_shutdown(self):
        loop = asyncio.get_running_loop()
        with mock.patch.object(scanbot, 'shutdown', mock.AsyncMock()) as shutdown:
            scanbot.install_signal_handlers(loop)
            try:
                os.kill(os.getpid(), signal.SIGTERM)
                await until(lambda: shutdown.await_count == 1)
            finally:
                for sig in (signal.SIGTERM, signal.SIGINT):
                    loop.remove_signal_handler(sig)
        shutdown.assert_awaited_once_with('SIGTERM')


if __name__ == '__main__':
    unittest.main()
