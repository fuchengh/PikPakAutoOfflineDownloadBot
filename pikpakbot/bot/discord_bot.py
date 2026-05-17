"""Discord bot — feature parity with the Telegram bot.

Runs in its own daemon thread with its own asyncio loop, so the existing
sync python-telegram-bot v13 main loop is unaffected.

Slash commands mirror the TG commands: /p, /clean, /retry, /path, /status,
/history, /help, /account.

Slow sync work (login, PikPak API calls, file cleanup) is dispatched onto
the default executor with loop.run_in_executor so we don't block Discord's
event loop. Pipeline downloads remain in their own threading.Thread, same
shape as the TG path.
"""
import asyncio
import logging
import threading
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import config
from config import USER, PASSWORD, AUTO_DELETE, record_config
from pikpakbot import state
from pikpakbot.notifier import DiscordNotifier
from pikpakbot.pikpak_client import (
    login,
    registerFuc,
    pikpak_headers,
    get_folder_all,
    get_stuck_tasks,
    retry_stuck_tasks,
    delete_files,
    delete_trash,
    delete_offline_tasks,
    empty_trash,
)
from pikpakbot.pipeline import (
    process_magnet,
    thread_list,
    batch_results,
    batch_lock,
    check_download_thread_status,
)


_client: Optional[commands.Bot] = None
_client_lock = threading.Lock()


def get_client() -> Optional[commands.Bot]:
    return _client


def is_ready() -> bool:
    return _client is not None and _client.is_ready()


_STAGE_LABELS = {
    state.STAGE_QUEUED: '⏳ 待處理',
    state.STAGE_OFFLINE: '☁️ PikPak 離線中',
    state.STAGE_DOWNLOAD: '⬇️ Aria2 下載中',
    state.STAGE_CLEANUP: '🧹 清理中',
    state.STAGE_COMPLETE: '✅ 完成',
    state.STAGE_FAILED: '❌ 失敗',
    state.STAGE_CANCELED: '⛔ 已取消',
}


def _stage_label(stage):
    return _STAGE_LABELS.get(stage, stage)


def _age(ts):
    import time
    delta = max(0, int(time.time() - ts))
    if delta < 60:
        return f'{delta}s'
    if delta < 3600:
        return f'{delta // 60}m'
    if delta < 86400:
        return f'{delta // 3600}h'
    return f'{delta // 86400}d'


