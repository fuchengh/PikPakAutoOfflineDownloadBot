import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
from discord.ext import commands
import pytest

import config
from pikpakbot import state, pipeline
from pikpakbot.bot import discord_bot as bot_module
from pikpakbot.notifier import DiscordNotifier


def interaction(user=123):
    return SimpleNamespace(user=SimpleNamespace(id=user), channel=None, channel_id=123,
        type=discord.InteractionType.component, data={},
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock(), edit_message=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()), edit_original_response=AsyncMock())


def channel(thread=False):
    result = Mock(spec=discord.Thread) if thread else Mock()
    result.archived = False
    result.send = AsyncMock(return_value=SimpleNamespace(id=100))
    result.edit = AsyncMock()
    result.message = SimpleNamespace(edit=AsyncMock())
    result.get_partial_message = Mock(return_value=result.message)
    return result


def test_retry_ack_precedes_work_and_double_click_starts_once(monkeypatch, magnet):
    tid = state.create_task(magnet=magnet, stage=state.STAGE_FAILED)
    a, b = interaction(), interaction()
    async def run_sync(func, *args):
        assert a.response.defer.await_count or b.response.defer.await_count
        await asyncio.sleep(0)
        return func(*args)
    monkeypatch.setattr(bot_module, '_run_sync', run_sync)
    async def run():
        await asyncio.gather(bot_module._handle_retry_click(a, tid), bot_module._handle_retry_click(b, tid))
    asyncio.run(run())
    assert pipeline._executor.submit.call_count == 1
    assert a.response.defer.await_count == b.response.defer.await_count == 1
    assert state.get_task(tid)['retry_child']


def test_cloud_task_without_magnet_can_retry():
    tid = state.create_task(cloud_file_id='file', account='a', source_kind='cloud', stage=state.STAGE_FAILED)
    intr = interaction()
    asyncio.run(bot_module._handle_retry_click(intr, tid))
    child = state.get_task(tid)['retry_child']
    assert state.get_task(child)['cloud_file_id'] == 'file'
    assert pipeline._executor.submit.call_count == 1


def test_progress_serialized_and_edits_without_fetch():
    async def run():
        ch = channel()
        async def send(**kwargs):
            await asyncio.sleep(0)
            return SimpleNamespace(id=100)
        ch.send.side_effect = send
        client = SimpleNamespace(get_channel=lambda _: ch)
        notify = DiscordNotifier(123)
        await asyncio.gather(notify._progress_async(client, 'task', 'first'),
                             notify._progress_async(client, 'task', 'second'))
        assert ch.send.await_count == 1
        assert ch.message.edit.await_count == 1
        assert ch.message.edit.call_args.kwargs['embed'].description == 'second'
        ch.fetch_message.assert_not_called()
    asyncio.run(run())


def test_final_progress_finishes_before_archive_and_stale_update_is_dropped():
    async def run():
        ch, order = channel(thread=True), []
        gate = asyncio.Event()
        waiting = asyncio.Event()
        async def edit(**kwargs):
            waiting.set()
            await gate.wait()
            order.append('progress')
        ch.message.edit.side_effect = edit
        ch.edit.side_effect = lambda **kw: order.append('archive')
        client = SimpleNamespace(get_channel=lambda _: ch)
        notify = DiscordNotifier(123)
        notify._progress_msgs['task'] = 100
        progress = asyncio.create_task(notify._progress_async(client, 'task', '✅ complete'))
        await waiting.wait()
        final = asyncio.create_task(notify._finalize_async(client, True, 'file'))
        await asyncio.sleep(0)
        assert not final.done()
        gate.set()
        await asyncio.gather(progress, final)
        await notify._progress_async(client, 'task', 'stale')
        assert order == ['progress', 'archive']
        ch.edit.assert_awaited_once_with(name='✅ file', archived=True)
    asyncio.run(run())


def test_retry_reuses_persisted_message_and_reopens_archived_thread():
    tid = state.create_task(discord_channel_id=123, discord_message_id=100)
    async def run():
        ch = channel(thread=True)
        ch.archived = True
        client = SimpleNamespace(get_channel=lambda _: ch)
        notify = DiscordNotifier(123)
        notify.bind_task(tid)
        await notify._progress_async(client, tid, 'retry')
        ch.edit.assert_awaited_once_with(archived=False)
        ch.send.assert_not_awaited()
        ch.get_partial_message.assert_called_once_with(100)
    asyncio.run(run())


