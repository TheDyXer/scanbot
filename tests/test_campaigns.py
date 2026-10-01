"""
Tests for campaigns: lists and targets bigger than one scan run in parts, one after another, after a preview and a
confirm:yes. Covers the span helpers (big targets expanded part by part), the preview and its estimate, the API
default, stopping and resuming in the middle, waiting for direct pings, and the RIPEstat cache.

Run from the repository root:  python -m unittest discover -s tests
"""
import asyncio
import ipaddress
import os
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

# bot.py exits at import time without a token
os.environ.setdefault('DISCORD_TOKEN', 'test-token')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import bot as scanbot  # noqa: E402
import jobs  # noqa: E402

IPS = [f'1.0.0.{n}' for n in range(1, 8)]  # 7 entries: with 3 per part, a campaign of 3 parts (3, 3, 1)
Target = scanbot.TargetSpec


def ip(n):
    return int(ipaddress.IPv4Address(n))


def online(entry, source):
    return scanbot.make_result(entry, entry, 3, 20, ['Steve'], '1.21', 'hello', source=source)


def make_ctx(user_id=1, slash=True):
    progress = mock.MagicMock(edit=mock.AsyncMock(), id=555)
    ctx = mock.MagicMock()
    ctx.author = types.SimpleNamespace(id=user_id, mention=f'<@{user_id}>')
    ctx.guild = types.SimpleNamespace(id=10)
    ctx.permissions = types.SimpleNamespace(manage_messages=False)
    ctx.defer = mock.AsyncMock()
    ctx.channel = mock.MagicMock()
    ctx.channel.id = 100
    ctx.channel.guild = None
    ctx.channel.send = mock.AsyncMock(return_value=progress)
    ctx.channel.get_partial_message = mock.MagicMock(return_value=mock.MagicMock(edit=mock.AsyncMock()))
    ctx.send = mock.AsyncMock(return_value=progress)
    ctx.progress = progress
    if not slash:
        ctx.interaction = None
        ctx.clean_prefix = '!'
    return ctx


def attachment(entries=IPS):
    data = ''.join(f'{e}\n' for e in entries).encode()
    return types.SimpleNamespace(filename='ips.txt', size=len(data), read=mock.AsyncMock(return_value=data))


def texts(mock_send):
    return [call.args[0] for call in mock_send.await_args_list if call.args]


def posted(ctx):
    return texts(ctx.send) + texts(ctx.channel.send)


async def until(condition, timeout=3):
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("condition never became true")
        await asyncio.sleep(0.01)