async def _run_sync(func, *args, **kwargs):
    """Run a blocking sync function on the default executor."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: func(*args, **kwargs))


def _register_commands(bot: commands.Bot):
    """Wire all slash commands onto the bot's tree. Called once before run()."""

    @bot.tree.command(name='help', description='指令說明')
    async def help_cmd(interaction: discord.Interaction):
        await interaction.response.send_message(
            "**指令簡介**\n"
            "/p `<magnet>` — 自動離線+aria2下載+釋放雲端空間\n"
            "/status — 查看進行中任務\n"
            "/history `[n]` — 查看最近 n 個任務\n"
            "/clean `<mode>` — 清空雲端硬碟+離線記錄\n"
            "/path — 管理 PikPak 離線下載路徑\n"
            "/retry `[threshold]` — 重試卡住的任務\n"
            "/retry-list `[threshold]` — 列出卡住的任務\n"
            "/account-list — 列出帳號\n"
            "/account-toggle `<account>` `<on|off>` — 切換自動清理\n",
            ephemeral=False,
        )

    # ---- /p submit magnets ----
    @bot.tree.command(name='p', description='Submit magnet URLs for offline + aria2 download')
    @app_commands.describe(magnets='Magnet links (whitespace-separated). 可以一次貼多個。',
                            offline_path='Optional override for the PikPak offline path (absolute)')
    async def p_cmd(interaction: discord.Interaction, magnets: str, offline_path: Optional[str] = None):
        argv = magnets.split()
        if not argv:
            await interaction.response.send_message('Usage: /p <magnet>...', ephemeral=True)
            return
        await interaction.response.defer()

        # Resolve offline_path: explicit param > config.PIKPAK_OFFLINE_PATH (if not default)
        resolved_offline = offline_path
        if not resolved_offline and str(config.PIKPAK_OFFLINE_PATH) not in ("None", "/My Pack"):
            resolved_offline = config.PIKPAK_OFFLINE_PATH

        import uuid
        batch_id = str(uuid.uuid4())[:8]
        with batch_lock:
            batch_results[batch_id] = {'total': len(argv), 'processed': 0, 'results': []}

        notifier = DiscordNotifier(interaction.channel_id)
        for each_magnet in argv:
            t = threading.Thread(
                target=process_magnet,
                args=[notifier, each_magnet, resolved_offline, batch_id, None, None],
            )
            thread_list.append(t)
            t.start()

        path_note = f' (path={resolved_offline})' if resolved_offline else ''
        await interaction.followup.send(f'📥 已加入 {len(argv)} 個磁力下載任務{path_note}')

    # ---- /status ----
    @bot.tree.command(name='status', description='List in-flight tasks')
    async def status_cmd(interaction: discord.Interaction):
        tasks = state.list_active()
        if not tasks:
            await interaction.response.send_message('✅ 目前沒有進行中的任務')
            return
        lines = [f'📋 **進行中任務 ({len(tasks)})**']
        for t in tasks:
            name = t.get('name') or '(尚未取得名稱)'
            acc = (t.get('account') or '').split('@')[0] or '-'
            prog = t.get('progress', 0) or 0
            prog_str = f' {prog}%' if prog else ''
            age = _age(t['created_at'])
            lines.append(
                f"\n`{t['task_id']}` {_stage_label(t['stage'])}{prog_str}"
                f"\n  {name}"
                f"\n  帳號: {acc} | 已花費 {age}"
            )
        await interaction.response.send_message('\n'.join(lines)[:2000])

    # ---- /history ----
    @bot.tree.command(name='history', description='Recent terminal tasks (default 20)')
    @app_commands.describe(n='How many recent tasks to show (1-50, default 20)')
    async def history_cmd(interaction: discord.Interaction, n: Optional[int] = 20):
        limit = max(1, min(50, n or 20))
        tasks = state.list_recent(limit=limit)
        if not tasks:
            await interaction.response.send_message('📜 沒有任務記錄')
            return
        lines = [f'📜 **最近 {len(tasks)} 個任務**']
        for t in tasks:
            name = t.get('name') or t.get('magnet') or '(unknown)'
            if len(name) > 60:
                name = name[:57] + '...'
            acc = (t.get('account') or '').split('@')[0] or '-'
            end_ts = t.get('completed_at') or t.get('updated_at')
            age = _age(end_ts)
            err = t.get('error')
            line = f"{_stage_label(t['stage'])} `{t['task_id']}` {name} ({acc}) — {age} ago"
            if err and t['stage'] == state.STAGE_FAILED:
                line += f"\n  ↳ {err}"
            lines.append(line)
        await interaction.response.send_message('\n'.join(lines)[:2000])

    # ---- /clean ----
    @bot.tree.command(name='clean', description='Clear cloud files / offline records')
    @app_commands.describe(mode='all / deep / tasks / tasks_error / <account-name>')
    async def clean_cmd(interaction: discord.Interaction, mode: str):
        if check_download_thread_status():
            await interaction.response.send_message('其他指令正在運行，為避免衝突，請稍後再試~', ephemeral=True)
            return
        await interaction.response.defer()
        notifier = DiscordNotifier(interaction.channel_id)

        def _do_clean():
            if mode in ('d', 'deep'):
                _clean_deep(notifier)
            elif mode in ('t', 'tasks'):
                _clean_tasks(notifier, phase_filter=None)
            elif mode == 'tasks_error':
                _clean_tasks(notifier, phase_filter='PHASE_TYPE_ERROR')
            elif mode in ('a', 'all'):
                _clean_all_accounts(notifier)
            elif mode in USER:
                _clean_specific(mode, notifier)
            else:
                notifier.send(f'未知 mode: {mode}（支援 all / deep / tasks / tasks_error / <account>）')

        threading.Thread(target=_do_clean, daemon=True).start()
        await interaction.followup.send(f'🔄 已開始清理 (mode={mode})')

    # ---- /retry ----
    @bot.tree.command(name='retry', description='Retry stuck offline tasks')
    @app_commands.describe(threshold='Progress threshold (0-100, default 90)')
    async def retry_cmd(interaction: discord.Interaction, threshold: Optional[int] = 90):
        threshold = max(0, min(100, threshold or 90))
        await interaction.response.defer()
        notifier = DiscordNotifier(interaction.channel_id)

        def _do_retry():
            total_success = 0
            total_fail = 0
            all_lines = []
            for account in USER:
                s, f, results = retry_stuck_tasks(account, threshold, delete_cloud_files=True, notifier=notifier)
                total_success += s
                total_fail += f
                for r in results:
                    icon = '✅' if r['status'] == 'success' else '❌'
                    all_lines.append(f"{icon} {r['name']}")
            if total_success + total_fail == 0:
                notifier.send(f'✅ 沒有找到進度 >= {threshold}% 的卡住任務')
                return
            msg = f"📋 **重試結果**\n✅ 成功: {total_success}  ❌ 失敗: {total_fail}\n" + '\n'.join(all_lines)
            notifier.send(msg)

        threading.Thread(target=_do_retry, daemon=True).start()
        await interaction.followup.send(f'🔄 正在重試進度 >= {threshold}% 的卡住任務...')

    @bot.tree.command(name='retry-list', description='List stuck offline tasks (no action)')
    @app_commands.describe(threshold='Progress threshold (0-100, default 90)')
    async def retry_list_cmd(interaction: discord.Interaction, threshold: Optional[int] = 90):
        threshold = max(0, min(100, threshold or 90))
        await interaction.response.defer()

        def _do_list():
            lines = [f'📋 **卡住的任務列表** (進度 >= {threshold}%)']
            total = 0
            for account in USER:
                stuck = get_stuck_tasks(account, threshold)
                if stuck:
                    lines.append(f"\n**帳號: {account}**")
                    for t in stuck:
                        lines.append(f"  • {t['name']} ({t['progress']}%)")
                    total += len(stuck)
            if total == 0:
                lines.append('\n✅ 沒有找到卡住的任務')
            else:
                lines.append(f'\n共 {total} 個任務卡住')
            return '\n'.join(lines)[:2000]

        text = await _run_sync(_do_list)
        await interaction.followup.send(text)

    # ---- /path ----
    @bot.tree.command(name='path', description='Manage PikPak offline download path')
    @app_commands.describe(action='info / default / set', value='Absolute path when action=set')
    async def path_cmd(interaction: discord.Interaction, action: Optional[str] = 'info',
                       value: Optional[str] = None):
        if action == 'info':
            if config.PIKPAK_OFFLINE_PATH == 'None':
                await interaction.response.send_message('當前離線下載路徑為預設路徑：`/My Pack`')
            else:
                await interaction.response.send_message(f'當前離線下載路徑為：`{config.PIKPAK_OFFLINE_PATH}`')
            return
        if action == 'default':
            config.PIKPAK_OFFLINE_PATH = 'None'
            record_config()
            await interaction.response.send_message('已恢復預設路徑：`/My Pack`')
            return
        if action == 'set':
            import os as _os
            if not value or not _os.path.isabs(value):
                await interaction.response.send_message('value 必須是絕對路徑，例如 /downloads', ephemeral=True)
                return
            config.PIKPAK_OFFLINE_PATH = value
            record_config()
            await interaction.response.send_message(f'已設置離線下載路徑：`{config.PIKPAK_OFFLINE_PATH}`')
            return
        await interaction.response.send_message('action 必須是 info / default / set', ephemeral=True)

    # ---- /account-list ----
    @bot.tree.command(name='account-list', description='List PikPak accounts')
    async def account_list_cmd(interaction: discord.Interaction):
        if not USER:
            await interaction.response.send_message('沒有帳號')
            return
        lines = ['**帳號列表**']
        for u in USER:
            auto = AUTO_DELETE.get(u, 'True (預設)')
            lines.append(f"  • `{u}` — auto-delete: {auto}")
        await interaction.response.send_message('\n'.join(lines)[:2000])

    # ---- /account-add ----
    @bot.tree.command(name='account-add', description='Add a PikPak account (prepend to list)')
    @app_commands.describe(account='Account (email/phone)', password='Account password')
    async def account_add_cmd(interaction: discord.Interaction, account: str, password: str):
        USER.insert(0, account)
        PASSWORD.insert(0, password)
        pikpak_headers.insert(0, None)
        record_config()
        await interaction.response.send_message(f'已加入帳號 `{account}`', ephemeral=True)

    # ---- /account-delete ----
    @bot.tree.command(name='account-delete', description='Remove a PikPak account')
    @app_commands.describe(account='Account to remove')
    async def account_delete_cmd(interaction: discord.Interaction, account: str):
        try:
            idx = USER.index(account)
        except ValueError:
            await interaction.response.send_message(f'帳號 {account} 不存在', ephemeral=True)
            return
        USER.pop(idx)
        PASSWORD.pop(idx)
        pikpak_headers.pop(idx)
        AUTO_DELETE.pop(account, None)
        record_config()
        await interaction.response.send_message(f'已刪除帳號 `{account}`')

    # ---- /account-toggle ----
    @bot.tree.command(name='account-toggle', description='Toggle auto-delete for an account')
    @app_commands.describe(account='Account name', mode='on or off')
    async def account_toggle_cmd(interaction: discord.Interaction, account: str, mode: str):
        if account not in USER:
            await interaction.response.send_message(f'帳號 {account} 不存在', ephemeral=True)
            return
        if mode not in ('on', 'off'):
            await interaction.response.send_message('mode 必須是 on 或 off', ephemeral=True)
            return
        AUTO_DELETE[account] = 'True' if mode == 'on' else 'False'
        record_config()
        await interaction.response.send_message(f'帳號 `{account}` 自動清理已設為 {mode}')

    # ---- /account-new (register a free PikPak account) ----
    @bot.tree.command(name='account-new', description='Register a free PikPak account')
    async def account_new_cmd(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        register = await _run_sync(registerFuc)
        if not register:
            await interaction.followup.send('註冊失敗，請重試！', ephemeral=True)
            return
        USER.insert(0, register['account'])
        PASSWORD.insert(0, register['password'])
        pikpak_headers.insert(0, None)
        record_config()
        await interaction.followup.send(f"已註冊新帳號 `{register['account']}`", ephemeral=True)


# ---- Sync helpers used by /clean (run in threads so we don't block) ----

def _clean_deep(notifier):
    for acc in USER:
        login(acc)
        parts = []
        all_ids = list(get_folder_all(acc))
        if all_ids:
            delete_files(all_ids, acc, mode='all')
            parts.append(f"已刪除 {len(all_ids)} 個檔案")
        if empty_trash(acc):
            parts.append("回收站已清空")
        success, fail = delete_offline_tasks(acc)
        if success > 0:
            parts.append(f"已清理 {success} 個離線任務記錄")
        notifier.send(f"帳號 {acc} 深度清理完成:\n" + '\n'.join(f'  ✅ {p}' for p in parts) if parts
                      else f"帳號 {acc} 無需清理")


def _clean_tasks(notifier, phase_filter):
    for acc in USER:
        login(acc)
        success, fail = delete_offline_tasks(acc, phase_filter=phase_filter)
        if success or fail:
            notifier.send(f"帳號 {acc} 離線任務記錄清理: ✅ {success}, ❌ {fail}")
        else:
            notifier.send(f"帳號 {acc} 沒有需要清理的離線任務記錄")


def _clean_all_accounts(notifier):
    for acc in USER:
        _clean_specific(acc, notifier)


def _clean_specific(account, notifier):
    login(account)
    parts = []
    all_ids = list(get_folder_all(account))
    if all_ids:
        delete_files(all_ids, account, mode='all')
        delete_trash(all_ids, account, mode='all')
        parts.append(f"已刪除 {len(all_ids)} 個檔案")
    success, fail = delete_offline_tasks(account, phase_filter='PHASE_TYPE_ERROR')
    if success > 0:
        parts.append(f"已清理 {success} 個失敗的離線任務記錄")
    notifier.send(f"帳號 {account} 清空完成:\n" + '\n'.join(f'  ✅ {p}' for p in parts) if parts
                  else f"帳號 {account} 雲端硬碟無需清空")


def start_discord(token: str):
    """Run the Discord client. Blocking — call from a daemon thread."""
    global _client

    intents = discord.Intents.default()
    bot = commands.Bot(command_prefix='/', intents=intents, help_command=None)

    @bot.event
    async def on_ready():
        logging.info(f'Discord 已連接: {bot.user} (id={bot.user.id})')
        try:
            synced = await bot.tree.sync()
            logging.info(f'Discord: 同步了 {len(synced)} 個 slash command')
        except Exception as e:
            logging.error(f'Discord slash command 同步失敗: {e}')

    @bot.event
    async def on_error(event, *args, **kwargs):
        logging.exception(f'Discord 事件 {event} 發生未預期錯誤')

    _register_commands(bot)

    with _client_lock:
        _client = bot

    try:
        bot.run(token, log_handler=None)
    except Exception as e:
        logging.error(f'Discord client 結束: {e}')
    finally:
        with _client_lock:
            _client = None
