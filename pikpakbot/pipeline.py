import logging
import os
import re
import shutil
import threading
from pathlib import Path
from time import sleep, time

import requests

from config import USER, AUTO_DELETE, ARIA2_DOWNLOAD_PATH
from pikpakbot import aria2_client
from pikpakbot import state
from pikpakbot.notifier import ActionButton, Notifier, NullNotifier


def _retry_buttons(task_id):
    """Standard [Retry] [Dismiss] button row for failure notifications."""
    return [ActionButton('🔄 重試', 'retry', task_id), ActionButton('✖️ 忽略', 'dismiss', task_id)]


def cleanup_failed_download_dir(task_name):
    """
    Safely delete a failed task's aria2 download directory before retrying.

    Returns (deleted: bool, message: str). All deletion paths are heavily guarded:
    - empty / non-string name → skip
    - name contains '/', '\\\\' or '..' → skip (path-traversal defense)
    - resolved path must be strictly INSIDE ARIA2_DOWNLOAD_PATH
    - resolved path must not equal ARIA2_DOWNLOAD_PATH itself
    - must be a real directory (not a symlink, not a file)
    - must not be in use by another in-flight task with the same `name`
    """
    if not task_name or not isinstance(task_name, str):
        return False, "no task name"

    if any(c in task_name for c in ('/', '\\')) or '..' in task_name:
        return False, f"task name {task_name!r} contains path separator or '..'"

    try:
        root = Path(ARIA2_DOWNLOAD_PATH).resolve()
        target = (Path(ARIA2_DOWNLOAD_PATH) / task_name).resolve()
    except OSError as e:
        return False, f"path resolve failed: {e}"

    if target == root:
        return False, "target resolved to download root itself"
    try:
        target.relative_to(root)
    except ValueError:
        return False, f"target {target} is outside {root}"

    if target.is_symlink():
        return False, "target is a symlink, refusing to delete"
    if not target.exists():
        return False, "directory does not exist (nothing to clean)"
    if not target.is_dir():
        return False, "target is not a directory"

    # Don't pull the rug from under a concurrent task using the same name.
    actives = state.list_active()
    if any(a.get('name') == task_name for a in actives):
        return False, f"another in-flight task still owns name {task_name!r}"

    try:
        shutil.rmtree(target)
        return True, f"deleted {target}"
    except Exception as e:
        return False, f"rmtree failed: {e}"
from pikpakbot.pikpak_client import (
    magnet_upload,
    get_offline_list,
    get_download_url,
    get_folder_all_file,
    delete_files,
    delete_trash,
)

thread_list = []
batch_lock = threading.Lock()
batch_results = {}


def record_batch_result(batch_id, status, name, message, notifier: Notifier):
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


