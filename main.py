import logging
import threading

import config
from config import ADMIN_IDS
from pikpakbot import updater, dispatcher
from pikpakbot.bot.telegram import register_handlers
from pikpakbot.notifier import CompositeNotifier, DiscordNotifier, NullNotifier, TelegramNotifier
from pikpakbot.pipeline import startup_recovery, stuck_task_watchdog


def _configure_logging():
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)


def _maybe_start_discord():
    """Start Discord bot in a daemon thread if DISCORD_TOKEN is configured."""
    token = getattr(config, 'DISCORD_TOKEN', '')
    if not token:
        logging.info("DISCORD_TOKEN 未設定，跳過 Discord bot 啟動")
        return False
    from pikpakbot.bot.discord_bot import start_discord
    threading.Thread(target=start_discord, args=(token,), daemon=True).start()
    logging.info("Discord bot 線程已啟動")
    return True


def _build_admin_notifier(discord_enabled: bool):
    """Compose the notifier used for autonomous events (recovery, watchdog).

    Fans out to TG admin + Discord channel so a single bot dying doesn't
    silence the autonomous notifications.
    """
    sinks = []
    if ADMIN_IDS:
        sinks.append(TelegramNotifier(updater.bot, ADMIN_IDS[0]))
    if discord_enabled and getattr(config, 'DISCORD_CHANNEL_ID', 0):
        sinks.append(DiscordNotifier(config.DISCORD_CHANNEL_ID))
    if not sinks:
        logging.warning("沒有設定任何 admin 通知管道（ADMIN_IDS 跟 DISCORD_CHANNEL_ID 都空）")
        return NullNotifier()
    return CompositeNotifier(*sinks)


def main():
    _configure_logging()
    register_handlers(dispatcher)
    discord_enabled = _maybe_start_discord()

    admin_notifier = _build_admin_notifier(discord_enabled)

    threading.Thread(target=startup_recovery, args=(admin_notifier,), daemon=True).start()
    threading.Thread(target=stuck_task_watchdog, args=(admin_notifier,), daemon=True).start()

    updater.start_polling()
    updater.idle()


if __name__ == '__main__':
    main()
