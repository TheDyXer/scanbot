"""
Tests for the richer results: the network (AS) of each server, and the extra fields a ping or mcstatus.io reports.

Run from the repository root:  python -m unittest discover -s tests
"""
import os
import sys
import time
import types
import unittest
from unittest import mock

# bot.py exits at import time without a token
os.environ.setdefault('DISCORD_TOKEN', 'test-token')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from mcstatus import BedrockServer, JavaServer  # noqa: E402

import bot  # noqa: E402
import pinger  # noqa: E402


class FakeReader:
    """Stands in for an open maxminddb database."""

    def __init__(self, records):
        self.records = records

    def get(self, ip):
        if ip == 'not-an-ip':
            raise ValueError(ip)
        return self.records.get(ip)


class OfflineNetworkTests(unittest.TestCase):
    def test_networks_come_from_the_asn_database(self):
        reader = FakeReader({'93.87.0.1': {'autonomous_system_number': 8400,
                                           'autonomous_system_organization': 'TELEKOM SRBIJA a.d.'},
                             '5.6.7.8': {'autonomous_system_number': 64500}})
        with mock.patch.object(bot, 'asn_db', reader):
            found = bot.lookup_networks(['93.87.0.1', '5.6.7.8', '1.1.1.1', 'not-an-ip'])
        self.assertEqual(found, {'93.87.0.1': (8400, 'TELEKOM SRBIJA a.d.'), '5.6.7.8': (64500, '')})

    def test_no_database_knows_nothing(self):
        with mock.patch.object(bot, 'asn_db', None):
            self.assertEqual(bot.lookup_networks(['93.87.0.1']), {})

    def test_the_default_path_is_where_the_image_puts_the_database(self):
        # The Dockerfile saves it as /app/dbip-asn-lite.mmdb, next to /app/bot.py
        self.assertEqual(os.path.basename(bot.GEO_ASN_DB_PATH), 'dbip-asn-lite.mmdb')
        self.assertEqual(os.path.dirname(bot.GEO_ASN_DB_PATH), os.path.dirname(bot.GEO_DB_PATH))


class IpApiNetworkTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        for p in (mock.patch.object(bot, 'GEO_DELAY', 0), mock.patch.object(bot, 'geo_pacer', bot.Pacer())):
            p.start()
            self.addCleanup(p.stop)

    def session(self, answer):
        self.payloads = []
        test = self

        class Response:
            status = 200

            async def json(self):
                return answer

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        class Session:
            def post(self, url, json=None, timeout=None):
                test.payloads.append(json)
                return Response()

        return Session()

    def test_the_as_field_is_split_into_number_and_name(self):
        self.assertEqual(bot.parse_as('AS8400 Telekom Srbija a.d.'), (8400, 'Telekom Srbija a.d.'))
        self.assertEqual(bot.parse_as('AS64500', isp='Some ISP'), (64500, 'Some ISP'))
        for odd in ('', None, 'Telekom', 'ASX 1'):
            with self.subTest(value=odd):
                self.assertIsNone(bot.parse_as(odd))

    async def test_networks_are_asked_for_and_added(self):
        session = self.session([{'query': '93.87.0.1', 'countryCode': 'RS', 'as': 'AS8400 Telekom Srbija a.d.'},
                                {'query': '5.6.7.8', 'countryCode': 'DE', 'as': 'AS3320 Deutsche Telekom AG'}])
        networks = {'5.6.7.8': (64500, 'Known')}
        locations = await bot.batch_get_locations(session, ['93.87.0.1', '5.6.7.8'], networks=networks)
        self.assertEqual(locations, {'93.87.0.1': 'RS', '5.6.7.8': 'DE'})
        self.assertEqual(networks, {'93.87.0.1': (8400, 'Telekom Srbija a.d.'), '5.6.7.8': (64500, 'Known')})
        self.assertEqual(self.payloads[0][0]['fields'], 'query,countryCode,as,isp')

    async def test_without_networks_only_countries_are_asked_for(self):
        session = self.session([{'query': '1.2.3.4', 'countryCode': 'US'}])
        self.assertEqual(await bot.batch_get_locations(session, ['1.2.3.4']), {'1.2.3.4': 'US'})
        self.assertEqual(self.payloads[0][0]['fields'], 'query,countryCode')


