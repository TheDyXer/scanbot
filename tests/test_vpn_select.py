"""
Tests for vpn_select.py, which picks the fastest VPN city for the installer.

Run from the repository root:  python -m unittest discover -s tests
"""
import io
import json
import os
import random
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import vpn_select  # noqa: E402

SERVERS = [
    {'vpn': 'wireguard', 'country': 'Serbia', 'city': 'Belgrade', 'hostname': 'rs-beg-wg-101', 'ips': ['1.1.1.1', '2a00::1']},
    {'vpn': 'wireguard', 'country': 'Serbia', 'city': 'Belgrade', 'hostname': 'rs-beg-wg-102', 'ips': ['1.1.1.2']},
    {'vpn': 'wireguard', 'country': 'Serbia', 'city': 'Belgrade', 'hostname': 'rs-beg-wg-103', 'ips': ['1.1.1.3']},
    {'vpn': 'wireguard', 'country': 'Austria', 'city': 'Vienna', 'hostname': 'at-vie-wg-001', 'ips': ['2.2.2.1'], 'free': True},
    {'vpn': 'openvpn', 'country': 'Germany', 'city': 'Frankfurt', 'hostname': 'de-fra-ovpn-001', 'ips': ['3.3.3.1']},
    {'vpn': 'wireguard', 'country': 'Japan', 'city': 'Tokyo', 'hostname': 'jp-tyo-wg-001', 'ips': ['2001:db8::1']},
    {'vpn': 'wireguard', 'country': 'Norway', 'city': '', 'hostname': 'no-wg-001', 'ips': ['4.4.4.1']},
]


class GroupByCityTests(unittest.TestCase):
    def test_keeps_wireguard_servers_with_an_ipv4_address_and_a_city(self):
        cities = vpn_select.group_by_city(SERVERS, rng=random.Random(1))
        self.assertEqual(set(cities), {('Serbia', 'Belgrade'), ('Austria', 'Vienna')})

    def test_takes_at_most_per_city_servers_and_only_ipv4(self):
        cities = vpn_select.group_by_city(SERVERS, per_city=2, rng=random.Random(1))
        self.assertEqual(len(cities[('Serbia', 'Belgrade')]), 2)
        self.assertTrue(all(':' not in ip for ips in cities.values() for ip in ips))

    def test_free_only_keeps_free_servers(self):
        cities = vpn_select.group_by_city(SERVERS, free_only=True)
        self.assertEqual(cities, {('Austria', 'Vienna'): ['2.2.2.1']})


class RankTests(unittest.TestCase):
    def test_fastest_first_and_silent_cities_last(self):
        rows = vpn_select.rank({
            ('Austria', 'Vienna'): [25.0, 24.0, 90.0],
            ('Japan', 'Tokyo'): [],
            ('Serbia', 'Belgrade'): [9.0, 11.0, 10.0],
        })
        self.assertEqual([r[1] for r in rows], ['Belgrade', 'Vienna', 'Tokyo'])
        self.assertEqual(rows[0][2], 10.0)  # median, so one slow reply doesn't matter
        self.assertIsNone(rows[2][2])

    def test_format_is_tab_separated_and_limited_to_top(self):
        rows = [('Serbia', 'Belgrade', 9.6), ('Austria', 'Vienna', 24.2), ('Japan', 'Tokyo', None)]
        self.assertEqual(vpn_select.format_rows(rows, 2), '1\tSerbia\tBelgrade\t10\n2\tAustria\tVienna\t24')
        self.assertEqual(vpn_select.format_rows(rows, 5).splitlines()[2], '3\tJapan\tTokyo\t-')


class MeasureTests(unittest.TestCase):
    def test_collects_every_servers_replies_per_city(self):
        fake_rtts = {'1.1.1.1': [10.0, 12.0], '1.1.1.2': [11.0], '2.2.2.1': []}
        results = vpn_select.measure(
            {('Serbia', 'Belgrade'): ['1.1.1.1', '1.1.1.2'], ('Austria', 'Vienna'): ['2.2.2.1']},
            ping_fn=lambda ip, count, timeout: fake_rtts[ip])
        self.assertEqual(sorted(results[('Serbia', 'Belgrade')]), [10.0, 11.0, 12.0])
        self.assertEqual(results[('Austria', 'Vienna')], [])


class IcmpTests(unittest.TestCase):
    def test_checksum_of_a_known_echo_request(self):
        # Echo request, id 1, seq 1, no payload: checksum 0xF7FD
        header = bytes([8, 0, 0, 0, 0, 1, 0, 1])
        self.assertEqual(vpn_select.icmp_checksum(header), 0xF7FD)

    def test_checksum_handles_odd_lengths(self):
        self.assertEqual(vpn_select.icmp_checksum(b'\x01'), vpn_select.icmp_checksum(b'\x01\x00'))


class LoadServersTests(unittest.TestCase):
    def test_reads_gluetuns_servers_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'servers.json')
            with open(path, 'w', encoding='utf-8') as f:
                json.dump({'mullvad': {'servers': SERVERS[:2]}}, f)
            self.assertEqual(vpn_select.load_servers('mullvad', path), SERVERS[:2])

    def test_falls_back_to_the_online_list(self):
        body = io.BytesIO(json.dumps({'servers': SERVERS[3:4]}).encode())
        with mock.patch.object(vpn_select.urllib.request, 'urlopen', return_value=body) as urlopen:
            self.assertEqual(vpn_select.load_servers('protonvpn', '/nonexistent/servers.json'), SERVERS[3:4])
        request = urlopen.call_args.args[0]
        self.assertIn('protonvpn.json', request.full_url)
        self.assertIn('scanbot', request.get_header('User-agent'))


class MainTests(unittest.TestCase):
    def test_prints_the_fastest_cities(self):
        with mock.patch.object(vpn_select, 'load_servers', return_value=SERVERS), \
             mock.patch.object(vpn_select, 'ping', side_effect=lambda ip, c, t: [5.0] if ip.startswith('1.') else [30.0]), \
             mock.patch('sys.stdout', new_callable=io.StringIO) as out, \
             mock.patch('sys.stderr', new_callable=io.StringIO):
            self.assertEqual(vpn_select.main(['--provider', 'mullvad', '--top', '1']), 0)
        self.assertEqual(out.getvalue().strip(), '1\tSerbia\tBelgrade\t5')

    def test_says_so_when_nothing_answers(self):
        with mock.patch.object(vpn_select, 'load_servers', return_value=SERVERS), \
             mock.patch.object(vpn_select, 'ping', return_value=[]), \
             mock.patch('sys.stderr', new_callable=io.StringIO) as err:
            self.assertEqual(vpn_select.main(['--provider', 'mullvad']), 2)
        self.assertIn('ICMP', err.getvalue())


if __name__ == '__main__':
    unittest.main()