def _run_aria2_phase(notifier: Notifier, file_id, account, task_id, batch_id, source_label):
    """Push a PikPak cloud item to aria2, poll till done, then delete it from cloud.

    Shared by `process_magnet` (after the offline phase succeeds) and
    `download_cloud_file` (manual cloud pull). The caller is responsible for
    creating the state task, the outer try/except wrapping, and the final
    notifier.finalize hook. This helper only handles the aria2 phase itself.

    `source_label` is used in log messages for context (magnet hash for /p flow,
    cloud file name for /dl flow).

    Raises exceptions to the caller — only catches and recovers from per-file
    transient errors. Cloud deletion at the end is scoped to the file_id passed
    in (and successfully-downloaded children of it), so concurrent tasks don't
    interfere.
    """
    gid = {}
    download_headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10.9; rv:50.0) Gecko/20100101 Firefox/50.0'
    }

    down_name, down_url = get_download_url(file_id, account)
    if not down_name:
        msg = f'找不到 PikPak file_id {file_id} 或無法存取'
        notifier.send(f'❌ {msg}')
        logging.error(msg)
        state.update_task(task_id, stage=state.STAGE_FAILED, error=msg)
        record_batch_result(batch_id, 'fail', source_label, msg, notifier)
        return

    if down_url == "":
        logging.info(f"來源{source_label}內容為資料夾:{down_name}，準備提取出每個檔案並下載")

        for name, url, down_file_id, path in get_folder_all_file(file_id, f"{down_name}/", account):
            push_flag = False
            for tries in range(5):
                try:
                    response = aria2_client.add_uri(url, ARIA2_DOWNLOAD_PATH + '/' + path, name,
                                                    download_headers)
                    push_flag = True
                    break
                except requests.exceptions.ReadTimeout:
                    logging.warning(f'{name}第{tries + 1}(/5)次推送下載超時，將重試！')
                    continue
                except ValueError:
                    logging.warning(f'{name}第{tries + 1}(/5)次推送下載出錯，可能是frp故障，將重試！')
                    sleep(5)
                    continue
            if not push_flag:
                print_info = f'{name}推送aria2下載失敗！該檔案直連如下，請手動下載：\n{url}'
                notifier.send(print_info)
                logging.error(print_info)
                continue

            gid[response['result']] = [f'{name}', down_file_id, url]
            logging.info(f'{path}{name}推送aria2下載')

        notifier.progress(
            task_id,
            f'⬇️ aria2 下載中\n資料夾: {down_name}\n檔案數: {len(gid)}\n進度: 0% (剛推送)'
        )
        logging.info(f'{down_name}資料夾下所有檔案已推送aria2下載，請耐心等待...')

    else:
        logging.info(f'{source_label}內容為單檔案，將直接推送aria2下載')

        push_flag = False
        for tries in range(5):
            try:
                response = aria2_client.add_uri(down_url, ARIA2_DOWNLOAD_PATH, down_name, download_headers)
                push_flag = True
                break
            except requests.exceptions.ReadTimeout:
                logging.warning(f'{down_name}第{tries + 1}(/5)次推送aria2下載超時，將重試！')
                continue
            except ValueError:
                logging.warning(f'{down_name}第{tries + 1}(/5)次推送aria2下載出錯，可能是frp故障，將重試！')
                sleep(5)
                continue
            except Exception as e:
                logging.warning(f'{down_name}第{tries + 1}(/5)次推送aria2下載發生未知錯誤: {e}，將重試！')
                sleep(2)
                continue

        if not push_flag:
            notifier.progress(task_id, f'❌ aria2 推送失敗 (多次重試無效)\n檔案: {down_name}')
            notifier.send(f'直連: {down_url}', buttons=_retry_buttons(task_id))
            logging.error(f'{down_name}推送aria2下載失敗（多次重試無效）！直連: {down_url}')
            record_batch_result(batch_id, 'fail', down_name, "推送Aria2失敗", notifier)
            state.update_task(task_id, name=down_name, stage=state.STAGE_FAILED, error="推送Aria2失敗")
            return

        gid[response['result']] = [down_name, file_id, down_url]
        notifier.progress(
            task_id,
            f'⬇️ aria2 下載中\n檔案: {down_name}\n進度: 0% (剛推送)'
        )
        logging.info(f'{down_name}已推送aria2下載，請耐心等待...')

    state.update_task(task_id, name=down_name, stage=state.STAGE_DOWNLOAD, progress=0)
    logging.info(f'睡眠30s，之後將開始查詢{down_name}下載進度...')
    sleep(30)
    download_done = False
    complete_file_id = []
    failed_gid = {}
    total_files = max(1, len(gid))
    gid_bytes = {g: {'completed': 0, 'total': 0} for g in gid.keys()}
    ARIA_ERROR_MAX_RETRIES = 3
    file_error_retries = {}
    while not download_done:
        temp_gid = gid.copy()
        status_counts = {}
        for each_gid in gid.keys():
            try:
                response = aria2_client.tell_status(
                    each_gid,
                    ["gid", "status", "errorMessage", "dir", "completedLength", "totalLength"],
                )
            except requests.exceptions.ReadTimeout:
                logging.warning(f'查詢GID{each_gid}時網路請求超時，將跳過此次查詢！')
                status_counts['tell_status_timeout'] = status_counts.get('tell_status_timeout', 0) + 1
                continue
            except ValueError:
                logging.warning(f'查詢GID{each_gid}時返回結果錯誤，可能是frp故障，將跳過此次查詢！')
                sleep(5)
                continue

            try:
                status = response['result']['status']
                status_counts[status] = status_counts.get(status, 0) + 1
                try:
                    gid_bytes[each_gid] = {
                        'completed': int(response['result'].get('completedLength', 0) or 0),
                        'total': int(response['result'].get('totalLength', 0) or 0),
                    }
                except (TypeError, ValueError):
                    pass
                if status == 'complete':
                    temp_gid.pop(each_gid)
                    complete_file_id.append(gid[each_gid][1])
                elif status == 'removed':
                    notifier.send(f'aria2 任務 {gid[each_gid][0]} 被標記為 removed（已被刪除），視為失敗')
                    logging.warning(f'aria2 GID {each_gid} ({gid[each_gid][0]}) status=removed，標記失敗')
                    failed_gid[each_gid] = temp_gid.pop(each_gid)
                elif status == 'error':
                    error_message = response["result"]["errorMessage"]
                    file_id_for_this = gid[each_gid][1]
                    retries_so_far = file_error_retries.get(file_id_for_this, 0)

                    if retries_so_far >= ARIA_ERROR_MAX_RETRIES:
                        print_info = (
                            f'aria2下載{gid[each_gid][0]}重試 {ARIA_ERROR_MAX_RETRIES} 次仍失敗！'
                            f'\n錯誤訊息：{error_message}'
                            f'\n該檔案直連如下，請手動下載並反饋bug：\n{gid[each_gid][2]}'
                        )
                        notifier.send(print_info)
                        logging.warning(print_info)
                        failed_gid[each_gid] = temp_gid.pop(each_gid)
                        continue

                    retry_down_name, retry_the_url = get_download_url(file_id_for_this, account)
                    if not retry_the_url:
                        print_info = f'aria2下載{gid[each_gid][0]}出錯後無法取得新下載連結！錯誤：{error_message}'
                        notifier.send(print_info)
                        logging.error(print_info)
                        failed_gid[each_gid] = temp_gid.pop(each_gid)
                        continue

                    repush_flag = False
                    new_response = None
                    for tries in range(5):
                        try:
                            new_response = aria2_client.add_uri(
                                retry_the_url, response["result"]["dir"],
                                retry_down_name, download_headers,
                            )
                            repush_flag = True
                            break
                        except requests.exceptions.ReadTimeout:
                            logging.warning(f'{retry_down_name}重新推送第{tries + 1}/5次網路超時，將重試')
                            continue
                        except ValueError:
                            logging.warning(f'{retry_down_name}重新推送第{tries + 1}/5次返回錯誤（可能 frp 故障），將重試')
                            sleep(5)
                            continue
                    if not repush_flag:
                        print_info = f'{retry_down_name}重新推送失敗！直連：\n{retry_the_url}'
                        notifier.send(print_info)
                        logging.error(print_info)
                        failed_gid[each_gid] = temp_gid.pop(each_gid)
                        continue

                    new_gid = new_response['result']
                    file_error_retries[file_id_for_this] = retries_so_far + 1
                    temp_gid[new_gid] = [retry_down_name, file_id_for_this, retry_the_url]
                    temp_gid.pop(each_gid)
                    if each_gid in gid_bytes:
                        gid_bytes[new_gid] = gid_bytes.pop(each_gid)
                    logging.warning(
                        f'aria2下載 {gid[each_gid][0]} 出錯（{error_message}），'
                        f'已重新推送 ({retries_so_far + 1}/{ARIA_ERROR_MAX_RETRIES})'
                    )

            except KeyError:
                notifier.send(f'aria2下載{gid[each_gid][0]}任務被刪除！')
                logging.warning(f'aria2下載{gid[each_gid][0]}任務被刪除！')
                failed_gid[each_gid] = temp_gid.pop(each_gid)

        gid = temp_gid
        if len(gid) == 0:
            download_done = True
            print_info = f'aria2下載已完成：\n{down_name}\n共{len(complete_file_id) + len(failed_gid)}個檔案，' \
                         f'其中{len(complete_file_id)}個成功，{len(failed_gid)}個失敗'

            logging.info(f"Aria2下載完成，準備清理PikPak檔案... (成功: {len(complete_file_id)}, 失敗: {len(failed_gid)})")
            state.update_task(task_id, stage=state.STAGE_CLEANUP, progress=100)
            sleep(2)

            if len(failed_gid):
                print_info += '，下載失敗檔案為：\n'
                for values in failed_gid.values():
                    print_info += values[0] + '\n'

                # Only delete the successfully-downloaded child file_ids, never the
                # whole folder — leaves the failed ones in cloud for inspection.
                status_a = False
                status_b = False
                for _ in range(3):
                    if not status_a:
                        status_a = delete_files(complete_file_id, account)
                    if not status_b:
                        status_b = delete_trash(complete_file_id, account)
                    if status_a and status_b:
                        break
                    sleep(2)

                if status_a:
                    logging.info(f'帳號{account}已刪除{down_name}中下載成功的雲端硬碟檔案')
                if status_b:
                    logging.info(f'帳號{account}已刪除{down_name}中下載成功的垃圾桶檔案')

                if status_a and status_b:
                    print_info += f'帳號{account}中下載成功的雲端硬碟檔案已刪除\n'
                elif account in AUTO_DELETE and AUTO_DELETE[account] == 'False':
                    print_info += f'帳號{account}未開啟自動刪除\n'
                else:
                    print_info += f'帳號{account}中下載成功的雲端硬碟檔案刪除失敗，請手動刪除\n'

                notifier.progress(task_id, '❌ ' + print_info)
                logging.info(print_info)

                # Buttons live on their own message so progress() can keep editing
                # the status line.
                notifier.send(
                    f'部分檔案失敗。`/clean {account}` 可清空此帳號所有檔案',
                    parse_mode='Markdown',
                    buttons=_retry_buttons(task_id),
                )
                record_batch_result(batch_id, 'fail', down_name,
                                    f"部分檔案下載失敗: {len(failed_gid)}個", notifier)
                state.update_task(task_id, stage=state.STAGE_FAILED,
                                  error=f"部分檔案下載失敗: {len(failed_gid)}個")
            else:
                # All successful — safe to delete the whole file_id we were given.
                status_a = False
                status_b = False
                for _ in range(3):
                    if not status_a:
                        status_a = delete_files(file_id, account)
                    if not status_b:
                        status_b = delete_trash(file_id, account)
                    if status_a and status_b:
                        break
                    sleep(2)

                if status_a:
                    logging.info(f'帳號{account}已刪除{down_name}雲端硬碟檔案')
                if status_b:
                    logging.info(f'帳號{account}已刪除{down_name}垃圾桶檔案')

                if status_a and status_b:
                    print_info += f'\n帳號{account}中該檔案的雲端硬碟空間已釋放'
                elif account in AUTO_DELETE and AUTO_DELETE[account] == 'False':
                    print_info += f'\n帳號{account}未開啟自動刪除'
                else:
                    print_info += f'\n帳號{account}中該檔案的雲端硬碟空間釋放失敗，請手動刪除'
                notifier.progress(task_id, '✅ ' + print_info)
                logging.info(print_info)

                record_batch_result(batch_id, 'success', down_name, "", notifier)
                state.update_task(task_id, stage=state.STAGE_COMPLETE)
        else:
            done_count = len(complete_file_id) + len(failed_gid)
            total_bytes = sum(b['total'] for b in gid_bytes.values())
            if total_bytes > 0:
                done_bytes = sum(b['completed'] for b in gid_bytes.values())
                aria_progress = int(done_bytes / total_bytes * 100)
                byte_summary = f'{done_bytes / 1e9:.2f}/{total_bytes / 1e9:.2f} GB'
            else:
                aria_progress = int(done_count / total_files * 100)
                byte_summary = ''
            state.update_task(task_id, progress=aria_progress)
            status_summary = ', '.join(f'{k}={v}' for k, v in sorted(status_counts.items())) or '(no responses)'
            progress_lines = [f'⬇️ aria2 下載中', f'檔案: {down_name}']
            if total_files > 1:
                progress_lines.append(f'檔案: {done_count}/{total_files} 完成')
            if byte_summary:
                progress_lines.append(f'位元組: {byte_summary}')
            progress_lines.append(f'進度: {aria_progress}%')
            notifier.progress(task_id, '\n'.join(progress_lines))
            logging.info(
                f'aria2下載{down_name}還未完成 ({done_count}/{total_files} files, {byte_summary} = {aria_progress}%) — '
                f'status: {status_summary}，睡眠20s後再查...'
            )
            sleep(20)