class ScanLookupTests(unittest.IsolatedAsyncioTestCase):
    """Which addresses a scan sends to ip-api.com, and whose answer wins."""

    IPS = b'1.1.1.1\n2.2.2.2\n3.3.3.3\n'

    async def asyncSetUp(self):
        self.asked = []
        self.ctx = mock.MagicMock()
        self.ctx.author = types.SimpleNamespace(id=1, mention='<@1>')
        self.ctx.guild = types.SimpleNamespace(id=10)
        self.ctx.defer = mock.AsyncMock()
        self.ctx.send = mock.AsyncMock()
        self.ctx.channel.send = mock.AsyncMock()
        self.send_results = mock.AsyncMock()

        async def fake_run_direct(ips, results, state, edition='java', stop=None):
            results.update({ip: bot.make_result(ip, ip, 1, 10, [], '1.21', '') for ip in ips})
            return []

        async def fake_locations(session, ips, stop=None, networks=None):
            self.asked.append(list(ips))
            networks.update({ip: (64500, 'From ip-api') for ip in ips})
            return {ip: 'XX' for ip in ips}

        for p in (mock.patch.object(bot, 'run_direct', fake_run_direct),
                  mock.patch.object(bot, 'batch_get_locations', fake_locations),
                  mock.patch.object(bot, 'send_results', self.send_results),
                  mock.patch.object(bot, 'lookup_countries', lambda ips: {'1.1.1.1': 'AU', '2.2.2.2': 'DE'}),
                  mock.patch.object(bot, 'lookup_networks', lambda ips: {'1.1.1.1': (13335, 'Cloudflare')}),
                  mock.patch.object(bot, 'PINGER_URL', ''),
                  mock.patch.object(bot, 'set_status', mock.AsyncMock()),
                  mock.patch.object(bot.bot, 'direct_ok', True),
                  mock.patch.object(bot.bot, 'direct_ok_bedrock', True),
                  mock.patch.object(bot.bot, 'probed_at', time.monotonic()),
                  mock.patch.dict(bot.scans, clear=True),
                  mock.patch.object(bot, 'queue', [])):
            p.start()
            self.addCleanup(p.stop)

    async def scan(self):
        attachment = types.SimpleNamespace(filename='ips.txt', size=len(self.IPS),
                                           read=mock.AsyncMock(return_value=self.IPS))
        await bot.bot.get_command('scan').callback(self.ctx, attachment, edition='java', api='off')
        return self.send_results.await_args

    async def test_ip_api_gets_what_either_database_does_not_know_and_the_databases_win(self):
        with mock.patch.object(bot, 'asn_db', FakeReader({})):
            call = await self.scan()
        self.assertEqual(self.asked, [['2.2.2.2', '3.3.3.3']])
        locations, networks = call.args[2], call.kwargs['networks']
        self.assertEqual(locations, {'1.1.1.1': 'AU', '2.2.2.2': 'DE', '3.3.3.3': 'XX'})
        self.assertEqual(networks['1.1.1.1'], (13335, 'Cloudflare'))
        self.assertEqual(networks['2.2.2.2'], (64500, 'From ip-api'))

    async def test_without_a_network_database_only_unknown_countries_go_to_ip_api(self):
        # A failed download must not send every server to ip-api.com
        with mock.patch.object(bot, 'asn_db', None):
            await self.scan()
        self.assertEqual(self.asked, [['3.3.3.3']])


def java_status(**extra):
    status = types.SimpleNamespace(
        players=types.SimpleNamespace(online=3, max=20, sample=None),
        version=types.SimpleNamespace(name='1.21.4', protocol=769),
        motd=types.SimpleNamespace(to_plain=lambda: 'hi'),
        latency=45.678, enforces_secure_chat=True, is_modded=True,
        forge_data=types.SimpleNamespace(mods=[object()] * 12))
    status.__dict__.update(extra)
    return status


class PingerFieldTests(unittest.IsolatedAsyncioTestCase):
    async def ping(self, cls, status, edition):
        with mock.patch.object(cls, 'async_status', mock.AsyncMock(return_value=status)):
            return await pinger.ping('1.2.3.4', 25565, edition, 3)

    async def test_java_ping_reports_latency_protocol_secure_chat_and_mods(self):
        result = await self.ping(JavaServer, java_status(), 'java')
        self.assertEqual({k: result[k] for k in ('latency', 'protocol', 'secure_chat', 'modded', 'mod_count')},
                         {'latency': 45.7, 'protocol': 769, 'secure_chat': True, 'modded': True, 'mod_count': 12})

    async def test_a_server_without_forge_has_no_mod_count(self):
        result = await self.ping(JavaServer, java_status(is_modded=False, forge_data=None), 'java')
        self.assertEqual((result['modded'], result['mod_count']), (False, None))

    async def test_bedrock_ping_reports_gamemode_map_and_brand(self):
        status = types.SimpleNamespace(
            players=types.SimpleNamespace(online=64, max=100),
            version=types.SimpleNamespace(name='1.21.50', protocol=766, brand='MCPE'),
            motd=types.SimpleNamespace(to_plain=lambda: 'hey'), latency=12.04, gamemode='Survival', map_name='Bedrock level')
        result = await self.ping(BedrockServer, status, 'bedrock')
        self.assertEqual({k: result[k] for k in ('latency', 'protocol', 'gamemode', 'map', 'brand')},
                         {'latency': 12.0, 'protocol': 766, 'gamemode': 'Survival', 'map': 'Bedrock level',
                          'brand': 'MCPE'})

    async def test_an_older_mcstatus_without_the_new_fields_still_counts_as_online(self):
        status = types.SimpleNamespace(players=types.SimpleNamespace(online=3, max=20, sample=None),
                                       version=types.SimpleNamespace(name='1.8'),
                                       motd=types.SimpleNamespace(to_plain=lambda: 'old'))
        result = await self.ping(JavaServer, status, 'java')
        self.assertEqual((result['players'], result['latency'], result['modded'], result['mod_count']), (3, None, None, None))


