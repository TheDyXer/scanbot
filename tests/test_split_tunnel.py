"""
Tests for the split-tunnel VPN: only pings go through the pinger in the VPN container,
and they never fall back to this machine's own connection.

Run from the repository root:  python -m unittest discover -s tests
"""
import asyncio
import os
import socket
import sys
import time
import types
import unittest
from unittest import mock

# bot.py exits at import time without a token
os.environ.setdefault('DISCORD_TOKEN', 'test-token')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from aiohttp import web  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402
from mcstatus import JavaServer  # noqa: E402

import bot as scanbot  # noqa: E402
import pinger  # noqa: E402

STATUS = {"players": 7, "max": 50, "names": ["Steve"], "version": "1.21", "motd": "hello"}


def closed_port():
    """A local port nothing listens on."""
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


class PingerServiceTests(unittest.IsolatedAsyncioTestCase):
    """The pinger's own HTTP service, as it runs inside the VPN container."""

    async def asyncSetUp(self):
        self.client = TestClient(TestServer(pinger.make_app()))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)

    async def post(self, body, status=STATUS):
        with mock.patch.object(pinger, 'ping', mock.AsyncMock(return_value=status)) as ping:
            response = await self.client.post('/ping', json=body)
            return response.status, await response.json(), ping

    async def test_health(self):
        response = await self.client.get('/health')
        self.assertEqual(response.status, 200)

    async def test_online_and_offline_answers(self):
        code, data, ping = await self.post({"edition": "java", "ip": "1.2.3.4", "port": 25565, "timeout": 3})
        self.assertEqual(code, 200)
        self.assertEqual(data, {"online": True, **STATUS})
        ping.assert_awaited_once_with('1.2.3.4', 25565, 'java', 3.0)

        code, data, _ = await self.post({"edition": "bedrock", "ip": "1.2.3.4", "port": 19132}, status=None)
        self.assertEqual((code, data), (200, {"online": False}))

    async def test_bad_requests_are_refused_without_pinging(self):
        bad = [
            {"edition": "java", "ip": "192.168.1.10", "port": 25565},   # private
            {"edition": "java", "ip": "127.0.0.1", "port": 25565},      # this machine
            {"edition": "java", "ip": "10.64.0.1", "port": 25565},      # the VPN's own network
            {"edition": "java", "ip": "play.example.com", "port": 25565},  # names are never taken
            {"edition": "java", "ip": "1.2.3.4", "port": 0},
            {"edition": "java", "ip": "1.2.3.4", "port": 70000},
            {"edition": "java", "ip": "1.2.3.4", "port": True},
            {"edition": "java", "ip": "1.2.3.4", "port": "25565"},
            {"edition": "pocket", "ip": "1.2.3.4", "port": 25565},
            {"edition": "java", "ip": "1.2.3.4", "port": 25565, "timeout": 60},
            ["not", "an", "object"],
        ]
        for body in bad:
            with self.subTest(body=body):
                code, data, ping = await self.post(body)
                self.assertEqual(code, 400)
                self.assertIn('error', data)
                ping.assert_not_awaited()

    async def test_invalid_json_is_refused(self):
        response = await self.client.post('/ping', data=b'{nope', headers={'Content-Type': 'application/json'})
        self.assertEqual(response.status, 400)


