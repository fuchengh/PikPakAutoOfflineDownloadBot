import asyncio
from concurrent.futures import ThreadPoolExecutor, wait
from pathlib import Path
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import requests

from pikpakbot import pipeline as p, state, http
from pikpakbot.notifier import DiscordNotifier, NullNotifier


def test_rpc_outage_reserves_paths_across_restart_and_transfers_to_retry(monkeypatch):
    tid = state.create_task(cloud_file_id='file', account='a')
    directory = p.ARIA2_DOWNLOAD_PATH
    state.set_manifest(tid, 'file.bin', 'file', [('file', 'file.bin', directory)])
    state.save_file(tid, 'file', 'file.bin', directory, gid='known-gid', status='active', destination_owned=1)
    monkeypatch.setattr(p.aria2_client, 'tell_status_many', Mock(side_effect=requests.Timeout('rpc down')))
    p._run_task(tid, NullNotifier())
    path_key = 'path:' + __import__('os').path.normcase(str(Path(directory) / 'file.bin'))
    with pytest.raises(state.TaskConflict):
        state.claim('unrelated', path_key)
    state.sweep_interrupted()
    with pytest.raises(state.TaskConflict):
        state.claim('unrelated', path_key)
    child = state.claim_retry(tid)
    state.claim(child, path_key)
    assert state.files(child)[0]['gid'] == 'known-gid'


def test_real_worker_pool_bound_and_finished_jobs_removed(monkeypatch, magnet):
    gate = threading.Event()
    lock = threading.Lock()
    active = [0, 0]
    def download(*args):
        with lock:
            active[0] += 1
            active[1] = max(active)
        assert gate.wait(timeout=5)
        with lock:
            active[0] -= 1
        return 'done'
    monkeypatch.setattr(p, '_offline', lambda *a: ('file', 'a'))
    monkeypatch.setattr(p, '_run_aria2_phase', download)
    with ThreadPoolExecutor(max_workers=2) as executor:
        monkeypatch.setattr(p, '_executor', executor)
        ids = []
        try:
            for number in range(5):
                ids.append(p.submit_magnet(NullNotifier(), f'magnet:?xt=urn:btih:{number:040x}'))
            futures = list(p._jobs)
            assert p.check_download_thread_status()
        finally:
            gate.set()
        wait(futures, timeout=5)
    assert active[1] <= 2
    assert not p.check_download_thread_status()
    assert all(state.get_task(tid)['stage'] == state.STAGE_COMPLETE for tid in ids)


def test_queue_overflow_rejected_without_spawning_second_job(monkeypatch, magnet):
    monkeypatch.setattr(p, '_slots', threading.BoundedSemaphore(1))
    p.submit_magnet(NullNotifier(), magnet)
    with pytest.raises(state.TaskConflict, match='佇列已滿'):
        p.submit_magnet(NullNotifier(), magnet.replace('a' * 40, 'b' * 40))
    assert p._executor.submit.call_count == 1
    assert len(state.list_active()) == 1


def test_http_session_reused_per_worker_not_shared_between_workers(monkeypatch):
    monkeypatch.setattr(http, '_local', threading.local())
    barrier = threading.Barrier(2)
    def run():
        first = http.session()
        barrier.wait(timeout=5)
        assert http.session() is first
        return first
    with ThreadPoolExecutor(max_workers=2) as executor:
        a, b = list(executor.map(lambda _: run(), range(2)))
    assert a is not b
    a.close()
    b.close()


def test_retry_created_before_parent_message_delivery_still_reuses_message(magnet):
    parent = state.create_task(magnet=magnet, stage=state.STAGE_FAILED, discord_channel_id=123)
    child = state.claim_retry(parent)
    # The failed worker's asynchronous send completes after the child row exists.
    state.update_task(parent, discord_message_id=99)
    async def run():
        ch = SimpleNamespace(get_partial_message=Mock(return_value=SimpleNamespace(edit=AsyncMock())),
                             send=AsyncMock())
        notifier = DiscordNotifier(123)
        await notifier._progress_async(SimpleNamespace(get_channel=lambda _: ch), child, 'retry')
        ch.get_partial_message.assert_called_once_with(99)
        ch.send.assert_not_awaited()
        assert state.get_task(child)['discord_message_id'] == 99
    asyncio.run(run())


