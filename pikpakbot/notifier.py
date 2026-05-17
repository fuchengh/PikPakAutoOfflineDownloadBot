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

    def finalize(self, *, success: bool, name: str) -> None:
        """Called once when the task reaches a terminal stage.

        Bot-specific cleanup hook. Discord uses it to rename + archive
        threads. TG is a no-op (no per-task channel concept).
        """
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

    def finalize(self, *, success, name):
        # TG has no per-task channel; nothing to clean up.
        pass


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

    def finalize(self, *, success, name):
        for n in self._notifiers:
            try:
                if hasattr(n, 'finalize'):
                    n.finalize(success=success, name=name)
            except Exception as e:
                logging.error(f"CompositeNotifier finalize {type(n).__name__} failed: {e}")


class NullNotifier:
    """Drop messages on the floor. For tests or callers that opt out of notifications."""
    def send(self, text, *, parse_mode=None, buttons=None):
        pass

    def finalize(self, *, success, name):
        pass


_EMBED_COLOR_BY_PREFIX = {
    '✅': 0x2ecc71,  # green — success
    '❌': 0xe74c3c,  # red — failure
    '⚠️': 0xf1c40f,  # yellow — warning
    '🔄': 0xf1c40f,  # yellow — retrying
    '📥': 0x3498db,  # blue — submitting
    '📋': 0x3498db,  # blue — info / list
    '📜': 0x3498db,  # blue — history
    '🧹': 0x3498db,  # blue — cleanup
    '☁️': 0x3498db,  # blue — offline progress
    '⬇️': 0x3498db,  # blue — download progress
}


def _color_for_text(text: str) -> int:
    """Heuristic embed color based on the leading emoji."""
    for prefix, color in _EMBED_COLOR_BY_PREFIX.items():
        if text.startswith(prefix):
            return color
    return 0x95a5a6  # neutral grey


class DiscordNotifier:
    """Schedules sends onto the Discord client's asyncio loop from sync code.

    The bridge is fire-and-forget — sync callers don't block waiting for the
    Discord API. If the client isn't ready yet (e.g. during bot boot), the
    message is dropped with a warning.

    channel_id can be either a text channel id or a thread id; the Discord
    API treats both the same for `get_channel` / `send`.
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

        import discord
        view = None
        if buttons:
            view = discord.ui.View(timeout=None)
            for b in buttons:
                view.add_item(discord.ui.Button(label=b.label, custom_id=b.callback_data))

        # Embed description caps at 4096 chars; plain content caps at 2000.
        # Using embed lets us show longer status without truncation, plus colored sidebar.
        embed = discord.Embed(description=text[:4000], color=_color_for_text(text))
        try:
            await channel.send(embed=embed, view=view)
        except Exception as e:
            logging.error(f"DiscordNotifier send failed: {e}")

    def finalize(self, *, success, name):
        """Rename + archive the bound thread (if any) when the task ends."""
        from pikpakbot.bot import discord_bot

        client = discord_bot.get_client()
        if not client or not client.is_ready() or not self.channel_id:
            return

        import asyncio
        try:
            asyncio.run_coroutine_threadsafe(
                self._finalize_async(client, success, name),
                client.loop,
            )
        except Exception as e:
            logging.error(f"DiscordNotifier finalize scheduling failed: {e}")

    async def _finalize_async(self, client, success, name):
        import discord
        try:
            channel = client.get_channel(self.channel_id)
            if channel is None:
                channel = await client.fetch_channel(self.channel_id)
        except Exception as e:
            logging.error(f"DiscordNotifier finalize: channel {self.channel_id} not found: {e}")
            return

        # Only act on threads — regular channels shouldn't be renamed/archived.
        if not isinstance(channel, discord.Thread):
            return

        icon = '✅' if success else '❌'
        new_name = f'{icon} {name}'[:100]  # Discord thread name cap
        try:
            if success:
                # Rename and archive in one edit; archived threads are hidden from
                # the active list but still browsable.
                await channel.edit(name=new_name, archived=True)
            else:
                # Failure: rename but stay active so the retry buttons remain usable.
                await channel.edit(name=new_name)
        except Exception as e:
            logging.error(f"DiscordNotifier finalize edit failed: {e}")
