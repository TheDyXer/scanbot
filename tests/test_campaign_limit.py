"""
Tests for MAX_CONCURRENT_CAMPAIGNS: at most that many campaigns hold a scan slot at once, so the other slots stay free
for normal scans. A campaign over the limit waits in the queue, and normal scans queued after it may start first.
Covers the slot rules, resuming after a restart, the queue message, the preview, /help and the startup warning.

Run from the repository root:  python -m unittest discover -s tests
"""
import asyncio
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

# bot.py exits at import time without a token
os.environ.setdefault('DISCORD_TOKEN', 'test-token')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.dirname(__file__))

import bot as scanbot  # noqa: E402
import jobs  # noqa: E402
import pinger  # noqa: E402
import test_campaigns as campaigns  # noqa: E402  (its harness; a module, so its tests don't run twice)


class SlotTests(unittest.TestCase):
    """The slot rules on their own, with 5 slots and one campaign at a time unless a test says otherwise."""

    def setUp(self):
        for p in (mock.patch.dict(scanbot.scans, clear=True),
                  mock.patch.object(scanbot, 'queue', []),
                  mock.patch.object(scanbot, 'shutting_down', False),
                  mock.patch.object(scanbot, 'MAX_CONCURRENT_SCANS', 5),
                  mock.patch.object(scanbot, 'MAX_CONCURRENT_CAMPAIGNS', 1)):
            p.start()
            self.addCleanup(p.stop)

    def scan(self, owner, parts=1):
        scan = scanbot.Scan(types.SimpleNamespace(id=owner, mention=f'<@{owner}>'), 10)
        scan.job = types.SimpleNamespace(parts=parts)
        scanbot.scans[owner] = scan
        return scan

    def campaign(self, owner):
        return self.scan(owner, parts=3)

    def test_only_scans_in_more_than_one_part_are_campaigns(self):
        self.assertTrue(scanbot.is_campaign(self.campaign(1)))
        self.assertFalse(scanbot.is_campaign(self.scan(2)))
        self.assertFalse(scanbot.is_campaign(self.scan(3, parts=0)))
        bare = scanbot.Scan(types.SimpleNamespace(id=4, mention='<@4>'), 10)  # No job yet
        self.assertFalse(scanbot.is_campaign(bare))

    def test_a_second_campaign_waits_although_slots_are_free(self):
        self.assertEqual(scanbot.claim_slot(self.campaign(1)), 0)
        second = self.campaign(2)
        self.assertEqual(scanbot.claim_slot(second), 1)
        self.assertFalse(second.running)
        self.assertEqual((scanbot.running_count(), scanbot.running_campaigns()), (1, 1))

    def test_a_normal_scan_starts_ahead_of_a_waiting_campaign(self):
        scanbot.claim_slot(self.campaign(1))
        waiting = self.campaign(2)
        scanbot.claim_slot(waiting)
        normal = self.scan(3)
        self.assertEqual(scanbot.claim_slot(normal), 0)
        self.assertTrue(normal.running)
        self.assertEqual(scanbot.queue, [waiting])

    def test_a_normal_scan_still_queues_when_every_slot_is_busy(self):
        with mock.patch.object(scanbot, 'MAX_CONCURRENT_SCANS', 2):
            scanbot.claim_slot(self.campaign(1))
            scanbot.claim_slot(self.scan(2))
            self.assertEqual(scanbot.claim_slot(self.scan(3)), 1)

    def test_a_freed_slot_goes_to_the_normal_scan_behind_a_waiting_campaign(self):
        with mock.patch.object(scanbot, 'MAX_CONCURRENT_SCANS', 2):
            scanbot.claim_slot(self.campaign(1))
            normal = self.scan(2)
            scanbot.claim_slot(normal)
            waiting, later = self.campaign(3), self.scan(4)
            self.assertEqual((scanbot.claim_slot(waiting), scanbot.claim_slot(later)), (1, 2))
            scanbot.release(normal)
            self.assertTrue(later.running and later.turn.is_set())
            self.assertFalse(waiting.running or waiting.turn.is_set())
            self.assertEqual(scanbot.queue, [waiting])

    def test_when_a_campaign_ends_the_oldest_waiting_campaign_starts(self):
        first = self.campaign(1)
        scanbot.claim_slot(first)
        second, third = self.campaign(2), self.campaign(3)
        scanbot.claim_slot(second)
        scanbot.claim_slot(third)
        scanbot.release(first)
        self.assertTrue(second.running and second.turn.is_set())
        self.assertFalse(third.running)
        self.assertEqual(scanbot.queue, [third])

    def test_a_waiting_campaign_keeps_its_turn_over_later_normal_scans(self):
        # All slots busy with normal scans, no campaign running: the campaign queued first gets the next slot
        with mock.patch.object(scanbot, 'MAX_CONCURRENT_SCANS', 2):
            first = self.scan(1)
            scanbot.claim_slot(first)
            scanbot.claim_slot(self.scan(2))
            waiting, later = self.campaign(3), self.scan(4)
            scanbot.claim_slot(waiting)
            scanbot.claim_slot(later)
            scanbot.release(first)
            self.assertTrue(waiting.running)
            self.assertEqual(scanbot.queue, [later])

    def test_the_limit_can_be_raised(self):
        with mock.patch.object(scanbot, 'MAX_CONCURRENT_CAMPAIGNS', 2):
            self.assertEqual([scanbot.claim_slot(self.campaign(n)) for n in (1, 2, 3)], [0, 0, 1])

    def test_nothing_starts_while_the_bot_shuts_down(self):
        first = self.campaign(1)
        scanbot.claim_slot(first)
        normal = self.scan(2)
        with mock.patch.object(scanbot, 'MAX_CONCURRENT_SCANS', 1):
            scanbot.claim_slot(normal)
            with mock.patch.object(scanbot, 'shutting_down', True):
                scanbot.release(first)
        self.assertFalse(normal.running)