class RemotePingTests(unittest.IsolatedAsyncioTestCase):
    """The bot's side: with PINGER_URL set, every ping is an HTTP request to the pinger."""

    async def asyncSetUp(self):
        self.requests = []
        self.reply = {"online": True, **STATUS}

        async def fake_pinger(request):
            self.requests.append(await request.json())
            return web.json_response(self.reply)

        app = web.Application()
        app.add_routes([web.post('/ping', fake_pinger)])
        self.server = TestServer(app)
        await self.server.start_server()
        self.addAsyncCleanup(self.server.close)

        async def local_ping_must_not_happen(*args, **kwargs):
            raise AssertionError("pinged over this machine's own connection")

        for p in (mock.patch.object(scanbot, 'PINGER_URL', str(self.server.make_url('')).rstrip('/')),
                  mock.patch.object(scanbot, 'pinger_session', None),
                  mock.patch.object(scanbot.pinger, 'ping', local_ping_must_not_happen)):
            p.start()
            self.addCleanup(p.stop)
        self.addAsyncCleanup(self.close_session)

    async def close_session(self):
        if scanbot.pinger_session is not None:
            await scanbot.pinger_session.close()

    async def test_java_ping_goes_to_the_pinger(self):
        with mock.patch.object(JavaServer, 'async_lookup', mock.AsyncMock(return_value=JavaServer('1.2.3.4', 25566))):
            result = await scanbot.check_direct('play.example.com')
        self.assertEqual(self.requests, [{"edition": "java", "ip": "1.2.3.4", "port": 25566,
                                          "timeout": scanbot.DIRECT_TIMEOUT}])
        self.assertEqual((result['ip'], result['address'], result['players'], result['max'], result['names']),
                         ('play.example.com', '1.2.3.4', 7, 50, ['Steve']))

    async def test_bedrock_ping_goes_to_the_pinger(self):
        result = await scanbot.check_direct_bedrock('1.2.3.4:19133')
        self.assertEqual(self.requests[0]['edition'], 'bedrock')
        self.assertEqual(self.requests[0]['port'], 19133)
        self.assertEqual((result['edition'], result['players']), ('Bedrock', 7))

    async def test_extra_fields_travel_through_the_pinger_with_their_types(self):
        self.reply = {"online": True, **STATUS, "latency": 45.7, "protocol": 769, "secure_chat": True,
                      "modded": "yes", "icon": "data:image/png;base64,AAAA"}
        result = await scanbot.check_direct('1.2.3.4')
        self.assertEqual((result['latency'], result['protocol'], result['secure_chat'], result['source']),
                         (45.7, 769, True, 'direct'))
        self.assertIsNone(result['modded'])  # Not a bool, so dropped
        self.assertNotIn('icon', result)

    async def test_offline_answer(self):
        self.reply = {"online": False}
        self.assertIsNone(await scanbot.ping_server('1.2.3.4', 25565, 'java'))

    async def test_odd_answers_are_cleaned_up(self):
        self.reply = {"online": True, "players": "7", "max": None, "names": ["Steve", 5, None],
                      "version": 1.21, "motd": {"text": "hi"}}
        result = await scanbot.ping_server('1.2.3.4', 25565, 'java')
        self.assertEqual(result['names'], ['Steve'])
        self.assertIsNone(result['version'])
        self.assertEqual(result['motd'], '')
        made = scanbot.make_result('x', '1.2.3.4', result['players'], result['max'], result['names'],
                                   result['version'], result['motd'])
        self.assertEqual((made['players'], made['max'], made['version']), (7, 0, 'Unknown'))

    async def test_no_dns_or_private_address_reaches_the_pinger(self):
        with self.assertRaises(scanbot.BlockedAddress):
            await scanbot.check_direct_bedrock('192.168.1.5')
        self.assertEqual(self.requests, [])


