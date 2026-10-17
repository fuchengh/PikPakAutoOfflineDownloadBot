"""Importing state/clients must not initialize a bot or contact Telegram."""


def build_telegram():
    from telegram.ext import Updater
    from config import TG_API_URL, TOKEN
    base = TG_API_URL.rstrip('/')
    return Updater(token=TOKEN, base_url=f'{base}/bot', base_file_url=f'{base}/file/bot')
