import logging
import os
import re
import threading
import time
import uuid

import telegram
from telegram import Update
from telegram.ext import CallbackContext, CallbackQueryHandler, CommandHandler, Handler, MessageHandler, Filters

import config
from config import ADMIN_IDS, USER, PASSWORD, AUTO_DELETE, record_config
from pikpakbot import state
from pikpakbot.notifier import TelegramNotifier
from pikpakbot.pikpak_client import (
    login,
    registerFuc,
    pikpak_headers,
    get_folder_all,
    get_list,
    get_stuck_tasks,
    retry_stuck_tasks,
    delete_files,
    delete_trash,
    delete_offline_tasks,
    empty_trash,
    get_my_vip,
)
from pikpakbot.pipeline import (
    process_magnet,
    download_cloud_file,
    thread_list,
    batch_results,
    batch_lock,
    check_download_thread_status,
    cleanup_failed_download_dir,
)


def _human_size(n):
    try:
        n = int(n or 0)
    except (TypeError, ValueError):
        return '?'
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if n < 1024:
            return f'{n:.0f}{unit}' if unit == 'B' else f'{n:.1f}{unit}'
        n /= 1024
    return f'{n:.1f}PB'


_STAGE_LABELS = {
    state.STAGE_QUEUED: '⏳ 待處理',
    state.STAGE_OFFLINE: '☁️ PikPak 離線中',
    state.STAGE_DOWNLOAD: '⬇️ Aria2 下載中',
    state.STAGE_CLEANUP: '🧹 清理中',
    state.STAGE_COMPLETE: '✅ 完成',
    state.STAGE_FAILED: '❌ 失敗',
    state.STAGE_CANCELED: '⛔ 已取消',
}


def _stage_label(stage):
    return _STAGE_LABELS.get(stage, stage)


def _age(ts):
    delta = max(0, int(time.time() - ts))
    if delta < 60:
        return f'{delta}s'
    if delta < 3600:
        return f'{delta // 60}m'
    if delta < 86400:
        return f'{delta // 3600}h'
    return f'{delta // 86400}d'


# 用户限制：Stack Overflow 用户@Majid提供的方法
# from: https://stackoverflow.com/questions/62466399/how-can-i-restrict-a-telegram-bots-use-to-some-users-only#answers-header
class AdminHandler(Handler):
    def __init__(self):
        super().__init__(self.cb)

    def cb(self, update: telegram.Update, context):
        if update.callback_query:
            update.callback_query.answer('Unauthorized', show_alert=True)
        elif update.message:
            update.message.reply_text('Unauthorized access')

    def check_update(self, update: telegram.update.Update):
        if update.callback_query:
            return str(update.callback_query.from_user.id) not in ADMIN_IDS
        if update.message is None or str(update.message.from_user.id) not in ADMIN_IDS:
            return True

        return False


def start(update: Update, context: CallbackContext):
    context.bot.send_message(chat_id=update.effective_chat.id,
                             text="【指令簡介】\n"
                                  "/p\t自動離線+aria2下載+釋放雲端硬碟空間\n"
                                  "/ls [folder_id]\t列出 PikPak 雲端內容\n"
                                  "/dl <file_id>\t下載指定的雲端檔案/資料夾到本機\n"
                                  "/status\t查看目前進行中的任務\n"
                                  "/history [n]\t查看最近 n 個任務記錄（預設 20）\n"
                                  "/account\t管理帳號（發送/account查看使用說明）\n"
                                  "/clean\t清空雲端硬碟+離線任務記錄（發送/clean查看使用說明）\n"
                                  "/path\t管理pikpak離線下載的路徑\n"
                                  "/retry\t重試卡住的離線任務（發送/retry查看使用說明）\n")


