import asyncio
import logging
import re
import threading
from time import sleep

import requests
from pikpakapi import PikPakApi

from config import USER, PASSWORD, AUTO_DELETE

PIKPAK_API_URL = "https://api-drive.mypikpak.com"
PIKPAK_USER_URL = "https://user.mypikpak.com"

pikpak_headers = [None] * len(USER)
pikpak_clients = [None] * len(USER)
login_lock = threading.Lock()


def registerFuc():
    try:
        url = 'https://pikpak.kinh.cc/GetFreeAccount.php'
        resp = requests.get(url)
        account = resp.json()['Data'].split('|')[0].split(':')[1].strip()
        password = resp.json()['Data'].split('|')[1].split(':')[1].strip()
        if account and password:
            return {'account': account, 'password': password}
        else:
            return False
    except Exception as e:
        logging.error(e)
        return False


def auto_delete_judge(account):
    try:
        status = AUTO_DELETE[account]
        if status.upper() == 'TRUE':
            return 'on'
        else:
            return 'off'
    except Exception as e:
        logging.error(f"{e}未配置，默認開啟自動刪除")
        return 'on'


def login(account):
    with login_lock:
        index = USER.index(account)

        login_admin = account
        login_password = PASSWORD[index]

        client = PikPakApi(
            username=login_admin,
            password=login_password,
        )

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(client.login())
            loop.run_until_complete(client.refresh_access_token())
        finally:
            loop.close()
        headers = client.get_headers()
        pikpak_headers[index] = headers.copy()
        pikpak_clients[index] = client

        logging.info(f"帳號{account}登入成功！")


def get_headers(account):
    index = USER.index(account)
    if not pikpak_headers[index]:
        login(account)
    return pikpak_headers[index]


def get_clients(account):
    index = USER.index(account)
    if not pikpak_clients[index]:
        login(account)
    return pikpak_clients[index]


def magnet_upload(file_url, account, parent_id=None, offline_path=None):
    login_headers = get_headers(account)
    client = get_clients(account)
    torrent_url = f"{PIKPAK_API_URL}/drive/v1/files"
    if offline_path:
        parent_ids = asyncio.run(client.path_to_id(path=offline_path, create=True))
        if parent_ids and offline_path.split("/")[-1] == parent_ids[-1]["name"]:
            parent_id = parent_ids[-1]["id"]

    torrent_data = {
        "kind": "drive#file",
        "name": "",
        "upload_type": "UPLOAD_TYPE_URL",
        "url": {"url": file_url},
        "folder_type": "DOWNLOAD" if not parent_id else "",
        "parent_id": parent_id,
    }
    torrent_result = requests.post(url=torrent_url, headers=login_headers, json=torrent_data, timeout=5).json()

    if "error" in torrent_result:
        if torrent_result['error_code'] == 16:
            logging.info(f"帳號{account}登入過期，正在重新登入")
            login(account)
            login_headers = get_headers(account)
            torrent_result = requests.post(url=torrent_url, headers=login_headers, json=torrent_data, timeout=5).json()
        else:
            logging.error(f"帳號{account}提交離線下載任務失敗，錯誤訊息：{torrent_result['error_description']}")
            return None, None

    file_url_part = re.search(r'^(magnet:\?).*(xt=.+?)(&|$)', file_url)
    if file_url_part:
        file_url_simple = ''.join(file_url_part.groups()[:-1])
        logging.info(f"帳號{account}添加離線任務:{file_url_simple}")
    else:
        logging.info(f"帳號{account}添加離線任務:{file_url}")

    return torrent_result['task']['id'], torrent_result['task']['name']


def get_offline_list(account):
    login_headers = get_headers(account)
    tasks = []
    next_page_token = ""

    while True:
        offline_list_url = f"{PIKPAK_API_URL}/drive/v1/tasks?type=offline&page_token={next_page_token}&thumbnail_size=SIZE_LARGE&filters=%7B%7D&with=reference_resource"
        offline_list_info = requests.get(url=offline_list_url, headers=login_headers, timeout=5).json()
        if "error" in offline_list_info:
            if offline_list_info['error_code'] == 16:
                logging.info(f"帳號{account}登入過期，正在重新登入")
                login(account)
                login_headers = get_headers(account)
                continue
            else:
                logging.error(f"帳號{account}獲取離線任務失敗，錯誤訊息：{offline_list_info.get('error_description')}")
                return tasks

        tasks.extend(offline_list_info.get('tasks', []))

        next_page_token = offline_list_info.get('next_page_token', '')
        if not next_page_token:
            break

    return tasks


