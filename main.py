import logging
import threading

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


def main():
    _configure_logging()
    register_handlers(dispatcher)

    # Notifier for autonomous events (startup_recovery, stuck_task_watchdog,
    # unhandled exceptions inside background threads). Wraps the TG admin
    # channel today; will become CompositeNotifier(TG, Discord) once Discord
    # is wired in.
    admin_notifier = TelegramNotifier(updater.bot, ADMIN_IDS[0]) if ADMIN_IDS else None

    threading.Thread(target=startup_recovery, args=(admin_notifier,), daemon=True).start()
    threading.Thread(target=stuck_task_watchdog, args=(admin_notifier,), daemon=True).start()

    updater.start_polling()
    updater.idle()


if __name__ == '__main__':
    main()
