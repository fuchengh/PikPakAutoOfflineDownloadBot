"""Bounded workers, durable attempts and conservative cloud cleanup."""
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading
from time import monotonic, sleep, time
import uuid

import requests
import config
from config import USER, ARIA2_DOWNLOAD_PATH
from pikpakbot import aria2_client, state
from pikpakbot.links import source_key
from pikpakbot.notifier import ActionButton, NullNotifier
from pikpakbot.pikpak_client import (
    PikPakError, AmbiguousMutation, magnet_upload, get_offline_list, get_download_url,
    get_folder_all_file, delete_files, delete_trash, retry_offline_task,
    get_stuck_tasks, auto_delete_judge,
)

_executor = ThreadPoolExecutor(max_workers=max(1, getattr(config, 'DOWNLOAD_WORKERS', 4)),
                               thread_name_prefix='pikpak')
_slots = threading.BoundedSemaphore(max(1, getattr(config, 'MAX_QUEUED_TASKS', 128)))
_jobs_lock = threading.Lock()
_jobs = set()
batch_lock = threading.Lock()
batch_results = {}


def _retry_buttons(task_id):
    return [ActionButton('🔄 重試', 'retry', task_id), ActionButton('✖️ 忽略', 'dismiss', task_id)]


def _bind(notifier, task_id):
    if hasattr(notifier, 'bind_task'):
        notifier.bind_task(task_id)


def _launch(task_id, notifier, batch_id=None):
    notifier = notifier or NullNotifier()
    if not _slots.acquire(blocking=False):
        state.update_task(task_id, stage=state.STAGE_FAILED, error='下載佇列已滿，請稍後重試')
        notifier.send('❌ 下載佇列已滿', buttons=_retry_buttons(task_id))
        record_batch_result(batch_id, 'fail', task_id, '下載佇列已滿', notifier)
        raise state.TaskConflict('下載佇列已滿，請稍後重試')
    try:
        row = state.get_task(task_id)
        state.claim(task_id, 'worker:' + task_id)
        if row['magnet']:
            state.claim(task_id, 'source:' + source_key(row['magnet']))
        if row['cloud_task_id']:
            state.claim(task_id, f'cloud-task:{row["account"]}:{row["cloud_task_id"]}')
        if row['cloud_file_id']:
            state.claim(task_id, f'cloud-file:{row["account"]}:{row["cloud_file_id"]}')
        _bind(notifier, task_id)
        future = _executor.submit(_run_task, task_id, notifier, batch_id)
        with _jobs_lock:
            _jobs.add(future)
        def finished(done):
            with _jobs_lock:
                _jobs.discard(done)
            _slots.release()
        future.add_done_callback(finished)
    except Exception as exc:
        state.release(task_id, preserve_files=True)
        state.update_task(task_id, stage=state.STAGE_FAILED, error=str(exc))
        _slots.release()
        notifier.send(f'❌ {exc}', buttons=_retry_buttons(task_id))
        record_batch_result(batch_id, 'fail', task_id, str(exc), notifier)
        raise
    return task_id


def submit_magnet(notifier, magnet, offline_path=None, batch_id=None):
    source_key(magnet)
    task_id = state.create_task(magnet=magnet, offline_path=offline_path)
    return _launch(task_id, notifier, batch_id)


def submit_cloud_file(notifier, file_id, account):
    task_id = state.create_task(account=account, source_kind='cloud', cloud_file_id=file_id)
    return _launch(task_id, notifier)


def retry_task(notifier, task_id, *, recover=False):
    child = state.claim_retry(task_id)
    row = state.get_task(child)
    if recover:
        state.update_task(child, auto_retries=state.get_task(task_id)['auto_retries'])
    if row['cloud_task_id'] and not row['cloud_file_id']:
        state.update_task(child, retry_requested=0 if recover else 1)
    return _launch(child, notifier)


def check_download_thread_status():
    with _jobs_lock:
        return bool(_jobs)


