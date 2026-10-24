from pathlib import Path
from unittest.mock import Mock

import pytest
import requests

import config
from pikpakbot import pipeline as p, state
from pikpakbot.notifier import NullNotifier


def task(**kwargs):
    return state.create_task(account='a', cloud_file_id='folder', **kwargs)


def cloud(monkeypatch, entries=None):
    entries = entries or [('ok.bin', 'url', 'ok', 'folder/'), ('bad.bin', 'url', 'bad', 'folder/')]
    monkeypatch.setattr(p, 'get_download_url', lambda fid, account: ('folder', '') if fid == 'folder' else (fid + '.bin', 'url'))
    monkeypatch.setattr(p, 'get_folder_all_file', lambda *a: iter(entries))
    deleted, trashed = Mock(return_value=True), Mock(return_value=True)
    monkeypatch.setattr(p, 'delete_files', deleted)
    monkeypatch.setattr(p, 'delete_trash', trashed)
    return deleted, trashed


def complete_rpc(monkeypatch):
    downloads = {}
    def add(url, directory, name, headers, *, gid):
        Path(directory).mkdir(parents=True, exist_ok=True)
        (Path(directory) / name).write_bytes(b'0123456789')
        downloads[gid] = {'status': 'complete', 'totalLength': '10', 'completedLength': '10'}
        return {'result': gid}
    push = Mock(side_effect=add)
    monkeypatch.setattr(p.aria2_client, 'add_uri', push)
    monkeypatch.setattr(p.aria2_client, 'tell_status_many', lambda gids: {
        gid: downloads.get(gid, {'rpc_error': 'GID not found'}) for gid in gids})
    return push, downloads


def test_failed_submission_cannot_delete_parent_or_cloud_files(monkeypatch):
    deleted, trashed = cloud(monkeypatch)
    push, downloads = complete_rpc(monkeypatch)
    normal = push.side_effect
    def add(url, directory, name, headers, *, gid):
        if name == 'bad.bin':
            raise requests.Timeout('submit failed')
        return normal(url, directory, name, headers, gid=gid)
    push.side_effect = add
    tid = task()
    p._run_task(tid, NullNotifier())
    assert state.get_task(tid)['stage'] == state.STAGE_FAILED
    assert len(state.files(tid)) == 2
    assert 'submit failed' in state.get_task(tid)['error']
    deleted.assert_not_called()
    trashed.assert_not_called()


def test_partial_retry_preserves_completed_file_and_skips_download(monkeypatch):
    deleted, _ = cloud(monkeypatch)
    push, downloads = complete_rpc(monkeypatch)
    normal = push.side_effect
    def add(url, directory, name, headers, *, gid):
        if name == 'bad.bin':
            raise requests.Timeout('offline')
        return normal(url, directory, name, headers, gid=gid)
    push.side_effect = add
    parent = task()
    p._run_task(parent, NullNotifier())
    child = state.claim_retry(parent)
    push.reset_mock()
    push.side_effect = normal
    p._run_task(child, NullNotifier())
    assert state.get_task(child)['stage'] == state.STAGE_COMPLETE
    assert [call.args[2] for call in push.call_args_list] == ['bad.bin']
    assert {i['status'] for i in state.files(child)} == {'complete'}
    assert set(deleted.call_args.args[0]) == {'ok', 'bad'}
    assert 'folder' not in deleted.call_args.args[0]


def test_enumeration_failure_before_any_submission(monkeypatch):
    deleted, _ = cloud(monkeypatch)
    def broken(*args):
        yield 'ok.bin', 'url', 'ok', 'folder/'
        raise p.PikPakError('page 2 failed')
    monkeypatch.setattr(p, 'get_folder_all_file', broken)
    push = Mock()
    monkeypatch.setattr(p.aria2_client, 'add_uri', push)
    tid = task()
    p._run_task(tid, NullNotifier())
    assert 'page 2 failed' in state.get_task(tid)['error']
    assert not state.get_task(tid)['manifest_ready']
    push.assert_not_called()
    deleted.assert_not_called()


def test_empty_cloud_folder_not_deleted(monkeypatch):
    deleted, _ = cloud(monkeypatch)
    monkeypatch.setattr(p, 'get_folder_all_file', lambda *a: iter([]))
    tid = task()
    p._run_task(tid, NullNotifier())
    assert state.get_task(tid)['stage'] == state.STAGE_FAILED
    deleted.assert_not_called()


