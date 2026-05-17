import logging
import re
import threading
from time import sleep, time

import requests

from config import USER, AUTO_DELETE, ARIA2_DOWNLOAD_PATH
from pikpakbot import aria2_client
from pikpakbot import state
from pikpakbot.notifier import ActionButton, Notifier, NullNotifier


def _retry_buttons(task_id):
    """Standard [Retry] [Dismiss] button row for failure notifications."""
    return [ActionButton('🔄 重試', 'retry', task_id), ActionButton('✖️ 忽略', 'dismiss', task_id)]
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

        if batch_results[batch_id]['processed'] == batch_results[batch_id]['total']:
            results = batch_results[batch_id]['results']
            success_count = sum(1 for r in results if r['status'] == 'success')
            fail_count = sum(1 for r in results if r['status'] == 'fail')

            summary = f"📋 <b>下載任務匯總 (Batch Summary)</b>\n"
            summary += f"-------------------------\n"
            summary += f"✅ 成功: {success_count}\n"
            summary += f"❌ 失敗: {fail_count}\n"
            summary += f"-------------------------\n"

            for i, res in enumerate(results, 1):
                icon = "✅" if res['status'] == 'success' else "❌"
                summary += f"{i}. {icon} {res['name']}\n"
                if res['message']:
                    summary += f"   └ {res['message']}\n"

            notifier.send(summary, parse_mode='HTML')

            del batch_results[batch_id]


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
                    print_info = f'{mag_url_simple}所有帳號均離線下載失敗！可能是所有帳號免費離線次數用盡，或者檔案大小超過雲端硬碟剩餘容量！'
                    notifier.send(print_info, buttons=_retry_buttons(task_id))
                    logging.warning(print_info)
                    record_batch_result(batch_id, 'fail', mag_url_simple, "所有帳號離線失敗", notifier)
                    state.update_task(task_id, stage=state.STAGE_FAILED, error="所有帳號離線失敗")
                    return
                continue

            state.update_task(task_id, name=mag_name, account=each_account, stage=state.STAGE_OFFLINE)
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
                                print_info = f'帳號{each_account}離線下載磁力已完成：\n{mag_url_simple}\n檔案名稱：{mag_name}'
                                notifier.send(print_info)
                                logging.info(print_info)
                            elif each_down['progress'] == 100:
                                done = True
                                file_id = each_down['file_id']
                                print_info = f'帳號{each_account}離線下載磁力已完成:\n{mag_url_simple}\n但含有訊息：' \
                                             f'{msg.strip()}！\n檔案名稱：{mag_name}'
                                notifier.send(print_info)
                                logging.warning(print_info)
                            else:
                                current_file_name = each_down.get('file_name') or each_down.get('name') or mag_name or mag_url_simple
                                logging.info(
                                    f'帳號{each_account}離線下載 "{current_file_name}" 還未完成，進度{each_down["progress"]}%...'
                                )
                                state.update_task(task_id, progress=int(each_down["progress"]))
                                sleep(10)
                            break
                    if not find:
                        not_found_count += 1
                        if not_found_count >= 5:
                            print_info = f'帳號{each_account}離線下載{mag_url_simple}的任務被取消（或多次查詢未找到）！'
                            notifier.send(print_info, buttons=_retry_buttons(task_id))
                            logging.warning(print_info)
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
                print_info = f'帳號{each_account}離線下載{mag_url_simple}的任務超時（1小時）！已取消該任務！'
                notifier.send(print_info, buttons=_retry_buttons(task_id))
                logging.warning(print_info)
                record_batch_result(batch_id, 'fail', mag_name if mag_name else mag_url_simple,
                                    "離線下載超時", notifier)
                state.update_task(task_id, stage=state.STAGE_FAILED, error="離線下載超時（1小時）")
                return
            else:
                continue

        if mag_id and find and done:
            gid = {}
            download_headers = {
                'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10.9; rv:50.0) Gecko/20100101 Firefox/50.0'
            }

            down_name, down_url = get_download_url(file_id, each_account)
            if down_url == "":
                logging.info(f"磁力{mag_url_simple}內容為資料夾:{down_name}，準備提取出每個檔案並下載")

                for name, url, down_file_id, path in get_folder_all_file(file_id, f"{down_name}/", each_account):
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

                notifier.send(f'資料夾已推送aria2下載：\n{down_name}\n請耐心等待...')
                logging.info(f'{down_name}資料夾下所有檔案已推送aria2下載，請耐心等待...')

            else:
                logging.info(f'{mag_url_simple}內容為單檔案，將直接推送aria2下載')

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
                    print_info = f'{down_name}推送aria2下載失敗（多次重試無效）！該檔案直連如下，請手動下載：\n{down_url}'
                    notifier.send(print_info, buttons=_retry_buttons(task_id))
                    logging.error(print_info)
                    record_batch_result(batch_id, 'fail', down_name, "推送Aria2失敗", notifier)
                    state.update_task(task_id, name=down_name, stage=state.STAGE_FAILED, error="推送Aria2失敗")
                    return

                gid[response['result']] = [down_name, file_id, down_url]
                notifier.send(f'檔案已推送aria2下載：\n{down_name}\n請耐心等待...')
                logging.info(f'{down_name}已推送aria2下載，請耐心等待...')

            state.update_task(task_id, name=down_name, stage=state.STAGE_DOWNLOAD, progress=0)
            logging.info(f'睡眠30s，之後將開始查詢{down_name}下載進度...')
            sleep(30)
            download_done = False
            complete_file_id = []
            failed_gid = {}
            total_files = max(1, len(gid))  # snapshot for file-count display
            # gid -> {completed: bytes, total: bytes}. Persists across iterations so
            # finished files keep contributing to byte-weighted progress.
            gid_bytes = {g: {'completed': 0, 'total': 0} for g in gid.keys()}
            # gid -> aria2-error retry count (file ID may stay the same across retries;
            # we count by file_id since each repush yields a new gid).
            ARIA_ERROR_MAX_RETRIES = 3
            file_error_retries = {}  # pikpak_file_id -> int
            while not download_done:
                temp_gid = gid.copy()
                status_counts = {}  # for diagnostic log after this iteration
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
                        # Track bytes for weighted progress (last-known values stick after pop).
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
                            # aria2 marked this download as removed (manual or auto).
                            # Bot would otherwise poll forever; treat as failed.
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

                            # Refresh the download URL (PikPak URLs are short-lived and
                            # may be pointing to a flaky CDN node) and re-push to aria2.
                            # Same dir + out means aria2 picks up the .aria2 control file
                            # and resumes from where it left off.
                            retry_down_name, retry_the_url = get_download_url(file_id_for_this, each_account)
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
                                    logging.warning(
                                        f'{retry_down_name}重新推送第{tries + 1}/5次網路超時，將重試')
                                    continue
                                except ValueError:
                                    logging.warning(
                                        f'{retry_down_name}重新推送第{tries + 1}/5次返回錯誤（可能 frp 故障），將重試')
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
                            # Carry byte-tracking entry over so progress doesn't reset to 0.
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

                        status_a = False
                        status_b = False
                        for _ in range(3):
                            if not status_a:
                                status_a = delete_files(complete_file_id, each_account)
                            if not status_b:
                                status_b = delete_trash(complete_file_id, each_account)
                            if status_a and status_b:
                                break
                            sleep(2)

                        if status_a:
                            logging.info(f'帳號{each_account}已刪除{down_name}中下載成功的雲端硬碟檔案')
                        if status_b:
                            logging.info(f'帳號{each_account}已刪除{down_name}中下載成功的垃圾桶檔案')

                        if status_a and status_b:
                            print_info += f'帳號{each_account}中下載成功的雲端硬碟檔案已刪除\n'
                        elif each_account in AUTO_DELETE and AUTO_DELETE[each_account] == 'False':
                            print_info += f'帳號{each_account}未開啟自動刪除\n'
                        else:
                            print_info += f'帳號{each_account}中下載成功的雲端硬碟檔案刪除失敗，請手動刪除\n'

                        notifier.send(print_info, buttons=_retry_buttons(task_id))
                        logging.info(print_info)

                        print_info = f'對於下載失敗的檔案可使用指令：\n`/clean {each_account}`清空此帳號下所有檔案\n~~或者使用臨時指令：~~' \
                                     f'\n~~`/download {each_account}`重試下載此帳號下所有檔案~~'
                        notifier.send(print_info, parse_mode='Markdown')
                        logging.info(print_info)
                        record_batch_result(batch_id, 'fail', down_name,
                                            f"部分檔案下載失敗: {len(failed_gid)}個", notifier)
                        state.update_task(task_id, stage=state.STAGE_FAILED,
                                          error=f"部分檔案下載失敗: {len(failed_gid)}個")
                    else:
                        status_a = False
                        status_b = False
                        for _ in range(3):
                            if not status_a:
                                status_a = delete_files(file_id, each_account)
                            if not status_b:
                                status_b = delete_trash(file_id, each_account)
                            if status_a and status_b:
                                break
                            sleep(2)

                        if status_a:
                            logging.info(f'帳號{each_account}已刪除{down_name}雲端硬碟檔案')
                        if status_b:
                            logging.info(f'帳號{each_account}已刪除{down_name}垃圾桶檔案')

                        if status_a and status_b:
                            print_info += f'\n帳號{each_account}中該檔案的雲端硬碟空間已釋放'
                        elif each_account in AUTO_DELETE and AUTO_DELETE[each_account] == 'False':
                            print_info += f'\n帳號{each_account}未開啟自動刪除'
                        else:
                            print_info += f'\n帳號{each_account}中該檔案的雲端硬碟空間釋放失敗，請手動刪除'
                        notifier.send(print_info)
                        logging.info(print_info)

                        record_batch_result(batch_id, 'success', down_name, "", notifier)
                        state.update_task(task_id, stage=state.STAGE_COMPLETE)
                else:
                    done_count = len(complete_file_id) + len(failed_gid)
                    total_bytes = sum(b['total'] for b in gid_bytes.values())
                    if total_bytes > 0:
                        done_bytes = sum(b['completed'] for b in gid_bytes.values())
                        aria_progress = int(done_bytes / total_bytes * 100)
                        byte_summary = f', {done_bytes / 1e9:.2f}GB/{total_bytes / 1e9:.2f}GB'
                    else:
                        # Fallback when aria2 doesn't know any file sizes yet (rare).
                        aria_progress = int(done_count / total_files * 100)
                        byte_summary = ''
                    state.update_task(task_id, progress=aria_progress)
                    status_summary = ', '.join(f'{k}={v}' for k, v in sorted(status_counts.items())) or '(no responses)'
                    logging.info(
                        f'aria2下載{down_name}還未完成 ({done_count}/{total_files} files{byte_summary} = {aria_progress}%) — '
                        f'status: {status_summary}，睡眠20s後再查...'
                    )
                    sleep(20)

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
                    thread_list.append(threading.Thread(
                        target=process_magnet,
                        args=[admin_notifier, None, None, None, task_info, account]
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