class ResumeTests(unittest.IsolatedAsyncioTestCase):
    """Scans saved before a restart get their slots back in the order they were started, campaigns within the limit."""

    async def test_resumed_campaigns_respect_the_limit_and_normal_scans_go_past_them(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = jobs.JobStore.open(tmp.name)
        for owner, parts in ((1, 3), (2, 3), (3, 1), (4, 3)):
            job = jobs.new_job(owner, f'<@{owner}>', 10, 100, 'java', False, {'kind': 'file', 'description': 'x'},
                               7, '')
            job.parts, job.part_size, job.status = parts, 3, 'running'
            job.created_at = f'2026-10-01T00:00:0{owner}Z'
            store.write_list(job, [f'1.0.0.{n}' for n in range(1, 8)])
            store.save(job)
        with mock.patch.dict(scanbot.scans, clear=True), mock.patch.object(scanbot, 'queue', []), \
                mock.patch.object(scanbot, 'shutting_down', False), \
                mock.patch.object(scanbot, 'MAX_CONCURRENT_SCANS', 5), \
                mock.patch.object(scanbot, 'MAX_CONCURRENT_CAMPAIGNS', 1):
            pending = scanbot.register_resumable(store)
            self.assertEqual([(scan.owner.id, place) for scan, _, place in pending], [(1, 0), (2, 1), (3, 0), (4, 2)])
            self.assertEqual([s.owner.id for s in scanbot.queue], [2, 4])


class CommandTests(unittest.IsolatedAsyncioTestCase):
    """Through the scan command, with the campaign tests' fake pings: 3 addresses per part."""

    async def asyncSetUp(self):
        await campaigns.CampaignTests.asyncSetUp(self)
        for p in (mock.patch.object(scanbot, 'MAX_CONCURRENT_SCANS', 5),
                  mock.patch.object(scanbot, 'MAX_CONCURRENT_CAMPAIGNS', 1)):
            p.start()
            self.addCleanup(p.stop)

    def start(self, ctx, entries, **options):
        return asyncio.create_task(self.scan_command(ctx, campaigns.attachment(entries), **options))

    async def test_a_second_campaign_waits_while_a_normal_scan_goes_ahead(self):
        self.slow = {'1.0.0.2'}  # The first campaign waits here, in its first part
        first = campaigns.make_ctx(1)
        first_task = self.start(first, campaigns.IPS, confirm='yes')
        await campaigns.until(lambda: self.waiting == 1)

        second = campaigns.make_ctx(2)
        second_list = [f'2.0.0.{n}' for n in range(1, 8)]
        second_task = self.start(second, second_list, confirm='yes')
        await campaigns.until(lambda: any('Queued' in t for t in campaigns.texts(second.send)))
        [queued] = [t for t in campaigns.texts(second.send) if 'Queued' in t]
        self.assertIn("🕒 **Queued** (#1). A campaign is already running (only one runs at a time), so your "
                      "campaign of 7 IPs starts automatically when one ends; normal scans keep their slots meanwhile.",
                      queued)
        self.assertNotIn('2.0.0.1', self.checked)

        # A normal scan doesn't wait behind the queued campaign
        self.online.add('3.0.0.1')
        normal = campaigns.make_ctx(3)
        await asyncio.wait_for(self.scan_command(normal, campaigns.attachment(['3.0.0.1', '3.0.0.2'])), 5)
        self.assertTrue(any('Scan Complete' in t for t in campaigns.posted(normal)))
        self.assertFalse(any('Queued' in t for t in campaigns.posted(normal)))
        self.assertNotIn('2.0.0.1', self.checked)  # The second campaign is still waiting

        # When the first campaign ends, the second one runs
        self.gate.set()
        await asyncio.wait_for(asyncio.gather(first_task, second_task), 10)
        self.assertTrue(any('Scan Complete' in t for t in campaigns.posted(first)))
        self.assertTrue(any('Scan Complete' in t for t in campaigns.posted(second)))
        self.assertEqual([e for e in self.checked if e.startswith('2.')], second_list)
        self.assertEqual(scanbot.scans, {})

    async def test_stopping_the_queued_campaign_takes_it_out_of_the_queue(self):
        self.slow = {'1.0.0.2'}
        first_task = self.start(campaigns.make_ctx(1), campaigns.IPS, confirm='yes')
        await campaigns.until(lambda: self.waiting == 1)
        second = campaigns.make_ctx(2)
        second_task = self.start(second, [f'2.0.0.{n}' for n in range(1, 8)], confirm='yes')
        await campaigns.until(lambda: len(scanbot.queue) == 1)

        await self.stop_command(second)
        await asyncio.wait_for(second_task, 5)
        self.assertEqual(scanbot.queue, [])
        self.gate.set()
        await asyncio.wait_for(first_task, 10)
        self.assertNotIn('2.0.0.1', self.checked)

    async def test_the_preview_says_when_the_campaign_would_wait(self):
        running = scanbot.Scan(types.SimpleNamespace(id=9, mention='<@9>'), 10)
        running.job, running.running = types.SimpleNamespace(parts=4), True
        scanbot.scans[9] = running
        ctx = campaigns.make_ctx(1)
        await asyncio.wait_for(self.scan_command(ctx, campaigns.attachment(campaigns.IPS)), 5)
        [reply] = campaigns.texts(ctx.send)
        self.assertIn("🕒 A campaign is already running (only one runs at a time): this one would wait in the queue "
                      "until one ends.", reply)
        self.assertLess(reply.index('🕒'), reply.index('▶️ To start it'))

    async def test_the_preview_says_nothing_about_waiting_when_a_campaign_slot_is_free(self):
        ctx = campaigns.make_ctx(1)
        await asyncio.wait_for(self.scan_command(ctx, campaigns.attachment(campaigns.IPS)), 5)
        [reply] = campaigns.texts(ctx.send)
        self.assertNotIn('already running', reply)

    async def test_with_a_higher_limit_the_text_names_it(self):
        with mock.patch.object(scanbot, 'MAX_CONCURRENT_CAMPAIGNS', 2):
            for owner in (8, 9):
                running = scanbot.Scan(types.SimpleNamespace(id=owner, mention=f'<@{owner}>'), 10)
                running.job, running.running = types.SimpleNamespace(parts=4), True
                scanbot.scans[owner] = running
            self.assertEqual(scanbot.campaigns_running_text(), "2 campaigns are already running (at most 2 at a time)")
            self.assertIn("At most 2 campaigns run at a time", scanbot.campaign_slots_text())


class HelpAndSettingsTests(unittest.IsolatedAsyncioTestCase):
    async def test_help_names_the_campaign_limit(self):
        ctx = mock.MagicMock(send=mock.AsyncMock())
        await scanbot.bot.get_command('help').callback(ctx)
        embed = ctx.send.await_args.kwargs['embed']
        [field] = [f for f in embed.fields if f.name.startswith('Campaigns')]
        self.assertIn("Only one campaign runs at a time; others wait in the queue while normal scans go on.",
                      field.value)
        self.assertLessEqual(len(field.value), 1024)
        self.assertLessEqual(len(embed), 6000)

    def test_the_default_is_one_campaign(self):
        self.assertEqual(scanbot.MAX_CONCURRENT_CAMPAIGNS, 1)

    def test_campaigns_that_can_take_every_slot_are_warned_about(self):
        with mock.patch.object(scanbot, 'MAX_CONCURRENT_SCANS', 3), \
                mock.patch.object(scanbot, 'MAX_CONCURRENT_CAMPAIGNS', 3), \
                mock.patch.object(pinger, 'raise_file_limit', return_value=None), \
                self.assertLogs('scanbot', 'WARNING') as logs:
            scanbot.check_settings()
        self.assertIn('MAX_CONCURRENT_CAMPAIGNS is 3: campaigns can take all 3 scan slots', logs.output[0])

    def test_one_slot_and_one_campaign_is_not_warned_about(self):
        with mock.patch.object(scanbot, 'MAX_CONCURRENT_SCANS', 1), \
                mock.patch.object(scanbot, 'MAX_CONCURRENT_CAMPAIGNS', 1), \
                mock.patch.object(pinger, 'raise_file_limit', return_value=None), \
                self.assertNoLogs('scanbot', 'WARNING'):
            scanbot.check_settings()


if __name__ == '__main__':
    unittest.main()
