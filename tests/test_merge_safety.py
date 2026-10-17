"""Regressions for the remaining PR review comments checked before merge."""
import importlib.util
import os
from pathlib import Path
import stat
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

import config
from pikpakbot import pipeline as p, state
from pikpakbot.notifier import NullNotifier


@pytest.mark.parametrize('restart', [False, True])
def test_rpc_outage_keeps_source_reserved_until_retry_completes(monkeypatch, magnet, restart):
    upload, push = Mock(), Mock()
    monkeypatch.setattr(p, 'magnet_upload', upload)
    monkeypatch.setattr(p.aria2_client, 'add_uri', push)
    tid = p.submit_magnet(NullNotifier(), magnet)
    state.update_task(tid, account='a', cloud_task_id='cloud', cloud_file_id='file')
    directory = p.ARIA2_DOWNLOAD_PATH
    state.set_manifest(tid, 'file.bin', 'file', [('file', 'file.bin', directory)])
    state.save_file(tid, 'file', 'file.bin', directory, gid='known-gid', status='active', destination_owned=1)
    monkeypatch.setattr(p.aria2_client, 'tell_status_many', Mock(side_effect=requests.Timeout('rpc down')))
    p._run_task(tid, NullNotifier())
    if restart:
        state._conn.close()
        state._conn = None
        state.sweep_interrupted()
        p.startup_recovery(NullNotifier())
    with pytest.raises(state.TaskConflict):
        p.submit_magnet(NullNotifier(), magnet)
    child = p.retry_task(NullNotifier(), tid)
    # The permitted child must adopt the existing GID, without a second upload.
    (Path(directory) / 'file.bin').write_bytes(b'data')
    monkeypatch.setattr(p.aria2_client, 'tell_status_many', lambda gids: {
        'known-gid': {'status': 'complete', 'completedLength': '4', 'totalLength': '4'}})
    monkeypatch.setattr(p, 'auto_delete_judge', lambda _: 'off')
    p._run_task(child, NullNotifier())
    assert state.get_task(child)['stage'] == state.STAGE_COMPLETE
    upload.assert_not_called()
    push.assert_not_called()
    state.claim('later-task', 'source:' + p.source_key(magnet))


@pytest.fixture
def copied_config(tmp_path):
    path = tmp_path / 'config_copy.py'
    path.write_text(Path(config.__file__).read_text(encoding='utf-8'), encoding='utf-8')
    spec = importlib.util.spec_from_file_location('config_copy', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, path


@pytest.mark.parametrize('mode', [0o600, 0o640, 0o644])
def test_config_replacement_preserves_existing_mode_and_owner(copied_config, mode):
    module, path = copied_config
    path.chmod(mode)
    before = path.stat()
    module.DISCORD_ADMIN_IDS = [987]
    module.record_config()
    after = path.stat()
    assert stat.S_IMODE(after.st_mode) == stat.S_IMODE(before.st_mode)
    assert (after.st_uid, after.st_gid) == (before.st_uid, before.st_gid)
    assert 'DISCORD_ADMIN_IDS = [987]' in path.read_text(encoding='utf-8')


@pytest.mark.parametrize('denied', [False, True])
def test_config_restores_bind_mount_owner_before_replace(copied_config, monkeypatch, denied):
    module, path = copied_config
    original = path.read_bytes()
    before = path.stat()
    real_fstat = os.fstat
    owner = (before.st_uid + 1000, before.st_gid + 1000)
    # Simulate a bind mount owned by a different host user, on any test OS.
    def fstat(fd):
        result = real_fstat(fd)
        if result.st_ino == before.st_ino:
            return SimpleNamespace(st_uid=owner[0], st_gid=owner[1], st_mode=result.st_mode)
        return result
    monkeypatch.setattr(os, 'fstat', fstat)
    chown = Mock(side_effect=PermissionError('chown denied') if denied else None)
    monkeypatch.setattr(os, 'fchown', chown, raising=False)
    initial_files = set(path.parent.iterdir())
    module.DISCORD_ADMIN_IDS = [987]
    if denied:
        with pytest.raises(PermissionError):
            module.record_config()
        assert path.read_bytes() == original
    else:
        module.record_config()
        assert 'DISCORD_ADMIN_IDS = [987]' in path.read_text(encoding='utf-8')
    chown.assert_called_once()
    assert chown.call_args.args[1:] == owner
    assert set(path.parent.iterdir()) == initial_files