def _offline(task_id, notifier):
    row = state.get_task(task_id)
    if not row['cloud_task_id']:
        errors = []
        accounts = [row['account']] if row['account'] else list(USER)
        for account in accounts:
            try:
                state.update_task(task_id, submission_started=1)
                cloud_id, name = magnet_upload(row['magnet'], account, offline_path=row['offline_path'])
                state.claim(task_id, f'cloud-task:{account}:{cloud_id}')
                state.update_task(task_id, account=account, name=name, cloud_task_id=cloud_id)
                break
            except (requests.RequestException, AmbiguousMutation) as exc:
                raise PikPakError(f'提交結果不確定，請先檢查 PikPak 離線列表，避免重複提交：{exc}') from exc
            except PikPakError as exc:
                errors.append(str(exc))
        else:
            raise PikPakError('提交離線任務失敗：' + ('; '.join(errors) or '沒有設定帳號'))
    row = state.get_task(task_id)
    state.update_task(task_id, stage=state.STAGE_OFFLINE)
    started = monotonic()
    missing = errors = 0
    best_progress = -1
    changed_at = monotonic()
    limit = getattr(config, 'OFFLINE_MAX_SECONDS', 7200)
    stall = getattr(config, 'OFFLINE_STALL_SECONDS', 1200)
    while monotonic() - started < limit:
        row = state.get_task(task_id)
        try:
            snapshot = get_offline_list(row['account'])
        except (requests.RequestException, PikPakError) as exc:
            errors += 1
            if errors >= 6:
                raise PikPakError(f'離線狀態連續查詢失敗（不代表任務被刪除）：{exc}') from exc
            sleep(10)
            continue
        errors = 0
        cloud = next((t for t in snapshot if t['id'] == row['cloud_task_id']), None)
        if cloud is None:
            missing += 1
            if missing >= 5:
                raise PikPakError('成功查詢完整列表五次仍找不到離線任務')
            sleep(10)
            continue
        missing = 0
        phase = cloud.get('phase')
        row = state.get_task(task_id)
        progress = int(cloud.get('progress') or 0)
        message = str(cloud.get('message') or cloud.get('error_description') or '')
        if phase == 'PHASE_TYPE_COMPLETE':
            if not cloud.get('file_id'):
                raise PikPakError('離線完成回應缺少 file_id')
            file_id = cloud['file_id']
            state.claim(task_id, f'cloud-file:{row["account"]}:{file_id}')
            state.update_task(task_id, cloud_file_id=file_id, retry_requested=0)
            return file_id, row['account']
        if progress > best_progress:
            best_progress, changed_at = progress, monotonic()
            state.update_task(task_id, last_progress_at=time())
        stalled = monotonic() - changed_at >= stall
        manual = bool(row['retry_requested'])
        if manual or (stalled and phase in ('PHASE_TYPE_RUNNING', 'PHASE_TYPE_PENDING')):
            if not manual and row['auto_retries'] >= getattr(config, 'MAX_AUTO_RETRIES', 3):
                raise PikPakError('離線任務持續停滯，自動重試次數已用盡')
            state.update_task(task_id, retry_requested=0, last_retry_at=time(),
                              auto_retries=row['auto_retries'] + (0 if manual else 1))
            ok, result = retry_offline_task(row['cloud_task_id'], row['account'])
            if not ok:
                raise PikPakError(f'PikPak 重試失敗：{result}')
            new_id = (result.get('task') or {}).get('id') or row['cloud_task_id']
            state.claim(task_id, f'cloud-task:{row["account"]}:{new_id}')
            state.update_task(task_id, cloud_task_id=new_id)
            best_progress, changed_at = -1, monotonic()
            notifier.progress(task_id, f'🔄 已重試離線任務\n{row["name"]}')
            sleep(10)
            continue
        if phase == 'PHASE_TYPE_ERROR':
            raise PikPakError(f'PikPak 離線失敗 code={cloud.get("error_code", "unknown")}: {message}')
        if phase not in ('PHASE_TYPE_RUNNING', 'PHASE_TYPE_PENDING'):
            raise PikPakError(f'PikPak 未知離線狀態 {phase}: {message}')
        state.update_task(task_id, progress=progress)
        notifier.progress(task_id, f'☁️ PikPak 離線下載中\n檔案: {row["name"]}\n進度: {progress}%')
        sleep(10)
    raise PikPakError('離線監控超時；已保留雲端任務，可稍後重試接續')


