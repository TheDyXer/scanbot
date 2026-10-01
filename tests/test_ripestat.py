"""
Tests for looking up an AS number's or a country's IPv4 networks through RIPEstat, against a local server that
answers like it (the shapes checked against stat.ripe.net in October 2026).

Run from the repository root:  python -m unittest discover -s tests
"""
import ipaddress
import os
import socket
import sys
import unittest
from unittest import mock

# bot.py exits at import time without a token
os.environ.setdefault('DISCORD_TOKEN', 'test-token')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from aiohttp import web  # noqa: E402
from aiohttp.test_utils import TestServer  # noqa: E402

import bot as scanbot  # noqa: E402


def closed_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def announced(*prefixes):
    return {'status': 'ok', 'data': {'prefixes': [{'prefix': p, 'timelines': []} for p in prefixes],
                                     'resource': '8400'}}


def country(*prefixes):
    return {'status': 'ok', 'data': {'resources': {'asn': [], 'ipv4': list(prefixes), 'ipv6': ['2a00::/16']}}}


class RipestatTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.requests = []  # (call, query, User-Agent)
        self.reply = (200, announced('1.2.0.0/24'))
        app = web.Application()
        app.router.add_get('/data/{call}/data.json', self.answer)
        self.server = TestServer(app, host='127.0.0.1')
        await self.server.start_server()
        patcher = mock.patch.object(scanbot, 'RIPESTAT_URL',
                                    f"http://127.0.0.1:{self.server.port}/data/{{call}}/data.json")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.session = scanbot.api_session()

    async def asyncTearDown(self):
        await self.session.close()
        await self.server.close()

    async def answer(self, request):
        self.requests.append((request.match_info['call'], dict(request.query), request.headers.get('User-Agent')))
        status, body = self.reply
        if isinstance(body, str):
            return web.Response(text=body, status=status)
        return web.json_response(body, status=status)

    async def test_an_asn_gives_its_ipv4_networks_with_overlaps_merged(self):
        self.reply = (200, announced('1.2.0.0/24', '1.2.1.0/24', '1.2.0.0/23', '5.6.7.0/24', '2001:db8::/32'))
        networks = await scanbot.prefixes_for_asn(self.session, 'AS8400')
        self.assertEqual(networks, [ipaddress.IPv4Network('1.2.0.0/23'), ipaddress.IPv4Network('5.6.7.0/24')])

    async def test_the_asn_is_asked_for_by_number_with_sourceapp_and_user_agent(self):
        for value in ('AS8400', 'as8400', '8400', ' AS8400 '):
            await scanbot.prefixes_for_asn(self.session, value)
        self.assertEqual({(call, query['resource'], query['sourceapp'], agent)
                          for call, query, agent in self.requests},
                         {('announced-prefixes', 'AS8400', 'scanbot', scanbot.USER_AGENT)})

    async def test_a_bad_asn_is_refused_before_asking(self):
        for value in ('foo', 'AS', 'AS-8400', 'AS99999999999', '0', 'AS4294967296'):
            with self.subTest(value=value), self.assertRaises(scanbot.TargetError) as caught:
                await scanbot.prefixes_for_asn(self.session, value)
            self.assertIn("isn't an AS number", str(caught.exception))
        self.assertEqual(self.requests, [])

    async def test_a_country_gives_its_registered_ipv4_networks(self):
        self.reply = (200, country('5.22.160.0/19', '5.57.72.0/21'))
        networks = await scanbot.prefixes_for_country(self.session, 'rs')
        self.assertEqual(networks, [ipaddress.IPv4Network('5.22.160.0/19'), ipaddress.IPv4Network('5.57.72.0/21')])
        call, query, _ = self.requests[0]
        self.assertEqual((call, query['resource'], query['v4_format'], query['sourceapp']),
                         ('country-resource-list', 'RS', 'prefix', 'scanbot'))

    async def test_a_bad_country_code_is_refused_before_asking(self):
        for value in ('XYZ', 'R1', '', 'Serbia'):
            with self.subTest(value=value), self.assertRaises(scanbot.TargetError):
                await scanbot.prefixes_for_country(self.session, value)
        self.assertEqual(self.requests, [])

    async def test_nothing_found_says_so(self):
        self.reply = (200, announced('2001:db8::/32'))
        with self.assertRaises(scanbot.TargetError) as caught:
            await scanbot.prefixes_for_asn(self.session, 'AS8400')
        self.assertEqual(str(caught.exception), "AS8400 announces no IPv4 prefixes.")
        self.reply = (200, country())
        with self.assertRaises(scanbot.TargetError) as caught:
            await scanbot.prefixes_for_country(self.session, 'xx')
        self.assertEqual(str(caught.exception), "No IPv4 space is registered to XX.")

    async def test_errors_become_a_clear_message(self):
        cases = [((500, 'oops'), "RIPEstat answered HTTP 500 for AS8400"),
                 ((200, {'status': 'error', 'messages': [['error', 'x']], 'data': {}}), "RIPEstat couldn't look up AS8400"),
                 ((200, 'not json'), "RIPEstat didn't answer for AS8400")]
        for reply, message in cases:
            self.reply = reply
            with self.subTest(reply=reply), self.assertRaises(scanbot.LookupFailed) as caught:
                await scanbot.prefixes_for_asn(self.session, 'AS8400')
            self.assertIn(message, str(caught.exception))

    async def test_an_unreachable_ripestat_is_a_clear_message_too(self):
        with mock.patch.object(scanbot, 'RIPESTAT_URL', f"http://127.0.0.1:{closed_port()}/data/{{call}}/data.json"):
            with self.assertRaises(scanbot.LookupFailed) as caught:
                await scanbot.prefixes_for_asn(self.session, 'AS8400')
        self.assertIn("RIPEstat didn't answer for AS8400", str(caught.exception))


class CountTests(unittest.TestCase):
    def test_the_count_matches_what_expanding_gives(self):
        networks = [ipaddress.IPv4Network(n) for n in ('1.2.3.0/30', '1.2.3.4/31', '1.2.3.8/32', '5.6.7.0/24')]
        self.assertEqual(scanbot.address_count(networks), 2 + 2 + 1 + 254)
        self.assertEqual(len(list(scanbot.expand_prefixes(networks))), scanbot.address_count(networks))

    def test_expanding_is_lazy_and_takes_a_port(self):
        addresses = scanbot.expand_prefixes([ipaddress.IPv4Network('11.0.0.0/8')], port=25570)
        self.assertEqual([next(addresses), next(addresses)], ['11.0.0.1:25570', '11.0.0.2:25570'])

    def test_private_addresses_in_announced_networks_are_left_out(self):
        networks = [ipaddress.IPv4Network('10.0.0.0/30'), ipaddress.IPv4Network('1.2.3.0/30')]
        self.assertEqual(list(scanbot.expand_prefixes(networks)), ['1.2.3.1', '1.2.3.2'])


if __name__ == '__main__':
    unittest.main()
