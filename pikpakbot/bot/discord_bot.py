"""Discord bot scaffold.

Runs in its own daemon thread with its own asyncio loop, so the existing
sync python-telegram-bot v13 main loop is unaffected. Commands and retry
buttons are added in subsequent commits; for now this just connects and
syncs an (initially empty) slash command tree.

DiscordNotifier (in pikpakbot.notifier) is the bridge from the sync world
into this loop via asyncio.run_coroutine_threadsafe.
"""
import asyncio
import logging
import threading
from typing import Optional

import discord
from discord.ext import commands


_client: Optional[commands.Bot] = None
_client_lock = threading.Lock()


def get_client() -> Optional[commands.Bot]:
    """Return the running Discord client, or None if not yet started."""
    return _client


def is_ready() -> bool:
    return _client is not None and _client.is_ready()


def start_discord(token: str):
    """Run the Discord client. Blocking — call from a daemon thread.

    The client is exposed via get_client() once connected so DiscordNotifier
    can schedule sends from worker threads via asyncio.run_coroutine_threadsafe.
    """
    global _client

    intents = discord.Intents.default()
    # message_content is needed for prefix commands; we use slash commands so
    # this stays default-off (avoids Discord's privileged intent requirement).
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

    with _client_lock:
        _client = bot

    try:
        bot.run(token, log_handler=None)  # log_handler=None lets our logging config apply
    except Exception as e:
        logging.error(f'Discord client 結束: {e}')
    finally:
        with _client_lock:
            _client = None
