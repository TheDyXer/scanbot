"""
Tests for blocking private and local addresses.

Run from the repository root:  python -m unittest discover -s tests
"""
import asyncio
import ipaddress
import os
import sys
import types
import unittest
from unittest import mock

# bot.py exits at import time without a token
os.environ.setdefault('DISCORD_TOKEN', 'test-token')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import bot  # noqa: E402
import dns.exception  # noqa: E402
import dns.resolver  # noqa: E402
from mcstatus import JavaServer  # noqa: E402

PRIVATE = [
    '127.0.0.1', '127.0.0.1:25566', '10.0.0.5', '172.16.0.1', '172.31.255.255', '192.168.11.220:25565',
    '169.254.1.1', '100.64.0.1', '0.0.0.0', '198.18.0.1', '224.0.0.1', '239.255.255.250',
    '255.255.255.255', '240.0.0.1',
    'localhost', 'LOCALHOST', 'localhost.', 'localhost:25565', 'router', 'nas.lan', 'printer.local',
    'box.internal', 'foo.localhost', 'x.home.arpa', 'host.localdomain',
]
PUBLIC = ['8.8.8.8', '1.1.1.1:25565', 'play.example.com', 'mc.example.com:25566', '172.32.0.1', '11.0.0.1']


def fake_answer(*addresses):
    return [types.SimpleNamespace(address=a) for a in addresses]


class ParseIpsTests(unittest.TestCase):
    def test_private_entries_are_blocked(self):
        for entry in PRIVATE:
            with self.subTest(entry=entry):
                unique, invalid, duplicates, blocked = bot.parse_ips(entry)
                self.assertEqual((unique, invalid, duplicates, blocked), ([], 0, 0, 1))

    def test_public_entries_are_kept(self):
        for entry in PUBLIC:
            with self.subTest(entry=entry):
                unique, invalid, duplicates, blocked = bot.parse_ips(entry)
                self.assertEqual((unique, invalid, duplicates, blocked), ([entry], 0, 0, 0))

    def test_counts_are_reported_separately(self):
        text = "\n".join([
            "# a comment", "", "8.8.8.8", "8.8.8.8", "not valid!", "192.168.1.1", "localhost", "1.1.1.1",
        ])
        unique, invalid, duplicates, blocked = bot.parse_ips(text)
        self.assertEqual(unique, ['8.8.8.8', '1.1.1.1'])
        self.assertEqual((invalid, duplicates, blocked), (1, 1, 2))

    def test_duplicate_private_entries_count_as_blocked_not_duplicates(self):
        unique, invalid, duplicates, blocked = bot.parse_ips("10.0.0.1\n10.0.0.1")
        self.assertEqual((unique, invalid, duplicates, blocked), ([], 0, 0, 2))


class ResolvePublicAddressTests(unittest.IsolatedAsyncioTestCase):
    async def test_public_name_returns_its_address(self):
        with mock.patch.object(bot.QUAD9, 'resolve', mock.AsyncMock(return_value=fake_answer('93.184.216.34'))):
            self.assertEqual(await bot.resolve_public_address('example.com'), ipaddress.ip_address('93.184.216.34'))

    async def test_name_pointing_at_a_private_address_is_blocked(self):
        with mock.patch.object(bot.QUAD9, 'resolve', mock.AsyncMock(return_value=fake_answer('192.168.11.220'))):
            with self.assertRaises(bot.BlockedAddress):
                await bot.resolve_public_address('evil.example.com')

    async def test_any_private_record_blocks_the_whole_name(self):
        answer = fake_answer('93.184.216.34', '10.0.0.1')
        with mock.patch.object(bot.QUAD9, 'resolve', mock.AsyncMock(return_value=answer)):
            with self.assertRaises(bot.BlockedAddress):
                await bot.resolve_public_address('mixed.example.com')

    async def test_unresolvable_name_returns_none(self):
        with mock.patch.object(bot.QUAD9, 'resolve', mock.AsyncMock(side_effect=dns.resolver.NXDOMAIN())):
            self.assertIsNone(await bot.resolve_public_address('nope.example.com'))

    async def test_dns_failure_returns_none_and_never_a_guess(self):
        with mock.patch.object(bot.QUAD9, 'resolve', mock.AsyncMock(side_effect=dns.exception.Timeout())):
            self.assertIsNone(await bot.resolve_public_address('slow.example.com'))

    async def test_empty_answer_returns_none(self):
        with mock.patch.object(bot.QUAD9, 'resolve', mock.AsyncMock(return_value=[])):
            self.assertIsNone(await bot.resolve_public_address('aaaa-only.example.com'))

    async def test_ip_literals_skip_dns(self):
        resolve = mock.AsyncMock()
        with mock.patch.object(bot.QUAD9, 'resolve', resolve):
            self.assertEqual(await bot.resolve_public_address('8.8.8.8'), ipaddress.ip_address('8.8.8.8'))
            with self.assertRaises(bot.BlockedAddress):
                await bot.resolve_public_address('127.0.0.1')
        resolve.assert_not_called()

    async def test_internal_names_are_blocked_without_dns(self):
        resolve = mock.AsyncMock()
        with mock.patch.object(bot.QUAD9, 'resolve', resolve):
            for name in ('localhost', 'nas.lan', 'router'):
                with self.assertRaises(bot.BlockedAddress):
                    await bot.resolve_public_address(name)
        resolve.assert_not_called()