def get_download_url(file_id, account):
    for tries in range(3):
        try:
            login_headers = get_headers(account)
            download_url = f"{PIKPAK_API_URL}/drive/v1/files/{file_id}?_magic=2021&thumbnail_size=SIZE_LARGE"
            download_info = requests.get(url=download_url, headers=login_headers, timeout=5).json()

            if "error" in download_info:
                if download_info['error_code'] == 16:
                    logging.info(f"帳號{account}登入過期，正在重新登入")
                    login(account)
                    login_headers = get_headers(account)
                    download_info = requests.get(url=download_url, headers=login_headers, timeout=5).json()

                if "error" in download_info:
                    logging.error(f"帳號{account}獲取檔案下載資訊失敗，錯誤訊息：{download_info['error_description']}")
                    sleep(2)
                    continue

            return download_info['name'], download_info['web_content_link']

        except Exception as e:
            logging.error(f'帳號{account}獲取檔案下載資訊失敗（第{tries + 1}/3次）：{e}')
            sleep(2)
            continue

    return "", ""


def get_list(folder_id, account):
    try:
        file_list = []
        login_headers = get_headers(account)
        list_url = f"{PIKPAK_API_URL}/drive/v1/files?parent_id={folder_id}&thumbnail_size=SIZE_LARGE" + \
                   "&filters=%7B%22trashed%22:%7B%22eq%22:false%7D%7D"
        list_result = requests.get(url=list_url, headers=login_headers, timeout=5).json()
        if "error" in list_result:
            if list_result['error_code'] == 16:
                logging.info(f"帳號{account}登入過期，正在重新登入")
                login(account)
                login_headers = get_headers(account)
                list_result = requests.get(url=list_url, headers=login_headers, timeout=5).json()
            else:
                logging.error(f"帳號{account}獲取資料夾下檔案id失敗，錯誤訊息：{list_result['error_description']}")
                return file_list

        file_list += list_result['files']

        while list_result['next_page_token'] != "":
            list_url = f"{PIKPAK_API_URL}/drive/v1/files?parent_id={folder_id}&page_token=" + list_result[
                'next_page_token'] + \
                       "&thumbnail_size=SIZE_LARGE" + "&filters=%7B%22trashed%22:%7B%22eq%22:false%7D%7D "

            list_result = requests.get(url=list_url, headers=login_headers, timeout=5).json()

            file_list += list_result['files']

        return file_list

    except Exception as e:
        logging.error(f"帳號{account}獲取資料夾下檔案id失敗:{e}")
        return []


def get_folder_all_file(folder_id, path, account):
    folder_list = get_list(folder_id, account)
    for a in folder_list:
        if a["kind"] == "drive#file":
            down_name, down_url = get_download_url(a["id"], account)
            if down_name == "":
                continue
            yield down_name, down_url, a['id'], path
        elif a['name'] == 'My Pack' and folder_id == '':
            yield from get_folder_all_file(a["id"], path, account)
        else:
            new_path = path + a['name'] + "/"
            yield from get_folder_all_file(a["id"], new_path, account)


def get_folder_all(account):
    folder_list = get_list('', account)
    for a in folder_list:
        if a["kind"] == "drive#file":
            yield a['id']
        elif a["name"] == 'My Pack':
            for b in get_list(a['id'], account):
                yield b['id']
        else:
            yield a['id']


