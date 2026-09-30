"""
Tests for switching Mullvad servers when the current one is down.

Run from the repository root:  python -m unittest discover -s tests
"""
import os
import sys
import unittest
from unittest import mock

# bot.py exits at import time without a token
os.environ.setdefault('DISCORD_TOKEN', 'test-token')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from aiohttp import web  # noqa: E402
from aiohttp.test_utils import TestServer  # noqa: E402

import bot as scanbot  # noqa: E402
import vpn_switch  # noqa: E402

MIN = 60


def relay(hostname, city, active=True):
    return {"hostname": hostname, "city_name": city, "active": active, "ipv4_addr_in": "1.2.3.4"}


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class SwitcherTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.relays = [relay('rs-beg-wg-101', 'Belgrade'), relay('rs-beg-wg-102', 'Belgrade'),
                       relay('hr-zag-wg-001', 'Zagreb'), relay('hr-zag-wg-002', 'Zagreb'),
                       relay('hu-bud-wg-101', 'Budapest'), relay('de-fra-wg-001', 'Frankfurt')]
        self.pinned = []
        self.unknown_to_gluetun = set()
        self.gluetun_down = False
        self.fetches = 0
        self.api_down = False
        self.clock = Clock()

        async def fetch():
            self.fetches += 1
            if self.api_down:
                raise OSError("no internet")
            return [dict(r) for r in self.relays]

        async def set_server(hostname):
            if self.gluetun_down:
                raise OSError("connection refused")
            self.pinned.append(hostname)
            return hostname not in self.unknown_to_gluetun

        self.switcher = vpn_switch.MullvadSwitcher(['Belgrade', 'zagreb', 'Budapest'], fetch, set_server, clock=self.clock)

    def set_active(self, hostname, active):
        for r in self.relays:
            if r['hostname'] == hostname:
                r['active'] = active

    async def tick(self, vpn_ok=True, minutes=5):
        self.clock.now += minutes * MIN  # Past the relay list cache, like the 5-minute checks
        return await self.switcher.tick(vpn_ok)

    async def test_starts_on_the_first_server_of_the_chosen_city(self):
        self.assertFalse(await self.tick())  # Picking the first server isn't a switch
        self.assertEqual(self.switcher.current, 'rs-beg-wg-101')
        self.assertEqual(self.pinned, ['rs-beg-wg-101'])

    async def test_keeps_telling_gluetun_the_same_server(self):
        # gluetun ignores an unchanged setting, and forgets it when it restarts: repeating it re-pins
        await self.tick()
        await self.tick()
        await self.tick()
        self.assertEqual(self.pinned, ['rs-beg-wg-101'] * 3)

    async def test_offline_on_mullvads_site_moves_to_the_other_server_in_the_city(self):
        await self.tick()
        self.set_active('rs-beg-wg-101', False)
        with self.assertLogs('scanbot.vpn', level='WARNING') as logs:
            self.assertTrue(await self.tick())
        self.assertEqual(self.switcher.current, 'rs-beg-wg-102')
        self.assertIn("offline on Mullvad's site", logs.output[0])

    async def test_whole_city_offline_moves_to_the_next_fastest_city(self):
        await self.tick()
        self.set_active('rs-beg-wg-101', False)
        self.set_active('rs-beg-wg-102', False)
        self.assertTrue(await self.tick())
        self.assertEqual(self.switcher.current, 'hr-zag-wg-001')

    async def test_cities_not_in_the_list_are_never_used(self):
        await self.tick()
        for hostname in ('rs-beg-wg-101', 'rs-beg-wg-102', 'hr-zag-wg-001', 'hr-zag-wg-002', 'hu-bud-wg-101'):
            self.set_active(hostname, False)
        with self.assertLogs('scanbot.vpn', level='WARNING') as logs:
            self.assertFalse(await self.tick())
        self.assertEqual(self.switcher.current, 'rs-beg-wg-101')  # Frankfurt isn't one of the cities
        self.assertIn('No other Mullvad server', logs.output[0])

    async def test_two_failed_checks_in_a_row_move_to_another_server(self):
        await self.tick()
        self.assertFalse(await self.tick(vpn_ok=False, minutes=1))
        self.assertEqual(self.switcher.current, 'rs-beg-wg-101')
        self.assertTrue(await self.tick(vpn_ok=False, minutes=1))
        self.assertEqual(self.switcher.current, 'rs-beg-wg-102')

    async def test_one_good_check_resets_the_count(self):
        await self.tick()
        await self.tick(vpn_ok=False, minutes=1)
        await self.tick(vpn_ok=True, minutes=1)
        self.assertFalse(await self.tick(vpn_ok=False, minutes=1))
        self.assertEqual(self.switcher.current, 'rs-beg-wg-101')

    async def test_unreachable_server_waits_30_minutes_then_longer_each_time(self):
        await self.tick()
        await self.tick(vpn_ok=False, minutes=1)
        await self.tick(vpn_ok=False, minutes=1)  # 101 unreachable: on 102 now
        self.assertEqual(self.switcher.current, 'rs-beg-wg-102')

        await self.tick(minutes=29)
        self.assertEqual(self.switcher.current, 'rs-beg-wg-102')  # 101 still resting
        await self.tick(minutes=2)
        self.assertEqual(self.switcher.current, 'rs-beg-wg-101')  # Back after 30 minutes

        await self.tick(vpn_ok=False, minutes=1)
        await self.tick(vpn_ok=False, minutes=1)  # Fails again: now it rests an hour
        self.assertEqual(self.switcher.current, 'rs-beg-wg-102')
        await self.tick(minutes=35)
        self.assertEqual(self.switcher.current, 'rs-beg-wg-102')
        await self.tick(minutes=30)
        self.assertEqual(self.switcher.current, 'rs-beg-wg-101')

    async def test_moves_back_once_the_better_server_has_been_up_30_minutes(self):
        await self.tick()
        self.set_active('rs-beg-wg-101', False)
        await self.tick()
        self.assertEqual(self.switcher.current, 'rs-beg-wg-102')

        self.set_active('rs-beg-wg-101', True)
        await self.tick(minutes=5)
        await self.tick(minutes=20)
        self.assertEqual(self.switcher.current, 'rs-beg-wg-102')  # Up for 25 minutes: not yet
        with self.assertLogs('scanbot.vpn', level='WARNING') as logs:
            self.assertTrue(await self.tick(minutes=10))
        self.assertEqual(self.switcher.current, 'rs-beg-wg-101')
        self.assertIn('back to rs-beg-wg-101', logs.output[0])

    async def test_a_server_that_blinks_off_starts_its_30_minutes_again(self):
        await self.tick()
        self.set_active('rs-beg-wg-101', False)
        await self.tick()
        self.set_active('rs-beg-wg-101', True)
        await self.tick(minutes=25)
        self.set_active('rs-beg-wg-101', False)
        await self.tick(minutes=5)
        self.set_active('rs-beg-wg-101', True)
        await self.tick(minutes=5)
        await self.tick(minutes=25)
        self.assertEqual(self.switcher.current, 'rs-beg-wg-102')

    async def test_nothing_is_judged_while_mullvads_list_cant_be_fetched(self):
        # If this machine's own internet is down, the VPN looks down too: don't move servers then
        await self.tick()
        self.api_down = True
        with self.assertLogs('scanbot.vpn', level='WARNING'):
            for _ in range(4):
                self.assertFalse(await self.tick(vpn_ok=False, minutes=1))
        self.assertEqual(self.switcher.current, 'rs-beg-wg-101')
        self.assertEqual(self.switcher.fails, 0)
        self.assertEqual(self.pinned[-1], 'rs-beg-wg-101')  # Still kept pinned

    async def test_the_list_is_fetched_at_most_every_5_minutes(self):
        await self.tick()
        await self.tick(minutes=1)
        await self.tick(minutes=1)
        self.assertEqual(self.fetches, 1)
        await self.tick(minutes=4)
        self.assertEqual(self.fetches, 2)

    async def test_a_failed_check_always_asks_mullvad_again(self):
        await self.tick()
        await self.tick(vpn_ok=False, minutes=1)
        await self.tick(vpn_ok=False, minutes=1)
        self.assertEqual(self.fetches, 3)

    async def test_servers_gluetun_doesnt_know_are_skipped(self):
        self.unknown_to_gluetun = {'rs-beg-wg-101'}
        with self.assertLogs('scanbot.vpn', level='WARNING') as logs:
            await self.tick()
        self.assertEqual(self.switcher.current, 'rs-beg-wg-102')
        self.assertIn("doesn't know", logs.output[0])
        self.set_active('rs-beg-wg-102', False)
        await self.tick()
        self.assertEqual(self.switcher.current, 'hr-zag-wg-001')  # 101 isn't tried again

    async def test_unreachable_gluetun_is_tried_once_per_check(self):
        self.gluetun_down = True
        with self.assertLogs('scanbot.vpn', level='WARNING') as logs:
            self.assertFalse(await self.tick())
        self.assertIsNone(self.switcher.current)
        self.assertEqual(len(logs.output), 1)  # Not one warning per candidate server
        self.gluetun_down = False
        await self.tick()
        self.assertEqual(self.switcher.current, 'rs-beg-wg-101')