class KillSwitchTests(unittest.IsolatedAsyncioTestCase):
    """If the pinger can't be reached, the answer is 'no answer': never a ping from here."""

    async def asyncSetUp(self):
        self.local = mock.AsyncMock(side_effect=AssertionError("pinged over this machine's own connection"))
        for p in (mock.patch.object(scanbot, 'pinger_session', None),
                  mock.patch.object(scanbot.pinger, 'ping', self.local)):
            p.start()
            self.addCleanup(p.stop)
        self.addAsyncCleanup(self.close_session)

    async def close_session(self):
        if scanbot.pinger_session is not None:
            await scanbot.pinger_session.close()

    async def test_unreachable_pinger_means_no_answer_and_no_local_ping(self):
        with mock.patch.object(scanbot, 'PINGER_URL', f'http://127.0.0.1:{closed_port()}'):
            for check in (scanbot.ping_server('1.2.3.4', 25565, 'java'), scanbot.check_direct_bedrock('1.2.3.4')):
                started = time.monotonic()
                self.assertIsNone(await check)
                # Refused, not waited out (Linux refuses at once; Windows retries for about 2 s)
                self.assertLess(time.monotonic() - started, scanbot.DIRECT_TIMEOUT + 2)
        self.local.assert_not_awaited()

    async def test_pinger_errors_mean_no_answer(self):
        async def broken(request):
            return web.Response(status=500, text='boom')

        app = web.Application()
        app.add_routes([web.post('/ping', broken)])
        server = TestServer(app)
        await server.start_server()
        self.addAsyncCleanup(server.close)
        with mock.patch.object(scanbot, 'PINGER_URL', str(server.make_url('')).rstrip('/')):
            self.assertIsNone(await scanbot.ping_server('1.2.3.4', 25565, 'java'))
        self.local.assert_not_awaited()

    async def test_without_a_vpn_the_bot_pings_itself(self):
        self.local.side_effect = None
        self.local.return_value = STATUS
        with mock.patch.object(scanbot, 'PINGER_URL', ''):
            self.assertEqual(await scanbot.ping_server('1.2.3.4', 25565, 'java'), STATUS)
        self.local.assert_awaited_once_with('1.2.3.4', 25565, 'java', scanbot.DIRECT_TIMEOUT)


class VpnCheckTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.probe_answers = {'java': False, 'bedrock': False}
        self.probes = []

        async def fake_probe(edition='java'):
            self.probes.append(edition)
            return self.probe_answers[edition]

        self.set_status = mock.AsyncMock()
        for p in (mock.patch.object(scanbot, 'PINGER_URL', 'http://gluetun:8765'),
                  mock.patch.object(scanbot, 'probe_direct', fake_probe),
                  mock.patch.object(scanbot, 'set_status', self.set_status),
                  mock.patch.object(scanbot.bot, 'direct_ok', None),
                  mock.patch.object(scanbot.bot, 'direct_ok_bedrock', None),
                  mock.patch.object(scanbot.bot, 'probed_at', 0.0)):
            p.start()
            self.addCleanup(p.stop)

    async def test_first_check_logs_that_the_vpn_is_down(self):
        with self.assertLogs('scanbot', level='WARNING') as logs:
            await scanbot.check_vpn()
        self.assertTrue(scanbot.vpn_down())
        self.assertIn('API only', logs.output[0])

    async def test_vpn_coming_back_is_logged_and_shown(self):
        await scanbot.check_vpn()
        self.probe_answers = {'java': True, 'bedrock': True}
        with self.assertLogs('scanbot', level='INFO') as logs:
            await scanbot.check_vpn()
        self.assertFalse(scanbot.vpn_down())
        self.assertIn('work', logs.output[0])
        self.assertNotIn('VPN down', self.set_status.await_args.args[0])

    async def test_a_scan_rechecks_a_down_vpn_but_not_one_just_checked(self):
        await scanbot.check_vpn()
        self.probes.clear()
        self.assertFalse(await scanbot.direct_pings_work('java'))
        self.assertEqual(self.probes, [])  # Checked less than 10 s ago

        scanbot.bot.probed_at = time.monotonic() - 11
        self.probe_answers = {'java': True, 'bedrock': True}
        self.assertTrue(await scanbot.direct_pings_work('java'))
        self.assertEqual(sorted(self.probes), ['bedrock', 'java'])

    async def test_a_working_vpn_is_not_rechecked_per_scan(self):
        self.probe_answers = {'java': True, 'bedrock': True}
        await scanbot.check_vpn()
        self.probes.clear()
        scanbot.bot.probed_at = 0.0
        self.assertTrue(await scanbot.direct_pings_work('java'))
        self.assertEqual(self.probes, [])

    async def test_presence_says_vpn_down(self):
        await scanbot.check_vpn()
        with mock.patch.dict(scanbot.scans, clear=True), mock.patch.object(scanbot, 'queue', []):
            await scanbot.update_presence()
        self.assertEqual(self.set_status.await_args.args[0], 'Idle | Waiting for IPs · VPN down')

    async def test_without_a_vpn_nothing_is_probed_or_shown(self):
        with mock.patch.object(scanbot, 'PINGER_URL', ''), mock.patch.dict(scanbot.scans, clear=True), \
                mock.patch.object(scanbot, 'queue', []):
            scanbot.bot.direct_ok, scanbot.bot.direct_ok_bedrock = False, False
            self.assertFalse(scanbot.vpn_down())
            self.assertFalse(await scanbot.direct_pings_work('java'))
            await scanbot.update_presence()
        self.assertEqual(self.probes, [])
        self.assertEqual(self.set_status.await_args.args[0], 'Idle | Waiting for IPs')


