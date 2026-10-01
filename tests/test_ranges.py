"""
Tests for range lines in a list: networks (1.2.3.0/24), ranges (1.2.3.10-1.2.3.20) and wildcards (1.2.3.*), with an
optional port, and the limit of MAX_IPS_PER_SCAN addresses per list.

Run from the repository root:  python -m unittest discover -s tests
"""
import os
import sys
import time
import unittest
from unittest import mock

# bot.py exits at import time without a token
os.environ.setdefault('DISCORD_TOKEN', 'test-token')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import bot as scanbot  # noqa: E402


def parse(text, edition='java'):
    return scanbot.parse_list(text, edition)


class RangeLineTests(unittest.TestCase):
    def test_a_network_skips_its_network_and_broadcast_address(self):
        parsed = parse('1.2.3.0/30')
        self.assertEqual(parsed.addresses, ['1.2.3.1', '1.2.3.2'])
        self.assertEqual((parsed.expanded_lines, parsed.expanded_addresses), (1, 2))

    def test_a_31_and_a_32_keep_every_address(self):
        self.assertEqual(parse('1.2.3.4/31').addresses, ['1.2.3.4', '1.2.3.5'])
        self.assertEqual(parse('1.2.3.9/32').addresses, ['1.2.3.9'])

    def test_host_bits_in_a_network_are_ignored(self):
        self.assertEqual(parse('1.2.3.5/30').addresses, ['1.2.3.5', '1.2.3.6'])

    def test_a_range_is_inclusive_and_in_order(self):
        self.assertEqual(parse('1.2.3.10-1.2.3.12').addresses, ['1.2.3.10', '1.2.3.11', '1.2.3.12'])
        self.assertEqual(parse('1.2.3.255-1.2.4.1').addresses, ['1.2.3.255', '1.2.4.0', '1.2.4.1'])

    def test_a_wildcard_is_a_24(self):
        addresses = parse('1.2.3.*').addresses
        self.assertEqual((len(addresses), addresses[0], addresses[-1]), (254, '1.2.3.1', '1.2.3.254'))

    def test_a_port_applies_to_every_address(self):
        self.assertEqual(parse('1.2.3.0/30:25570').addresses, ['1.2.3.1:25570', '1.2.3.2:25570'])
        self.assertEqual(parse('1.2.3.1-1.2.3.2:19133').addresses, ['1.2.3.1:19133', '1.2.3.2:19133'])
        self.assertEqual(parse('1.2.3.*:25566').addresses[0], '1.2.3.1:25566')

    def test_private_networks_count_once_and_are_never_expanded(self):
        began = time.monotonic()
        for line in ('10.0.0.0/8', '100.64.0.0/10', '192.168.1.0/24', '127.0.0.1-127.0.0.9', '172.16.0.*'):
            with self.subTest(line=line):
                parsed = parse(line)
                self.assertEqual((parsed.addresses, parsed.blocked, parsed.expanded_lines), ([], 1, 0))
        self.assertLess(time.monotonic() - began, 0.5)

    def test_a_range_across_a_private_block_keeps_only_the_public_addresses(self):
        parsed = parse('9.255.255.254-10.0.0.1')
        self.assertEqual(parsed.addresses, ['9.255.255.254', '9.255.255.255'])
        self.assertEqual((parsed.blocked, parsed.expanded_addresses), (2, 2))

    def test_broken_range_lines_are_invalid(self):
        for line in ('1.2.3.9-1.2.3.1', '1.2.3.0/33', '1.2.3.256/24', '2001:db8::/32', '1.2.3.0/30:70000',
                     '1.2.3.0/30:0', '1.2.3.300-1.2.3.400', '01.2.3.0/24'):
            with self.subTest(line=line):
                parsed = parse(line)
                self.assertEqual((parsed.addresses, parsed.invalid), ([], 1))

    def test_typos_that_look_like_addresses_are_invalid_not_hostnames(self):
        for line in ('1.2.3.999', '1.2.3.4-9', '1234', '1.2.3.4.5'):
            with self.subTest(line=line):
                self.assertEqual(parse(line).invalid, 1)

    def test_hostnames_still_work(self):
        self.assertEqual(parse('mc.example.com\nplay.my-server.net:25566\nxn--mc-8ka.de').addresses,
                         ['mc.example.com', 'play.my-server.net:25566', 'xn--mc-8ka.de'])

    def test_expanded_addresses_are_deduplicated_against_plain_lines(self):
        parsed = parse('1.2.3.1\n1.2.3.0/30\n1.2.3.2:25565')
        self.assertEqual(parsed.addresses, ['1.2.3.1', '1.2.3.2'])
        self.assertEqual(parsed.duplicates, 2)

    def test_the_default_bedrock_port_is_the_same_server(self):
        self.assertEqual(parse('1.2.3.0/30\n1.2.3.1:19132', 'bedrock').duplicates, 1)

    def test_parse_ips_still_returns_four_numbers(self):
        self.assertEqual(scanbot.parse_ips('1.2.3.0/30\nbad line\n10.0.0.1\n1.2.3.1'),
                         (['1.2.3.1', '1.2.3.2'], 1, 1, 1))