def process_magnet(notifier: Notifier, magnet, offline_path=None, batch_id=None,
                   resume_task=None, target_account=None):
    if notifier is None:
        notifier = NullNotifier()

    mag_url_simple = magnet
    if resume_task:
        mag_url_simple = f"恢復任務: {resume_task.get('name', 'Unknown')}"
    elif str(magnet).startswith("magnet:?"):
        mag_url_part = re.search(r'^(magnet:\?).*(xt=.+?)(&|$)', magnet)
        mag_url_simple = ''.join(mag_url_part.groups()[:-1])

    # Register task for /status & /history. Resume tasks start mid-pipeline.
    if resume_task:
        task_id = state.create_task(
            magnet=None,
            name=resume_task.get('name'),
            account=target_account,
            stage=state.STAGE_OFFLINE,
        )
    else:
        task_id = state.create_task(magnet=magnet, stage=state.STAGE_QUEUED)

    try:
        # First heartbeat — shows up in the thread immediately so the user knows
        # the task is alive even before PikPak has accepted the magnet.
        notifier.progress(task_id, f'📥 接受任務，提交至 PikPak 中...\n{mag_url_simple}')

        for each_account in USER:
            if resume_task and each_account != target_account:
                continue

            mag_id, mag_name = None, None

            if resume_task:
                mag_id = resume_task['id']
                mag_name = resume_task['name']
                logging.info(f"正在恢復帳號 {each_account} 的任務: {mag_name}")
            else:
                for tries in range(3):
                    try:
                        mag_id, mag_name = magnet_upload(magnet, each_account, offline_path=offline_path)
                        if mag_id:
                            break
                    except requests.exceptions.ReadTimeout:
                        logging.warning(f"帳號{each_account}添加磁力鏈接超時，重試第{tries + 1}/3次...")
                        sleep(2)
                    except Exception as e:
                        logging.warning(f"帳號{each_account}添加磁力鏈接發生錯誤: {e}，重試第{tries + 1}/3次...")
                        sleep(2)

            if not mag_id:
                if each_account == USER[-1]:
                    notifier.progress(
                        task_id,
                        f'❌ 所有帳號均離線下載失敗\n{mag_url_simple}\n可能原因: 免費離線次數用盡 / 雲端容量不足'
                    )
                    notifier.send('要重試嗎？', buttons=_retry_buttons(task_id))
                    logging.warning(f'{mag_url_simple}所有帳號均離線下載失敗！')
                    record_batch_result(batch_id, 'fail', mag_url_simple, "所有帳號離線失敗", notifier)
                    state.update_task(task_id, stage=state.STAGE_FAILED, error="所有帳號離線失敗")
                    return
                continue

            state.update_task(task_id, name=mag_name, account=each_account, stage=state.STAGE_OFFLINE)
            notifier.progress(
                task_id,
                f'☁️ 已提交 PikPak 離線下載\n檔案: {mag_name or mag_url_simple}\n帳號: {each_account}\n進度: 0% (剛開始)'
            )
            done = False
            logging.info('5s後將檢查離線下載進度...')
            sleep(5)
            offline_start = time()
            not_found_count = 0
            while (not done) and (time() - offline_start < 60 * 60):
                try:
                    temp = get_offline_list(each_account)
                    find = False
                    for each_down in temp:
                        if each_down['id'] == mag_id:
                            find = True
                            not_found_count = 0

                            msg = each_down.get('message', '')
                            if "file deleted" in msg.lower() or "file_deleted" in msg.lower():
                                logging.info(f"帳號{each_account}離線任務 {mag_name} 檔案已在雲端刪除，跳過處理")
                                find = False
                                break

                            if each_down['progress'] == 100 and msg == 'Saved':
                                done = True
                                file_id = each_down['file_id']
                                notifier.progress(
                                    task_id,
                                    f'✅ 離線下載完成\n檔案: {mag_name}\n帳號: {each_account}\n準備推送 aria2...'
                                )
                                logging.info(f'帳號{each_account}離線下載磁力已完成：{mag_url_simple} 檔案名稱：{mag_name}')
                            elif each_down['progress'] == 100:
                                done = True
                                file_id = each_down['file_id']
                                notifier.progress(
                                    task_id,
                                    f'⚠️ 離線下載完成（含警告）\n檔案: {mag_name}\n帳號: {each_account}\n訊息: {msg.strip()}\n準備推送 aria2...'
                                )
                                logging.warning(
                                    f'帳號{each_account}離線下載磁力已完成: {mag_url_simple} 但含有訊息：{msg.strip()}！檔案名稱：{mag_name}'
                                )
                            else:
                                current_file_name = each_down.get('file_name') or each_down.get('name') or mag_name or mag_url_simple
                                pct = int(each_down["progress"])
                                elapsed = int(time() - offline_start)
                                notifier.progress(
                                    task_id,
                                    f'☁️ PikPak 離線下載中\n檔案: {current_file_name}\n帳號: {each_account}\n進度: {pct}%\n已等待: {elapsed // 60}分{elapsed % 60}秒'
                                )
                                logging.info(
                                    f'帳號{each_account}離線下載 "{current_file_name}" 還未完成，進度{pct}%...'
                                )
                                state.update_task(task_id, progress=pct)
                                sleep(10)
                            break
                    if not find:
                        not_found_count += 1
                        if not_found_count >= 5:
                            notifier.progress(
                                task_id,
                                f'❌ 離線任務被取消或多次查詢未找到\n{mag_url_simple}\n帳號: {each_account}'
                            )
                            notifier.send('要重試嗎？', buttons=_retry_buttons(task_id))
                            logging.warning(f'帳號{each_account}離線下載{mag_url_simple}的任務被取消（或多次查詢未找到）！')
                            state.update_task(task_id, stage=state.STAGE_FAILED, error="離線任務被取消或多次查詢未找到")
                            break
                        else:
                            logging.warning(f"帳號{each_account}未找到任務{mag_id}，重試({not_found_count}/5)...")
                            sleep(5)
                            continue
                except Exception as e:
                    logging.warning(f"監控離線下載進度時發生錯誤 (將自動重試): {e}")
                    sleep(5)
                    continue

            if (find and done) or (not find and not done):
                if not done:
                    record_batch_result(batch_id, 'fail', mag_name if mag_name else mag_url_simple,
                                        "離線任務被取消或失敗", notifier)
                    state.update_task(task_id, stage=state.STAGE_FAILED, error="離線任務被取消或失敗")
                    return
                break
            elif find and not done:
                notifier.progress(
                    task_id,
                    f'❌ 離線下載超時 (1 小時)\n檔案: {mag_name or mag_url_simple}\n帳號: {each_account}'
                )
                notifier.send('要重試嗎？', buttons=_retry_buttons(task_id))
                logging.warning(f'帳號{each_account}離線下載{mag_url_simple}的任務超時（1小時）！已取消該任務！')
                record_batch_result(batch_id, 'fail', mag_name if mag_name else mag_url_simple,
                                    "離線下載超時", notifier)
                state.update_task(task_id, stage=state.STAGE_FAILED, error="離線下載超時（1小時）")
                return
            else:
                continue

        if mag_id and find and done:
            _run_aria2_phase(notifier, file_id, each_account, task_id, batch_id, mag_url_simple)

    except requests.exceptions.ReadTimeout:
        logging.warning(f'下載磁力{mag_url_simple}期間發生網路請求超時，但任務可能仍在進行中。')
    except Exception as e:
        logging.error(f"處理磁力{mag_url_simple}時發生未知錯誤: {e}")
        record_batch_result(batch_id, 'fail', mag_url_simple, f"發生未知錯誤: {str(e)}", notifier)
        state.update_task(task_id, stage=state.STAGE_FAILED, error=f"未知錯誤: {e}")
    finally:
        # Safety net: if process_magnet exits and the task is still in a
        # non-terminal stage (e.g. interrupted, unexpected return path), mark
        # it failed so /status / /history don't show it as forever-running.
        state.mark_failed_if_not_terminal(task_id, "process_magnet ended without explicit terminal stage")

        # Notify the bot-side hook so Discord threads can rename + archive.
        try:
            row = state.get_task(task_id)
            if row:
                success = row['stage'] == state.STAGE_COMPLETE
                display_name = row.get('name') or mag_url_simple or 'task'
                notifier.finalize(success=success, name=display_name)
        except Exception as e:
            logging.error(f"notifier.finalize failed: {e}")


