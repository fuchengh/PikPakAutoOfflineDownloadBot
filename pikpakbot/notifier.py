"""Notifier abstraction so pipeline code stays bot-agnostic.

Pipeline functions (process_magnet, record_batch_result, startup_recovery,
stuck_task_watchdog) used to call `context.bot.send_message(...)` or
`updater.bot.send_message(...)` directly. After this abstraction:

- TG handlers build a TelegramNotifier bound to the originating chat and pass
  it into pipeline calls — so responses reach whoever asked.
- main.py builds an admin Notifier (TG-only today, Composite(TG, Discord)
  once Discord lands) and passes it to autonomous pieces like
  startup_recovery and stuck_task_watchdog.
- Discord can later add a DiscordNotifier that schedules sends onto the
  Discord asyncio loop via run_coroutine_threadsafe.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable, Optional, Protocol


@dataclass(frozen=True)
class ActionButton:
    """A bot-agnostic button. Each Notifier renders into its native format."""
    label: str
    action: str
    task_id: str

    @property
    def callback_data(self) -> str:
        return f"{self.action}:{self.task_id}"


class Notifier(Protocol):
    def send(self, text: str, *, parse_mode: Optional[str] = None,
             buttons: Optional[Iterable[ActionButton]] = None) -> None:
        ...


class TelegramNotifier:
    def __init__(self, bot, chat_id):
        self.bot = bot
        self.chat_id = chat_id

    def send(self, text, *, parse_mode=None, buttons=None):
        try:
            kwargs = {'chat_id': self.chat_id, 'text': text}
            if parse_mode:
                kwargs['parse_mode'] = parse_mode
            if buttons:
                from telegram import InlineKeyboardButton, InlineKeyboardMarkup
                row = [InlineKeyboardButton(b.label, callback_data=b.callback_data) for b in buttons]
                kwargs['reply_markup'] = InlineKeyboardMarkup([row])
            self.bot.send_message(**kwargs)
        except Exception as e:
            logging.error(f"TelegramNotifier send failed: {e}")


class CompositeNotifier:
    """Fan out to multiple notifiers. One failing notifier doesn't block others."""
    def __init__(self, *notifiers):
        self._notifiers = [n for n in notifiers if n is not None]

    def send(self, text, *, parse_mode=None, buttons=None):
        for n in self._notifiers:
            try:
                n.send(text, parse_mode=parse_mode, buttons=buttons)
            except Exception as e:
                logging.error(f"CompositeNotifier sub-notifier {type(n).__name__} failed: {e}")


class NullNotifier:
    """Drop messages on the floor. For tests or callers that opt out of notifications."""
    def send(self, text, *, parse_mode=None, buttons=None):
        pass


class DiscordNotifier:
    """Schedules sends onto the Discord client's asyncio loop from sync code.

    The bridge is fire-and-forget — sync callers don't block waiting for the
    Discord API. If the client isn't ready yet (e.g. during bot boot), the
    message is dropped with a warning.
    """
    def __init__(self, channel_id):
        self.channel_id = int(channel_id) if channel_id else 0

    def send(self, text, *, parse_mode=None, buttons=None):
        # Lazy import to keep notifier.py importable when discord.py isn't installed.
        from pikpakbot.bot import discord_bot

        client = discord_bot.get_client()
        if not client or not client.is_ready() or not self.channel_id:
            logging.warning(
                f"DiscordNotifier dropped message (client_ready={client.is_ready() if client else False}, "
                f"channel={self.channel_id}): {text[:80]}"
            )
            return

        import asyncio
        try:
            asyncio.run_coroutine_threadsafe(
                self._send_async(client, text, buttons),
                client.loop,
            )
        except Exception as e:
            logging.error(f"DiscordNotifier scheduling failed: {e}")

    async def _send_async(self, client, text, buttons):
        try:
            channel = client.get_channel(self.channel_id)
            if channel is None:
                channel = await client.fetch_channel(self.channel_id)
        except Exception as e:
            logging.error(f"DiscordNotifier: channel {self.channel_id} not found: {e}")
            return

        view = None
        if buttons:
            import discord
            view = discord.ui.View(timeout=None)
            for b in buttons:
                view.add_item(discord.ui.Button(label=b.label, custom_id=b.callback_data))

        # Discord caps a single message at 2000 chars.
        try:
            await channel.send(text[:2000], view=view)
        except Exception as e:
            logging.error(f"DiscordNotifier send failed: {e}")