class CleanStatusTests(unittest.TestCase):
    BASE = {"players": 1, "max": 2, "names": [], "version": "1.21", "motd": ""}

    def test_new_fields_keep_their_types(self):
        status = bot.clean_status({**self.BASE, "latency": 45.7, "protocol": 769, "secure_chat": False,
                                   "modded": True, "mod_count": 3, "gamemode": "Survival", "map": "w", "brand": "MCPE"})
        self.assertEqual((status['latency'], status['protocol'], status['secure_chat'], status['mod_count'],
                          status['brand']), (45.7, 769, False, 3, 'MCPE'))

    def test_odd_types_are_dropped(self):
        status = bot.clean_status({**self.BASE, "latency": True, "protocol": "769", "secure_chat": 1,
                                   "mod_count": 2.5, "gamemode": 7})
        self.assertEqual([status[k] for k in ('latency', 'protocol', 'secure_chat', 'mod_count', 'gamemode')],
                         [None] * 5)

    def test_an_older_pinger_without_them_is_fine(self):
        status = bot.clean_status(dict(self.BASE))
        self.assertTrue(all(status[k] is None for k in bot.STATUS_FIELDS))
        self.assertEqual(status['players'], 1)

    def test_unknown_keys_from_a_newer_pinger_are_dropped(self):
        self.assertNotIn('icon', bot.clean_status({**self.BASE, "icon": "data:image/png;base64,..."}))


class ApiFieldTests(unittest.TestCase):
    def test_java_reply_fills_software_plugins_mods_and_the_eula_flag(self):
        reply = {'online': True, 'ip_address': '144.172.67.4', 'eula_blocked': False,
                 'version': {'name_clean': '1.20.1', 'protocol': 47}, 'players': {'online': 71, 'max': 100},
                 'mods': [{'name': 'iron-chests', 'version': '13.2.11'}, {'name': 'ae2', 'version': '11.7.2'}],
                 'software': 'Paper', 'plugins': [{'name': 'WorldEdit', 'version': '7.2.14'}, {'version': 'x'}]}
        result = bot.parse_api_status('demo.mcstatus.io', reply)
        self.assertEqual({k: result[k] for k in ('protocol', 'software', 'plugins', 'modded', 'mod_count',
                                                 'eula_blocked', 'source')},
                         {'protocol': 47, 'software': 'Paper', 'plugins': ['WorldEdit'], 'modded': True,
                          'mod_count': 2, 'eula_blocked': False, 'source': 'mcstatus.io'})

    def test_a_vanilla_server_has_no_mods_and_no_software(self):
        reply = {'online': True, 'version': {'name_clean': '1.21'}, 'players': {}, 'mods': [], 'software': None,
                 'plugins': []}
        result = bot.parse_api_status('1.2.3.4', reply)
        self.assertEqual((result['modded'], result['mod_count'], result['software'], result['plugins']),
                         (False, None, None, []))


class ReportTests(unittest.TestCase):
    SERVER = bot.make_result('1.2.3.4', '93.87.0.1', 3, 20, [], '1.21', '', latency=44.6, protocol=767,
                             software='Paper', secure_chat=True, mod_count=12, modded=True, source='direct')

    def test_chat_shows_only_the_latency(self):
        chat = bot.format_entry(self.SERVER, {}, networks={'93.87.0.1': (8400, 'Telekom Srbija')})
        self.assertIn('Ver: 1.21 · 45 ms', chat)
        self.assertNotIn('AS8400', chat)

    def test_the_text_file_gets_a_details_line(self):
        text = bot.format_entry(self.SERVER, {}, markdown=False, networks={'93.87.0.1': (8400, 'Telekom Srbija')})
        self.assertIn('└ 🌐 AS8400 Telekom Srbija · protocol 767 · Paper · secure chat · 12 mods', text)

    def test_nothing_known_means_no_details_line(self):
        bare = bot.make_result('1.2.3.4', '1.2.3.4', 1, 2, [], '1.21', '')
        self.assertNotIn('🌐', bot.format_entry(bare, {}, markdown=False))


if __name__ == '__main__':
    unittest.main()