class SpanTests(unittest.TestCase):
    """Big targets are kept as address ranges and expanded one part at a time."""

    def test_counts_leave_out_private_addresses(self):
        spans = [[ip('9.255.255.250'), ip('10.0.0.5')], [ip('1.2.3.0'), ip('1.2.3.9')]]  # The first ends in 10/8
        self.assertEqual(scanbot.span_count(spans), 22)
        self.assertEqual(scanbot.special_count(*spans[0]), 6)
        self.assertEqual(scanbot.public_count(spans), 16)

    def test_the_parts_together_are_exactly_the_whole_target_in_order(self):
        networks = [ipaddress.IPv4Network(n) for n in ('1.2.3.0/29', '9.255.255.248/29', '10.0.0.0/30', '5.5.5.5/32')]
        everything = list(scanbot.expand_prefixes(networks))
        spans = scanbot.network_spans(networks)
        for size in (1, 3, 7, 100):
            with self.subTest(size=size):
                count = scanbot.span_count(spans)
                parts = [list(scanbot.span_entries(spans, start, start + size))
                         for start in range(0, count, size)]
                self.assertEqual([e for part in parts for e in part], everything)
                self.assertTrue(all(len(part) <= size for part in parts))

    def test_a_part_far_into_a_huge_target_comes_at_once(self):
        spans = [[ip('1.0.0.0'), ip('1.255.255.255')]]  # 16.7 million addresses
        began = time.monotonic()
        part = list(scanbot.span_entries(spans, 15_000_000, 15_000_003, port=25570))
        self.assertLess(time.monotonic() - began, 0.5)
        self.assertEqual(part, ['1.228.225.192:25570', '1.228.225.193:25570', '1.228.225.194:25570'])

    def test_a_list_may_hold_up_to_the_limit_given(self):
        text = "\n".join(IPS)
        with mock.patch.object(scanbot, 'MAX_IPS_PER_SCAN', 3):
            self.assertEqual(scanbot.parse_list(text, limit=10).addresses, IPS)
            with self.assertRaises(scanbot.TooManyAddresses) as caught:
                scanbot.parse_list(text, limit=5)
            reply = caught.exception.reply()
        self.assertEqual(caught.exception.line_number, 6)
        self.assertIn("takes the list past 5 addresses, the most a campaign takes", reply)

    def test_a_range_line_stays_scan_sized_and_points_to_a_target(self):
        with self.assertRaises(scanbot.TooManyAddresses) as caught:
            scanbot.parse_list("1.2.0.0/16\n", limit=2_000_000)
        self.assertEqual(caught.exception.reply(),
                         "❌ **Too many IPs:** line 1 (`1.2.0.0/16`) has 65,534 addresses; a range line in a list may "
                         "have at most 30000. Scan it with `/scan target:cidr:1.2.0.0/16` instead: a target that big "
                         "runs as a campaign.")


class HelperTests(unittest.TestCase):
    def test_rough_durations(self):
        self.assertEqual(scanbot.rough_duration(20), "1 m")
        self.assertEqual(scanbot.rough_duration(45 * 60), "45 m")
        self.assertEqual(scanbot.rough_duration(3 * 3600 + 26 * 60), "3 h 26 m")
        self.assertEqual(scanbot.rough_duration(2 * 86400 + 4 * 3600), "2 d 4 h")

    def test_the_estimate_is_the_worst_case_from_the_settings(self):
        with mock.patch.object(scanbot, 'DIRECT_TIMEOUT', 3), mock.patch.object(scanbot, 'DIRECT_CONCURRENCY', 300), \
                mock.patch.object(scanbot, 'API_DELAY', 0.2):
            self.assertEqual(scanbot.campaign_estimate(30000, True, False), 300)
            self.assertEqual(scanbot.campaign_estimate(30000, True, True), 300 + 6000)
            self.assertEqual(scanbot.campaign_estimate(30000, False, True), 6000)

    def test_the_command_to_start_it_is_written_the_way_the_user_wrote_theirs(self):
        target = Target('asn', 'AS8400')
        self.assertEqual(scanbot.confirm_command(make_ctx(), None, target, 'java', 'auto'),
                         "/scan target:asn:AS8400 confirm:yes")
        self.assertEqual(scanbot.confirm_command(make_ctx(), object(), None, 'bedrock', 'on'),
                         "/scan file:(the same file) edition:bedrock api:on confirm:yes")
        self.assertEqual(scanbot.confirm_command(make_ctx(slash=False), None, target, 'java', 'auto'),
                         "!scan asn:AS8400 java auto yes")
        self.assertEqual(scanbot.confirm_command(make_ctx(slash=False), object(), None, 'java', 'off'),
                         "!scan java off yes (with the same file attached)")

    def test_a_fresh_campaign_has_no_progress_and_a_later_part_has(self):
        job = jobs.new_job(1, '<@1>', 10, 100, 'java', False, {'kind': 'file'}, 7)
        self.assertFalse(scanbot.has_progress(job))
        job.cursor = {'phase': 'direct', 'index': 0}  # Saved before campaigns existed
        self.assertFalse(scanbot.has_progress(job))
        job.cursor = {'part': 1, 'phase': 'direct', 'index': 0}
        self.assertTrue(scanbot.has_progress(job))

    def test_the_progress_line_names_the_part(self):
        state = {'phase': 'Pinging servers', 'done': 2, 'total': 3, 'found': 5, 'owner': '<@1>', 'part': 2, 'parts': 3}
        self.assertEqual(scanbot.progress_text(state), "🔎 <@1> · Part 2/3 · **Pinging servers:** 2/3 · **Found:** 5")
        state['parts'] = 1
        self.assertNotIn('Part', scanbot.progress_text(state))