def download_cloud_file(notifier: Notifier, file_id, account):
    """Manual /dl entry: pull an existing PikPak cloud file/folder to local.

    Skips the offline phase entirely (the file is already in cloud) and goes
    straight to the aria2 push + cleanup pipeline. Cloud deletion is scoped to
    the given file_id, so concurrent /p flows or other /dl calls are unaffected.
    """
    if notifier is None:
        notifier = NullNotifier()

    task_id = state.create_task(
        magnet=None,
        account=account,
        stage=state.STAGE_DOWNLOAD,
    )
    source_label = f'cloud:{file_id}'

    try:
        _run_aria2_phase(notifier, file_id, account, task_id, None, source_label)
    except requests.exceptions.ReadTimeout:
        logging.warning(f'下載 cloud file {file_id} 期間發生網路請求超時，但任務可能仍在進行中。')
    except Exception as e:
        logging.error(f"處理 cloud file {file_id} 時發生未知錯誤: {e}")
        state.update_task(task_id, stage=state.STAGE_FAILED, error=f"未知錯誤: {e}")
    finally:
        state.mark_failed_if_not_terminal(task_id, "download_cloud_file ended without explicit terminal stage")
        try:
            row = state.get_task(task_id)
            if row:
                success = row['stage'] == state.STAGE_COMPLETE
                display_name = row.get('name') or source_label
                notifier.finalize(success=success, name=display_name)
        except Exception as e:
            logging.error(f"notifier.finalize failed: {e}")


