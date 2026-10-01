"""
Tests for the API phase's resilience: the 3-second server timeout and the query switch sent to mcstatus.io, the
shared pacer slowing down after a rate limit, the fallback to mcsrvstat.us for Java servers, and servers neither
service could check being counted as unchecked instead of offline.

Run from the repository root:  python -m unittest discover -s tests
"""
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
from aiohttp.test_utils import TestServer  # noqa: E402

import bot as scanbot  # noqa: E402

MCSTATUS_ONLINE = {'online': True, 'ip_address': '1.2.3.4', 'players': {'online': 3, 'max': 20, 'list': []},
                   'version': {'name_clean': '1.21', 'protocol': 767}, 'motd': {'clean': 'hello'}}
# The shape of a live mcsrvstat.us v3 answer (checked against api.mcsrvstat.us/3/demo.mcstatus.io, October 2026)
MCSRVSTAT_ONLINE = {
    'online': True, 'ip': '5.6.7.8', 'port': 25565, 'hostname': 'play.example.com', 'version': '§aPaper 1.21.4',
    'protocol': {'version': 769, 'name': '1.21.4'},
    'players': {'online': 2, 'max': 100, 'list': [{'name': 'Alex', 'uuid': 'a'}, {'name': 'Steve', 'uuid': 'b'}]},
    'motd': {'raw': ['§a&gt;&gt;&gt; Welcome'], 'clean': ['&gt;&gt;&gt; Welcome', '  to the server '],
             'html': ['<span>&gt;&gt;&gt; Welcome</span>']},
    'mods': [{'name': 'forge', 'version': '1'}], 'plugins': [{'name': 'EssentialsX', 'version': '2.20'}],
    'software': 'Paper', 'eula_blocked': False, 'debug': {'cachehit': False},
}
# What it says about a dead address: note the `ip`, which must not be used
MCSRVSTAT_OFFLINE = {'ip': '127.0.0.1', 'port': 25565, 'online': False, 'debug': {'cachehit': False}}


def closed_port():
    """A local port nothing listens on."""
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class FakeServices:
    """
    mcstatus.io and mcsrvstat.us on one local server. `plan[(service, entry)]` is a list of (status, body) replies
    given in turn, the last one repeating; anything not planned is offline.
    """
    def __init__(self):
        self.plan = {}
        self.requests = []  # (service, edition, entry, query, User-Agent)

    def app(self):
        app = web.Application()
        app.router.add_get('/v2/status/{edition}/{entry}', self.mcstatus)
        app.router.add_get('/3/{entry}', self.mcsrvstat)
        return app

    def reply(self, service, request, edition):
        entry = request.match_info['entry']
        self.requests.append((service, edition, entry, dict(request.query), request.headers.get('User-Agent')))
        replies = self.plan.get((service, entry)) or [(200, {'online': False})]
        status, body = replies.pop(0) if len(replies) > 1 else replies[0]
        return web.json_response(body, status=status)

    async def mcstatus(self, request):
        return self.reply('mcstatus.io', request, request.match_info['edition'])

    async def mcsrvstat(self, request):
        return self.reply('mcsrvstat.us', request, 'java')

    def asked(self, service):
        return [entry for s, _, entry, _, _ in self.requests if s == service]


