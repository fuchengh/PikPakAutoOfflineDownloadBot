"""Regression cases from PR #1's second review, including crash boundaries."""
from pathlib import Path
import sqlite3
from unittest.mock import Mock

import pytest
import requests

from pikpakbot import pipeline as p, state, pikpak_client as pk
from pikpakbot.notifier import NullNotifier


def reboot():
    # Load durable records through a new SQLite connection, as a new process does.
    state._conn.close()
    state._conn = None
    state.sweep_interrupted()
    p.startup_recovery(NullNotifier())


@pytest.mark.parametrize('existing', ['file', 'control', 'both'])
def test_new_task_refuses_unowned_existing_data_before_any_submission(monkeypatch, existing):
    path = Path(p.ARIA2_DOWNLOAD_PATH) / 'same.bin'
    if existing in ('file', 'both'):
        path.write_bytes(b'old-local-content')
    if existing in ('control', 'both'):
        Path(str(path) + '.aria2').write_bytes(b'another download control file')
    monkeypatch.setattr(p, 'get_download_url', lambda *args: ('same.bin', 'unused'))
    push, trash, purge = Mock(), Mock(), Mock()
    monkeypatch.setattr(p.aria2_client, 'add_uri', push)
    monkeypatch.setattr(p, 'delete_files', trash)
    monkeypatch.setattr(p, 'delete_trash', purge)
    task_id = p.submit_cloud_file(NullNotifier(), 'file', 'a')
    p._run_task(task_id, NullNotifier())
    assert state.get_task(task_id)['stage'] == state.STAGE_FAILED
    push.assert_not_called()
    trash.assert_not_called()
    purge.assert_not_called()
    if path.exists():
        assert path.read_bytes() == b'old-local-content'


def test_retry_without_control_file_does_not_guess_byte_offset(monkeypatch):
    directory = p.ARIA2_DOWNLOAD_PATH
    tid = state.create_task(account='a', cloud_file_id='file', stage=state.STAGE_FAILED)
    state.set_manifest(tid, 'same.bin', 'file', [('file', 'same.bin', directory)])
    state.save_file(tid, 'file', 'same.bin', directory, gid='old-gid', destination_owned=1)
    path = Path(directory) / 'same.bin'
    path.write_bytes(b'not-proven-complete')
    monkeypatch.setattr(p.aria2_client, 'tell_status_many', lambda gids: {g: {'rpc_error': 'GID not found'} for g in gids})
    monkeypatch.setattr(p, 'get_download_url', lambda *args: ('same.bin', 'unused'))
    push, trash = Mock(), Mock()
    monkeypatch.setattr(p.aria2_client, 'add_uri', push)
    monkeypatch.setattr(p, 'delete_files', trash)
    child = p.retry_task(NullNotifier(), tid)
    p._run_task(child, NullNotifier())
    assert '控制檔' in state.get_task(child)['error']
    assert path.read_bytes() == b'not-proven-complete'
    push.assert_not_called()
    trash.assert_not_called()


def test_retry_after_accepted_timeout_must_not_submit_magnet_twice(monkeypatch, magnet):
    monkeypatch.setattr(p, 'USER', ['a'])
    accepted = []
    def upload(*args, **kwargs):
        accepted.append('cloud-' + str(len(accepted) + 1))
        raise requests.Timeout('provider accepted upload; response was lost')
    monkeypatch.setattr(p, 'magnet_upload', upload)
    parent = p.submit_magnet(NullNotifier(), magnet)
    notifier = Mock()
    p._run_task(parent, notifier)
    assert state.get_task(parent)['submission_started'] == 1
    assert state.get_task(parent)['account'] == 'a'
    assert all(b.action != 'retry' for b in notifier.send.call_args.kwargs['buttons'])
    for recover in (False, True):
        with pytest.raises(state.TaskConflict, match='尚未確認'):
            p.retry_task(NullNotifier(), parent, recover=recover)
    assert not state.get_task(parent)['retry_child']
    # A fresh /p must not bypass the protection, including after a restart.
    for _ in range(2):
        with pytest.raises(state.TaskConflict):
            p.submit_magnet(NullNotifier(), magnet)
        reboot()
    assert accepted == ['cloud-1']


