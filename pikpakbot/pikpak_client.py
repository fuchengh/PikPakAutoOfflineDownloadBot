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

DEFAULT_TIMEOUT = 30
DEFAULT_MAX_ATTEMPTS = 3

pikpak_headers = [None] * len(USER)
pikpak_clients = [None] * len(USER)
login_lock = threading.Lock()


class PikPakError(Exception):
    """Raised when a PikPak API call fails after exhausting retries."""


def _pikpak_request(method, url, account, *, max_attempts=DEFAULT_MAX_ATTEMPTS,
                    timeout=DEFAULT_TIMEOUT, **kwargs):
    """
    Make a PikPak HTTP request with automatic recovery.

    Handles:
      - Session expiry (error_code 16 or HTTP 401) -> re-login, retry (does not consume an attempt; one re-login per call).
      - Transient network errors (ReadTimeout / ConnectionError) -> exponential backoff.
      - Rate limit (HTTP 429 / body contains "too_frequent") -> longer backoff.

    Returns (response, parsed_json_or_empty_dict).
    Raises the last network exception (or PikPakError) if all attempts fail.

    Caller still inspects the returned dict for application-level errors that
    aren't auto-recoverable (e.g. error_description for unknown magnet).
    """
    last_exc = None
    relogin_used = False

    for attempt in range(max_attempts):
        try:
            login_headers = get_headers(account)
            resp = requests.request(method, url, headers=login_headers, timeout=timeout, **kwargs)

            try:
                data = resp.json() if resp.content else {}
            except ValueError:
                data = {}

            session_expired = (
                resp.status_code == 401
                or (isinstance(data, dict) and data.get('error_code') == 16)
            )
            if session_expired and not relogin_used:
                logging.info(f"帳號{account}登入過期，正在重新登入")
                login(account)
                relogin_used = True
                continue

            body_text = resp.text if resp.status_code >= 400 else ''
            rate_limited = (
                resp.status_code == 429
                or 'too_frequent' in body_text.lower()
                or 'too frequent' in body_text.lower()
            )
            if rate_limited:
                wait = 5 * (2 ** attempt)
                logging.warning(
                    f"帳號{account}被 PikPak 限流 (HTTP {resp.status_code})，{wait}s 後重試 ({attempt + 1}/{max_attempts})"
                )
                sleep(wait)
                continue

            return resp, data

        except (requests.exceptions.ReadTimeout,
                requests.exceptions.ConnectTimeout,
                requests.exceptions.ConnectionError) as e:
            last_exc = e
            wait = 2 ** attempt
            logging.warning(
                f"帳號{account} {method} 請求失敗 ({attempt + 1}/{max_attempts}): {e}，{wait}s 後重試"
            )
            sleep(wait)

    if last_exc:
        raise last_exc
    raise PikPakError(f"帳號{account} {method} {url} 重試 {max_attempts} 次後仍失敗")


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

    try:
        _resp, torrent_result = _pikpak_request('POST', torrent_url, account, json=torrent_data)
    except Exception as e:
        logging.error(f"帳號{account}提交離線下載任務失敗: {e}")
        return None, None

    if "error" in torrent_result:
        logging.error(f"帳號{account}提交離線下載任務失敗，錯誤訊息：{torrent_result.get('error_description')}")
        return None, None

    file_url_part = re.search(r'^(magnet:\?).*(xt=.+?)(&|$)', file_url)
    if file_url_part:
        file_url_simple = ''.join(file_url_part.groups()[:-1])
        logging.info(f"帳號{account}添加離線任務:{file_url_simple}")
    else:
        logging.info(f"帳號{account}添加離線任務:{file_url}")

    return torrent_result['task']['id'], torrent_result['task']['name']


def get_offline_list(account):
    tasks = []
    next_page_token = ""

    while True:
        offline_list_url = (
            f"{PIKPAK_API_URL}/drive/v1/tasks?type=offline&page_token={next_page_token}"
            "&thumbnail_size=SIZE_LARGE&filters=%7B%7D&with=reference_resource"
        )
        try:
            _resp, offline_list_info = _pikpak_request('GET', offline_list_url, account)
        except Exception as e:
            logging.error(f"帳號{account}獲取離線任務失敗: {e}")
            return tasks

        if "error" in offline_list_info:
            logging.error(f"帳號{account}獲取離線任務失敗，錯誤訊息：{offline_list_info.get('error_description')}")
            return tasks

        tasks.extend(offline_list_info.get('tasks', []))

        next_page_token = offline_list_info.get('next_page_token', '')
        if not next_page_token:
            break

    return tasks


