import asyncio
import logging
import threading
from time import sleep, monotonic
from random import uniform

from pikpakbot.http import session
from pikpakbot import state
import config

import requests
from pikpakapi import PikPakApi

from config import USER, PASSWORD, AUTO_DELETE

PIKPAK_API_URL = "https://api-drive.mypikpak.com"
PIKPAK_USER_URL = "https://user.mypikpak.com"

DEFAULT_TIMEOUT = 30
DEFAULT_MAX_ATTEMPTS = 3

pikpak_headers = {}
pikpak_clients = {}
login_lock = threading.RLock()
_snapshot_lock = threading.Lock()
_snapshot_locks = {}
_snapshots = {}


def forget_account(account):
    with login_lock:
        pikpak_headers.pop(account, None)
        pikpak_clients.pop(account, None)
    invalidate_offline(account)


def invalidate_offline(account):
    with _snapshot_lock:
        _snapshots.pop(account, None)



class PikPakError(Exception):
    """Raised when a PikPak API call fails after exhausting retries."""


class AmbiguousMutation(PikPakError):
    """The provider may have accepted a write; don't submit it again elsewhere."""


def _pikpak_request(method, url, account, *, max_attempts=DEFAULT_MAX_ATTEMPTS,
                    timeout=DEFAULT_TIMEOUT, **kwargs):
    """Strict responses; retry transient reads with jitter, never return partial data.

    A timed-out mutation may have succeeded: don't blindly duplicate POSTs.
    Authentication refresh has its own one-shot budget.
    """
    relogin_used = False
    attempt = 0
    while attempt < max_attempts:
        try:
            resp = session().request(method, url, headers=get_headers(account),
                                     timeout=timeout, **kwargs)
        except (requests.Timeout, requests.ConnectionError):
            attempt += 1
            if method.upper() not in ('GET', 'DELETE') or attempt >= max_attempts:
                raise
            sleep(2 ** (attempt - 1) + uniform(0, 1))
            continue
        try:
            data = resp.json() if resp.content else {}
        except ValueError as exc:
            error_type = AmbiguousMutation if method.upper() == 'POST' else PikPakError
            raise error_type(f'PikPak HTTP {resp.status_code}: invalid JSON') from exc
        if not isinstance(data, dict):
            error_type = AmbiguousMutation if method.upper() == 'POST' else PikPakError
            raise error_type('PikPak 回應不是 JSON object')
        if (resp.status_code == 401 or str(data.get('error_code')) == '16') and not relogin_used:
            login(account)
            relogin_used = True
            continue
        description = str(data.get('error_description') or data.get('error') or '')
        transient = resp.status_code == 429 or (resp.status_code >= 500 and method.upper() == 'GET')
        if transient:
            attempt += 1
            if attempt < max_attempts:
                try:
                    wait = min(60, max(0, float(resp.headers.get('Retry-After', 0))))
                except (ValueError, TypeError):
                    wait = 0
                sleep(max(wait, 2 ** attempt) + uniform(0, 1))
                continue
        if resp.status_code >= 400 or data.get('error') or data.get('error_code') not in (None, 0, '0'):
            error_type = AmbiguousMutation if method.upper() == 'POST' and resp.status_code >= 500 else PikPakError
            raise error_type(f'PikPak HTTP {resp.status_code}, code={data.get("error_code")}: {description}')
        if method.upper() != 'GET':
            invalidate_offline(account)
        return resp, data
    raise PikPakError('PikPak 暫時錯誤重試次數已用盡')


def registerFuc():
    try:
        url = 'https://pikpak.kinh.cc/GetFreeAccount.php'
        resp = session().get(url, timeout=DEFAULT_TIMEOUT)
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
        pikpak_headers[account] = headers.copy()
        pikpak_clients[account] = client

        logging.info(f"帳號{account}登入成功！")


def get_headers(account):
    with login_lock:
        if account not in USER:
            raise PikPakError('帳號已移除')
        if account not in pikpak_headers:
            login(account)
        return pikpak_headers[account].copy()


def get_clients(account):
    with login_lock:
        if account not in USER:
            raise PikPakError('帳號已移除')
        if account not in pikpak_clients:
            login(account)
        return pikpak_clients[account]


def magnet_upload(file_url, account, parent_id=None, offline_path=None):
    if offline_path:
        if not offline_path.startswith('/') or '..' in offline_path.split('/'):
            raise ValueError('離線路徑必須是絕對路徑且不能包含 ..')
        for part in filter(None, offline_path.split('/')):
            folders = get_list(parent_id or '', account)
            match = next((f for f in folders if f['name'] == part and f['kind'] == 'drive#folder'), None)
            if match:
                parent_id = match['id']
            else:
                _, folder = _pikpak_request('POST', f'{PIKPAK_API_URL}/drive/v1/files', account,
                    json={'kind': 'drive#folder', 'name': part, 'parent_id': parent_id or ''})
                parent_id = folder['id']
    _, result = _pikpak_request('POST', f'{PIKPAK_API_URL}/drive/v1/files', account, json={
        'kind': 'drive#file', 'name': '', 'upload_type': 'UPLOAD_TYPE_URL',
        'url': {'url': file_url}, 'folder_type': 'DOWNLOAD' if not parent_id else '',
        'parent_id': parent_id,
    })
    task = result.get('task') or {}
    if not task.get('id'):
        raise AmbiguousMutation('提交離線任務的回應缺少 task.id')
    return task['id'], task.get('name') or file_url[:80]