def pikpak(update: Update, context: CallbackContext):
    if context.args is None:
        argv = update.message.text.split()
    else:
        argv = context.args

    if len(argv) == 0:
        context.bot.send_message(chat_id=update.effective_chat.id, text='【用法】\n/p magnet1 [magnet2] [...]')
    else:
        print_info = '下載隊列添加離線磁力任務：\n'
        if os.path.isabs(argv[0]):
            temp_offline_path = argv[0]
            argv = argv[1:]
        else:
            temp_offline_path = None

        offline_path = None
        if temp_offline_path:
            offline_path = temp_offline_path
        elif str(config.PIKPAK_OFFLINE_PATH) not in ["None", "/My Pack"]:
            offline_path = config.PIKPAK_OFFLINE_PATH
        if offline_path:
            print_info += f'檢測到自定義下載路徑 {offline_path}，將離線到此路徑\n'
            logging.info(f'檢測到自定義下載路徑 {offline_path}，將離線到此路徑')

        batch_id = str(uuid.uuid4())[:8]
        with batch_lock:
            batch_results[batch_id] = {
                'total': len(argv),
                'processed': 0,
                'results': []
            }

        notifier = TelegramNotifier(context.bot, update.effective_chat.id)
        for each_magnet in argv:
            thread_list.append(threading.Thread(
                target=process_magnet,
                args=[notifier, each_magnet, offline_path, batch_id]
            ))
            thread_list[-1].start()

            mag_url_part = re.search(r'^(magnet:\?).*(xt=.+?)(&|$)', each_magnet)
            if mag_url_part:
                print_info += ''.join(mag_url_part.groups()[:-1])
            else:
                print_info += each_magnet
            print_info += '\n\n'

        context.bot.send_message(chat_id=update.effective_chat.id, text=print_info.rstrip())
        logging.info(print_info.rstrip())


def clean(update: Update, context: CallbackContext):
    argv = context.args

    if len(argv) == 0:
        context.bot.send_message(chat_id=update.effective_chat.id,
                                 text='【用法】\n'
                                      '`/clean all`\t清空所有帳號雲端硬碟+離線任務記錄\n'
                                      '`/clean deep`\t深度清理（檔案+回收站+所有離線任務記錄）\n'
                                      '`/clean tasks`\t只清理離線任務記錄（不刪檔案）\n'
                                      '`/clean tasks error`\t只清理失敗的離線任務記錄\n'
                                      '/clean 帳號1 [帳號2] [...]\t清空指定帳號',
                                 parse_mode='Markdown')

    elif check_download_thread_status():
        context.bot.send_message(chat_id=update.effective_chat.id, text='其他指令正在運行，為避免衝突，請稍後再試~')

    elif argv[0] in ['d', 'deep']:
        context.bot.send_message(chat_id=update.effective_chat.id, text='🔄 開始深度清理...')
        for temp_account in USER:
            login(temp_account)
            msg_parts = []

            all_file_id = list(get_folder_all(temp_account))
            if len(all_file_id) > 0:
                delete_files(all_file_id, temp_account, mode='all')
                msg_parts.append(f"已刪除 {len(all_file_id)} 個檔案")

            if empty_trash(temp_account):
                msg_parts.append("回收站已清空")

            success, fail = delete_offline_tasks(temp_account)
            if success > 0:
                msg_parts.append(f"已清理 {success} 個離線任務記錄")

            if msg_parts:
                result_msg = f'帳號{temp_account}深度清理完成:\n' + '\n'.join(f'  ✅ {p}' for p in msg_parts)
            else:
                result_msg = f'帳號{temp_account}無需清理'

            context.bot.send_message(chat_id=update.effective_chat.id, text=result_msg)
            logging.info(result_msg)

    elif argv[0] in ['t', 'tasks']:
        phase_filter = None
        if len(argv) >= 2 and argv[1] in ['e', 'error']:
            phase_filter = 'PHASE_TYPE_ERROR'
            context.bot.send_message(chat_id=update.effective_chat.id, text='🔄 正在清理失敗的離線任務記錄...')
        else:
            context.bot.send_message(chat_id=update.effective_chat.id, text='🔄 正在清理所有離線任務記錄...')

        for temp_account in USER:
            login(temp_account)
            success, fail = delete_offline_tasks(temp_account, phase_filter=phase_filter)
            if success > 0 or fail > 0:
                context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text=f'帳號{temp_account}離線任務記錄清理完成: ✅ {success} 個成功, ❌ {fail} 個失敗'
                )
            else:
                context.bot.send_message(chat_id=update.effective_chat.id,
                                         text=f'帳號{temp_account}沒有需要清理的離線任務記錄')

    elif argv[0] in ['a', 'all']:
        context.bot.send_message(chat_id=update.effective_chat.id, text='🔄 開始清空所有帳號...')
        for temp_account in USER:
            login(temp_account)
            msg_parts = []

            all_file_id = list(get_folder_all(temp_account))
            if len(all_file_id) > 0:
                delete_files(all_file_id, temp_account, mode='all')
                delete_trash(all_file_id, temp_account, mode='all')
                msg_parts.append(f"已刪除 {len(all_file_id)} 個檔案")

            success, fail = delete_offline_tasks(temp_account, phase_filter='PHASE_TYPE_ERROR')
            if success > 0:
                msg_parts.append(f"已清理 {success} 個失敗的離線任務記錄")

            if msg_parts:
                result_msg = f'帳號{temp_account}清空完成:\n' + '\n'.join(f'  ✅ {p}' for p in msg_parts)
            else:
                result_msg = f'帳號{temp_account}雲端硬碟無需清空'

            context.bot.send_message(chat_id=update.effective_chat.id, text=result_msg)
            logging.info(result_msg)

    else:
        for each_account in argv:
            if each_account in USER:
                login(each_account)
                msg_parts = []

                all_file_id = list(get_folder_all(each_account))
                if len(all_file_id) > 0:
                    delete_files(all_file_id, each_account, mode='all')
                    delete_trash(all_file_id, each_account, mode='all')
                    msg_parts.append(f"已刪除 {len(all_file_id)} 個檔案")

                success, fail = delete_offline_tasks(each_account, phase_filter='PHASE_TYPE_ERROR')
                if success > 0:
                    msg_parts.append(f"已清理 {success} 個失敗的離線任務記錄")

                if msg_parts:
                    result_msg = f'帳號{each_account}清空完成:\n' + '\n'.join(f'  ✅ {p}' for p in msg_parts)
                else:
                    result_msg = f'帳號{each_account}雲端硬碟無需清空'

                context.bot.send_message(chat_id=update.effective_chat.id, text=result_msg)
                logging.info(result_msg)

            else:
                context.bot.send_message(chat_id=update.effective_chat.id, text=f'帳號{each_account}不存在！')
                continue