class ControlServerTests(unittest.IsolatedAsyncioTestCase):
    """set_vpn_server talks to gluetun's control server."""

    async def asyncSetUp(self):
        self.requests = []
        self.status = 200

        async def settings(request):
            self.requests.append((request.headers.get('X-API-Key'), await request.json()))
            return web.Response(status=self.status, text='settings left unchanged' if self.status == 200 else 'nope')

        app = web.Application()
        app.add_routes([web.put('/v1/vpn/settings', settings)])
        self.server = TestServer(app)
        await self.server.start_server()
        self.addAsyncCleanup(self.server.close)
        for p in (mock.patch.object(scanbot, 'GLUETUN_URL', str(self.server.make_url('')).rstrip('/')),
                  mock.patch.object(scanbot, 'GLUETUN_API_KEY', 'secret-key'),
                  mock.patch.object(scanbot, 'pinger_session', None)):
            p.start()
            self.addCleanup(p.stop)
        self.addAsyncCleanup(self.close_session)

    async def close_session(self):
        if scanbot.pinger_session is not None:
            await scanbot.pinger_session.close()

    async def test_pins_one_hostname_and_clears_the_city_filters(self):
        self.assertTrue(await scanbot.set_vpn_server('rs-beg-wg-102'))
        key, body = self.requests[0]
        self.assertEqual(key, 'secret-key')
        self.assertEqual(body, {"provider": {"server_selection": {
            "hostnames": ["rs-beg-wg-102"], "cities": [], "countries": []}}})

    async def test_rejected_server_means_unknown(self):
        self.status = 400
        self.assertFalse(await scanbot.set_vpn_server('xx-new-wg-999'))

    async def test_wrong_key_is_an_error(self):
        self.status = 401
        with self.assertRaises(PermissionError):
            await scanbot.set_vpn_server('rs-beg-wg-102')