class CampaignTests(unittest.IsolatedAsyncioTestCase):
    """Real campaigns with fake pings, 3 addresses per part. Entries in self.slow wait at a gate."""

    async def asyncSetUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)  # Registered first, so it runs last
        self.store = jobs.JobStore.open(tmp.name)
        self.gate = asyncio.Event()
        self.slow, self.online, self.api_online = set(), {'1.0.0.1', '1.0.0.5'}, set()
        self.waiting = 0
        self.checked, self.api_checked = [], []
        self.direct = [True]  # What direct_pings_work answers, one by one (the last one repeats)
        self.close = mock.AsyncMock()
        self.scan_command = scanbot.bot.get_command('scan').callback
        self.stop_command = scanbot.bot.get_command('stop').callback
        self.prefixes_for_asn = mock.AsyncMock(return_value=[ipaddress.IPv4Network('1.2.3.0/29')])

        async def fake_direct(entry):
            self.checked.append(entry)
            if entry in self.slow:
                self.waiting += 1
                await asyncio.wait_for(self.gate.wait(), 5)
            return online(entry, 'direct') if entry in self.online else None

        async def fake_api(session, entry, edition='java'):
            self.api_checked.append(entry)
            return online(entry, 'mcstatus.io') if entry in self.api_online else None

        async def direct_pings_work(edition):
            return self.direct.pop(0) if len(self.direct) > 1 else self.direct[0]

        for p in (mock.patch.object(scanbot, 'store', self.store),
                  mock.patch.object(scanbot, 'check_direct', fake_direct),
                  mock.patch.object(scanbot, 'check_api', fake_api),
                  mock.patch.object(scanbot, 'direct_pings_work', direct_pings_work),
                  mock.patch.object(scanbot, 'prefixes_for_asn', self.prefixes_for_asn),
                  mock.patch.object(scanbot, 'batch_get_locations', mock.AsyncMock(return_value={})),
                  mock.patch.object(scanbot, 'geo_db', None),
                  mock.patch.object(scanbot, 'asn_db', None),
                  mock.patch.object(scanbot, 'PINGER_URL', ''),
                  mock.patch.object(scanbot, 'API_DELAY', 0),
                  mock.patch.object(scanbot, 'api_pacer', scanbot.Pacer()),
                  mock.patch.object(scanbot, 'MAX_IPS_PER_SCAN', 3),
                  mock.patch.object(scanbot, 'DIRECT_CONCURRENCY', 1),  # In list order, one at a time
                  mock.patch.object(scanbot, 'CHECKPOINT_INTERVAL', 0.01),
                  mock.patch.object(scanbot, 'VPN_CHECK_DOWN', 0.01),
                  mock.patch.object(scanbot, 'set_status', mock.AsyncMock()),
                  mock.patch.dict(scanbot.scans, clear=True),
                  mock.patch.dict(scanbot.target_cache, clear=True),
                  mock.patch.object(scanbot, 'queue', []),
                  mock.patch.object(scanbot, 'shutting_down', False),
                  mock.patch.object(scanbot.bot, 'direct_ok', True),
                  mock.patch.object(scanbot.bot, 'resumed', False)):
            p.start()
            self.addCleanup(p.stop)

    def saved(self):
        found = self.store.all()
        return found[0] if len(found) == 1 else found

    async def scan(self, ctx, entries=IPS, **options):
        await asyncio.wait_for(self.scan_command(ctx, attachment(entries), **options), 5)

    def results(self, ctx):
        [message] = [t for t in posted(ctx) if 'Scan Complete' in t or 'Scan stopped' in t]
        return message

    # --- Preview ---

    async def test_a_list_bigger_than_a_scan_gets_a_preview_and_nothing_starts(self):
        ctx = make_ctx()
        await self.scan(ctx)
        [reply] = texts(ctx.send)
        self.assertIn("📋 **Campaign preview:** ips.txt has 7 addresses: 3 parts of up to 3", reply)
        self.assertIn("with direct pings only", reply)
        self.assertIn("`/scan file:(the same file) confirm:yes`", reply)
        self.assertIn("With `api:on`", reply)
        self.assertEqual((self.checked, self.store.all(), scanbot.scans), ([], [], {}))

    async def test_a_preview_with_the_api_on_says_it_slows_everyones_checks(self):
        ctx = make_ctx()
        await self.scan(ctx, api='on')
        [reply] = texts(ctx.send)
        self.assertIn("it slows their API checks", reply)
        self.assertIn("api:on confirm:yes", reply)
        self.assertEqual(self.checked, [])

    # --- Running ---

    async def test_a_confirmed_campaign_runs_every_part_and_posts_one_result(self):
        ctx = make_ctx()
        await self.scan(ctx, confirm='yes')
        self.assertEqual(self.checked, IPS)
        self.assertEqual(self.api_checked, [])  # auto: off for a campaign
        self.assertIn("Campaign started", texts(ctx.send)[0])
        self.assertIn("7 IPs", texts(ctx.send)[0])
        self.assertIn("in 3 parts of up to 3", texts(ctx.send)[0])
        results = self.results(ctx)
        self.assertIn("📊 **Scan Complete!** (campaign: ips.txt, 3 parts)", results)
        self.assertIn("🔎 7 IPs", results)
        self.assertIn("1.0.0.1", results)
        self.assertIn("1.0.0.5", results)
        self.assertIn("5 didn't answer a direct ping and weren't retried", results)
        job = self.saved()
        self.assertEqual((job.status, job.parts, job.part_size, job.cursor['phase']), ('done', 3, 3, 'send'))

    async def test_a_scan_still_retries_through_the_api_by_default(self):
        ctx = make_ctx()
        await self.scan(ctx, entries=IPS[:3])
        self.assertEqual(self.api_checked, ['1.0.0.2', '1.0.0.3'])
        self.assertIn("Scan Complete!**", self.results(ctx))
        self.assertNotIn("campaign", self.results(ctx))

    async def test_a_campaign_with_the_api_on_retries_every_part(self):
        self.api_online = {'1.0.0.7'}
        ctx = make_ctx()
        await self.scan(ctx, api='on', confirm='yes')
        self.assertEqual(self.api_checked, ['1.0.0.2', '1.0.0.3', '1.0.0.4', '1.0.0.6', '1.0.0.7'])
        self.assertIn("1.0.0.7", self.results(ctx))

    async def test_the_api_phase_of_a_later_part_is_saved_as_that_part(self):
        # A restart there must carry on in part 2's API phase, not go back to part 1
        api_gate, reached = asyncio.Event(), asyncio.Event()

        async def slow_api(session, entry, edition='java'):
            self.api_checked.append(entry)
            if entry == '1.0.0.4':  # Part 2's first API check
                reached.set()
                await asyncio.wait_for(api_gate.wait(), 5)
            return None

        with mock.patch.object(scanbot, 'check_api', slow_api):
            task = asyncio.create_task(self.scan_command(make_ctx(), attachment(), api='on', confirm='yes'))
            await asyncio.wait_for(reached.wait(), 5)
            cursor = self.saved().cursor
            api_gate.set()
            await asyncio.wait_for(task, 5)
        self.assertEqual((cursor['part'], cursor['phase']), (1, 'api'))

    async def test_stopping_in_part_two_posts_what_every_part_found(self):
        self.slow = {'1.0.0.5'}
        ctx = make_ctx()
        task = asyncio.create_task(self.scan_command(ctx, attachment(), confirm='yes'))
        await until(lambda: self.waiting == 1)
        await self.stop_command(make_ctx())
        self.gate.set()
        await asyncio.wait_for(task, 5)
        results = self.results(ctx)
        self.assertIn("Scan stopped", results)
        self.assertIn("(campaign: ips.txt, 3 parts, stopped in part 2/3)", results)
        self.assertIn("1.0.0.1", results)  # Found in part 1
        self.assertNotIn("1.0.0.7", self.checked)  # Part 3 never ran
        self.assertEqual(self.saved().status, 'stopped')

    async def test_a_restart_in_part_two_carries_on_there_and_keeps_part_one(self):
        self.slow = {'1.0.0.5'}
        ctx = make_ctx()
        task = asyncio.create_task(self.scan_command(ctx, attachment(), confirm='yes'))
        await until(lambda: self.waiting == 1)
        shutdown = asyncio.create_task(scanbot.shutdown('SIGTERM', grace=5, close=self.close))
        await until(lambda: scanbot.shutting_down)
        self.gate.set()  # The ping in flight answers; nothing after it starts
        await asyncio.wait_for(shutdown, 5)
        await asyncio.wait_for(task, 5)
        scanbot.shutting_down = False
        job = self.saved()
        self.assertEqual((job.status, job.cursor), ('interrupted', {'part': 1, 'phase': 'direct', 'index': 2}))

        self.checked.clear()
        channel = ctx.channel
        with mock.patch.object(scanbot.bot, 'get_channel', mock.MagicMock(return_value=channel)):
            scanbot.start_resumed(scanbot.register_resumable(self.store))
            await asyncio.wait_for(asyncio.gather(*scanbot.resume_tasks), 5)
        self.assertEqual(self.checked, ['1.0.0.6', '1.0.0.7'])
        resumed = [t for t in texts(channel.send) if 'Campaign resumed' in t]
        self.assertEqual(len(resumed), 1)
        self.assertIn("In part 2 of 3: 2 of 3 were pinged before the restart, 2 found so far", resumed[0])
        results = [t for t in texts(channel.send) if 'Scan Complete' in t][0]
        self.assertIn("1.0.0.1", results)
        self.assertIn("1.0.0.5", results)
        self.assertEqual(self.saved().status, 'done')

    async def test_with_the_api_off_a_campaign_waits_for_direct_pings_instead_of_giving_up(self):
        # The check before queueing, run_job's, then part 2: down twice, then back
        self.direct = [True, True, False, False, True]
        ctx = make_ctx()
        await self.scan(ctx, confirm='yes')
        self.assertEqual(self.checked, IPS)
        self.assertIn("Scan Complete", self.results(ctx))
        self.assertEqual(self.direct, [True])

    async def test_a_campaign_waiting_for_direct_pings_can_be_stopped(self):
        self.direct = [True, True, False]
        ctx = make_ctx()
        task = asyncio.create_task(self.scan_command(ctx, attachment(), confirm='yes'))
        await until(lambda: len(self.checked) == 3)
        await asyncio.sleep(0.05)  # Waiting now
        await self.stop_command(make_ctx())
        await asyncio.wait_for(task, 5)
        self.assertEqual(self.checked, IPS[:3])
        self.assertIn("stopped in part 2/3", self.results(ctx))

    async def test_a_waiting_campaign_shows_it_in_its_progress(self):
        state = {'phase': 'Pinging servers'}
        self.direct = [False, False, True]
        stop = asyncio.Event()
        with mock.patch.object(scanbot, 'PINGER_URL', 'http://pinger'):
            self.assertTrue(await scanbot.wait_for_direct_pings('java', stop, state))
        self.assertEqual(state['phase'], "Waiting for direct pings (the VPN is down)")

    # --- Big targets ---

    async def test_a_big_target_runs_part_by_part_without_a_list_file(self):
        ctx = make_ctx()
        task = asyncio.create_task(self.scan_command(ctx, None, Target('asn', 'AS8400'), confirm='yes'))
        await until(lambda: len(self.checked) >= 1)
        job = self.saved()
        self.assertEqual(job.source['spans'], [[ip('1.2.3.1'), ip('1.2.3.6')]])
        self.assertIsNone(self.store.read_list(job))  # Only the spans are saved
        await asyncio.wait_for(task, 5)
        self.assertEqual(self.checked, [f'1.2.3.{n}' for n in range(1, 7)])
        self.assertIn("(campaign: AS8400 (1 prefix), 2 parts)", self.results(ctx))

    async def test_a_big_target_counts_and_scans_only_its_public_addresses(self):
        self.prefixes_for_asn.return_value = [ipaddress.IPv4Network('1.2.3.0/29'), ipaddress.IPv4Network('10.0.0.0/30')]
        ctx = make_ctx()
        await self.scan_command(ctx, None, Target('asn', 'AS8400'))
        self.assertIn("has 6 addresses (2 private or local ones are skipped)", texts(ctx.send)[0])
        ctx = make_ctx()
        await asyncio.wait_for(self.scan_command(ctx, None, Target('asn', 'AS8400'), confirm='yes'), 5)
        self.assertIn("6 IPs from AS8400", texts(ctx.send)[0])
        self.assertIn("🔎 6 IPs", self.results(ctx))
        self.assertEqual(self.checked, [f'1.2.3.{n}' for n in range(1, 7)])

    async def test_a_big_target_resumes_from_its_spans(self):
        job = jobs.new_job(1, '<@1>', 10, 100, 'java', False,
                           {'kind': 'target', 'description': 'AS8400 (1 prefix)',
                            'spans': [[ip('1.2.3.1'), ip('1.2.3.6')]], 'port': None}, 6)
        job.parts, job.part_size, job.status = 2, 3, 'interrupted'
        job.cursor = {'part': 1, 'phase': 'direct', 'index': 1}
        self.store.save(job)
        channel = make_ctx().channel
        with mock.patch.object(scanbot.bot, 'get_channel', mock.MagicMock(return_value=channel)):
            scanbot.start_resumed(scanbot.register_resumable(self.store))
            await asyncio.wait_for(asyncio.gather(*scanbot.resume_tasks), 5)
        self.assertEqual(self.checked, ['1.2.3.5', '1.2.3.6'])
        self.assertEqual(self.saved().status, 'done')

    async def test_ripestat_is_asked_once_for_the_preview_and_the_confirmed_run(self):
        await self.scan_command(make_ctx(), None, Target('asn', 'AS8400'))
        await self.scan_command(make_ctx(), None, Target('asn', '8400'), confirm='yes')
        self.assertEqual(self.prefixes_for_asn.await_count, 1)
        with mock.patch.object(scanbot, 'TARGET_CACHE_SECONDS', -1):
            await self.scan_command(make_ctx(), None, Target('asn', 'AS8400'))
        self.assertEqual(self.prefixes_for_asn.await_count, 2)


class ErrorReplyTests(unittest.IsolatedAsyncioTestCase):
    async def test_yes_in_the_editions_place_explains_the_order(self):
        ctx = mock.MagicMock(send=mock.AsyncMock())
        param = mock.MagicMock()
        param.name = 'edition'
        from discord.ext import commands
        await scanbot.bot.on_command_error(ctx, commands.BadLiteralArgument(param, ('java', 'bedrock'), [], 'yes'))
        self.assertIn("`!scan asn:AS8400 java auto yes`", ctx.send.await_args.args[0])


if __name__ == '__main__':
    unittest.main()