def delete_files(file_id, account, mode='normal'):
    if mode == 'normal':
        if auto_delete_judge(account) == 'off':
            logging.info('帳號{}未開啟自動清理'.format(account))
            return False
        else:
            logging.info('帳號{}開啟了自動清理'.format(account))
    login_headers = get_headers(account)
    delete_files_url = f"{PIKPAK_API_URL}/drive/v1/files:batchTrash"
    if type(file_id) == list:
        delete_files_data = {"ids": file_id}
    else:
        delete_files_data = {"ids": [file_id]}
    delete_files_result = requests.post(url=delete_files_url, headers=login_headers, json=delete_files_data,
                                        timeout=5).json()
    if "error" in delete_files_result:
        if delete_files_result['error_code'] == 16:
            logging.info(f"帳號{account}登入過期，正在重新登入")
            login(account)
            login_headers = get_headers(account)
            delete_files_result = requests.post(url=delete_files_url, headers=login_headers, json=delete_files_data,
                                                timeout=5).json()
        else:
            logging.error(f"帳號{account}刪除雲端硬碟檔案失敗，錯誤訊息：{delete_files_result['error_description']}")
            return False

    return True


def delete_trash(file_id, account, mode='normal'):
    if mode == 'normal':
        if auto_delete_judge(account) == 'off':
            logging.info('帳號{}未開啟自動清理'.format(account))
            return False
        else:
            logging.info('帳號{}開啟了自動清理'.format(account))
    login_headers = get_headers(account)
    delete_files_url = f"{PIKPAK_API_URL}/drive/v1/files:batchDelete"
    if type(file_id) == list:
        delete_files_data = {"ids": file_id}
    else:
        delete_files_data = {"ids": [file_id]}
    delete_files_result = requests.post(url=delete_files_url, headers=login_headers, json=delete_files_data,
                                        timeout=5).json()
    if "error" in delete_files_result:
        if delete_files_result['error_code'] == 16:
            logging.info(f"帳號{account}登入過期，正在重新登入")
            login(account)
            login_headers = get_headers(account)
            delete_files_result = requests.post(url=delete_files_url, headers=login_headers, json=delete_files_data,
                                                timeout=5).json()
        else:
            logging.error(f"帳號{account}刪除垃圾桶檔案失敗，錯誤訊息：{delete_files_result['error_description']}")
            return False

    return True


def delete_offline_tasks(account, task_ids=None, delete_files_too=False, phase_filter=None):
    """
    刪除離線任務記錄
    account: 帳號
    task_ids: 指定要刪除的任務 ID 列表，如果為 None 則根據 phase_filter 刪除
    delete_files_too: 是否同時刪除雲端檔案
    phase_filter: 篩選特定狀態的任務 (如 'PHASE_TYPE_ERROR')，None 表示全部

    返回: (success_count, fail_count)
    """
    login_headers = get_headers(account)

    if task_ids is None:
        tasks = get_offline_list(account)
        if phase_filter:
            task_ids = [t['id'] for t in tasks if t.get('phase') == phase_filter]
        else:
            task_ids = [t['id'] for t in tasks]

    if not task_ids:
        logging.info(f"帳號{account}沒有需要刪除的離線任務記錄")
        return 0, 0

    logging.info(f"帳號{account}準備刪除 {len(task_ids)} 個離線任務記錄")

    success_count = 0
    fail_count = 0
    batch_size = 50

    for i in range(0, len(task_ids), batch_size):
        batch = task_ids[i:i + batch_size]

        delete_url = f"{PIKPAK_API_URL}/drive/v1/tasks"
        params = {
            "task_ids": ",".join(batch),
            "delete_files": "true" if delete_files_too else "false",
        }

        try:
            result = requests.delete(url=delete_url, headers=login_headers, params=params, timeout=15)

            if result.status_code == 200:
                success_count += len(batch)
                logging.info(f"帳號{account}成功刪除 {len(batch)} 個離線任務記錄")
            else:
                if result.status_code == 401 or 'error_code' in result.text:
                    logging.info(f"帳號{account}登入過期，正在重新登入")
                    login(account)
                    login_headers = get_headers(account)
                    result = requests.delete(url=delete_url, headers=login_headers, params=params, timeout=15)
                    if result.status_code == 200:
                        success_count += len(batch)
                        logging.info(f"帳號{account}重試後成功刪除 {len(batch)} 個離線任務記錄")
                    else:
                        fail_count += len(batch)
                        logging.error(f"帳號{account}刪除離線任務記錄失敗: {result.text}")
                else:
                    fail_count += len(batch)
                    logging.error(f"帳號{account}刪除離線任務記錄失敗: {result.text}")
        except Exception as e:
            fail_count += len(batch)
            logging.error(f"帳號{account}刪除離線任務記錄時發生錯誤: {e}")

        sleep(1)

    logging.info(f"帳號{account}離線任務記錄清理完成: 成功 {success_count}, 失敗 {fail_count}")
    return success_count, fail_count


