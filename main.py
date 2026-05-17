import logging
import threading

from pikpakbot import updater, dispatcher
from pikpakbot.bot.telegram import register_handlers
from pikpakbot.pipeline import startup_recovery


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

    recovery_thread = threading.Thread(target=startup_recovery, daemon=True)
    recovery_thread.start()

    updater.start_polling()
    updater.idle()


if __name__ == '__main__':
    main()
