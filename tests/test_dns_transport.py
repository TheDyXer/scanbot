"""
Tests for choosing how the bot talks to Quad9: DNS-over-TLS (port 853), or DNS-over-HTTPS (port 443)
when a network blocks 853.

Run from the repository root:  python -m unittest discover -s tests
"""
import contextlib
import io
import logging
import os
import sys
import unittest
from unittest import mock

import dns.exception
import dns.nameserver
import dns.query

# bot.py exits at import time without a token
os.environ.setdefault('DISCORD_TOKEN', 'test-token')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import bot  # noqa: E402


class TransportChoiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        saved = bot.QUAD9.nameservers
        self.addCleanup(setattr, bot.QUAD9, 'nameservers', saved)

    def probing(self, *, dot, doh):
        """Patches the probe: each transport works if its flag is true. Returns the mock, to read its calls."""
        async def fake(nameservers):
            works = dot if isinstance(nameservers[0], dns.nameserver.DoTNameserver) else doh
            if not works:
                raise dns.exception.Timeout()
        probe = mock.AsyncMock(side_effect=fake)
        patcher = mock.patch.object(bot, 'probe_nameservers', probe)
        patcher.start()
        self.addCleanup(patcher.stop)
        return probe

    def assert_all(self, kind):
        self.assertTrue(bot.QUAD9.nameservers)
        for nameserver in bot.QUAD9.nameservers:
            self.assertIsInstance(nameserver, kind)

    async def test_auto_keeps_tls_when_port_853_works(self):
        self.probing(dot=True, doh=True)
        self.assertEqual(await bot.choose_dns_transport('auto'), 'dot')
        self.assert_all(dns.nameserver.DoTNameserver)

    async def test_auto_falls_back_to_https_when_port_853_is_blocked(self):
        self.probing(dot=False, doh=True)
        with self.assertLogs('scanbot', 'WARNING') as logs:
            self.assertEqual(await bot.choose_dns_transport('auto'), 'doh')
        self.assert_all(dns.nameserver.DoHNameserver)
        self.assertIn('853', logs.output[0])

    async def test_auto_fails_with_both_reasons_when_nothing_works(self):
        self.probing(dot=False, doh=False)
        before = list(bot.QUAD9.nameservers)
        with self.assertRaises(bot.DnsUnavailable) as caught:
            await bot.choose_dns_transport('auto')
        message = str(caught.exception)
        self.assertIn('853', message)
        self.assertIn('443', message)
        self.assertEqual(bot.QUAD9.nameservers, before)

    async def test_forced_tls_never_falls_back(self):
        probe = self.probing(dot=False, doh=True)
        with self.assertRaises(bot.DnsUnavailable):
            await bot.choose_dns_transport('dot')
        self.assertEqual(probe.await_count, 1)

    async def test_forced_https_skips_the_tls_probe(self):
        probe = self.probing(dot=False, doh=True)
        self.assertEqual(await bot.choose_dns_transport('doh'), 'doh')
        self.assertEqual(probe.await_count, 1)
        self.assert_all(dns.nameserver.DoHNameserver)

    async def test_missing_https_packages_are_named_instead_of_blamed_on_the_network(self):
        # Without httpx and h2, dnspython quietly tries HTTP/3 and every lookup says "not available"
        self.probing(dot=False, doh=True)
        with mock.patch.object(dns.query, 'have_doh', False), self.assertRaises(bot.DnsUnavailable) as caught:
            await bot.choose_dns_transport('auto')
        self.assertIn('h2', str(caught.exception))

    async def test_the_probe_reports_a_dead_server_as_a_dns_error(self):
        # The fallback only works if a blocked port surfaces as a DNSException, not some other error
        dead = [dns.nameserver.DoTNameserver('127.0.0.1', port=1, hostname='dns.quad9.net')]
        with mock.patch.object(bot, 'DNS_PROBE_LIFETIME', 0.5), self.assertRaises(dns.exception.DNSException):
            await bot.probe_nameservers(dead)

    async def test_the_probe_leaves_the_shared_resolver_alone(self):
        before = list(bot.QUAD9.nameservers)
        dead = [dns.nameserver.DoTNameserver('127.0.0.1', port=1, hostname='dns.quad9.net')]
        with mock.patch.object(bot, 'DNS_PROBE_LIFETIME', 0.5), contextlib.suppress(dns.exception.DNSException):
            await bot.probe_nameservers(dead)
        self.assertEqual(bot.QUAD9.nameservers, before)


class SettingTests(unittest.TestCase):
    def test_unset_means_auto(self):
        self.assertEqual(bot.parse_dns_transport(''), 'auto')

    def test_case_and_spaces_are_ignored(self):
        self.assertEqual(bot.parse_dns_transport(' DoH '), 'doh')

    def test_a_typo_is_rejected_with_the_valid_values(self):
        with self.assertRaises(ValueError) as caught:
            bot.parse_dns_transport('dto')
        for valid in ('auto', 'dot', 'doh'):
            self.assertIn(valid, str(caught.exception))

    def test_the_packages_dnspython_needs_for_https_are_installed(self):
        # dnspython enables DNS-over-HTTPS only when httpcore, httpx and h2 are all there (requirements.txt)
        self.assertTrue(dns.query.have_doh)

    def test_https_lookups_do_not_flood_the_log(self):
        # httpx logs every request at INFO, and in HTTPS mode each DNS lookup is one
        for name in ('httpx', 'httpcore'):
            self.assertGreaterEqual(logging.getLogger(name).level, logging.WARNING)

    def test_https_nameservers_never_use_the_system_resolver_to_find_quad9(self):
        for nameserver in bot.quad9_nameservers('doh'):
            self.assertIsInstance(nameserver, dns.nameserver.DoHNameserver)
            self.assertIn(nameserver.bootstrap_address, ('9.9.9.9', '149.112.112.112'))

    def test_tls_nameservers_are_the_two_quad9_addresses(self):
        addresses = [nameserver.answer_nameserver() for nameserver in bot.quad9_nameservers('dot')]
        self.assertEqual(addresses, ['9.9.9.9', '149.112.112.112'])


class StartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_dead_dns_stops_the_bot_with_a_readable_error_before_anything_else(self):
        failing = mock.AsyncMock(side_effect=bot.DnsUnavailable('DNS lookups failed: nothing answers'))
        geo = mock.AsyncMock()
        out = io.StringIO()
        with mock.patch.object(bot, 'choose_dns_transport', failing), \
                mock.patch.object(bot, 'load_geo_db', geo), \
                mock.patch.object(bot.discord.utils, 'setup_logging'), \
                contextlib.redirect_stdout(out):
            with self.assertRaises(SystemExit) as caught:
                await bot.main()
        self.assertNotEqual(caught.exception.code, 0)
        self.assertIn('Error: DNS lookups failed', out.getvalue())
        geo.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
