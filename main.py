import logging
import threading

import config
from config import ADMIN_IDS
from pikpakbot import updater, dispatcher
from pikpakbot.bot.telegram import register_handlers
from pikpakbot.notifier import TelegramNotifier
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
        return
    from pikpakbot.bot.discord_bot import start_discord
    threading.Thread(target=start_discord, args=(token,), daemon=True).start()
    logging.info("Discord bot 線程已啟動")


def main():
    _configure_logging()
    register_handlers(dispatcher)
    _maybe_start_discord()

    # Notifier for autonomous events (startup_recovery, stuck_task_watchdog,
    # unhandled exceptions inside background threads). Wraps the TG admin
    # channel today; will become CompositeNotifier(TG, Discord) in step 7.
    admin_notifier = TelegramNotifier(updater.bot, ADMIN_IDS[0]) if ADMIN_IDS else None

    threading.Thread(target=startup_recovery, args=(admin_notifier,), daemon=True).start()
    threading.Thread(target=stuck_task_watchdog, args=(admin_notifier,), daemon=True).start()

    updater.start_polling()
    updater.idle()


if __name__ == '__main__':
    main()