class ApiTestCase(unittest.IsolatedAsyncioTestCase):
    """Both services on a local server, asked through the bot's real HTTP session."""

    async def asyncSetUp(self):
        self.services = FakeServices()
        self.server = TestServer(self.services.app(), host='127.0.0.1')
        await self.server.start_server()
        base = f"http://127.0.0.1:{self.server.port}"
        self.health = scanbot.ApiHealth()
        self.pacer = scanbot.Pacer('mcstatus.io')
        for p in (mock.patch.object(scanbot, 'MC_API_URLS', {'java': f'{base}/v2/status/java/',
                                                              'bedrock': f'{base}/v2/status/bedrock/'}),
                  mock.patch.object(scanbot, 'MCSRVSTAT_URL', f'{base}/3/'),
                  mock.patch.object(scanbot, 'API_DELAY', 0.01),
                  mock.patch.object(scanbot, 'MCSRVSTAT_DELAY', 0.01),
                  mock.patch.object(scanbot, 'API_QUERY', True),
                  mock.patch.object(scanbot, 'api_pacer', self.pacer),
                  mock.patch.object(scanbot, 'mcsrvstat_pacer', scanbot.Pacer('mcsrvstat.us')),
                  mock.patch.object(scanbot, 'api_health', self.health)):
            p.start()
            self.addCleanup(p.stop)
        self.session = scanbot.api_session()

    async def asyncTearDown(self):
        await self.session.close()
        await self.server.close()

    def plan(self, service, entry, *replies):
        self.services.plan[(service, entry)] = list(replies)


class RequestTests(ApiTestCase):
    async def test_both_editions_ask_for_a_3_second_timeout_and_keep_the_query(self):
        await scanbot.check_api(self.session, 'a.example', 'java')
        await scanbot.check_api(self.session, 'b.example', 'bedrock')
        queries = {(edition, entry): query for _, edition, entry, query, _ in self.services.requests}
        self.assertEqual(queries, {('java', 'a.example'): {'timeout': '3'}, ('bedrock', 'b.example'): {'timeout': '3'}})

    async def test_api_query_off_turns_the_query_off_for_java_only(self):
        with mock.patch.object(scanbot, 'API_QUERY', False):
            await scanbot.check_api(self.session, 'a.example', 'java')
            await scanbot.check_api(self.session, 'b.example', 'bedrock')
        queries = [query for _, _, _, query, _ in self.services.requests]
        self.assertEqual(queries, [{'timeout': '3', 'query': 'false'}, {'timeout': '3'}])

    async def test_both_services_get_the_user_agent(self):
        self.plan('mcstatus.io', 'a.example', (500, {}))
        await scanbot.check_api(self.session, 'a.example')
        agents = {(service, agent) for service, _, _, _, agent in self.services.requests}
        self.assertEqual(agents, {('mcstatus.io', scanbot.USER_AGENT), ('mcsrvstat.us', scanbot.USER_AGENT)})

    async def test_an_offline_answer_from_mcstatus_is_final(self):
        result = await scanbot.check_api(self.session, 'dead.example')
        self.assertIsNone(result)
        self.assertEqual(self.services.asked('mcsrvstat.us'), [])

    async def test_an_online_answer_from_mcstatus(self):
        self.plan('mcstatus.io', '1.2.3.4', (200, MCSTATUS_ONLINE))
        result = await scanbot.check_api(self.session, '1.2.3.4')
        self.assertEqual((result['players'], result['source']), (3, 'mcstatus.io'))

    async def test_a_refused_address_is_offline_and_counts_as_an_answer(self):
        self.health.failures = 3
        self.plan('mcstatus.io', 'bad.example', (400, {'error': 'bad address'}))
        self.assertIsNone(await scanbot.check_api(self.session, 'bad.example'))
        self.assertEqual(self.health.failures, 0)
        self.assertEqual(self.services.asked('mcsrvstat.us'), [])


