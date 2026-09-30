"""
Tests for the country flags: keeping the offline database fresh while the bot runs, and ip-api.com's retry.

Run from the repository root:  python -m unittest discover -s tests
"""
import asyncio
import os
import sys
import types
import unittest
from unittest import mock

import aiohttp

# bot.py exits at import time without a token
os.environ.setdefault('DISCORD_TOKEN', 'test-token')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import bot  # noqa: E402


class FakeResponse:
    def __init__(self, status, data=None):
        self.status = status
        self.data = data or []

    async def json(self):
        return self.data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    """Answers each POST with the next response in the list (an exception is raised instead)."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.posts = []

    def post(self, url, json=None, timeout=None):
        self.posts.append([entry['query'] for entry in json])
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def answer(ips, code='US'):
    return FakeResponse(200, [{'query': ip, 'countryCode': code} for ip in ips])


class IpApiRetryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        for p in (mock.patch.object(bot, 'GEO_DELAY', 0), mock.patch.object(bot, 'geo_pacer', bot.Pacer())):
            p.start()
            self.addCleanup(p.stop)

    async def test_a_rate_limited_batch_is_retried_once(self):
        session = FakeSession(FakeResponse(429), answer(['1.2.3.4']))
        with self.assertLogs('scanbot', level='INFO') as logs:
            locations = await bot.batch_get_locations(session, ['1.2.3.4'])
        self.assertEqual(locations, {'1.2.3.4': 'US'})
        self.assertEqual(len(session.posts), 2)
        self.assertIn('retrying once', logs.output[0])

    async def test_a_network_error_is_retried_once(self):
        session = FakeSession(aiohttp.ClientConnectionError('reset'), answer(['1.2.3.4']))
        with self.assertLogs('scanbot', level='INFO'):
            self.assertEqual(await bot.batch_get_locations(session, ['1.2.3.4']), {'1.2.3.4': 'US'})

    async def test_a_second_failure_gives_up_on_that_batch_only(self):
        ips = [f"1.2.3.{n}" for n in range(150)]  # Two batches: 100 and 50
        session = FakeSession(FakeResponse(429), FakeResponse(503), answer(ips[100:], 'DE'))
        with self.assertLogs('scanbot', level='INFO') as logs:
            locations = await bot.batch_get_locations(session, ips)
        self.assertEqual(locations, {ip: 'DE' for ip in ips[100:]})
        self.assertEqual([len(p) for p in session.posts], [100, 100, 50])
        self.assertTrue(any('WARNING' in line and 'HTTP 503' in line for line in logs.output), logs.output)

    async def test_a_client_error_is_not_retried(self):
        session = FakeSession(FakeResponse(400), answer(['1.2.3.4']))
        with self.assertLogs('scanbot', level='WARNING'):
            self.assertEqual(await bot.batch_get_locations(session, ['1.2.3.4']), {})
        self.assertEqual(len(session.posts), 1)

    async def test_stop_during_the_retry_wait_ends_it(self):
        stop = asyncio.Event()

        class StoppingSession(FakeSession):
            def post(self, url, json=None, timeout=None):
                stop.set()  # /stop arrives while the first try is out
                return super().post(url, json, timeout)

        session = StoppingSession(FakeResponse(429), answer(['1.2.3.4']))
        with self.assertLogs('scanbot', level='INFO'):
            self.assertEqual(await bot.batch_get_locations(session, ['1.2.3.4'], stop=stop), {})
        self.assertEqual(len(session.posts), 1)


class ApiSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_requests_say_who_is_asking(self):
        async with bot.api_session() as session:
            self.assertEqual(session.headers['User-Agent'], bot.USER_AGENT)


class DatabaseRefreshTests(unittest.IsolatedAsyncioTestCase):
    async def test_an_old_or_missing_database_is_loaded_again(self):
        for age in (bot.GEO_DB_MAX_AGE_DAYS + 1, None):
            with self.subTest(age=age), mock.patch.object(bot, 'geo_db_age_days', return_value=age), \
                    mock.patch.object(bot, 'load_geo_db', mock.AsyncMock()) as load:
                self.assertTrue(await bot.refresh_geo_db_if_old())
                load.assert_awaited_once()

    async def test_a_database_the_bot_cannot_replace_is_left_alone(self):
        # In Docker it's in the image (/app, owned by root): downloading a new one every day would be for nothing
        with mock.patch.object(bot, 'geo_db_age_days', return_value=bot.GEO_DB_MAX_AGE_DAYS + 1),                 mock.patch.object(bot.os, 'access', return_value=False),                 mock.patch.object(bot, 'load_geo_db', mock.AsyncMock()) as load:
            self.assertFalse(await bot.refresh_geo_db_if_old())
            load.assert_not_awaited()

    async def test_a_fresh_database_is_left_alone(self):
        with mock.patch.object(bot, 'geo_db_age_days', return_value=bot.GEO_DB_MAX_AGE_DAYS - 1), \
                mock.patch.object(bot, 'load_geo_db', mock.AsyncMock()) as load:
            self.assertFalse(await bot.refresh_geo_db_if_old())
            load.assert_not_awaited()

    async def test_the_new_database_replaces_the_old_one_which_is_then_closed(self):
        old = mock.MagicMock()
        new = mock.MagicMock()
        new.metadata.return_value = types.SimpleNamespace(build_epoch=1_790_000_000)
        with mock.patch.object(bot, 'geo_db', old), mock.patch.object(bot, 'geo_db_age_days', return_value=1), \
                mock.patch.object(bot.maxminddb, 'open_database', return_value=new):
            with self.assertLogs('scanbot', level='INFO'):
                await bot.load_geo_db()
            self.assertIs(bot.geo_db, new)
        old.close.assert_called_once()

    async def test_a_new_database_that_does_not_open_keeps_the_old_one(self):
        old = mock.MagicMock()
        with mock.patch.object(bot, 'geo_db', old), mock.patch.object(bot, 'geo_db_age_days', return_value=1), \
                mock.patch.object(bot.maxminddb, 'open_database', side_effect=OSError('broken')):
            with self.assertLogs('scanbot', level='WARNING') as logs:
                await bot.load_geo_db()
            self.assertIs(bot.geo_db, old)
        old.close.assert_not_called()
        self.assertIn('still using the old one', logs.output[0])

    async def test_the_watcher_checks_the_age_after_each_interval(self):
        checked = asyncio.Event()

        async def refresh():
            checked.set()

        with mock.patch.object(bot, 'GEO_DB_CHECK', 0.01), mock.patch.object(bot, 'refresh_geo_db_if_old', refresh):
            watcher = asyncio.create_task(bot.watch_geo_db())
            await asyncio.wait_for(checked.wait(), 2)
            watcher.cancel()


if __name__ == '__main__':
    unittest.main()
