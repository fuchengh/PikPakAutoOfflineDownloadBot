from telegram.ext import Updater

from config import TG_API_URL, TOKEN

if TG_API_URL[-1] == '/':
    updater = Updater(token=TOKEN, base_url=f"{TG_API_URL}bot", base_file_url=f"{TG_API_URL}file/bot")
else:
    updater = Updater(token=TOKEN, base_url=f"{TG_API_URL}/bot", base_file_url=f"{TG_API_URL}/file/bot")

dispatcher = updater.dispatcher