def get_download_url(file_id, account):
    download_url = f"{PIKPAK_API_URL}/drive/v1/files/{file_id}?_magic=2021&thumbnail_size=SIZE_LARGE"

    try:
        _resp, download_info = _pikpak_request('GET', download_url, account)
    except Exception as e:
        logging.error(f"帳號{account}獲取檔案下載資訊失敗: {e}")
        return "", ""

    if "error" in download_info:
        logging.error(f"帳號{account}獲取檔案下載資訊失敗，錯誤訊息：{download_info.get('error_description')}")
        return "", ""

    return download_info['name'], download_info['web_content_link']


def get_list(folder_id, account):
    file_list = []
    list_url = (
        f"{PIKPAK_API_URL}/drive/v1/files?parent_id={folder_id}&thumbnail_size=SIZE_LARGE"
        "&filters=%7B%22trashed%22:%7B%22eq%22:false%7D%7D"
    )

    try:
        _resp, list_result = _pikpak_request('GET', list_url, account)
    except Exception as e:
        logging.error(f"帳號{account}獲取資料夾下檔案id失敗: {e}")
        return file_list

    if "error" in list_result:
        logging.error(f"帳號{account}獲取資料夾下檔案id失敗，錯誤訊息：{list_result.get('error_description')}")
        return file_list

    file_list += list_result['files']

    while list_result.get('next_page_token'):
        list_url = (
            f"{PIKPAK_API_URL}/drive/v1/files?parent_id={folder_id}&page_token={list_result['next_page_token']}"
            "&thumbnail_size=SIZE_LARGE&filters=%7B%22trashed%22:%7B%22eq%22:false%7D%7D"
        )
        try:
            _resp, list_result = _pikpak_request('GET', list_url, account)
        except Exception as e:
            logging.error(f"帳號{account}獲取資料夾下檔案id分頁失敗: {e}")
            return file_list

        if "error" in list_result:
            logging.error(f"帳號{account}獲取資料夾下檔案id分頁失敗，錯誤訊息：{list_result.get('error_description')}")
            return file_list

        file_list += list_result.get('files', [])

    return file_list


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


def _batch_delete(file_id, account, endpoint, action_label):
    delete_url = f"{PIKPAK_API_URL}{endpoint}"
    if isinstance(file_id, list):
        payload = {"ids": file_id}
    else:
        payload = {"ids": [file_id]}

    try:
        _resp, result = _pikpak_request('POST', delete_url, account, json=payload)
    except Exception as e:
        logging.error(f"帳號{account}{action_label}失敗: {e}")
        return False

    if "error" in result:
        logging.error(f"帳號{account}{action_label}失敗，錯誤訊息：{result.get('error_description')}")
        return False

    return True


def delete_files(file_id, account, mode='normal'):
    if mode == 'normal':
        if auto_delete_judge(account) == 'off':
            logging.info('帳號{}未開啟自動清理'.format(account))
            return False
        else:
            logging.info('帳號{}開啟了自動清理'.format(account))
    return _batch_delete(file_id, account, "/drive/v1/files:batchTrash", "刪除雲端硬碟檔案")


def delete_trash(file_id, account, mode='normal'):
    if mode == 'normal':
        if auto_delete_judge(account) == 'off':
            logging.info('帳號{}未開啟自動清理'.format(account))
            return False
        else:
            logging.info('帳號{}開啟了自動清理'.format(account))
    return _batch_delete(file_id, account, "/drive/v1/files:batchDelete", "刪除垃圾桶檔案")


def delete_offline_tasks(account, task_ids=None, delete_files_too=False, phase_filter=None):
    """
    刪除離線任務記錄
    account: 帳號
    task_ids: 指定要刪除的任務 ID 列表，如果為 None 則根據 phase_filter 刪除
    delete_files_too: 是否同時刪除雲端檔案
    phase_filter: 篩選特定狀態的任務 (如 'PHASE_TYPE_ERROR')，None 表示全部

    返回: (success_count, fail_count)
    """
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
            resp, _data = _pikpak_request('DELETE', delete_url, account, params=params)
            if resp.status_code == 200:
                success_count += len(batch)
                logging.info(f"帳號{account}成功刪除 {len(batch)} 個離線任務記錄")
            else:
                fail_count += len(batch)
                logging.error(f"帳號{account}刪除離線任務記錄失敗: HTTP {resp.status_code} {resp.text}")
        except Exception as e:
            fail_count += len(batch)
            logging.error(f"帳號{account}刪除離線任務記錄時發生錯誤: {e}")

        sleep(1)

    logging.info(f"帳號{account}離線任務記錄清理完成: 成功 {success_count}, 失敗 {fail_count}")
    return success_count, fail_count