def check_download_thread_status():
    # In-place filter so other modules that imported `thread_list` keep seeing the same list.
    thread_list[:] = [t for t in thread_list if t.is_alive()]

    if len(thread_list):
        return True
    else:
        return False


def startup_recovery(admin_notifier: Notifier):
    """Bot 啟動時檢查是否有未完成的任務並恢復監控"""
    logging.info("正在檢查是否有未完成的任務需要恢復...")
    try:
        for account in USER:
            tasks = get_offline_list(account)
            resumed_count = 0
            for task in tasks:
                phase = task.get('phase')
                progress = int(task.get('progress', 0))
                message = task.get('message', '')

                if "file deleted" in message.lower() or "file_deleted" in message.lower():
                    continue

                if phase == 'PHASE_TYPE_RUNNING' or (phase == 'PHASE_TYPE_COMPLETE' and progress == 100):
                    task_info = {
                        'id': task.get('id'),
                        'name': task.get('name') or task.get('file_name')
                    }
                    # Per-task sub-channel (Discord thread) so recovery noise
                    # stays out of the main channel. TG returns self.
                    task_notifier = admin_notifier.create_task_channel(task_info['name'] or '恢復任務')
                    thread_list.append(threading.Thread(
                        target=process_magnet,
                        args=[task_notifier, None, None, None, task_info, account]
                    ))
                    thread_list[-1].start()
                    resumed_count += 1
                    sleep(1)

            if resumed_count > 0:
                logging.info(f"已從帳號 {account} 恢復 {resumed_count} 個任務")
    except Exception as e:
        logging.error(f"啟動恢復任務失敗: {e}")