def test_lost_add_response_adopts_reserved_gid_without_duplicate(monkeypatch):
    cloud(monkeypatch, [('one.bin', 'url', 'one', '')])
    push, downloads = complete_rpc(monkeypatch)
    normal = push.side_effect
    def timeout_after_accept(*args, **kwargs):
        normal(*args, **kwargs)
        raise requests.Timeout('response lost')
    push.side_effect = timeout_after_accept
    tid = task()
    p._run_task(tid, NullNotifier())
    assert state.get_task(tid)['stage'] == state.STAGE_COMPLETE
    assert push.call_count == 1
    assert state.files(tid)[0]['gid'] == push.call_args.kwargs['gid']


def test_existing_gid_is_adopted_after_restart(monkeypatch):
    cloud(monkeypatch, [('one.bin', 'url', 'one', '')])
    push, downloads = complete_rpc(monkeypatch)
    tid = task()
    directory = str(Path(p.ARIA2_DOWNLOAD_PATH))
    state.set_manifest(tid, 'one.bin', 'one', [('one', 'one.bin', directory)])
    state.save_file(tid, 'one', 'one.bin', directory, gid='old-gid', destination_owned=1)
    (Path(directory) / 'one.bin').write_bytes(b'0123456789')
    downloads['old-gid'] = {'status': 'complete', 'totalLength': '10', 'completedLength': '10'}
    p._run_task(tid, NullNotifier())
    push.assert_not_called()
    assert state.get_task(tid)['stage'] == state.STAGE_COMPLETE


def test_stale_aria_complete_with_missing_local_file_not_cleaned(monkeypatch):
    deleted, _ = cloud(monkeypatch, [('one.bin', 'url', 'one', '')])
    monkeypatch.setattr(p.aria2_client, 'add_uri', lambda *a, **kw: {'result': kw['gid']})
    monkeypatch.setattr(p.aria2_client, 'tell_status_many', lambda gids: {
        gid: {'status': 'complete', 'totalLength': '10', 'completedLength': '10'} for gid in gids})
    tid = task()
    p._run_task(tid, NullNotifier())
    assert state.get_task(tid)['stage'] == state.STAGE_FAILED
    deleted.assert_not_called()


def test_success_cleans_only_verified_leaf_ids(monkeypatch):
    deleted, trashed = cloud(monkeypatch)
    complete_rpc(monkeypatch)
    tid = task()
    p._run_task(tid, NullNotifier())
    assert state.get_task(tid)['stage'] == state.STAGE_COMPLETE
    assert deleted.call_count == trashed.call_count == 1
    assert set(deleted.call_args.args[0]) == set(trashed.call_args.args[0]) == {'ok', 'bad'}
    assert deleted.call_args.args[1] == 'a'


def test_same_destination_conflict_submits_nothing(monkeypatch):
    deleted, _ = cloud(monkeypatch, [('one.bin', 'url', 'one', '')])
    push, _ = complete_rpc(monkeypatch)
    state.claim('other', 'path:' + str(Path(p.ARIA2_DOWNLOAD_PATH) / 'one.bin').lower())
    # normcase is platform-specific.
    import os
    state.claim('other', 'path:' + os.path.normcase(str(Path(p.ARIA2_DOWNLOAD_PATH) / 'one.bin')))
    tid = task()
    p._run_task(tid, NullNotifier())
    assert state.get_task(tid)['stage'] == state.STAGE_FAILED
    push.assert_not_called()
    deleted.assert_not_called()


@pytest.mark.parametrize('directory,name', [('../outside', 'x'), ('', '../file'), ('', 'a/b')])
def test_remote_paths_cannot_escape_output_root(directory, name):
    with pytest.raises(ValueError):
        p._safe_destination(directory, name)


def test_provider_error_immediate_and_preserved(monkeypatch, magnet):
    tid = state.create_task(magnet=magnet, cloud_task_id='cloud', account='a')
    poll = Mock(return_value=[{'id': 'cloud', 'phase': 'PHASE_TYPE_ERROR', 'progress': 99,
                              'error_code': 42, 'message': 'quota exhausted'}])
    monkeypatch.setattr(p, 'get_offline_list', poll)
    p._run_task(tid, NullNotifier())
    assert poll.call_count == 1
    assert '42' in state.get_task(tid)['error'] and 'quota exhausted' in state.get_task(tid)['error']


