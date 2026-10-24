"""Exercise real HTTP policy -> worker -> SQLite -> retry/restart boundaries."""
from unittest.mock import Mock

import pytest
import requests

from pikpakbot import pikpak_client as pk, pipeline as p, state
from pikpakbot.cloud_guard import CloudBlocked
from pikpakbot.notifier import NullNotifier


def response(status, body=None, malformed=False):
    return Mock(status_code=status, content=b'body', headers={},
                json=Mock(side_effect=ValueError('HTML') if malformed else None,
                          return_value=body or {}))


def task(monkeypatch, magnet, retry):
    monkeypatch.setattr(p, 'USER', ['a'])
    tid = p.submit_magnet(NullNotifier(), magnet)
    if retry:
        state.update_task(tid, account='a', cloud_task_id='cloud', retry_requested=1)
        monkeypatch.setattr(p, 'get_offline_list', lambda *args: [
            {'id': 'cloud', 'phase': 'PHASE_TYPE_ERROR'}])
    return tid


def reboot():
    state._conn.close()
    state._conn = None
    state.sweep_interrupted()
    p.startup_recovery(NullNotifier())


@pytest.mark.parametrize('retry', [False, True])
@pytest.mark.parametrize('failure', ['rate', 'html_rate', 'auth', 'guard_disk'])
def test_rejected_write_stays_manually_retryable_across_restart(monkeypatch, magnet, retry, failure):
    tid = task(monkeypatch, magnet, retry)
    flag = 'retry_inflight' if retry else 'submission_started'
    def send(*args, **kwargs):
        assert state.get_task(tid)[flag] == 1
        return response(401 if failure == 'auth' else 429,
                        {'error': 'too frequent'}, malformed=failure == 'html_rate')
    transport = Mock(request=Mock(side_effect=send))
    monkeypatch.setattr(pk, 'session', lambda: transport)
    if failure == 'guard_disk':
        monkeypatch.setattr(pk.guard, '_save', Mock(side_effect=CloudBlocked('disk unavailable')))
    if failure == 'auth':
        transport.request.side_effect = lambda *a, **kw: response(401, {'error_code': 16})
        def login(account):
            assert state.get_task(tid)[flag] == 0
            pk.guard.fail(account, 'rate_limit')
            raise pk.PikPakError('signin rate limited')
        monkeypatch.setattr(pk, 'login', login)
    p._run_task(tid, NullNotifier())
    assert not state.uncertain_mutation(state.get_task(tid))
    assert state.get_task(tid)['stage'] == state.STAGE_FAILED
    reboot()
    assert not state.get_task(tid)['retry_child']  # no automatic resubmit
    child = p.retry_task(NullNotifier(), tid)
    assert state.get_task(child)['retry_of'] == tid
    assert transport.request.call_count == 1


@pytest.mark.parametrize('retry', [False, True])
@pytest.mark.parametrize('failure', ['timeout', '500_auth', 'invalid_success'])
def test_unknown_write_remains_reserved_across_restart(monkeypatch, magnet, retry, failure):
    tid = task(monkeypatch, magnet, retry)
    transport = Mock(request=Mock(
        side_effect=requests.Timeout('response lost') if failure == 'timeout' else None,
        return_value=response(500 if failure == '500_auth' else 200,
                              {'error_code': 16}, malformed=failure == 'invalid_success')))
    monkeypatch.setattr(pk, 'session', lambda: transport)
    login = Mock()
    monkeypatch.setattr(pk, 'login', login)
    p._run_task(tid, NullNotifier())
    assert state.uncertain_mutation(state.get_task(tid))
    reboot()
    with pytest.raises(state.TaskConflict, match='尚未確認'):
        p.retry_task(NullNotifier(), tid)
    with pytest.raises(state.TaskConflict):
        p.submit_magnet(NullNotifier(), magnet)
    assert transport.request.call_count == 1
    login.assert_not_called()


@pytest.mark.parametrize('failure', ['folder_query', 'folder_create', 'invalid_path'])
def test_folder_preparation_never_marks_magnet_submitted(monkeypatch, magnet, failure):
    tid = task(monkeypatch, magnet, False)
    state.update_task(tid, offline_path='/folder' if failure != 'invalid_path' else '../bad')
    calls = []
    def send(method, url, **kwargs):
        calls.append(method)
        assert state.get_task(tid)['submission_started'] == 0
        if failure == 'folder_create' and method == 'GET':
            return response(200, {'files': []})
        raise requests.Timeout('preparation response lost')
    monkeypatch.setattr(pk, 'session', lambda: Mock(request=send))
    p._run_task(tid, NullNotifier())
    assert state.get_task(tid)['stage'] == state.STAGE_FAILED
    assert not state.uncertain_mutation(state.get_task(tid))
    reboot()
    assert p.retry_task(NullNotifier(), tid)
    assert calls == {'folder_query': ['GET'], 'folder_create': ['GET', 'POST'], 'invalid_path': []}[failure]


@pytest.mark.parametrize('retry', [False, True])
def test_rejected_then_authenticated_post_timeout_is_still_unknown(monkeypatch, magnet, retry):
    tid = task(monkeypatch, magnet, retry)
    flag = 'retry_inflight' if retry else 'submission_started'
    calls = []
    def send(*args, **kwargs):
        calls.append('POST')
        assert state.get_task(tid)[flag] == 1
        if len(calls) == 1:
            return response(401, {'error_code': 16})
        raise requests.Timeout('second response lost')
    monkeypatch.setattr(pk, 'session', lambda: Mock(request=send))
    def login(account):
        assert state.get_task(tid)[flag] == 0
    monkeypatch.setattr(pk, 'login', login)
    p._run_task(tid, NullNotifier())
    assert state.uncertain_mutation(state.get_task(tid))
    reboot()
    assert not state.get_task(tid)['retry_child']
    assert len(calls) == 2


def test_crash_during_reauthentication_recovers_without_unknown_marker(monkeypatch, magnet):
    tid = task(monkeypatch, magnet, False)
    transport = Mock(request=Mock(return_value=response(401, {'error_code': 16})))
    monkeypatch.setattr(pk, 'session', lambda: transport)
    monkeypatch.setattr(pk, 'login', Mock(side_effect=SystemExit('process stopped')))
    with pytest.raises(SystemExit):
        p._offline(tid, NullNotifier())
    assert not state.uncertain_mutation(state.get_task(tid))
    reboot()
    assert state.get_task(tid)['retry_child']
    assert transport.request.call_count == 1