class SetupTests(unittest.IsolatedAsyncioTestCase):
    def env(self, **values):
        defaults = dict(PINGER_URL='http://gluetun:8765', VPN_PROVIDER='Mullvad', GLUETUN_URL='http://gluetun:8000',
                        GLUETUN_API_KEY='k', VPN_FALLBACK_CITIES='Belgrade,Zagreb', VPN_LOCATION='Belgrade, Serbia')
        defaults.update(values)
        return [mock.patch.object(scanbot, name, value) for name, value in defaults.items()]

    def make(self, **values):
        patches = self.env(**values)
        for p in patches:
            p.start()
        try:
            return scanbot.make_vpn_switcher()
        finally:
            for p in patches:
                p.stop()

    def test_on_for_mullvad_with_a_key(self):
        with self.assertLogs('scanbot', level='INFO'):
            switcher = self.make()
        self.assertEqual(switcher.cities, ['belgrade', 'zagreb'])

    def test_off_for_other_providers_and_without_a_vpn(self):
        self.assertIsNone(self.make(VPN_PROVIDER='Proton VPN'))
        self.assertIsNone(self.make(VPN_PROVIDER='Cloudflare WARP'))
        self.assertIsNone(self.make(PINGER_URL=''))

    def test_off_without_a_key_and_says_how_to_turn_it_on(self):
        with self.assertLogs('scanbot', level='INFO') as logs:
            self.assertIsNone(self.make(GLUETUN_API_KEY=''))
        self.assertIn('installer again', logs.output[0])

    def test_without_fallback_cities_only_the_chosen_city_is_used(self):
        with self.assertLogs('scanbot', level='INFO'):
            switcher = self.make(VPN_FALLBACK_CITIES='')
        self.assertEqual(switcher.cities, ['belgrade'])

    async def test_a_switch_shortens_the_wait_before_the_next_check(self):
        switcher = mock.MagicMock(tick=mock.AsyncMock(return_value=True))
        with mock.patch.object(scanbot.bot, 'vpn_switcher', switcher), mock.patch.object(scanbot, 'PINGER_URL', 'x'), \
                mock.patch.object(scanbot.bot, 'direct_ok', False), mock.patch.object(scanbot.bot, 'direct_ok_bedrock', False):
            self.assertEqual(await scanbot.switch_vpn_server(), scanbot.VPN_SWITCH_SETTLE)
            switcher.tick.assert_awaited_once_with(vpn_ok=False)
            switcher.tick.return_value = False
            self.assertEqual(await scanbot.switch_vpn_server(), scanbot.VPN_CHECK_DOWN)

    async def test_no_switcher_means_normal_checks(self):
        with mock.patch.object(scanbot.bot, 'vpn_switcher', None), mock.patch.object(scanbot, 'PINGER_URL', 'x'), \
                mock.patch.object(scanbot.bot, 'direct_ok', True), mock.patch.object(scanbot.bot, 'direct_ok_bedrock', True):
            self.assertEqual(await scanbot.switch_vpn_server(), scanbot.VPN_CHECK_UP)


if __name__ == '__main__':
    unittest.main()