def empty_trash(account):
    """清空回收站中的所有檔案"""
    login_headers = get_headers(account)
    empty_url = f"{PIKPAK_API_URL}/drive/v1/files/trash:empty"

    try:
        result = requests.post(url=empty_url, headers=login_headers, json={}, timeout=15)

        if result.status_code == 200:
            logging.info(f"帳號{account}回收站已清空")
            return True
        else:
            if 'error_code' in result.text:
                login(account)
                login_headers = get_headers(account)
                result = requests.post(url=empty_url, headers=login_headers, json={}, timeout=15)
                if result.status_code == 200:
                    logging.info(f"帳號{account}回收站已清空")
                    return True
            logging.error(f"帳號{account}清空回收站失敗: {result.text}")
            return False
    except Exception as e:
        logging.error(f"帳號{account}清空回收站時發生錯誤: {e}")
        return False


def retry_offline_task(task_id, account):
    """
    使用 PikPak 的 RETRY 功能重新開始離線任務
    這會讓 PikPak 重新嘗試下載，不需要原始 magnet link
    """
    login_headers = get_headers(account)
    retry_url = f"{PIKPAK_API_URL}/drive/v1/task"
    retry_data = {
        "type": "offline",
        "create_type": "RETRY",
        "id": task_id,
    }

    try:
        result = requests.post(url=retry_url, headers=login_headers, json=retry_data, timeout=10).json()

        if "error" in result:
            if result['error_code'] == 16:
                logging.info(f"帳號{account}登入過期，正在重新登入")
                login(account)
                login_headers = get_headers(account)
                result = requests.post(url=retry_url, headers=login_headers, json=retry_data, timeout=10).json()
            else:
                logging.error(f"帳號{account}重試任務失敗: {result.get('error_description', result)}")
                return False, result.get('error_description', 'Unknown error')

        logging.info(f"帳號{account}成功重試任務 {task_id}")
        return True, result
    except Exception as e:
        logging.error(f"帳號{account}重試任務時發生錯誤: {e}")
        return False, str(e)


def delete_offline_task(task_ids, account, delete_files=False):
    """
    刪除離線任務
    task_ids: 單個 task_id 或 list of task_ids
    delete_files: 是否同時刪除雲端檔案
    """
    login_headers = get_headers(account)
    delete_url = f"{PIKPAK_API_URL}/drive/v1/tasks"

    if isinstance(task_ids, str):
        task_ids = [task_ids]

    params = {
        "task_ids": ",".join(task_ids),
        "delete_files": "true" if delete_files else "false",
    }

    try:
        result = requests.delete(url=delete_url, headers=login_headers, params=params, timeout=10)

        if result.status_code == 200:
            logging.info(f"帳號{account}成功刪除 {len(task_ids)} 個任務")
            return True, None
        else:
            error_msg = result.text
            logging.error(f"帳號{account}刪除任務失敗: {error_msg}")
            return False, error_msg
    except Exception as e:
        logging.error(f"帳號{account}刪除任務時發生錯誤: {e}")
        return False, str(e)