STUCK_WATCHDOG_INTERVAL_SECONDS = 20 * 60
STUCK_WATCHDOG_PROGRESS_THRESHOLD = 99


def stuck_task_watchdog(admin_notifier: Notifier):
    """
    Background thread: periodically retry tasks left near 100% indefinitely.
    PikPak occasionally leaves offline tasks at 99% without completing; this
    removes the need to call /retry manually.
    """
    from pikpakbot.pikpak_client import retry_stuck_tasks

    logging.info(
        f"卡住任務自動重試已啟動 (每 {STUCK_WATCHDOG_INTERVAL_SECONDS // 60} 分鐘掃描，"
        f"閾值 {STUCK_WATCHDOG_PROGRESS_THRESHOLD}%)"
    )
    while True:
        sleep(STUCK_WATCHDOG_INTERVAL_SECONDS)
        for account in USER:
            try:
                success, fail, _ = retry_stuck_tasks(
                    account,
                    min_progress=STUCK_WATCHDOG_PROGRESS_THRESHOLD,
                    delete_cloud_files=True,
                    notifier=admin_notifier,
                )
                if success or fail:
                    logging.info(f"自動重試卡住任務 ({account}): 成功 {success}, 失敗 {fail}")
            except Exception as e:
                logging.error(f"自動重試卡住任務時發生錯誤 (帳號 {account}): {e}")