def print_user_vip():
    print_info = '帳號      vip\n'
    for each_user in USER:
        flag = get_my_vip(each_user)
        if flag == 0:
            flag = '√'
        elif flag == 1:
            flag = '×'
        elif flag == 2:
            flag = '?'
        else:
            flag = '××'
        print_info += f' `{each_user}`\[{flag}]\n'
    return print_info.rstrip()


def print_user():
    print_info = "帳號：\n"
    for each_user in USER:
        print_info += f'`{each_user}`\n'
    return print_info.rstrip()


def print_user_pd():
    print_info = "帳號：\n"
    for each_user, each_password in zip(USER, PASSWORD):
        print_info += f'`{each_user}`\n`{each_password}`\n\n'
    return print_info.rstrip()


def print_user_auto_delete():
    print_info = "帳號      自動清理\n"
    for key, value in AUTO_DELETE.items():
        print_info += f'`{key}`\[{value}]\n'
    return print_info.rstrip()


def account_manage(update: Update, context: CallbackContext):
    argv = context.args

    if len(argv) == 0:
        context.bot.send_message(chat_id=update.effective_chat.id,
                                 text='【用法】\n'
                                      '羅列帳號：/account l/list \[pd]\[vip]\[status]\n'
                                      '添加帳號：/account a/add 帳號 密碼\n'
                                      '刪除帳號：/account d/delete 帳號1\n'
                                      '註冊帳號：/account n/new\n'
                                      '是否開啟清空雲端硬碟（預設開啟）：\n'
                                      '/account on 帳號1 帳號2\n'
                                      '/account off 帳號1 帳號2\n'
                                      '【範例】\n'
                                      '`/account l`\n'
                                      '`/account l vip`\n'
                                      '`/account l status`\n'
                                      '`/account a` 123@qq.com 123\n'
                                      '`/account d` 123@qq.com\n'
                                      '`/account n`\n'
                                      '`/account on` 123@qq.com\n'
                                      '`/account off` 123@qq.com',
                                 parse_mode='Markdown')

    elif argv[0] in ['l', 'list']:
        if len(argv) == 2 and argv[1] == 'vip':
            context.bot.send_message(chat_id=update.effective_chat.id, text=print_user_vip(), parse_mode='Markdown')
        elif len(argv) == 2 and argv[1] == 'status':
            context.bot.send_message(chat_id=update.effective_chat.id, text=print_user_auto_delete(),
                                     parse_mode='Markdown')
        elif len(argv) == 2 and argv[1] == 'pd':
            context.bot.send_message(chat_id=update.effective_chat.id, text=print_user_pd(), parse_mode='Markdown')
        else:
            context.bot.send_message(chat_id=update.effective_chat.id, text=print_user(), parse_mode='Markdown')

    elif argv[0] in ['a', 'add']:
        if len(argv) == 3:
            USER.insert(0, argv[1])
            PASSWORD.insert(0, argv[2])
            pikpak_headers.insert(0, None)
            record_config()

            print_info = print_user()
            context.bot.send_message(chat_id=update.effective_chat.id, text=print_info, parse_mode='Markdown')
        else:
            context.bot.send_message(chat_id=update.effective_chat.id, text='參數個數錯誤，請檢查！')

    elif argv[0] in ['n', 'new']:
        if len(argv) == 1:
            register = registerFuc()
            if register:
                USER.insert(0, register['account'])
                PASSWORD.insert(0, register['password'])
                pikpak_headers.insert(0, None)
                record_config()
                print_info = print_user()
                context.bot.send_message(chat_id=update.effective_chat.id, text=print_info, parse_mode='Markdown')
            else:
                context.bot.send_message(chat_id=update.effective_chat.id, text='註冊失敗，請重試！')
        else:
            context.bot.send_message(chat_id=update.effective_chat.id, text='參數個數錯誤，請檢查！')

    elif argv[0] in ['d', 'delete']:
        if len(argv) > 1:
            for each_account in argv[1:]:
                try:
                    temp_account_index = USER.index(each_account)
                except ValueError:
                    context.bot.send_message(chat_id=update.effective_chat.id, text=f'帳號{each_account}不存在')
                    continue
                USER.pop(temp_account_index)
                PASSWORD.pop(temp_account_index)
                pikpak_headers.pop(temp_account_index)

                if each_account in AUTO_DELETE:
                    AUTO_DELETE.pop(each_account)
                for key in list(AUTO_DELETE.keys()):
                    if key not in USER:
                        AUTO_DELETE.pop(key)

                record_config()

                print_info = print_user()
                context.bot.send_message(chat_id=update.effective_chat.id, text=print_info, parse_mode='Markdown')
        else:
            context.bot.send_message(chat_id=update.effective_chat.id, text='參數個數錯誤，請檢查！')

    elif argv[0] in ['on', 'off']:
        if len(argv) > 1:
            for each_account in argv[1:]:
                try:
                    if each_account not in USER:
                        context.bot.send_message(chat_id=update.effective_chat.id, text=f'帳號{each_account}不存在')
                        continue
                    if argv[0] == 'on':
                        AUTO_DELETE[each_account] = 'True'
                    elif argv[0] == 'off':
                        AUTO_DELETE[each_account] = 'False'
                except ValueError:
                    context.bot.send_message(chat_id=update.effective_chat.id, text=f'帳號{each_account}不存在')
                    continue
            record_config()
            print_info = print_user_auto_delete()
            context.bot.send_message(chat_id=update.effective_chat.id, text=print_info, parse_mode='Markdown')
        else:
            context.bot.send_message(chat_id=update.effective_chat.id, text='參數個數錯誤，請檢查！')
    else:
        context.bot.send_message(chat_id=update.effective_chat.id, text='不存在的指令語法！')