def test_transient_edit_error_keeps_message_id_without_spamming():
    async def run():
        ch = channel()
        ch.message.edit.side_effect = RuntimeError('temporary error')
        notify = DiscordNotifier(123)
        notify._progress_msgs['task'] = 100
        await notify._progress_async(SimpleNamespace(get_channel=lambda _: ch), 'task', 'update')
        assert notify._progress_msgs['task'] == 100
        ch.send.assert_not_awaited()
    asyncio.run(run())


def test_deleted_message_is_replaced_and_saved():
    tid = state.create_task(discord_channel_id=123, discord_message_id=100)
    async def run():
        ch = channel()
        ch.message.edit.side_effect = discord.NotFound(SimpleNamespace(status=404, reason='missing'), 'missing')
        ch.send.return_value = SimpleNamespace(id=200)
        await DiscordNotifier(123)._progress_async(SimpleNamespace(get_channel=lambda _: ch), tid, 'update')
        assert state.get_task(tid)['discord_message_id'] == 200
        assert ch.send.await_count == 1
    asyncio.run(run())


def test_failed_final_message_not_archived():
    async def run():
        ch = channel(thread=True)
        ch.send.side_effect = RuntimeError('offline')
        client = SimpleNamespace(get_channel=lambda _: ch)
        notify = DiscordNotifier(123)
        await notify._progress_async(client, 'task', 'complete')
        await notify._finalize_async(client, True, 'file')
        assert 'archived' not in ch.edit.call_args.kwargs
    asyncio.run(run())


def test_other_active_task_in_same_thread_prevents_archive():
    state.create_task(discord_channel_id=123)
    async def run():
        ch = channel(thread=True)
        notify = DiscordNotifier(123)
        notify._last_delivery_ok = True
        await notify._finalize_async(SimpleNamespace(get_channel=lambda _: ch), True, 'file')
        ch.edit.assert_not_awaited()
    asyncio.run(run())


def test_slash_parser_dispatches_each_magnet(monkeypatch, magnet):
    submit = Mock()
    monkeypatch.setattr(bot_module, 'submit_magnet', submit)
    async def run():
        bot = commands.Bot(command_prefix='/', intents=discord.Intents.none())
        bot_module._register_commands(bot)
        await bot.tree.get_command('p').callback(interaction(), magnet + magnet.replace('a' * 40, 'b' * 40))
        await bot.close()
    asyncio.run(run())
    assert submit.call_count == 2


@pytest.mark.parametrize('user,allowed,owner,expected', [
    (1, [1], False, True), (2, [1], True, False), (1, [], True, True), (2, [], False, False),
])
def test_slash_and_button_access_checks(monkeypatch, user, allowed, owner, expected):
    monkeypatch.setattr(config, 'DISCORD_ADMIN_IDS', allowed)
    handler = AsyncMock()
    monkeypatch.setattr(bot_module, '_handle_retry_click', handler)
    async def run():
        bot = commands.Bot(command_prefix='/', intents=discord.Intents.none())
        bot.is_owner = AsyncMock(return_value=owner)
        bot_module._register_commands(bot)
        assert await bot.tree.interaction_check(interaction(user)) is expected
        intr = interaction(user)
        intr.data = {'custom_id': 'retry:task'}
        await bot.extra_events['on_interaction'][0](intr)
        assert handler.await_count == int(expected)
        await bot.close()
    asyncio.run(run())


def test_zero_threshold_remains_zero(monkeypatch):
    listed = Mock(return_value=[])
    monkeypatch.setattr(bot_module, 'get_stuck_tasks', listed)
    monkeypatch.setattr(bot_module, 'USER', ['a'])
    async def run():
        bot = commands.Bot(command_prefix='/', intents=discord.Intents.none())
        bot_module._register_commands(bot)
        await bot.tree.get_command('retry-list').callback(interaction(), 0)
        await bot.close()
    asyncio.run(run())
    listed.assert_called_once_with('a', 0)


def test_retry_list_query_error_is_shown_to_user(monkeypatch):
    monkeypatch.setattr(bot_module, 'get_stuck_tasks', Mock(side_effect=RuntimeError('503 unavailable')))
    monkeypatch.setattr(bot_module, 'USER', ['a'])
    async def run():
        bot = commands.Bot(command_prefix='/', intents=discord.Intents.none())
        bot_module._register_commands(bot)
        intr = interaction()
        await bot.tree.get_command('retry-list').callback(intr)
        text = intr.followup.send.call_args.args[0]
        assert '503 unavailable' in text and '查詢失敗' in text
        assert '沒有找到' not in text
        await bot.close()
    asyncio.run(run())
