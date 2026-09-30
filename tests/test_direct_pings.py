"""
Tests for direct pings: IP addresses skip the SRV lookup, a scan pings with a fixed number of workers instead of
one task per server, and all scans together stay under the bot-wide limit.

Run from the repository root:  python -m unittest discover -s tests
"""
import asyncio
import os
import sys
import unittest
from unittest import mock

# bot.py exits at import time without a token
os.environ.setdefault('DISCORD_TOKEN', 'test-token')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from mcstatus import JavaServer  # noqa: E402

import bot as scanbot  # noqa: E402

STATUS = {"players": 2, "max": 20, "names": [], "version": "1.21", "motd": "hi"}


def public_ips(count, start=0):
    return [f"8.{(i >> 16) & 255}.{(i >> 8) & 255}.{i & 255}" for i in range(start, start + count)]


def new_state():
    return {'phase': '', 'done': 0, 'total': 0, 'found': 0, 'blocked': 0}


class IpAddressTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pings = []

        async def fake_ping(address, port, edition):
            self.pings.append((address, port, edition))
            return dict(STATUS)

        async def no_lookup(*args, **kwargs):
            raise AssertionError("an IP address must not be looked up")

        for p in (mock.patch.object(scanbot, 'ping_server', fake_ping),
                  mock.patch.object(JavaServer, 'async_lookup', no_lookup)):
            p.start()
            self.addCleanup(p.stop)

    async def test_an_ip_is_pinged_on_the_default_port_without_a_lookup(self):
        result = await scanbot.check_direct('8.8.8.8')
        self.assertEqual(self.pings, [('8.8.8.8', 25565, 'java')])
        self.assertEqual((result['ip'], result['address'], result['players']), ('8.8.8.8', '8.8.8.8', 2))

    async def test_an_ip_with_a_port_is_pinged_on_that_port(self):
        await scanbot.check_direct('8.8.8.8:25570')
        self.assertEqual(self.pings, [('8.8.8.8', 25570, 'java')])

    async def test_an_impossible_port_is_offline_without_a_ping(self):
        for entry in ('8.8.8.8:0', '8.8.8.8:65536', '8.8.8.8:99999'):
            with self.subTest(entry=entry):
                self.assertIsNone(await scanbot.check_direct(entry))
        self.assertEqual(self.pings, [])

    async def test_a_private_ip_is_still_blocked(self):
        with self.assertRaises(scanbot.BlockedAddress):
            await scanbot.check_direct('192.168.1.10')
        self.assertEqual(self.pings, [])

    async def test_a_hostname_still_follows_its_srv_record(self):
        async def lookup(address, timeout=None):
            self.assertEqual(address, 'play.example.net')
            return JavaServer('8.8.4.4', 25566)  # Where the SRV record points

        with mock.patch.object(JavaServer, 'async_lookup', lookup):
            result = await scanbot.check_direct('play.example.net')
        self.assertEqual(self.pings, [('8.8.4.4', 25566, 'java')])
        self.assertEqual((result['ip'], result['address']), ('play.example.net', '8.8.4.4'))


class WorkerPoolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.in_flight = 0
        self.most = 0
        self.checked = []

        async def fake_check(ip):
            self.in_flight += 1
            self.most = max(self.most, self.in_flight)
            try:
                await asyncio.sleep(0.001)
            finally:
                self.in_flight -= 1
            self.checked.append(ip)
            return scanbot.make_result(ip, ip, 1, 10, [], '1.21', '') if ip.endswith('.7') else None

        self.fake_check = fake_check
        p = mock.patch.object(scanbot, 'check_direct', fake_check)
        p.start()
        self.addCleanup(p.stop)

    async def test_no_more_than_direct_concurrency_pings_at_once(self):
        ips, results = public_ips(100), {}
        with mock.patch.object(scanbot, 'DIRECT_CONCURRENCY', 7), \
                mock.patch.object(scanbot, 'DIRECT_CONCURRENCY_TOTAL', 1000):
            unanswered = await scanbot.run_direct(ips, results, new_state())
        self.assertEqual(self.most, 7)
        self.assertEqual(sorted(self.checked), sorted(ips))  # Each server exactly once
        self.assertEqual(list(results), [ip for ip in ips if ip.endswith('.7')])
        self.assertEqual(unanswered, [ip for ip in ips if not ip.endswith('.7')])  # In file order

    async def test_a_big_list_doesnt_become_one_task_per_server(self):
        gate = asyncio.Event()

        async def waiting_check(ip):
            await gate.wait()
            return None

        with mock.patch.object(scanbot, 'check_direct', waiting_check), \
                mock.patch.object(scanbot, 'DIRECT_CONCURRENCY', 7):
            task = asyncio.create_task(scanbot.run_direct(public_ips(5000), {}, new_state()))
            await asyncio.sleep(0.05)
            self.assertLessEqual(len(asyncio.all_tasks()), 7 + 5)
            gate.set()
            await asyncio.wait_for(task, 10)

    async def test_all_scans_together_stay_under_the_bot_wide_limit(self):
        with mock.patch.object(scanbot, 'DIRECT_CONCURRENCY', 10), \
                mock.patch.object(scanbot, 'DIRECT_CONCURRENCY_TOTAL', 12):
            await asyncio.gather(scanbot.run_direct(public_ips(100), {}, new_state()),
                                 scanbot.run_direct(public_ips(100, start=1000), {}, new_state()))
        self.assertEqual(self.most, 12)
        self.assertEqual(len(self.checked), 200)

    async def test_one_scan_alone_gets_its_full_concurrency(self):
        with mock.patch.object(scanbot, 'DIRECT_CONCURRENCY', 10), \
                mock.patch.object(scanbot, 'DIRECT_CONCURRENCY_TOTAL', 20):
            await scanbot.run_direct(public_ips(100), {}, new_state())
        self.assertEqual(self.most, 10)

    async def test_stop_ends_the_workers_quickly(self):
        stop, state = asyncio.Event(), new_state()

        async def slow_check(ip):
            await asyncio.sleep(0.05)
            return None

        with mock.patch.object(scanbot, 'check_direct', slow_check), \
                mock.patch.object(scanbot, 'DIRECT_CONCURRENCY', 50):
            task = asyncio.create_task(scanbot.run_direct(public_ips(20000), {}, state, stop=stop))
            await asyncio.sleep(0.2)
            stop.set()
            await asyncio.wait_for(task, 1)
        self.assertLess(state['done'], 20000)

    async def test_blocked_names_are_counted_and_not_returned(self):
        async def check(ip):
            if ip == 'router.example':
                raise scanbot.BlockedAddress(ip)
            return None

        state = new_state()
        with mock.patch.object(scanbot, 'check_direct', check):
            unanswered = await scanbot.run_direct(['8.8.8.8', 'router.example', '1.1.1.1'], {}, state)
        self.assertEqual(unanswered, ['8.8.8.8', '1.1.1.1'])
        self.assertEqual((state['done'], state['blocked']), (3, 1))

    async def test_an_empty_list_finishes_at_once(self):
        self.assertEqual(await asyncio.wait_for(scanbot.run_direct([], {}, new_state()), 1), [])


if __name__ == '__main__':
    unittest.main()