def cancel_offline_tasks_by_name(account, name, delete_cloud_files=True):
    """Cancel/delete all offline tasks on PikPak whose name matches `name`.

    Used by the retry button: before re-submitting a failed magnet, kill any
    leftover offline tasks (still downloading, errored, or completed-with-orphan-
    cloud-folder) so PikPak doesn't end up showing two duplicates and the cloud
    doesn't accumulate orphan folders.

    Returns (matched_count, success_count, fail_count). matched_count==0 means
    nothing to clean — not an error.
    """
    if not name or not account:
        return 0, 0, 0
    tasks = get_offline_list(account)
    matches = [t['id'] for t in tasks if t.get('name') == name and t.get('id')]
    if not matches:
        return 0, 0, 0
    logging.info(f"帳號{account} retry 前清理：找到 {len(matches)} 個同名舊離線任務 ({name!r})")
    success, fail = delete_offline_tasks(
        account, task_ids=matches, delete_files_too=delete_cloud_files
    )
    return len(matches), success, fail


def empty_trash(account):
    """清空回收站中的所有檔案"""
    empty_url = f"{PIKPAK_API_URL}/drive/v1/files/trash:empty"

    try:
        resp, _data = _pikpak_request('POST', empty_url, account, json={})
    except Exception as e:
        logging.error(f"帳號{account}清空回收站時發生錯誤: {e}")
        return False

    if resp.status_code == 200:
        logging.info(f"帳號{account}回收站已清空")
        return True

    logging.error(f"帳號{account}清空回收站失敗: HTTP {resp.status_code} {resp.text}")
    return False


def retry_offline_task(task_id, account):
    """
    使用 PikPak 的 RETRY 功能重新開始離線任務
    這會讓 PikPak 重新嘗試下載，不需要原始 magnet link
    """
    retry_url = f"{PIKPAK_API_URL}/drive/v1/task"
    retry_data = {
        "type": "offline",
        "create_type": "RETRY",
        "id": task_id,
    }

    try:
        _resp, result = _pikpak_request('POST', retry_url, account, json=retry_data)
    except Exception as e:
        logging.error(f"帳號{account}重試任務時發生錯誤: {e}")
        return False, str(e)

    if "error" in result:
        logging.error(f"帳號{account}重試任務失敗: {result.get('error_description', result)}")
        return False, result.get('error_description', 'Unknown error')

    logging.info(f"帳號{account}成功重試任務 {task_id}")
    return True, result


def delete_offline_task(task_ids, account, delete_files=False):
    """
    刪除離線任務
    task_ids: 單個 task_id 或 list of task_ids
    delete_files: 是否同時刪除雲端檔案
    """
    delete_url = f"{PIKPAK_API_URL}/drive/v1/tasks"

    if isinstance(task_ids, str):
        task_ids = [task_ids]

    params = {
        "task_ids": ",".join(task_ids),
        "delete_files": "true" if delete_files else "false",
    }

    try:
        resp, _data = _pikpak_request('DELETE', delete_url, account, params=params)
    except Exception as e:
        logging.error(f"帳號{account}刪除任務時發生錯誤: {e}")
        return False, str(e)

    if resp.status_code == 200:
        logging.info(f"帳號{account}成功刪除 {len(task_ids)} 個任務")
        return True, None

    logging.error(f"帳號{account}刪除任務失敗: HTTP {resp.status_code} {resp.text}")
    return False, resp.text


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


def retry_stuck_tasks(account, min_progress=90, delete_cloud_files=True, notifier=None):
    """
    找出並重試卡住的任務
    1. 找出進度 >= min_progress 但未完成的任務
    2. 刪除這些任務的雲端檔案 (可選)
    3. 使用 PikPak 的 RETRY 功能重新開始

    notifier 會傳給 spawn 出來的 process_magnet 線程，讓後續通知去對的地方。

    返回: (success_count, fail_count, results)
    """
    # 延遲導入以避免循環依賴 (pipeline 需要 pikpak_client)
    from pikpakbot.pipeline import process_magnet, thread_list
    from pikpakbot.notifier import NullNotifier

    if notifier is None:
        notifier = NullNotifier()

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
            # Per-task sub-channel so the watchdog's auto-retries don't all
            # dump their progress + failure into the main channel. TG no-op.
            task_notifier = notifier.create_task_channel(task_name or '卡住任務重試')
            thread_list.append(threading.Thread(
                target=process_magnet,
                args=[task_notifier, None, None, None, task_info, account]
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
    me_url = f"{PIKPAK_API_URL}/drive/v1/privilege/vip"

    try:
        _resp, me_result = _pikpak_request('GET', me_url, account)
    except Exception:
        return 3

    if "error" in me_result:
        logging.error(f"獲取vip訊息失敗{me_result.get('error_description')}")
        return 3

    if me_result['data']['status'] == 'ok':
        return 0
    elif me_result['data']['status'] == 'invalid':
        return 1
    else:
        return 2