def path(update: Update, context: CallbackContext):
    """設置網盤離線下載路徑"""
    argv = context.args
    if len(argv) == 0:
        context.bot.send_message(chat_id=update.effective_chat.id,
                                 text='【用法】\n'
                                      '設置離線路徑：`/path 路徑參數`\n'
                                      '查詢離線路徑：`/path info`\n'
                                      '恢復預設路徑：`/path default`\n'
                                      '【範例】\n'
                                      '`/path /downloads`\n'
                                      '路徑參數請使用絕對路徑，如`/downloads`',
                                 parse_mode='Markdown')
    elif argv[0] == 'info':
        if config.PIKPAK_OFFLINE_PATH == "None":
            context.bot.send_message(chat_id=update.effective_chat.id, text='當前離線下載路徑為預設路徑：`/My Pack`',
                                     parse_mode='Markdown')
        else:
            context.bot.send_message(chat_id=update.effective_chat.id,
                                     text=f'當前離線下載路徑為：`{config.PIKPAK_OFFLINE_PATH}`',
                                     parse_mode='Markdown')
    elif argv[0] == 'default':
        config.PIKPAK_OFFLINE_PATH = "None"
        record_config()
        context.bot.send_message(chat_id=update.effective_chat.id, text='已恢復預設路徑：`/My Pack`',
                                 parse_mode='Markdown')
    else:
        if not os.path.isabs(argv[0]):
            context.bot.send_message(chat_id=update.effective_chat.id, text='路徑參數請使用絕對路徑或指令不存在！')
            return
        config.PIKPAK_OFFLINE_PATH = argv[0]
        record_config()
        context.bot.send_message(chat_id=update.effective_chat.id,
                                 text=f'已設置離線下載路徑：`{config.PIKPAK_OFFLINE_PATH}`',
                                 parse_mode='Markdown')