class RateLimitTests(ApiTestCase):
    async def test_a_rate_limit_slows_the_shared_pacer_and_retries_in_turn(self):
        self.plan('mcstatus.io', '1.2.3.4', (429, {}), (200, MCSTATUS_ONLINE))
        with self.assertLogs('scanbot', 'WARNING') as logs:
            result = await scanbot.check_api(self.session, '1.2.3.4')
        self.assertEqual(result['source'], 'mcstatus.io')
        self.assertEqual(self.services.asked('mcstatus.io'), ['1.2.3.4', '1.2.3.4'])
        self.assertEqual(self.pacer.factor, 2)
        self.assertEqual(self.pacer.spacing(0.2), 0.4)
        self.assertEqual(len(logs.output), 1)
        self.assertIn('mcstatus.io is rate-limiting the bot; spacing requests 0.02 s apart', logs.output[0])
        self.assertEqual(self.health.failures, 0)

    async def test_the_retry_waits_on_the_shared_pacer(self):
        self.plan('mcstatus.io', '1.2.3.4', (429, {}), (200, MCSTATUS_ONLINE))
        wait = mock.AsyncMock()
        with mock.patch.object(self.pacer, 'wait', wait), self.assertLogs('scanbot', 'WARNING'):
            await scanbot.check_api(self.session, '1.2.3.4')
        wait.assert_awaited_once_with(scanbot.API_DELAY)

    async def test_a_server_that_stays_rate_limited_goes_to_mcsrvstat(self):
        self.plan('mcstatus.io', 'play.example.com', (429, {}))
        self.plan('mcsrvstat.us', 'play.example.com', (200, MCSRVSTAT_ONLINE))
        with self.assertLogs('scanbot', 'WARNING'):
            result = await scanbot.check_api(self.session, 'play.example.com')
        self.assertEqual(self.services.asked('mcstatus.io'), ['play.example.com'] * 3)
        self.assertEqual(result['source'], 'mcsrvstat.us')
        self.assertEqual(self.health.failures, 1)

    async def test_mcsrvstat_rate_limits_are_retried_once(self):
        self.plan('mcstatus.io', 'a.example', (500, {}))
        self.plan('mcsrvstat.us', 'a.example', (429, {}), (200, MCSRVSTAT_ONLINE))
        with self.assertLogs('scanbot', 'WARNING') as logs:
            result = await scanbot.check_api(self.session, 'a.example')
        self.assertEqual(result['source'], 'mcsrvstat.us')
        self.assertIn('mcsrvstat.us is rate-limiting the bot', logs.output[0])