class ScanDisplayTests(unittest.IsolatedAsyncioTestCase):
    """A scan while the VPN is down says so: start message, progress phase and results."""

    async def asyncSetUp(self):
        self.progress = mock.MagicMock(edit=mock.AsyncMock())
        self.ctx = mock.MagicMock()
        self.ctx.author = types.SimpleNamespace(id=1, mention='<@1>')
        self.ctx.guild = types.SimpleNamespace(id=10)
        self.ctx.defer = mock.AsyncMock()
        self.ctx.send = mock.AsyncMock(return_value=self.progress)
        self.ctx.channel.send = mock.AsyncMock(return_value=self.progress)
        self.phases = []

        async def fake_api(session, ip, edition='java'):
            return scanbot.make_result(ip, ip, 3, 20, [], '1.21', 'hello')

        real_run_api = scanbot.run_api

        async def recording_run_api(session, ips, results, state, *args, **kwargs):
            await real_run_api(session, ips, results, state, *args, **kwargs)
            self.phases.append(state['phase'])

        for p in (mock.patch.object(scanbot, 'check_api', fake_api),
                  mock.patch.object(scanbot, 'run_api', recording_run_api),
                  mock.patch.object(scanbot, 'batch_get_locations', mock.AsyncMock(return_value={})),
                  mock.patch.object(scanbot, 'API_DELAY', 0),
                  mock.patch.object(scanbot, 'api_pacer', scanbot.Pacer()),
                  mock.patch.object(scanbot, 'set_status', mock.AsyncMock()),
                  mock.patch.object(scanbot, 'probe_direct', mock.AsyncMock(return_value=False)),
                  mock.patch.object(scanbot.bot, 'direct_ok', False),
                  mock.patch.object(scanbot.bot, 'direct_ok_bedrock', False),
                  mock.patch.object(scanbot.bot, 'probed_at', time.monotonic()),
                  mock.patch.dict(scanbot.scans, clear=True),
                  mock.patch.object(scanbot, 'queue', [])):
            p.start()
            self.addCleanup(p.stop)

    def attachment(self):
        return types.SimpleNamespace(filename='ips.txt', size=20, read=mock.AsyncMock(return_value=b'8.8.8.8\n1.1.1.1\n'))

    def texts(self):
        calls = self.ctx.send.await_args_list + self.ctx.channel.send.await_args_list
        return [call.args[0] for call in calls if call.args]

    async def scan(self):
        await scanbot.bot.get_command('scan').callback(self.ctx, self.attachment())

    async def test_vpn_down_is_shown_everywhere(self):
        with mock.patch.object(scanbot, 'PINGER_URL', 'http://gluetun:8765'):
            await self.scan()
        started = [t for t in self.texts() if 'Scan started' in t][0]
        self.assertIn('VPN is down', started)
        self.assertEqual(self.phases, ['Checking servers via API (VPN down)'])
        done = [t for t in self.texts() if 'Scan Complete' in t][0]
        self.assertIn('VPN was down', done)

    async def test_api_only_without_a_vpn_does_not_mention_one(self):
        with mock.patch.object(scanbot, 'PINGER_URL', ''):
            await self.scan()
        self.assertFalse(any('VPN' in t for t in self.texts()), self.texts())
        self.assertEqual(self.phases, ['Checking servers via API'])


if __name__ == '__main__':
    unittest.main()