def retry(update: Update, context: CallbackContext):
    """重試卡住的離線下載任務"""
    argv = context.args

    min_progress = 90

    if len(argv) >= 1:
        try:
            min_progress = int(argv[0])
            if min_progress < 0 or min_progress > 100:
                context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text='進度閾值必須在 0-100 之間'
                )
                return
        except ValueError:
            if argv[0] not in ['list', 'l']:
                context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text='【用法】\n'
                         '查看卡住的任務：`/retry list` 或 `/retry l`\n'
                         '重試卡住的任務：`/retry [進度閾值]`\n'
                         '【範例】\n'
                         '`/retry` - 重試進度 >= 90% 的任務\n'
                         '`/retry 99` - 重試進度 >= 99% 的任務\n'
                         '`/retry list` - 列出所有卡住的任務',
                    parse_mode='Markdown'
                )
                return

    if len(argv) >= 1 and argv[0] in ['list', 'l']:
        list_min_progress = int(argv[1]) if len(argv) >= 2 else 90
        msg = f"📋 <b>卡住的任務列表</b> (進度 >= {list_min_progress}%)\n"
        msg += "─" * 25 + "\n"

        total_stuck = 0
        for account in USER:
            stuck = get_stuck_tasks(account, list_min_progress)
            if stuck:
                msg += f"\n<b>帳號: {account}</b>\n"
                for task in stuck:
                    msg += f"  • {task['name']} ({task['progress']}%)\n"
                total_stuck += len(stuck)

        if total_stuck == 0:
            msg += f"\n✅ 沒有找到卡住的任務"
        else:
            msg += f"\n─" + "─" * 24 + "\n"
            msg += f"共 {total_stuck} 個任務卡住"

        context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=msg,
            parse_mode='HTML'
        )
        return

    context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=f'🔄 正在查找並重試進度 >= {min_progress}% 的卡住任務...'
    )

    total_success = 0
    total_fail = 0
    all_results = []

    notifier = TelegramNotifier(context.bot, update.effective_chat.id)
    for account in USER:
        success, fail, results = retry_stuck_tasks(account, min_progress, delete_cloud_files=True, notifier=notifier)
        total_success += success
        total_fail += fail
        if results:
            all_results.append({'account': account, 'results': results})

    if total_success + total_fail == 0:
        context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f'✅ 沒有找到進度 >= {min_progress}% 的卡住任務'
        )
        return

    msg = f"📋 <b>重試結果</b>\n"
    msg += f"✅ 成功: {total_success}  ❌ 失敗: {total_fail}\n"

    for item in all_results:
        for r in item['results']:
            icon = "✅" if r['status'] == 'success' else "❌"
            msg += f"{icon} {r['name']}\n"

    context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=msg,
        parse_mode='HTML'
    )