def _safe_destination(directory, name):
    if not name or name in ('.', '..') or any(c in name for c in '/\\'):
        raise ValueError('檔案名稱包含路徑分隔符')
    root = Path(ARIA2_DOWNLOAD_PATH).resolve()
    unresolved = root / directory / name
    if any(p.is_symlink() for p in [unresolved, *unresolved.parents] if p != root and p.is_relative_to(root)):
        raise ValueError('下載路徑包含符號連結')
    target = unresolved.resolve()
    if target == root or not target.is_relative_to(root):
        raise ValueError('下載路徑超出 ARIA2_DOWNLOAD_PATH')
    return str(target.parent), target


def _saved_complete(item):
    path = Path(item['directory']) / item['name']
    return (item['status'] == 'complete' and path.is_file()
            and path.stat().st_size == item['size'] and not Path(str(path) + '.aria2').exists())


def _update_file(task_id, item, **fields):
    state.save_file(task_id, item['file_id'], item['name'], item['directory'], **fields)
    item.update(fields)


def _push(task_id, item, account):
    _, url = get_download_url(item['file_id'], account)
    if not url:
        raise PikPakError('檔案沒有下載連結')
    gid = item.get('gid') or uuid.uuid4().hex[:16]
    _update_file(task_id, item, gid=gid, status='active', error=None)
    for attempt in range(3):
        try:
            response = aria2_client.add_uri(url, item['directory'], item['name'],
                                           {'User-Agent': 'Mozilla/5.0'}, gid=gid)
            if response['result'] != gid:
                raise aria2_client.Aria2Error('aria2 returned an unexpected GID')
            return
        except (requests.RequestException, aria2_client.Aria2Error, ValueError):
            status = aria2_client.tell_status_many([gid])[gid]
            if 'rpc_error' not in status:
                return
            if 'not found' not in status['rpc_error'].lower() or attempt == 2:
                raise
            sleep(2 ** attempt)