def get_stuck_tasks(account, min_progress=90):
    """
    獲取卡住的離線任務
    min_progress: 最小進度閾值，預設 90%
    返回: [{id, name, progress, file_id}, ...]
    """
    tasks = get_offline_list(account)
    stuck = []

    logging.debug(f"帳號{account}共有 {len(tasks)} 個離線任務，篩選進度 >= {min_progress}%")

    for task in tasks:
        phase = task.get('phase', '')
        progress = int(task.get('progress', 0))
        message = task.get('message', '')
        name = task.get('name') or task.get('file_name') or 'Unknown'

        logging.debug(f"  任務: {name}, phase={phase}, progress={progress}%")

        if phase == 'PHASE_TYPE_COMPLETE' and progress == 100:
            continue

        if "file deleted" in message.lower() or "file_deleted" in message.lower():
            continue

        if phase == 'PHASE_TYPE_ERROR':
            continue

        if progress >= min_progress and progress < 100:
            stuck.append({
                'id': task.get('id'),
                'name': name,
                'progress': progress,
                'file_id': task.get('file_id'),
                'phase': phase,
            })
            logging.debug(f"    ↳ 判定為卡住的任務")

    return stuck


def retry_stuck_tasks(account, min_progress=90, delete_cloud_files=True):
    """
    找出並重試卡住的任務
    1. 找出進度 >= min_progress 但未完成的任務
    2. 刪除這些任務的雲端檔案 (可選)
    3. 使用 PikPak 的 RETRY 功能重新開始

    返回: (success_count, fail_count, results)
    """
    # 延遲導入以避免循環依賴 (pipeline 需要 pikpak_client)
    from pikpakbot.pipeline import process_magnet, thread_list

    stuck_tasks = get_stuck_tasks(account, min_progress)

    if not stuck_tasks:
        logging.info(f"帳號{account}沒有找到卡住的任務 (進度 >= {min_progress}%)")
        return 0, 0, []

    logging.info(f"🔄 帳號{account}找到 {len(stuck_tasks)} 個卡住的任務 (進度 >= {min_progress}%)")

    results = []
    success_count = 0
    fail_count = 0
    total = len(stuck_tasks)

    for i, task in enumerate(stuck_tasks, 1):
        task_id = task['id']
        task_name = task['name']
        file_id = task.get('file_id')
        progress = task['progress']

        logging.info(f"[{i}/{total}] 正在處理: {task_name} ({progress}%)")

        if delete_cloud_files and file_id:
            try:
                delete_files(file_id, account, mode='force')
                delete_trash(file_id, account, mode='force')
                logging.info(f"  ↳ 已刪除雲端不完整檔案")
            except Exception as e:
                logging.warning(f"  ↳ 刪除雲端檔案失敗 (繼續重試): {e}")

        success, result = retry_offline_task(task_id, account)

        if success:
            success_count += 1
            logging.info(f"  ↳ ✅ 已重新加入佇列")

            new_task_id = result.get('task', {}).get('id') if isinstance(result, dict) else None
            task_info = {
                'id': new_task_id or task_id,
                'name': task_name
            }
            thread_list.append(threading.Thread(
                target=process_magnet,
                args=[None, None, None, None, None, task_info, account]
            ))
            thread_list[-1].start()
            logging.info(f"  ↳ 已啟動監控線程，等待完成後將推送 Aria2")

            results.append({
                'name': task_name,
                'progress': progress,
                'status': 'success',
                'message': '已重新加入佇列並啟動監控'
            })
        else:
            fail_count += 1
            logging.error(f"  ↳ ❌ 重試失敗: {result}")
            results.append({
                'name': task_name,
                'progress': progress,
                'status': 'fail',
                'message': str(result)
            })

        sleep(2)

    logging.info(f"✅ 帳號{account}重試完成: 成功 {success_count}, 失敗 {fail_count}")
    return success_count, fail_count, results


def get_my_vip(account):
    try:
        login_headers = get_headers(account)

        me_url = f"{PIKPAK_API_URL}/drive/v1/privilege/vip"
        me_result = requests.get(url=me_url, headers=login_headers, timeout=5).json()
    except Exception:
        return 3

    if "error" in me_result:
        if me_result['error_code'] == 16:
            logging.info(f"帳號{account}登入過期，正在重新登入")
            login(account)
            login_headers = get_headers(account)
            me_result = requests.get(url=me_url, headers=login_headers, timeout=5).json()
        else:
            logging.error(f"獲取vip訊息失敗{me_result['error_description']}")
            return 3

    if me_result['data']['status'] == 'ok':
        return 0
    elif me_result['data']['status'] == 'invalid':
        return 1
    else:
        return 2