def test_retry_child_cannot_erase_uncertain_submission_marker(magnet):
    parent = state.create_task(magnet=magnet, stage=state.STAGE_FAILED, submission_started=1)
    with pytest.raises(state.TaskConflict, match='尚未確認'):
        state.claim_retry(parent)
    reboot()
    assert state.get_task(parent)['submission_started'] == 1
    assert not state.get_task(parent)['retry_child']


def test_worker_refuses_uncertain_submission_even_without_handler_guard(monkeypatch, magnet):
    tid = state.create_task(magnet=magnet, submission_started=1)
    upload = Mock()
    monkeypatch.setattr(p, 'magnet_upload', upload)
    p._run_task(tid, NullNotifier())
    assert '尚未確認' in state.get_task(tid)['error']
    upload.assert_not_called()


def test_restart_of_queued_manual_retry_preserves_retry_intent(monkeypatch):
    parent = state.create_task(account='a', cloud_task_id='cloud', stage=state.STAGE_FAILED)
    queued = p.retry_task(NullNotifier(), parent)
    assert state.get_task(queued)['retry_requested'] == 1
    reboot()
    recovered = state.get_task(queued)['retry_child']
    assert recovered
    poll = Mock(side_effect=[
        [{'id': 'cloud', 'phase': 'PHASE_TYPE_ERROR', 'progress': 0}],
        [{'id': 'cloud', 'phase': 'PHASE_TYPE_COMPLETE', 'file_id': 'file'}],
    ])
    retry = Mock(return_value=(True, {}))
    monkeypatch.setattr(p, 'get_offline_list', poll)
    monkeypatch.setattr(p, 'retry_offline_task', retry)
    monkeypatch.setattr(p, '_run_aria2_phase', Mock(return_value='done'))
    p._run_task(recovered, NullNotifier())
    retry.assert_called_once_with('cloud', 'a')
    assert state.get_task(recovered)['stage'] == state.STAGE_COMPLETE
    assert state.get_task(recovered)['retry_inflight'] == 0


def test_crash_between_retry_row_creation_and_queueing_keeps_request():
    parent = state.create_task(account='a', cloud_task_id='cloud', stage=state.STAGE_FAILED)
    child = state.claim_retry(parent)
    assert state.get_task(child)['retry_requested'] == 1
    reboot()
    recovered = state.get_task(child)['retry_child']
    assert state.get_task(recovered)['retry_requested'] == 1


@pytest.mark.parametrize('crash', [False, True])
def test_native_retry_unknown_outcome_is_not_replayed_after_restart(monkeypatch, magnet, crash):
    tid = state.create_task(magnet=magnet, account='a', cloud_task_id='old', retry_requested=1)
    state.claim(tid, 'source:' + p.source_key(magnet))
    state.claim(tid, 'cloud-task:a:old')
    monkeypatch.setattr(p, 'get_offline_list', lambda *args: [{'id': 'old', 'phase': 'PHASE_TYPE_ERROR'}])
    retry = Mock(side_effect=SystemExit('crash') if crash else p.AmbiguousMutation('response lost'))
    monkeypatch.setattr(p, 'retry_offline_task', retry)
    if crash:
        with pytest.raises(SystemExit):
            p._offline(tid, NullNotifier())
    else:
        p._run_task(tid, NullNotifier())
    assert state.get_task(tid)['retry_inflight'] == 1
    assert state.get_task(tid)['retry_requested'] == 0
    assert not state.request_offline_retry(tid)
    reboot()
    assert not state.get_task(tid)['retry_child']
    with pytest.raises(state.TaskConflict, match='尚未確認'):
        p.retry_task(NullNotifier(), tid)
    with pytest.raises(state.TaskConflict):
        p.submit_magnet(NullNotifier(), magnet)
    assert retry.call_count == 1