def _pages(endpoint, account, key, params):
    items, seen = [], set()
    token = ''
    while True:
        _, data = _pikpak_request('GET', f'{PIKPAK_API_URL}{endpoint}', account,
                                  params={**params, 'page_token': token})
        if not isinstance(data.get(key), list):
            raise PikPakError(f'PikPak 列表回應缺少 {key}')
        items.extend(data[key])
        token = data.get('next_page_token') or ''
        if not token:
            return items
        if token in seen:
            raise PikPakError('PikPak 分頁 token 重複；列表不完整')
        seen.add(token)


def get_offline_list(account, *, fresh=False):
    """Coalesce per-account reads, including failed snapshots; never use stale data."""
    with _snapshot_lock:
        lock = _snapshot_locks.setdefault(account, threading.Lock())
    with lock:
        with _snapshot_lock:
            cached = _snapshots.get(account)
        if not fresh and cached and monotonic() - cached[0] < 5:
            if isinstance(cached[1], Exception):
                raise cached[1]
            return [dict(t) for t in cached[1]]
        try:
            tasks = _pages('/drive/v1/tasks', account, 'tasks', {
                'type': 'offline', 'thumbnail_size': 'SIZE_LARGE', 'filters': '{}',
                'with': 'reference_resource'})
        except Exception as exc:
            with _snapshot_lock:
                _snapshots[account] = (monotonic(), exc)
            raise
        state.observe(account, tasks)
        with _snapshot_lock:
            _snapshots[account] = (monotonic(), tasks)
        return [dict(t) for t in tasks]


def get_download_url(file_id, account):
    _, data = _pikpak_request('GET', f'{PIKPAK_API_URL}/drive/v1/files/{file_id}', account,
                              params={'_magic': '2021', 'thumbnail_size': 'SIZE_LARGE'})
    if not data.get('name'):
        raise PikPakError(f'檔案 {file_id} 缺少名稱')
    if data.get('kind') == 'drive#folder':
        return data['name'], ''
    if not data.get('web_content_link'):
        raise PikPakError(f'檔案 {file_id} 尚無可用下載連結')
    return data['name'], data['web_content_link']


def get_list(folder_id, account):
    return _pages('/drive/v1/files', account, 'files', {
        'parent_id': folder_id, 'thumbnail_size': 'SIZE_LARGE',
        'filters': '{"trashed":{"eq":false}}'})


def get_folder_all_file(folder_id, path, account):
    for item in get_list(folder_id, account):
        if item['kind'] == 'drive#file':
            name, url = get_download_url(item['id'], account)
            if not url:
                raise PikPakError(f'檔案 {item["id"]} 缺少下載連結')
            yield name, url, item['id'], path
        else:
            # Validate every remote directory component before constructing paths.
            name = item['name']
            if name in ('.', '..') or any(c in name for c in '/\\'):
                raise PikPakError('雲端資料夾名稱包含路徑分隔符')
            yield from get_folder_all_file(item['id'], path + name + '/', account)


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
    except (requests.RequestException, AmbiguousMutation) as exc:
        raise AmbiguousMutation(f'PikPak RETRY 結果尚未確認：{exc}') from exc
    except PikPakError as e:
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


def get_stuck_tasks(account, min_progress=90, *, include_errors=True):
    tasks = get_offline_list(account)
    ages = state.observe(account, tasks)
    threshold = getattr(config, 'OFFLINE_STALL_SECONDS', 1200)
    result = []
    for task in tasks:
        phase = task.get('phase')
        progress = int(task.get('progress') or 0)
        message = str(task.get('message') or '').lower()
        if 'file deleted' in message or 'file_deleted' in message:
            continue
        failed = phase == 'PHASE_TYPE_ERROR'
        stalled = (phase in ('PHASE_TYPE_RUNNING', 'PHASE_TYPE_PENDING')
                   and progress >= min_progress and ages.get(task.get('id'), 0) >= threshold)
        if (include_errors and failed) or stalled:
            result.append({**task, 'name': task.get('name') or task.get('file_name') or 'Unknown',
                           'progress': progress})
    return result


def retry_stuck_tasks(account, min_progress=90, delete_cloud_files=False, notifier=None,
                      *, automatic=False):
    # Keep the old keyword for callers, but NEVER delete data to retry.
    from pikpakbot.pipeline import retry_offline_candidates
    try:
        return retry_offline_candidates(account, min_progress, notifier, automatic=automatic)
    except (requests.RequestException, PikPakError) as exc:
        return 0, 1, [{'name': account, 'status': 'fail', 'message': f'查詢失敗，未重試：{exc}'}]


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