def status(update: Update, context: CallbackContext):
    """List currently in-flight tasks (any non-terminal stage)."""
    tasks = state.list_active()
    if not tasks:
        context.bot.send_message(chat_id=update.effective_chat.id, text='✅ 目前沒有進行中的任務')
        return

    lines = [f'📋 <b>進行中任務 ({len(tasks)})</b>']
    for t in tasks:
        name = t.get('name') or '(尚未取得名稱)'
        acc = (t.get('account') or '').split('@')[0] or '-'
        prog = t.get('progress', 0) or 0
        prog_str = f' {prog}%' if prog else ''
        age = _age(t['created_at'])
        lines.append(
            f"\n<code>{t['task_id']}</code> {_stage_label(t['stage'])}{prog_str}"
            f"\n  {name}"
            f"\n  帳號: {acc} | 已花費 {age}"
        )
    context.bot.send_message(chat_id=update.effective_chat.id, text='\n'.join(lines), parse_mode='HTML')


def history(update: Update, context: CallbackContext):
    """List recent terminal-stage tasks (default last 20)."""
    argv = context.args
    limit = 20
    if argv:
        try:
            limit = max(1, min(50, int(argv[0])))
        except ValueError:
            pass

    tasks = state.list_recent(limit=limit)
    if not tasks:
        context.bot.send_message(chat_id=update.effective_chat.id, text='📜 沒有任務記錄')
        return

    lines = [f'📜 <b>最近 {len(tasks)} 個任務</b>']
    for t in tasks:
        name = t.get('name') or t.get('magnet') or '(unknown)'
        if len(name) > 60:
            name = name[:57] + '...'
        acc = (t.get('account') or '').split('@')[0] or '-'
        end_ts = t.get('completed_at') or t.get('updated_at')
        age = _age(end_ts)
        err = t.get('error')
        line = f"{_stage_label(t['stage'])} <code>{t['task_id']}</code> {name} ({acc}) — {age} ago"
        if err and t['stage'] == state.STAGE_FAILED:
            line += f"\n  ↳ {err}"
        lines.append(line)

    context.bot.send_message(chat_id=update.effective_chat.id, text='\n'.join(lines), parse_mode='HTML')


def ls_cmd(update: Update, context: CallbackContext):
    """List PikPak cloud contents. Usage: /ls [folder_id] [account]"""
    argv = context.args or []
    folder_id = argv[0] if argv else ''
    if folder_id == 'root':
        folder_id = ''

    account = USER[0] if USER else None
    if len(argv) >= 2:
        if argv[1] in USER:
            account = argv[1]
        else:
            context.bot.send_message(chat_id=update.effective_chat.id, text=f'帳號 {argv[1]} 不存在')
            return
    if not account:
        context.bot.send_message(chat_id=update.effective_chat.id, text='沒有設定帳號')
        return

    files = get_list(folder_id, account)
    if not files:
        context.bot.send_message(chat_id=update.effective_chat.id,
                                 text=f'📁 (空) — folder_id=`{folder_id or "root"}`, account=`{account}`',
                                 parse_mode='Markdown')
        return

    lines = [f'📁 *PikPak 雲端內容* (帳號: `{account}`)']
    if folder_id:
        lines[0] += f' folder=`{folder_id}`'
    for f in files:
        is_folder = f.get('kind') == 'drive#folder'
        icon = '📁' if is_folder else '📄'
        size = '' if is_folder else f' ({_human_size(f.get("size", 0))})'
        lines.append(f'{icon} {f["name"]}{size}')
        lines.append(f'   `{f["id"]}`')

    msg = '\n'.join(lines)
    if len(msg) > 4000:
        msg = msg[:4000] + '\n\n... (truncated, use folder_id to drill in)'
    context.bot.send_message(chat_id=update.effective_chat.id, text=msg, parse_mode='Markdown')


