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
DISCORD_ADMIN_IDS = []  # Discord user IDs; empty permits only the application owner/team
DOWNLOAD_WORKERS = 4
MAX_QUEUED_TASKS = 128  # running + waiting
OFFLINE_STALL_SECONDS = 1200
OFFLINE_MAX_SECONDS = 7200
ARIA2_STALL_SECONDS = 3600
MAX_AUTO_RETRIES = 3


def record_config():
    import logging
    import os

    # Keep the functions and comments in this file; update only literal settings.
    import ast
    import stat
    import tempfile
    config_path = os.path.abspath(__file__)
    with open(config_path, encoding='utf-8') as source:
        original_stat = os.fstat(source.fileno())
        lines = source.readlines()
    updates = []
    for node in ast.parse(''.join(lines)).body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name) and target.id.isupper() and target.id in globals():
                updates.append((node.lineno - 1, node.end_lineno, f'{target.id} = {globals()[target.id]!r}\n'))
    for start, end, text in reversed(updates):
        lines[start:end] = [text]
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=os.path.dirname(config_path),
                                         delete=False) as output:
            temporary = output.name
            output.writelines(lines)
            output.flush()
            if hasattr(os, 'fchown'):
                current_stat = os.fstat(output.fileno())
                if (current_stat.st_uid, current_stat.st_gid) != (original_stat.st_uid, original_stat.st_gid):
                    os.fchown(output.fileno(), original_stat.st_uid, original_stat.st_gid)
            # chown can clear permission bits, so restore the mode afterwards.
            os.chmod(temporary, stat.S_IMODE(original_stat.st_mode))
        os.replace(temporary, config_path)
    finally:
        if temporary is not None and os.path.exists(temporary):
            os.unlink(temporary)
    logging.info('已更新config.py文件')