class CheckDirectTests(unittest.IsolatedAsyncioTestCase):
    def status(self):
        return types.SimpleNamespace(
            players=types.SimpleNamespace(online=2, max=20, sample=[types.SimpleNamespace(name='Steve')]),
            version=types.SimpleNamespace(name='1.21'),
            motd=types.SimpleNamespace(to_plain=lambda: 'hello'),
        )

    async def test_srv_target_on_a_private_address_is_never_contacted(self):
        # The entry looks public, but its SRV record points at a LAN host
        srv_result = JavaServer('nas.example.net', 25565)
        async_status = mock.AsyncMock()
        with mock.patch.object(bot.JavaServer, 'async_lookup', mock.AsyncMock(return_value=srv_result)), \
                mock.patch.object(bot.QUAD9, 'resolve', mock.AsyncMock(return_value=fake_answer('192.168.11.5'))), \
                mock.patch.object(bot.JavaServer, 'async_status', async_status):
            with self.assertRaises(bot.BlockedAddress):
                await bot.check_direct('public-looking.example.com')
        async_status.assert_not_called()

    async def test_srv_target_that_is_an_ip_literal_is_checked(self):
        srv_result = JavaServer('10.1.2.3', 22)
        async_status = mock.AsyncMock()
        with mock.patch.object(bot.JavaServer, 'async_lookup', mock.AsyncMock(return_value=srv_result)), \
                mock.patch.object(bot.JavaServer, 'async_status', async_status):
            with self.assertRaises(bot.BlockedAddress):
                await bot.check_direct('public-looking.example.com')
        async_status.assert_not_called()

    async def test_public_server_is_pinged_at_the_checked_address(self):
        looked_up = JavaServer('mc.example.com', 25570)
        connected_to = []

        async def fake_status(server, **kwargs):
            connected_to.append((server.address.host, server.address.port))
            return self.status()

        with mock.patch.object(bot.JavaServer, 'async_lookup', mock.AsyncMock(return_value=looked_up)), \
                mock.patch.object(bot.QUAD9, 'resolve', mock.AsyncMock(return_value=fake_answer('93.184.216.34'))), \
                mock.patch.object(bot.JavaServer, 'async_status', fake_status):
            result = await bot.check_direct('mc.example.com')

        # Connects to the address that was checked, not to the name again
        self.assertEqual(connected_to, [('93.184.216.34', 25570)])
        self.assertEqual(result['ip'], 'mc.example.com')
        self.assertEqual(result['address'], '93.184.216.34')
        self.assertEqual((result['players'], result['max'], result['names']), (2, 20, ['Steve']))

    async def test_unexpected_resolver_error_does_not_kill_the_scan(self):
        looked_up = JavaServer('mc.example.com', 25565)
        with mock.patch.object(bot.JavaServer, 'async_lookup', mock.AsyncMock(return_value=looked_up)), \
                mock.patch.object(bot.QUAD9, 'resolve', mock.AsyncMock(side_effect=RuntimeError('boom'))):
            self.assertIsNone(await bot.check_direct('mc.example.com'))

    async def test_unresolvable_server_is_reported_as_not_answering(self):
        looked_up = JavaServer('gone.example.com', 25565)
        with mock.patch.object(bot.JavaServer, 'async_lookup', mock.AsyncMock(return_value=looked_up)), \
                mock.patch.object(bot.QUAD9, 'resolve', mock.AsyncMock(side_effect=dns.resolver.NXDOMAIN())):
            self.assertIsNone(await bot.check_direct('gone.example.com'))


class RunDirectTests(unittest.IsolatedAsyncioTestCase):
    async def test_blocked_entries_are_counted_and_not_retried(self):
        async def fake_check(ip):
            if ip == 'evil.example.com':
                raise bot.BlockedAddress(ip)
            if ip == 'up.example.com':
                return bot.make_result(ip, '93.184.216.34', 1, 20, [], '1.21', 'hi')
            return None

        ips = ['up.example.com', 'evil.example.com', 'down.example.com']
        results, state = {}, {'phase': '', 'done': 0, 'total': 0, 'found': 0, 'blocked': 0}
        with mock.patch.object(bot, 'check_direct', fake_check):
            retry = await bot.run_direct(ips, results, state)

        self.assertEqual(list(results), ['up.example.com'])
        self.assertEqual(retry, ['down.example.com'])  # blocked ones never reach the mcstatus.io fallback
        self.assertEqual((state['done'], state['found'], state['blocked']), (3, 1, 1))


if __name__ == '__main__':
    unittest.main()