def test_query_outage_does_not_become_task_cancellation(monkeypatch):
    tid = state.create_task(cloud_task_id='cloud', account='a')
    monkeypatch.setattr(p, 'get_offline_list', Mock(side_effect=p.PikPakError('503')))
    p._run_task(tid, NullNotifier())
    assert '連續查詢失敗' in state.get_task(tid)['error']
    assert '找不到' not in state.get_task(tid)['error']


def test_stalled_offline_retries_are_bounded_and_no_deletions(monkeypatch):
    monkeypatch.setattr(config, 'OFFLINE_STALL_SECONDS', 20)
    monkeypatch.setattr(config, 'MAX_AUTO_RETRIES', 2)
    tid = state.create_task(cloud_task_id='cloud', account='a')
    monkeypatch.setattr(p, 'get_offline_list', lambda *a: [
        {'id': 'cloud', 'phase': 'PHASE_TYPE_RUNNING', 'progress': 99}])
    def dispatch(*args, **kwargs):
        kwargs['before_send']()
        return True, {}
    retry, delete = Mock(side_effect=dispatch), Mock()
    monkeypatch.setattr(p, 'retry_offline_task', retry)
    monkeypatch.setattr(p, 'delete_files', delete)
    p._run_task(tid, NullNotifier())
    assert retry.call_count == 2
    assert '自動重試次數已用盡' in state.get_task(tid)['error']
    delete.assert_not_called()


def test_native_retry_new_id_stays_in_same_worker(monkeypatch):
    tid = state.create_task(cloud_task_id='old', account='a', retry_requested=1)
    poll = Mock(side_effect=[
        [{'id': 'old', 'phase': 'PHASE_TYPE_ERROR', 'progress': 0}],
        [{'id': 'new', 'phase': 'PHASE_TYPE_COMPLETE', 'progress': 100, 'file_id': 'file'}]])
    monkeypatch.setattr(p, 'get_offline_list', poll)
    monkeypatch.setattr(p, 'retry_offline_task', lambda *a, **kw: (True, {'task': {'id': 'new'}}))
    assert p._offline(tid, NullNotifier()) == ('file', 'a')
    assert state.get_task(tid)['cloud_task_id'] == 'new'
    p._executor.submit.assert_not_called()


def test_watchdog_does_not_spawn_for_owned_offline_job(monkeypatch):
    tid = state.create_task(cloud_task_id='cloud', account='a', stage=state.STAGE_OFFLINE)
    monkeypatch.setattr(p, 'get_stuck_tasks', lambda *a, **kw: [{'id': 'cloud', 'name': 'same'}])
    p.retry_offline_candidates('a', 0, automatic=True)
    assert state.get_task(tid)['retry_requested'] == 0
    p._executor.submit.assert_not_called()
    p.retry_offline_candidates('a', 0, automatic=False)
    assert state.get_task(tid)['retry_requested'] == 1
    p._executor.submit.assert_not_called()


def test_recovery_uses_persisted_ids_not_all_completed_cloud_tasks(monkeypatch):
    tid = state.create_task(cloud_file_id='f', account='a', stage=state.STAGE_DOWNLOAD)
    state.save_file(tid, 'f', 'f.bin', '/downloads', gid='saved')
    completed = state.create_task(cloud_file_id='other', stage=state.STAGE_COMPLETE)
    state.sweep_interrupted()
    notifier = NullNotifier()
    p.startup_recovery(notifier)
    assert p._executor.submit.call_count == 1
    child = state.get_task(tid)['retry_child']
    assert state.files(child)[0]['gid'] == 'saved'
    assert state.get_task(completed)['retry_child'] is None


def test_duplicate_sources_are_rejected_before_second_worker(magnet):
    p.submit_magnet(NullNotifier(), magnet)
    with pytest.raises(state.TaskConflict):
        p.submit_magnet(NullNotifier(), magnet + '&dn=other')
    assert p._executor.submit.call_count == 1


def test_completed_futures_release_queue_slots_and_registry(magnet):
    p.submit_magnet(NullNotifier(), magnet)
    future = next(iter(p._jobs))
    future.set_result(None)
    assert not p.check_download_thread_status()