class FallbackTests(ApiTestCase):
    async def test_a_single_server_error_falls_back_for_that_server_only(self):
        self.plan('mcstatus.io', 'a.example', (503, {}))
        self.plan('mcsrvstat.us', 'a.example', (200, MCSRVSTAT_ONLINE))
        self.plan('mcstatus.io', 'b.example', (200, MCSTATUS_ONLINE))
        first = await scanbot.check_api(self.session, 'a.example')
        second = await scanbot.check_api(self.session, 'b.example')
        self.assertEqual((first['source'], second['source']), ('mcsrvstat.us', 'mcstatus.io'))
        self.assertEqual(self.services.asked('mcsrvstat.us'), ['a.example'])
        self.assertEqual(self.health.failures, 0)  # mcstatus.io answered the second one

    async def test_an_unreachable_mcstatus_falls_back(self):
        with mock.patch.dict(scanbot.MC_API_URLS, java=f"http://127.0.0.1:{closed_port()}/v2/status/java/"):
            self.plan('mcsrvstat.us', 'a.example', (200, MCSRVSTAT_ONLINE))
            result = await scanbot.check_api(self.session, 'a.example')
        self.assertEqual(result['source'], 'mcsrvstat.us')
        self.assertEqual(self.health.failures, 1)

    async def test_five_failures_in_a_row_send_java_servers_to_mcsrvstat_for_a_while(self):
        entries = [f's{i}.example' for i in range(7)]
        for entry in entries:
            self.plan('mcstatus.io', entry, (500, {}))
        with self.assertLogs('scanbot', 'WARNING') as logs:
            for entry in entries:
                await scanbot.check_api(self.session, entry)
        # The first five went to mcstatus.io (and on to mcsrvstat.us); the rest skipped it
        self.assertEqual(self.services.asked('mcstatus.io'), entries[:5])
        self.assertEqual(self.services.asked('mcsrvstat.us'), entries)
        self.assertTrue(self.health.paused())
        self.assertEqual(len(logs.output), 1)
        self.assertIn('mcstatus.io failed 5 checks in a row; Java servers are checked through mcsrvstat.us for the '
                      'next 60 s', logs.output[0])

    async def test_bedrock_keeps_asking_mcstatus_during_a_pause_and_its_answer_ends_it(self):
        self.health.failures = scanbot.API_FAILURE_THRESHOLD
        self.health.paused_until = time.monotonic() + 60
        self.plan('mcstatus.io', 'bedrock.example', (200, {'online': True, 'players': {'online': 1, 'max': 10}}))
        with self.assertLogs('scanbot', 'INFO') as logs:
            result = await scanbot.check_api(self.session, 'bedrock.example', 'bedrock')
        self.assertEqual(result['edition'], 'Bedrock')
        self.assertFalse(self.health.paused())
        self.assertIn('mcstatus.io answers again', logs.output[0])

    async def test_after_the_pause_mcstatus_gets_the_next_java_server(self):
        self.health.failures = scanbot.API_FAILURE_THRESHOLD
        self.health.paused_until = time.monotonic() - 1  # The pause just ended
        self.plan('mcstatus.io', 'a.example', (200, MCSTATUS_ONLINE))
        with self.assertLogs('scanbot', 'INFO'):
            result = await scanbot.check_api(self.session, 'a.example')
        self.assertEqual(result['source'], 'mcstatus.io')
        self.assertEqual(self.health.failures, 0)

    async def test_a_failure_right_after_the_pause_pauses_again_without_a_new_warning(self):
        self.health.failures = scanbot.API_FAILURE_THRESHOLD
        self.health.paused_until = time.monotonic() - 1
        self.plan('mcstatus.io', 'a.example', (500, {}))
        with self.assertNoLogs('scanbot', 'WARNING'):
            await scanbot.check_api(self.session, 'a.example')
        self.assertTrue(self.health.paused())

    async def test_bedrock_servers_have_no_second_opinion(self):
        self.plan('mcstatus.io', 'bedrock.example', (500, {}))
        result = await scanbot.check_api(self.session, 'bedrock.example', 'bedrock')
        self.assertIs(result, scanbot.UNCHECKED)
        self.assertEqual(self.services.asked('mcsrvstat.us'), [])

    async def test_both_services_failing_is_unchecked_not_offline(self):
        self.plan('mcstatus.io', 'a.example', (500, {}))
        self.plan('mcsrvstat.us', 'a.example', (502, {}))
        self.assertIs(await scanbot.check_api(self.session, 'a.example'), scanbot.UNCHECKED)

    async def test_mcsrvstat_saying_offline_is_offline(self):
        self.plan('mcstatus.io', 'a.example', (500, {}))
        self.plan('mcsrvstat.us', 'a.example', (200, MCSRVSTAT_OFFLINE))
        self.assertIsNone(await scanbot.check_api(self.session, 'a.example'))


class PacerTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        patcher = mock.patch.object(scanbot, 'time', types.SimpleNamespace(monotonic=self.clock, time=time.time))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.pacer = scanbot.Pacer('mcstatus.io')

    def slow_down(self, sent_at=None):
        self.pacer.slow_down(0.2, self.clock.now if sent_at is None else sent_at)

    def test_each_rate_limit_doubles_the_spacing_up_to_eight_times(self):
        self.assertEqual(self.pacer.spacing(0.2), 0.2)
        factors = []
        with self.assertLogs('scanbot', 'WARNING') as logs:
            for _ in range(5):
                self.clock.now += 1
                self.slow_down()
                factors.append(self.pacer.factor)
        self.assertEqual(factors, [2, 4, 8, 8, 8])
        self.assertEqual(self.pacer.spacing(0.2), 1.6)
        self.assertEqual(len(logs.output), 3)  # Once per step up, not again at the top

    def test_refusals_of_requests_sent_before_the_slowdown_count_once(self):
        with self.assertLogs('scanbot', 'WARNING'):
            self.clock.now = 1001
            self.slow_down(sent_at=1000.5)
        for sent in (1000.6, 1000.8, 1000.99):  # Sent at the old speed, refused a moment later
            self.clock.now += 0.1
            self.slow_down(sent_at=sent)
        self.assertEqual(self.pacer.factor, 2)
        with self.assertLogs('scanbot', 'WARNING'):
            self.slow_down(sent_at=self.clock.now)  # Sent after the slowdown and still refused
        self.assertEqual(self.pacer.factor, 4)

    def test_the_next_request_waits_for_the_longer_spacing(self):
        with self.assertLogs('scanbot', 'WARNING'):
            self.slow_down()
        self.assertGreaterEqual(self.pacer._next, self.clock.now + 0.4)

    def test_back_to_normal_after_a_minute_without_a_rate_limit(self):
        with self.assertLogs('scanbot', 'WARNING'):
            self.slow_down()
            self.slow_down(sent_at=self.clock.now + 0.01)
        self.clock.now += 59
        self.assertAlmostEqual(self.pacer.spacing(0.2), 0.8)
        self.clock.now += 1
        with self.assertLogs('scanbot', 'INFO') as logs:
            self.assertEqual(self.pacer.spacing(0.2), 0.2)
        self.assertIn("mcstatus.io hasn't rate-limited the bot for 60 s; back to one request every 0.2 s",
                      logs.output[0])
        self.assertEqual(self.pacer.factor, 1)