def test_repeated_retry_requests_are_coalesced():
    tid = state.create_task(stage=state.STAGE_OFFLINE)
    assert state.request_offline_retry(tid)
    assert not state.request_offline_retry(tid)
    state.update_task(tid, retry_requested=0, last_retry_at=state.time.time())
    assert not state.request_offline_retry(tid)


def test_provider_alias_survives_native_retry_id_change():
    tid = state.create_task(cloud_task_id='old', account='a')
    state.claim(tid, 'cloud-task:a:old')
    state.update_task(tid, cloud_task_id='new', stage=state.STAGE_FAILED)
    state.release(tid)
    assert state.latest_cloud_task('a', 'old')['task_id'] == tid


def test_only_never_submitted_queued_magnets_auto_recover(magnet):
    queued = state.create_task(magnet=magnet)
    uncertain = state.create_task(magnet=magnet.replace('a' * 40, 'b' * 40), submission_started=1)
    state.sweep_interrupted()
    p.startup_recovery(NullNotifier())
    assert state.get_task(queued)['retry_child']
    assert not state.get_task(uncertain)['retry_child']


def test_byte_progress_is_reported_for_single_large_file(monkeypatch):
    directory = p.ARIA2_DOWNLOAD_PATH
    tid = state.create_task(cloud_file_id='file', account='a')
    state.set_manifest(tid, 'file.bin', 'file', [('file', 'file.bin', directory)])
    state.save_file(tid, 'file', 'file.bin', directory, gid='gid', destination_owned=1)
    calls = [0]
    def statuses(gids):
        calls[0] += 1
        if calls[0] == 1:
            return {'gid': {'status': 'active', 'completedLength': '50', 'totalLength': '100'}}
        assert state.get_task(tid)['progress'] == 50
        (Path(directory) / 'file.bin').write_bytes(b'x' * 100)
        return {'gid': {'status': 'complete', 'completedLength': '100', 'totalLength': '100'}}
    monkeypatch.setattr(p.aria2_client, 'tell_status_many', statuses)
    monkeypatch.setattr(p, 'auto_delete_judge', lambda _: 'off')
    p._run_task(tid, NullNotifier())
    assert state.get_task(tid)['stage'] == state.STAGE_COMPLETE


def test_cloud_only_worker_cannot_retry_until_finalization_finishes(monkeypatch):
    tid = p.submit_cloud_file(NullNotifier(), 'file', 'a')
    monkeypatch.setattr(p, '_run_aria2_phase', Mock(side_effect=p.PikPakError('download failed')))
    notifier = Mock()
    def finalize(**kwargs):
        assert state.get_task(tid)['stage'] == state.STAGE_FAILED
        with pytest.raises(state.TaskConflict, match='收尾'):
            state.claim_retry(tid)
    notifier.finalize.side_effect = finalize
    p._run_task(tid, notifier)
    notifier.finalize.assert_called_once()
    assert state.claim_retry(tid)


@pytest.mark.parametrize('error', [requests.Timeout('timeout'), p.AmbiguousMutation('503 after submit')])
def test_uncertain_submission_never_falls_back_to_another_account(monkeypatch, magnet, error):
    monkeypatch.setattr(p, 'USER', ['a', 'b'])
    def dispatch(*args, **kwargs):
        kwargs['before_send']()
        raise error
    upload = Mock(side_effect=dispatch)
    monkeypatch.setattr(p, 'magnet_upload', upload)
    tid = state.create_task(magnet=magnet)
    p._run_task(tid, NullNotifier())
    assert upload.call_count == 1
    assert state.get_task(tid)['submission_started'] == 1
    assert '提交結果不確定' in state.get_task(tid)['error']