class LimitTests(unittest.TestCase):
    def test_a_line_bigger_than_a_scan_is_refused_at_once_naming_it(self):
        began = time.monotonic()
        with self.assertRaises(scanbot.TooManyAddresses) as caught:
            parse('1.1.1.1\n# a comment\n11.0.0.0/8\n')
        self.assertLess(time.monotonic() - began, 0.5)
        e = caught.exception
        self.assertEqual((e.line_number, e.line, e.count), (3, '11.0.0.0/8', 16_777_214))
        self.assertEqual(e.reply(), "❌ **Too many IPs:** line 3 (`11.0.0.0/8`) has 16,777,214 addresses; a scan takes "
                                    f"at most {scanbot.MAX_IPS_PER_SCAN}.")

    def test_the_line_that_takes_the_list_past_the_limit_is_named(self):
        with mock.patch.object(scanbot, 'MAX_IPS_PER_SCAN', 10):
            with self.assertRaises(scanbot.TooManyAddresses) as caught:
                parse('1.2.3.0/29\n1.2.4.0/29\n1.2.5.0/29')
            self.assertEqual((caught.exception.line_number, caught.exception.count), (2, None))
            self.assertIn("line 2 (`1.2.4.0/29`) takes the list past 10 addresses", caught.exception.reply())

    def test_exactly_the_limit_is_fine_and_duplicates_dont_count(self):
        with mock.patch.object(scanbot, 'MAX_IPS_PER_SCAN', 12):
            self.assertEqual(len(parse('1.2.3.0/29\n1.2.4.0/29\n1.2.3.1\n1.2.4.0/29').addresses), 12)

    def test_a_private_network_bigger_than_a_scan_is_skipped_not_refused(self):
        self.assertEqual(parse('10.0.0.0/8\n1.2.3.4').addresses, ['1.2.3.4'])


class RepeatedRangeTests(unittest.TestCase):
    """A small file repeating a big range must not keep the bot busy: expanding costs time even for duplicates."""

    def refused_quickly(self, text):
        self.assertLessEqual(len(text.encode()), 2_000_000)
        real, budget = scanbot.range_entries, [3 * scanbot.MAX_IPS_PER_SCAN]

        def counted(*args):
            # Without the limit these files take up to an hour; stop after a few scans' worth so the test fails fast
            for entry in real(*args):
                budget[0] -= 1
                if budget[0] < 0:
                    raise AssertionError("expanded more than three scans' worth of addresses")
                yield entry

        began = time.monotonic()
        with mock.patch.object(scanbot, 'range_entries', counted), \
                self.assertRaises(scanbot.TooManyAddresses) as caught:
            parse(text)
        self.assertLess(time.monotonic() - began, 1)
        return caught.exception

    def test_a_2_mb_file_repeating_one_range_is_refused_at_once(self):
        e = self.refused_quickly('1.1.0.0/18\n' * 181_818)
        self.assertEqual((e.line_number, e.covered), (4, 4 * 16_382))
        self.assertIn("the range lines up to line 4 (`1.1.0.0/18`) cover 65,528 addresses", e.reply())
        self.assertIn(f"a scan takes at most {scanbot.MAX_IPS_PER_SCAN}", e.reply())

    def test_shifting_each_range_doesnt_get_around_it(self):
        e = self.refused_quickly(''.join(f'1.1.0.{i % 250}-1.1.63.{i % 250}\n' for i in range(80_000)))
        self.assertEqual(e.line_number, 4)

    def test_many_small_repeated_ranges_are_refused_too(self):
        e = self.refused_quickly('1.1.0.0/30\n' * 181_818)
        self.assertEqual(e.line_number, scanbot.RANGE_WORK_FACTOR * scanbot.MAX_IPS_PER_SCAN // 2 + 1)

    def test_ordinary_overlaps_are_fine(self):
        parsed = parse('1.2.0.0/18\n1.2.0.0/19\n1.2.3.*')
        self.assertEqual((len(parsed.addresses), parsed.duplicates), (16_382, 8_190 + 254))

    def test_a_2_mb_file_of_repeated_plain_lines_is_still_read(self):
        parsed = parse('1.1.1.1\n' * 250_000)
        self.assertEqual((parsed.addresses, parsed.duplicates), (['1.1.1.1'], 249_999))


if __name__ == '__main__':
    unittest.main()
