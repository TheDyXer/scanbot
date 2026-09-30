"""
Tests for the slash commands (hybrid commands: /scan and !scan share one implementation).

Run from the repository root:  python -m unittest discover -s tests
"""
import asyncio
import os
import sys
import types
import unittest
from unittest import mock

# bot.py exits at import time without a token
os.environ.setdefault('DISCORD_TOKEN', 'test-token')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import discord  # noqa: E402
from discord.ext import commands  # noqa: E402

import bot as scanbot  # noqa: E402


def http_error(cls, status):
    return cls(types.SimpleNamespace(status=status, reason='test'), 'test')


class RegistrationTests(unittest.TestCase):
    def test_slash_commands_exist(self):
        names = {c.name for c in scanbot.bot.tree.get_commands()}
        self.assertEqual(names, {'scan', 'stop', 'help'})

    def test_prefix_commands_still_work(self):
        for name in ('scan', 'check', 'stop', 'help'):
            with self.subTest(name=name):
                self.assertIsNotNone(scanbot.bot.get_command(name))
        self.assertIs(scanbot.bot.get_command('check'), scanbot.bot.get_command('scan'))

    def test_scan_takes_a_required_attachment_first(self):
        params = scanbot.bot.tree.get_command('scan').parameters
        self.assertEqual([p.name for p in params], ['file', 'edition'])
        self.assertEqual(params[0].type, discord.AppCommandOptionType.attachment)
        self.assertTrue(params[0].required)


class SettingsTests(unittest.TestCase):
    def test_env_flag(self):
        for value in ('1', 'true', 'TRUE', 'yes', ' on '):
            with self.subTest(value=value), mock.patch.dict(os.environ, {'SLASH_ONLY': value}):
                self.assertTrue(scanbot.env_flag('SLASH_ONLY'))
        for value in ('', '0', 'false', 'no', 'off', 'banana'):
            with self.subTest(value=value), mock.patch.dict(os.environ, {'SLASH_ONLY': value}):
                self.assertFalse(scanbot.env_flag('SLASH_ONLY'))
        with mock.patch.dict(os.environ, clear=True):
            self.assertFalse(scanbot.env_flag('SLASH_ONLY'))

    def test_message_content_intent_is_only_requested_for_prefix_commands(self):
        self.assertTrue(scanbot.build_intents(slash_only=False).message_content)
        self.assertFalse(scanbot.build_intents(slash_only=True).message_content)
        self.assertTrue(scanbot.build_intents(slash_only=True).dm_messages)


class SetupHookTests(unittest.IsolatedAsyncioTestCase):
    async def test_commands_are_synced_at_startup(self):
        sync = mock.AsyncMock(return_value=[object(), object(), object()])
        with mock.patch.object(scanbot.bot.tree, 'sync', sync):
            await scanbot.bot.setup_hook()
        sync.assert_awaited_once()

    async def test_sync_failure_does_not_stop_the_bot(self):
        sync = mock.AsyncMock(side_effect=http_error(discord.HTTPException, 500))
        with mock.patch.object(scanbot.bot.tree, 'sync', sync):
            await scanbot.bot.setup_hook()  # must not raise


class ErrorHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_attachment_gets_a_friendly_message(self):
        ctx = mock.MagicMock(send=mock.AsyncMock())
        error = commands.MissingRequiredAttachment(mock.MagicMock())
        await scanbot.bot.on_command_error(ctx, error)
        self.assertIn('.txt', ctx.send.await_args.args[0])

    async def test_unknown_commands_are_ignored(self):
        ctx = mock.MagicMock(send=mock.AsyncMock())
        await scanbot.bot.on_command_error(ctx, commands.CommandNotFound('nope'))
        ctx.send.assert_not_awaited()

    async def test_other_errors_are_logged_not_raised(self):
        ctx = mock.MagicMock(send=mock.AsyncMock())
        with self.assertLogs('scanbot', level='ERROR'):
            await scanbot.bot.on_command_error(ctx, commands.CommandError('boom'))


class ScanCommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        scanbot.bot.direct_ok = False  # API-only, so no network is needed
        self.progress = mock.MagicMock(edit=mock.AsyncMock())
        self.ctx = mock.MagicMock()
        self.ctx.send = mock.AsyncMock(return_value=self.progress)
        self.ctx.defer = mock.AsyncMock()
        self.ctx.channel.send = mock.AsyncMock(return_value=self.progress)
        self.scan = scanbot.bot.get_command('scan').callback

        async def fake_api(session, ip, edition='java'):
            return scanbot.make_result(ip, ip, 3, 20, ['Steve'], '1.21', 'hello')

        patches = [
            mock.patch.object(scanbot, 'check_api', fake_api),
            mock.patch.object(scanbot, 'batch_get_locations', mock.AsyncMock(return_value={})),
            mock.patch.object(scanbot, 'API_DELAY', 0),
            mock.patch.object(scanbot, 'set_status', mock.AsyncMock()),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def attachment(self, filename='ips.txt', data=b'8.8.8.8\n1.1.1.1\n'):
        return types.SimpleNamespace(filename=filename, read=mock.AsyncMock(return_value=data))

    def texts(self, mock_send):
        return [call.args[0] for call in mock_send.await_args_list if call.args]

    async def test_scan_defers_then_posts_progress_and_results_to_the_channel(self):
        await self.scan(self.ctx, self.attachment())

        self.ctx.defer.assert_awaited()
        # The interaction response is only the "started" message: its webhook stops working after 15 minutes
        self.assertEqual(len(self.ctx.send.await_args_list), 1)
        self.assertIn('Scan started', self.texts(self.ctx.send)[0])
        channel_texts = self.texts(self.ctx.channel.send)
        self.assertTrue(any('Scan Complete' in t and '8.8.8.8' in t for t in channel_texts), channel_texts)
        self.assertIn('Done', self.progress.edit.await_args.kwargs['content'])

    async def test_falls_back_to_the_interaction_when_the_channel_is_off_limits(self):
        self.ctx.channel.send.side_effect = http_error(discord.Forbidden, 403)
        await self.scan(self.ctx, self.attachment())
        self.assertTrue(any('Scan Complete' in t for t in self.texts(self.ctx.send)))

    async def test_uppercase_txt_extension_is_accepted(self):
        await self.scan(self.ctx, self.attachment(filename='IPS.TXT'))
        self.assertTrue(any('Scan Complete' in t for t in self.texts(self.ctx.channel.send)))

    async def test_other_file_types_are_refused(self):
        attachment = self.attachment(filename='ips.csv')
        await self.scan(self.ctx, attachment)
        self.assertIn('.txt', self.texts(self.ctx.send)[-1])
        attachment.read.assert_not_awaited()
        self.ctx.channel.send.assert_not_awaited()

    async def test_a_second_scan_by_the_same_person_is_refused(self):
        scanbot.scans[self.ctx.author.id] = scanbot.Scan(self.ctx.author, None)
        self.addCleanup(scanbot.scans.clear)
        await self.scan(self.ctx, self.attachment())
        self.assertIn('already have a scan', self.texts(self.ctx.send)[0])
        self.ctx.channel.send.assert_not_awaited()


class StopCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_stop_only_acts_while_you_have_a_scan(self):
        stop = scanbot.bot.get_command('stop').callback
        ctx = mock.MagicMock(send=mock.AsyncMock())
        await stop(ctx)
        self.assertIn("don't have a scan", ctx.send.await_args.args[0])

        scan = scanbot.Scan(ctx.author, None)
        scan.running = True
        scanbot.scans[ctx.author.id] = scan
        self.addCleanup(scanbot.scans.clear)
        await stop(ctx)
        self.assertTrue(scan.stop.is_set())
        self.assertIn('Stop requested', ctx.send.await_args.args[0])


if __name__ == '__main__':
    unittest.main()