class ApiHealthTests(unittest.TestCase):
    def test_four_failures_dont_pause(self):
        health = scanbot.ApiHealth()
        for _ in range(4):
            health.failed()
        self.assertFalse(health.paused())

    def test_an_answer_resets_the_count(self):
        health = scanbot.ApiHealth()
        for _ in range(4):
            health.failed()
        health.answered()
        health.failed()
        self.assertEqual(health.failures, 1)
        self.assertFalse(health.paused())


class ParseMcsrvstatTests(unittest.TestCase):
    def test_an_online_answer(self):
        r = scanbot.parse_mcsrvstat_status('play.example.com', MCSRVSTAT_ONLINE)
        self.assertEqual((r['ip'], r['address'], r['players'], r['max'], r['names']),
                         ('play.example.com', '5.6.7.8', 2, 100, ['Alex', 'Steve']))
        self.assertEqual(r['motd'], '>>> Welcome  to the server')  # Lines joined, HTML entities decoded
        self.assertEqual(r['version'], 'Paper 1.21.4')                # Colour codes removed
        self.assertEqual((r['protocol'], r['software'], r['plugins'], r['eula_blocked']),
                         (769, 'Paper', ['EssentialsX'], False))
        self.assertEqual((r['modded'], r['mod_count'], r['source'], r['edition']), (True, 1, 'mcsrvstat.us', 'Java'))

    def test_an_offline_answer_is_none_even_with_an_ip(self):
        self.assertIsNone(scanbot.parse_mcsrvstat_status('1.2.3.4', MCSRVSTAT_OFFLINE))
        self.assertIsNone(scanbot.parse_mcsrvstat_status('1.2.3.4', ['not', 'a', 'dict']))

    def test_odd_fields_are_tolerated(self):
        data = {'online': True, 'ip': 7, 'players': 'many', 'motd': 'hi', 'version': 5, 'protocol': 769,
                'mods': 'yes', 'plugins': 'all', 'software': '', 'eula_blocked': 'no'}
        r = scanbot.parse_mcsrvstat_status('a.example', data)
        self.assertEqual((r['address'], r['players'], r['names'], r['motd'], r['version']),
                         (None, 0, [], '', 'Unknown'))
        self.assertEqual((r['protocol'], r['modded'], r['plugins'], r['software'], r['eula_blocked']),
                         (None, None, None, None, None))

    def test_without_mods_or_plugins_the_server_isnt_called_vanilla(self):
        data = dict(MCSRVSTAT_ONLINE)
        del data['mods'], data['plugins']
        r = scanbot.parse_mcsrvstat_status('a.example', data)
        self.assertEqual((r['modded'], r['mod_count'], r['plugins']), (None, None, None))


class UncheckedTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_api_counts_unchecked_servers_apart_from_offline_ones(self):
        answers = {'up': scanbot.make_result('up', '1.2.3.4', 1, 10, [], '1.21', ''), 'down': None,
                   'lost1': scanbot.UNCHECKED, 'lost2': scanbot.UNCHECKED}

        async def fake_api(session, ip, edition='java'):
            return answers[ip]

        state = {'phase': '', 'done': 0, 'total': 0, 'found': 0, 'blocked': 0}  # No 'unchecked' key, as elsewhere
        results = {}
        with mock.patch.object(scanbot, 'check_api', fake_api), mock.patch.object(scanbot, 'API_DELAY', 0), \
                mock.patch.object(scanbot, 'api_pacer', scanbot.Pacer()):
            await scanbot.run_api(None, list(answers), results, state, retrying=True)
        self.assertEqual(list(results), ['up'])
        self.assertEqual((state['done'], state['found'], state['unchecked']), (4, 1, 2))

    def test_the_sentinel_is_falsy(self):
        self.assertFalse(scanbot.UNCHECKED)
        self.assertEqual(repr(scanbot.UNCHECKED), 'UNCHECKED')


class ResultsLineTests(unittest.IsolatedAsyncioTestCase):
    async def summary(self, **kwargs):
        ctx = mock.MagicMock()
        ctx.channel.send = mock.AsyncMock()
        await scanbot.send_results(ctx, [], {}, False, 10, 5.0, **kwargs)
        return ctx.channel.send.await_args.args[0]

    async def test_unchecked_servers_get_their_own_line_after_the_not_retried_one(self):
        text = await self.summary(not_retried=2, unchecked=3)
        lines = text.splitlines()
        retried = next(i for i, line in enumerate(lines) if "didn't answer a direct ping and weren't retried" in line)
        self.assertIn("3 couldn't be checked: mcstatus.io and mcsrvstat.us didn't answer, so they aren't counted as "
                      "offline", lines[retried + 1])

    async def test_bedrock_names_only_mcstatus(self):
        text = await self.summary(unchecked=1, edition='bedrock')
        self.assertIn("1 couldn't be checked: mcstatus.io didn't answer, so they aren't counted as offline", text)

    async def test_no_line_when_everything_was_checked(self):
        self.assertNotIn("couldn't be checked", await self.summary())


class WholeScanTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_scan_reports_the_servers_no_service_could_check(self):
        progress = mock.MagicMock(edit=mock.AsyncMock())
        ctx = mock.MagicMock()
        ctx.send = mock.AsyncMock(return_value=progress)
        ctx.defer = mock.AsyncMock()
        ctx.channel.send = mock.AsyncMock(return_value=progress)

        async def fake_api(session, ip, edition='java'):
            return scanbot.make_result(ip, ip, 3, 20, [], '1.21', '') if ip == '8.8.8.8' else scanbot.UNCHECKED

        data = b'8.8.8.8\n1.1.1.1\n9.9.9.9\n'
        file = types.SimpleNamespace(filename='ips.txt', size=len(data), read=mock.AsyncMock(return_value=data))
        for p in (mock.patch.object(scanbot.bot, 'direct_ok', False),  # API only, so no network is needed
                  mock.patch.object(scanbot, 'check_api', fake_api),
                  mock.patch.object(scanbot, 'batch_get_locations', mock.AsyncMock(return_value={})),
                  mock.patch.object(scanbot, 'API_DELAY', 0),
                  mock.patch.object(scanbot, 'api_pacer', scanbot.Pacer()),
                  mock.patch.object(scanbot, 'set_status', mock.AsyncMock()),
                  mock.patch.dict(scanbot.scans, clear=True),
                  mock.patch.object(scanbot, 'queue', [])):
            p.start()
            self.addCleanup(p.stop)

        await scanbot.bot.get_command('scan').callback(ctx, file)
        texts = [call.args[0] for call in ctx.channel.send.await_args_list if call.args]
        final = next(t for t in texts if 'Scan Complete' in t)
        self.assertIn("2 couldn't be checked: mcstatus.io and mcsrvstat.us didn't answer", final)
        self.assertIn('8.8.8.8', final)


if __name__ == '__main__':
    unittest.main()