def _run_aria2_phase(notifier, file_id, account, task_id):
    row = state.get_task(task_id)
    state.claim(task_id, f'cloud-file:{account}:{file_id}')
    if not row['manifest_ready']:
        name, url = get_download_url(file_id, account)
        manifest = [(name, url, file_id, '')] if url else list(get_folder_all_file(file_id, name + '/', account))
        if not manifest:
            raise PikPakError('未找到可下載檔案；保留雲端內容，未執行清理')
        destinations, entries = set(), []
        for name_, _, file_id_, directory in manifest:
            directory, path = _safe_destination(directory, name_)
            normalized = os.path.normcase(str(path))
            if normalized in destinations:
                raise ValueError('資料夾內有重複的本機輸出路徑，未開始下載')
            destinations.add(normalized)
            entries.append((file_id_, name_, directory))
        state.set_manifest(task_id, name, file_id, entries)
    items = state.files(task_id)
    if not items:
        raise PikPakError('下載清單不完整')
    for item in items:
        _, path = _safe_destination(item['directory'], item['name'])
        state.claim(task_id, 'path:' + os.path.normcase(str(path)))
        state.claim(task_id, f'cloud-file:{account}:{item["file_id"]}')
    state.update_task(task_id, stage=state.STAGE_DOWNLOAD, progress=0)
    pending = []
    for item in items:
        if _saved_complete(item):
            continue
        if item.get('gid'):
            pending.append(item)
        else:
            try:
                _push(task_id, item, account)
                pending.append(item)
            except Exception as exc:
                _update_file(task_id, item, status='failed', error=str(exc))
    changed_at, errors = monotonic(), 0
    previous_bytes, retries = {}, {}
    while pending:
        try:
            statuses = aria2_client.tell_status_many([item['gid'] for item in pending])
        except (requests.RequestException, aria2_client.Aria2Error, ValueError) as exc:
            errors += 1
            if errors >= 6:
                raise aria2_client.Aria2Error(f'aria2 連續查詢失敗；已保留 GID 供接續：{exc}') from exc
            sleep(10)
            continue
        errors = 0
        remaining, total_bytes, done_bytes = [], 0, 0
        for item in pending:
            status = statuses[item['gid']]
            size = int(status.get('totalLength') or item['size'] or 0)
            completed = int(status.get('completedLength') or 0)
            total_bytes += size
            done_bytes += completed
            if completed > previous_bytes.get(item['file_id'], -1):
                changed_at = monotonic()
            previous_bytes[item['file_id']] = completed
            local = Path(item['directory']) / item['name']
            verified_complete = (status.get('status') == 'complete' and completed == size
                                 and local.is_file() and local.stat().st_size == size
                                 and not Path(str(local) + '.aria2').exists())
            if verified_complete:
                _update_file(task_id, item, status='complete', size=size, error=None)
                changed_at = monotonic()
                continue
            rpc_error = status.get('rpc_error')
            retryable = status.get('status') in ('error', 'removed', 'complete') or (
                rpc_error and 'not found' in rpc_error.lower())
            if rpc_error and not retryable:
                raise aria2_client.Aria2Error(rpc_error)
            if retryable:
                if _saved_complete(item):
                    continue
                error = status.get('errorMessage') or rpc_error or status.get('status')
                count = retries.get(item['file_id'], 0)
                if count >= 3:
                    _update_file(task_id, item, status='failed', error=str(error))
                    continue
                retries[item['file_id']] = count + 1
                if status.get('status') in ('error', 'removed', 'complete'):
                    _update_file(task_id, item, gid=None)
                try:
                    _push(task_id, item, account)
                except Exception as exc:
                    _update_file(task_id, item, status='failed', error=str(exc))
                    continue
            _update_file(task_id, item, status='active', size=size)
            remaining.append(item)
        if remaining and monotonic() - changed_at >= getattr(config, 'ARIA2_STALL_SECONDS', 3600):
            raise aria2_client.Aria2Error('aria2 長時間沒有位元組進展；保留檔案與 GID，可重試接續')
        completed_count = sum(item['status'] == 'complete' for item in items)
        total_bytes = sum(item['size'] for item in items)
        done_bytes = sum(item['size'] if item['status'] == 'complete'
                         else previous_bytes.get(item['file_id'], 0) for item in items)
        progress = int(100 * done_bytes / total_bytes) if total_bytes else int(100 * completed_count / len(items))
        progress = min(99 if remaining else 100, progress)
        state.update_task(task_id, progress=progress)
        notifier.progress(task_id, f'⬇️ aria2 下載中\n檔案: {state.get_task(task_id)["name"]}\n'
                          f'完成: {completed_count}/{len(items)}\n進度: {progress}% ({done_bytes}/{total_bytes} bytes)')
        pending = remaining
        if pending:
            sleep(20)
    failed = [item for item in items if item['status'] != 'complete']
    if failed:
        raise aria2_client.Aria2Error('部分檔案下載失敗；全部雲端內容已保留：' +
                                   '; '.join(f'{item["name"]}: {item["error"]}' for item in failed))
    state.update_task(task_id, stage=state.STAGE_CLEANUP, progress=100)
    note = '已保留雲端檔案（自動清理關閉）'
    if auto_delete_judge(account) == 'on':
        # Delete only verified leaf IDs. Keep folder shells, avoiding deletion of
        # files added to the folder after enumeration by another client/task.
        ids = [item['file_id'] for item in items]
        cleaned = True
        for offset in range(0, len(ids), 100):
            batch = ids[offset:offset + 100]
            if not delete_files(batch, account) or not delete_trash(batch, account):
                cleaned = False
        note = '已釋放下載成功檔案的雲端空間' if cleaned else '下載完成；部分雲端清理失敗，請稍後檢查'
    return note


def _run_task(task_id, notifier, batch_id=None):
    try:
        row = state.get_task(task_id)
        notifier.progress(task_id, f'📥 處理任務 `{task_id}`')
        if row['cloud_file_id']:
            file_id, account = row['cloud_file_id'], row['account']
        else:
            file_id, account = _offline(task_id, notifier)
        note = _run_aria2_phase(notifier, file_id, account, task_id)
        state.update_task(task_id, stage=state.STAGE_COMPLETE, error=None)
        notifier.progress(task_id, f'✅ 下載完成\n{state.get_task(task_id)["name"]}\n{note}')
    except Exception as exc:
        logging.exception('Task %s failed', task_id)
        state.update_task(task_id, stage=state.STAGE_FAILED, error=str(exc)[:2000])
        notifier.progress(task_id, f'❌ 下載失敗\n{str(exc)[:1500]}')
        notifier.send(f'任務 `{task_id}` 可重試接續；已保留下載資料。', buttons=_retry_buttons(task_id))
    finally:
        state.mark_failed_if_not_terminal(task_id, 'worker ended without terminal state')
        row = state.get_task(task_id)
        success = row['stage'] == state.STAGE_COMPLETE
        try:
            record_batch_result(batch_id, 'success' if success else 'fail',
                                row['name'] or task_id, row['error'] or '', notifier)
            notifier.finalize(success=success, name=row['name'] or task_id)
        finally:
            state.release(task_id, preserve_files=not success)