def dl_cmd(update: Update, context: CallbackContext):
    """Download a specific PikPak cloud file/folder to local. Usage: /dl <file_id> [account]"""
    argv = context.args or []
    if not argv:
        context.bot.send_message(chat_id=update.effective_chat.id,
                                 text='【用法】\n`/dl <file_id> [account]`\n先用 `/ls` 取得 file_id',
                                 parse_mode='Markdown')
        return
    file_id = argv[0]
    account = USER[0] if USER else None
    if len(argv) >= 2:
        if argv[1] in USER:
            account = argv[1]
        else:
            context.bot.send_message(chat_id=update.effective_chat.id, text=f'帳號 {argv[1]} 不存在')
            return
    if not account:
        context.bot.send_message(chat_id=update.effective_chat.id, text='沒有設定帳號')
        return

    notifier = TelegramNotifier(context.bot, update.effective_chat.id)
    t = threading.Thread(target=download_cloud_file, args=[notifier, file_id, account])
    thread_list.append(t)
    t.start()

    context.bot.send_message(chat_id=update.effective_chat.id,
                             text=f'📥 已開始下載 PikPak file `{file_id}` (帳號 `{account}`)',
                             parse_mode='Markdown')


def handle_callback(update: Update, context: CallbackContext):
    """Handle inline button clicks (e.g. [Retry] on failure messages)."""
    query = update.callback_query
    try:
        query.answer()
    except Exception:
        pass

    data = query.data or ''
    if data.startswith('retry:'):
        task_id = data.split(':', 1)[1]
        task = state.get_task(task_id)
        if not task:
            try:
                query.edit_message_reply_markup(reply_markup=None)
                context.bot.send_message(chat_id=query.message.chat_id,
                                         text=f'❌ 找不到任務 {task_id}')
            except Exception:
                pass
            return

        magnet = task.get('magnet')
        if not magnet:
            context.bot.send_message(chat_id=query.message.chat_id,
                                     text=f'❌ 任務 {task_id} 沒有原始 magnet，無法重試（可能是 resume 任務）')
            return

        # Clean up the failed attempt's local download dir before re-running so
        # leftover .aria2 partials and the broken big file don't pile up.
        cleaned, cleanup_msg = cleanup_failed_download_dir(task.get('name'))
        logging.info(f"retry cleanup for {task_id} ({task.get('name')}): {cleanup_msg}")
        if cleaned:
            context.bot.send_message(chat_id=query.message.chat_id,
                                     text=f'🧹 已清掉舊下載資料夾 `{task.get("name")}`',
                                     parse_mode='Markdown')

        notifier = TelegramNotifier(context.bot, query.message.chat_id)
        thread_list.append(threading.Thread(
            target=process_magnet,
            args=[notifier, magnet, None, None, None, None],
        ))
        thread_list[-1].start()

        # Remove the buttons so the user can't double-click
        try:
            query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        context.bot.send_message(chat_id=query.message.chat_id,
                                 text=f'🔄 任務 {task_id} 已重新加入佇列')

    elif data.startswith('dismiss:'):
        try:
            query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass


def register_handlers(dispatcher):
    """Register all Telegram handlers on the dispatcher (order preserved from monolith)."""
    start_handler = CommandHandler(['start', 'help'], start)
    pikpak_handler = CommandHandler('p', pikpak)
    clean_handler = CommandHandler(['clean', 'clear'], clean)
    account_handler = CommandHandler('account', account_manage)
    path_handler = CommandHandler('path', path)
    retry_handler = CommandHandler('retry', retry)
    status_handler = CommandHandler('status', status)
    history_handler = CommandHandler('history', history)
    ls_handler = CommandHandler('ls', ls_cmd)
    dl_handler = CommandHandler('dl', dl_cmd)
    magnet_handler = MessageHandler(Filters.regex('^magnet:\?xt=urn:btih:[0-9a-fA-F]{40,}.*$'), pikpak)
    callback_handler = CallbackQueryHandler(handle_callback)

    dispatcher.add_handler(AdminHandler())
    dispatcher.add_handler(account_handler)
    dispatcher.add_handler(start_handler)
    dispatcher.add_handler(magnet_handler)
    dispatcher.add_handler(pikpak_handler)
    dispatcher.add_handler(clean_handler)
    dispatcher.add_handler(path_handler)
    dispatcher.add_handler(retry_handler)
    dispatcher.add_handler(status_handler)
    dispatcher.add_handler(history_handler)
    dispatcher.add_handler(ls_handler)
    dispatcher.add_handler(dl_handler)
    dispatcher.add_handler(callback_handler)
