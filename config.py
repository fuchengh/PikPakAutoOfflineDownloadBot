# TG机器人的令牌，tg找@BotFather创建机器人即可获取
TOKEN = 'token'
# TG用户ID，限制发送消息的用户
ADMIN_IDS = ['12345678']
# pikpak账号，可以为手机号、邮箱，支持任意多账号
USER = ["example_user1", "example_user2"]
# 账号对应的密码，注意与账号顺序对应！！！
PASSWORD = ["example_password1", "example_password2"]
# 自动删除配置，未配置默认开启自动删除，留空即可
AUTO_DELETE = {}
# 以下分别为aria2 RPC的协议（http/https）、host、端口、密钥
ARIA2_HTTPS = False
ARIA2_HOST = "aria2"
ARIA2_PORT = "6800"
ARIA2_SECRET = "pikpak_secret"
# aria2下载根目录 (Docker 內部路徑)
ARIA2_DOWNLOAD_PATH = "/downloads"
# 可以自定义TG API，也可以保持默认
TG_API_URL = 'https://api.telegram.org'

# 自定义Pikpak离线下载路径
PIKPAK_OFFLINE_PATH = "None"

# Discord bot (optional). Leave DISCORD_TOKEN empty to disable.
DISCORD_TOKEN = ''
DISCORD_CHANNEL_ID = 0  # int — channel where Discord side posts autonomous notifications


def record_config():
    import logging
    import os

    config_path = os.path.abspath(os.path.dirname(__file__)) + '/config.py'
    with open(config_path, 'w') as f:
        f.write(
            f'TOKEN = "{TOKEN}"\n'
            f'ADMIN_IDS = {ADMIN_IDS}\n'
            f'USER = {USER}\n'
            f'PASSWORD = {PASSWORD}\n'
            f'AUTO_DELETE = {AUTO_DELETE}\n'
            f'ARIA2_HTTPS = {ARIA2_HTTPS}\n'
            f'ARIA2_HOST = "{ARIA2_HOST}"\n'
            f'ARIA2_PORT = "{ARIA2_PORT}"\n'
            f'ARIA2_SECRET = "{ARIA2_SECRET}"\n'
            f'ARIA2_DOWNLOAD_PATH = "{ARIA2_DOWNLOAD_PATH}"\n'
            f'TG_API_URL = "{TG_API_URL}"\n'
            f'PIKPAK_OFFLINE_PATH = "{PIKPAK_OFFLINE_PATH}"\n'
            f'DISCORD_TOKEN = "{DISCORD_TOKEN}"\n'
            f'DISCORD_CHANNEL_ID = {DISCORD_CHANNEL_ID}\n')
    logging.info('已更新config.py文件')
