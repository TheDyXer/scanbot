"""
Tests for reading the list of servers: which ports are valid, and which lines count as the same server.

Run from the repository root:  python -m unittest discover -s tests
"""
import os
import sys
import unittest

# bot.py exits at import time without a token
os.environ.setdefault('DISCORD_TOKEN', 'test-token')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import bot  # noqa: E402


def parse(*lines, edition='java'):
    return bot.parse_ips("\n".join(lines), edition)


class PortTests(unittest.TestCase):
    def test_ports_outside_1_to_65535_are_invalid_lines(self):
        for entry in ('1.2.3.4:0', '1.2.3.4:65536', '1.2.3.4:99999', 'play.example.com:0'):
            with self.subTest(entry=entry):
                unique, invalid, duplicates, blocked = parse(entry)
                self.assertEqual((unique, invalid, duplicates, blocked), ([], 1, 0, 0))

    def test_ports_at_the_edges_are_valid(self):
        unique, invalid, _, _ = parse('1.2.3.4:1', '1.2.3.4:65535')
        self.assertEqual((unique, invalid), (['1.2.3.4:1', '1.2.3.4:65535'], 0))

    def test_a_private_address_with_a_bad_port_counts_as_invalid(self):
        # The port is checked first, so each line is counted once, as the first thing wrong with it
        self.assertEqual(parse('10.0.0.1:0')[1:], (1, 0, 0))


class DuplicateTests(unittest.TestCase):
    def test_case_and_a_trailing_dot_do_not_make_a_new_server(self):
        unique, _, duplicates, _ = parse('Play.Example.com', 'play.example.com', 'play.example.com.')
        self.assertEqual((unique, duplicates), (['Play.Example.com'], 2))

    def test_an_ip_with_the_java_port_is_the_ip_without_one(self):
        self.assertEqual(parse('1.2.3.4', '1.2.3.4:25565')[:3], (['1.2.3.4'], 0, 1))
        # The first spelling is kept as it was written
        self.assertEqual(parse('1.2.3.4:25565', '1.2.3.4')[:3], (['1.2.3.4:25565'], 0, 1))

    def test_a_java_hostname_with_the_default_port_is_a_different_server(self):
        # Without a port, mcstatus follows the name's SRV record, which may point to another port or host
        unique, _, duplicates, _ = parse('play.example.com', 'play.example.com:25565')
        self.assertEqual((unique, duplicates), (['play.example.com', 'play.example.com:25565'], 0))

    def test_other_ports_are_other_servers(self):
        self.assertEqual(parse('1.2.3.4', '1.2.3.4:25566')[0], ['1.2.3.4', '1.2.3.4:25566'])

    def test_leading_zeros_in_a_port_do_not_make_a_new_server(self):
        self.assertEqual(parse('1.2.3.4:80', '1.2.3.4:080')[:3], (['1.2.3.4:80'], 0, 1))

    def test_bedrock_has_no_srv_records_so_its_default_port_is_the_same_as_none(self):
        self.assertEqual(parse('play.example.com', 'play.example.com:19132', edition='bedrock')[2], 1)
        self.assertEqual(parse('1.2.3.4:19132', '1.2.3.4', edition='bedrock')[0], ['1.2.3.4:19132'])
        # 25565 isn't Bedrock's default
        self.assertEqual(parse('1.2.3.4', '1.2.3.4:25565', edition='bedrock')[2], 0)

    def test_private_duplicates_count_as_private_not_as_duplicates(self):
        self.assertEqual(parse('10.0.0.1', '10.0.0.1')[1:], (0, 0, 2))

    def test_a_mixed_list_gives_the_counts_the_start_message_shows(self):
        unique, invalid, duplicates, blocked = parse('Play.Example.com', 'play.example.com', '1.2.3.4:99999',
                                                     '1.2.3.4:0', '1.2.3.4', '1.2.3.4:25565')
        self.assertEqual((unique, invalid, duplicates, blocked), (['Play.Example.com', '1.2.3.4'], 2, 2, 0))


if __name__ == '__main__':
    unittest.main()
