"""
Tests for the slash commands (hybrid commands: /scan and !scan share one implementation).

Run from the repository root:  python -m unittest discover -s tests
"""
import asyncio
import io
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
        self.assertEqual(names, {'scan', 'stop', 'help', 'rescan', 'diff'})

    def test_prefix_commands_still_work(self):
        for name in ('scan', 'check', 'stop', 'help', 'rescan', 'diff'):
            with self.subTest(name=name):
                self.assertIsNotNone(scanbot.bot.get_command(name))
        self.assertIs(scanbot.bot.get_command('check'), scanbot.bot.get_command('scan'))

    def test_scan_takes_an_attachment_or_a_target_both_optional(self):
        params = scanbot.bot.tree.get_command('scan').parameters
        self.assertEqual([p.name for p in params], ['file', 'target', 'edition', 'api', 'confirm'])
        self.assertEqual(params[0].type, discord.AppCommandOptionType.attachment)
        self.assertEqual(params[1].type, discord.AppCommandOptionType.string)
        self.assertFalse(any(p.required for p in params))


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

    async def test_other_errors_are_logged_and_the_user_is_told(self):
        # After defer() a slash command would otherwise sit on "thinking..." forever
        ctx = mock.MagicMock(send=mock.AsyncMock())
        with self.assertLogs('scanbot', level='ERROR'):
            await scanbot.bot.on_command_error(ctx, commands.CommandError('boom'))
        self.assertIn('went wrong', ctx.send.await_args.args[0])

    async def test_failing_to_tell_the_user_is_not_fatal(self):
        ctx = mock.MagicMock(send=mock.AsyncMock(side_effect=http_error(discord.HTTPException, 500)))
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

    def attachment(self, filename='ips.txt', data=b'8.8.8.8\n1.1.1.1\n', size=None):
        return types.SimpleNamespace(filename=filename, size=len(data) if size is None else size,
                                     read=mock.AsyncMock(return_value=data))

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

    async def test_oversized_files_are_refused_before_they_are_read(self):
        attachment = self.attachment(size=scanbot.MAX_FILE_BYTES + 1)
        await self.scan(self.ctx, attachment)
        self.assertIn('too big', self.texts(self.ctx.send)[-1])
        attachment.read.assert_not_awaited()
        self.ctx.channel.send.assert_not_awaited()

    async def test_a_file_at_the_size_limit_is_accepted(self):
        await self.scan(self.ctx, self.attachment(size=scanbot.MAX_FILE_BYTES))
        self.assertTrue(any('Scan Complete' in t for t in self.texts(self.ctx.channel.send)))

    async def test_utf8_bom_does_not_cost_the_first_server(self):
        # Some editors save "UTF-8 with BOM"; the marker used to glue itself to the first line
        await self.scan(self.ctx, self.attachment(data=b'\xef\xbb\xbf8.8.8.8\n1.1.1.1\n'))
        start = self.texts(self.ctx.send)[0]
        self.assertIn('2 IPs', start)
        self.assertNotIn('invalid', start)

    async def test_a_file_that_is_not_utf8_gets_a_plain_answer(self):
        await self.scan(self.ctx, self.attachment(data=b'\xff\xfe8\x00.\x008\x00'))  # UTF-16, as Notepad used to save
        self.assertIn("isn't UTF-8 text", self.texts(self.ctx.send)[-1])
        self.ctx.channel.send.assert_not_awaited()

    async def test_a_failed_download_gets_a_plain_answer(self):
        attachment = self.attachment()
        attachment.read.side_effect = http_error(discord.NotFound, 404)
        with self.assertLogs('scanbot', level='WARNING'):
            await self.scan(self.ctx, attachment)
        self.assertIn("Couldn't download the attachment", self.texts(self.ctx.send)[-1])
        self.ctx.channel.send.assert_not_awaited()

    async def test_a_scan_survives_losing_its_messages(self):
        # No permission in the channel, and the slash command's reply stops working after the first message
        self.ctx.channel.send.side_effect = http_error(discord.Forbidden, 403)
        self.ctx.send.side_effect = [self.progress] + [http_error(discord.HTTPException, 401)] * 10
        with self.assertLogs('scanbot', level='WARNING') as logs:
            await self.scan(self.ctx, self.attachment())
        self.assertIn('message lost', "\n".join(logs.output))
        self.assertNotIn(self.ctx.author.id, scanbot.scans)

    async def test_a_second_scan_by_the_same_person_is_refused(self):
        scanbot.scans[self.ctx.author.id] = scanbot.Scan(self.ctx.author, None)
        self.addCleanup(scanbot.scans.clear)
        await self.scan(self.ctx, self.attachment())
        self.assertIn('already have a scan', self.texts(self.ctx.send)[0])
        self.ctx.channel.send.assert_not_awaited()


class SendChannelTests(unittest.IsolatedAsyncioTestCase):
    async def test_fallback_resends_files_from_the_start(self):
        # The failed upload already read the files to the end; without a rewind they arrive empty
        file = discord.File(io.BytesIO(b'hello'), filename='a.txt')
        position = {}

        async def failing_channel_send(*args, **kwargs):
            kwargs['files'][0].fp.read()
            raise http_error(discord.Forbidden, 403)

        async def fallback_send(*args, **kwargs):
            position['at'] = kwargs['files'][0].fp.tell()

        ctx = mock.MagicMock(send=fallback_send)
        ctx.channel.send = failing_channel_send
        await scanbot.send_channel(ctx, 'results', files=[file])
        self.assertEqual(position['at'], 0)

    async def test_an_expired_reply_is_logged_not_raised(self):
        ctx = mock.MagicMock(send=mock.AsyncMock(side_effect=http_error(discord.HTTPException, 401)))
        ctx.channel.send = mock.AsyncMock(side_effect=http_error(discord.Forbidden, 403))
        with self.assertLogs('scanbot', level='WARNING') as logs:
            self.assertIsNone(await scanbot.send_channel(ctx, 'hello'))
        self.assertIn('message lost', logs.output[0])

    async def test_plain_messages_still_fall_back(self):
        ctx = mock.MagicMock(send=mock.AsyncMock())
        ctx.channel.send = mock.AsyncMock(side_effect=http_error(discord.Forbidden, 403))
        await scanbot.send_channel(ctx, 'hello')
        ctx.send.assert_awaited_once_with('hello')


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