def _restore_notifier(row, fallback):
    from pikpakbot.notifier import DiscordNotifier
    if row.get('discord_channel_id'):
        return DiscordNotifier(row['discord_channel_id'])
    return fallback.create_task_channel(row.get('name') or '恢復任務')


def retry_offline_candidates(account, min_progress, notifier=None, *, automatic=False):
    notifier = notifier or NullNotifier()
    success = fail = 0
    results = []
    for cloud in get_stuck_tasks(account, min_progress, include_errors=not automatic):
        try:
            row = state.latest_cloud_task(account, cloud['id'])
            if row and row['stage'] not in state.TERMINAL_STAGES:
                if row['stage'] == state.STAGE_OFFLINE and not automatic:
                    if state.request_offline_retry(row['task_id']):
                        success += 1
                        results.append({'name': cloud['name'], 'status': 'success', 'message': '已通知現有 worker'})
                continue
            if row:
                if automatic:
                    continue  # Terminal failures need explicit manual retry.
                retry_task(_restore_notifier(row, notifier), row['task_id'])
            else:
                task_id = state.create_task(account=account, name=cloud['name'], source_kind='offline',
                                            cloud_task_id=cloud['id'], retry_requested=1)
                _launch(task_id, notifier.create_task_channel(cloud['name']))
            success += 1
            results.append({'name': cloud['name'], 'status': 'success', 'message': '已安排重試'})
        except Exception as exc:
            fail += 1
            results.append({'name': cloud['name'], 'status': 'fail', 'message': str(exc)})
    return success, fail, results


def startup_recovery(admin_notifier):
    for row in state.list_recent(limit=10000, stage_filter=state.STAGE_FAILED):
        if row['error'] != 'bot restarted while task was in flight' or row['retry_child']:
            continue
        if not (row['cloud_task_id'] or row['cloud_file_id'] or
                (row['magnet'] and row['interrupted_stage'] == state.STAGE_QUEUED and not row['submission_started'])):
            continue
        try:
            retry_task(_restore_notifier(row, admin_notifier), row['task_id'], recover=True)
        except Exception:
            logging.exception('Could not recover %s', row['task_id'])


def stuck_task_watchdog(admin_notifier):
    while True:
        sleep(60)
        for account in list(USER):
            try:
                retry_offline_candidates(account, 0, admin_notifier, automatic=True)
            except Exception:
                logging.exception('Watchdog could not query offline tasks')


def record_batch_result(batch_id, status, name, message, notifier):
    """Record one task's result in the batch + update the batch-level message.

    The batch summary goes to the batch_notifier (set by the /p handler to the
    channel where /p was invoked — main channel, not a per-task thread). Falls
    back to the per-task notifier if /p didn't register one (legacy callers).

    Uses progress() so the batch summary lives in ONE message that edits in
    place as tasks finish — no scrolling wall of partial summaries.
    """
    global batch_results
    if not batch_id:
        return

    with batch_lock:
        if batch_id not in batch_results:
            return

        batch_results[batch_id]['processed'] += 1
        batch_results[batch_id]['results'].append({
            'name': name,
            'status': status,
            'message': message
        })

        batch_notifier = batch_results[batch_id].get('notifier') or notifier
        results = batch_results[batch_id]['results']
        processed = batch_results[batch_id]['processed']
        total = batch_results[batch_id]['total']
        success_count = sum(1 for r in results if r['status'] == 'success')
        fail_count = sum(1 for r in results if r['status'] == 'fail')
        all_done = processed == total

        header = '📋 批次下載完成' if all_done else '📋 批次下載進行中'
        lines = [
            header,
            f'進度: {processed}/{total}',
            f'✅ 成功: {success_count}   ❌ 失敗: {fail_count}',
        ]
        if all_done:
            lines.append('───────────────')
            for i, res in enumerate(results, 1):
                icon = '✅' if res['status'] == 'success' else '❌'
                lines.append(f'{i}. {icon} {res["name"]}')
                if res['message']:
                    lines.append(f'   └ {res["message"]}')

        batch_notifier.progress(f'batch:{batch_id}', '\n'.join(lines))

        if all_done:
            del batch_results[batch_id]