def test_definite_native_retry_rejection_is_retryable(monkeypatch):
    tid = state.create_task(account='a', cloud_task_id='old', retry_requested=1)
    monkeypatch.setattr(p, 'get_offline_list', lambda *args: [{'id': 'old', 'phase': 'PHASE_TYPE_ERROR'}])
    monkeypatch.setattr(p, 'retry_offline_task', lambda *args: (False, 'quota rejected'))
    p._run_task(tid, NullNotifier())
    assert state.get_task(tid)['retry_inflight'] == 0
    child = p.retry_task(NullNotifier(), tid)
    assert state.get_task(child)['retry_requested'] == 1


def test_definite_upload_rejection_can_be_retried(monkeypatch, magnet):
    monkeypatch.setattr(p, 'USER', ['a'])
    monkeypatch.setattr(p, 'magnet_upload', Mock(side_effect=p.PikPakError('quota rejected')))
    tid = p.submit_magnet(NullNotifier(), magnet)
    p._run_task(tid, NullNotifier())
    assert state.get_task(tid)['submission_started'] == 0
    assert p.retry_task(NullNotifier(), tid)


def test_native_retry_client_keeps_ambiguous_outcome_distinct(monkeypatch):
    monkeypatch.setattr(pk, '_pikpak_request', Mock(side_effect=requests.Timeout('accepted, response lost')))
    with pytest.raises(pk.AmbiguousMutation):
        pk.retry_offline_task('cloud', 'a')
    monkeypatch.setattr(pk, '_pikpak_request', Mock(side_effect=pk.PikPakError('explicit rejection')))
    assert pk.retry_offline_task('cloud', 'a') == (False, 'explicit rejection')


def test_old_file_manifests_migrate_without_inventing_ownership():
    conn = sqlite3.connect(state.DB_PATH)
    conn.execute('''CREATE TABLE task_files(task_id TEXT, file_id TEXT, name TEXT, directory TEXT,
        gid TEXT, status TEXT, size INTEGER, error TEXT, PRIMARY KEY(task_id,file_id))''')
    conn.execute("INSERT INTO task_files VALUES ('old','file','name','dir','gid','complete',17,NULL)")
    conn.commit()
    conn.close()
    row = state.files('old')[0]
    assert row['destination_owned'] == 0 and row['gid'] == 'gid' and row['size'] == 17


def test_legacy_gid_without_destination_proof_cannot_authorize_cleanup(monkeypatch):
    directory = p.ARIA2_DOWNLOAD_PATH
    tid = state.create_task(account='a', cloud_file_id='file')
    state.set_manifest(tid, 'same.bin', 'file', [('file', 'same.bin', directory)])
    state.save_file(tid, 'file', 'same.bin', directory, gid='legacy', status='complete', size=17)
    (Path(directory) / 'same.bin').write_bytes(b'old-local-content')
    trash = Mock()
    monkeypatch.setattr(p, 'delete_files', trash)
    p._run_task(tid, NullNotifier())
    assert state.get_task(tid)['stage'] == state.STAGE_FAILED
    trash.assert_not_called()


def test_completed_file_is_rechecked_before_cleanup(monkeypatch):
    directory = p.ARIA2_DOWNLOAD_PATH
    tid = state.create_task(account='a', cloud_file_id='folder')
    state.set_manifest(tid, 'folder', 'folder', [('one', 'one.bin', directory), ('two', 'two.bin', directory)])
    for name in ('one', 'two'):
        state.save_file(tid, name, name + '.bin', directory, gid=name, destination_owned=1)
    calls = [0]
    def statuses(gids):
        calls[0] += 1
        if calls[0] == 1:
            (Path(directory) / 'one.bin').write_bytes(b'data')
            return {'one': {'status': 'complete', 'completedLength': '4', 'totalLength': '4'},
                    'two': {'status': 'active', 'completedLength': '0', 'totalLength': '4'}}
        (Path(directory) / 'one.bin').unlink()
        (Path(directory) / 'two.bin').write_bytes(b'data')
        return {'two': {'status': 'complete', 'completedLength': '4', 'totalLength': '4'}}
    monkeypatch.setattr(p.aria2_client, 'tell_status_many', statuses)
    trash = Mock()
    monkeypatch.setattr(p, 'delete_files', trash)
    p._run_task(tid, NullNotifier())
    assert '清理前' in state.get_task(tid)['error']
    trash.assert_not_called()
