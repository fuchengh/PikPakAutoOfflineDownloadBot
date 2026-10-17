from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest
import requests

from pikpakbot import aria2_client as aria, pikpak_client as pk


def response(data, code=200):
    return Mock(status_code=code, content=b'json', headers={}, json=Mock(return_value=data))


def test_later_folder_page_error_never_returns_partial_files(monkeypatch):
    api = Mock(side_effect=[(None, {'files': [{'id': 'f'}], 'next_page_token': 'next'}),
                            pk.PikPakError('provider down')])
    monkeypatch.setattr(pk, '_pikpak_request', api)
    with pytest.raises(pk.PikPakError, match='provider down'):
        pk.get_list('folder', 'a')


def test_repeated_pagination_token_rejected(monkeypatch):
    monkeypatch.setattr(pk, '_pikpak_request', Mock(return_value=(None, {'tasks': [], 'next_page_token': 'same'})))
    with pytest.raises(pk.PikPakError, match='token'):
        pk.get_offline_list('a')


def test_api_outage_is_not_an_empty_snapshot(monkeypatch):
    monkeypatch.setattr(pk, '_pikpak_request', Mock(side_effect=requests.Timeout('outage')))
    with pytest.raises(requests.Timeout):
        pk.get_offline_list('a')


def test_shared_snapshot_coalesces_parallel_reads(monkeypatch):
    api = Mock(return_value=(None, {'tasks': [{'id': 't', 'progress': 10}]}))
    monkeypatch.setattr(pk, '_pikpak_request', api)
    with ThreadPoolExecutor(max_workers=6) as pool:
        snapshots = list(pool.map(pk.get_offline_list, ['a'] * 12))
    assert api.call_count == 1
    snapshots[0][0]['progress'] = 99
    assert snapshots[1][0]['progress'] == 10
    pk.invalidate_offline('a')
    pk.get_offline_list('a')
    assert api.call_count == 2


@pytest.mark.parametrize('data', [{}, {'files': None}, {'files': 'not-a-list'}])
def test_malformed_list_is_not_empty_success(monkeypatch, data):
    monkeypatch.setattr(pk, '_pikpak_request', Mock(return_value=(None, data)))
    with pytest.raises(pk.PikPakError):
        pk.get_list('folder', 'a')


def test_metadata_error_is_not_silently_skipped(monkeypatch):
    monkeypatch.setattr(pk, 'get_list', lambda *a: [{'id': 'file', 'kind': 'drive#file'}])
    monkeypatch.setattr(pk, 'get_download_url', Mock(side_effect=pk.PikPakError('no link')))
    with pytest.raises(pk.PikPakError):
        list(pk.get_folder_all_file('folder', 'name/', 'a'))


def test_native_folder_without_link_is_distinct_from_incomplete_file(monkeypatch):
    monkeypatch.setattr(pk, '_pikpak_request', Mock(return_value=(None, {'kind': 'drive#folder', 'name': 'dir'})))
    assert pk.get_download_url('folder', 'a') == ('dir', '')
    monkeypatch.setattr(pk, '_pikpak_request', Mock(return_value=(None, {'kind': 'drive#file', 'name': 'dir'})))
    with pytest.raises(pk.PikPakError):
        pk.get_download_url('file', 'a')


def test_stuck_filter_includes_error_but_not_fresh_99_percent(monkeypatch):
    tasks = [{'id': 'bad', 'phase': 'PHASE_TYPE_ERROR', 'progress': 0},
             {'id': 'new', 'phase': 'PHASE_TYPE_RUNNING', 'progress': 99}]
    monkeypatch.setattr(pk, 'get_offline_list', lambda *a: tasks)
    assert [t['id'] for t in pk.get_stuck_tasks('a')] == ['bad']
    assert pk.get_stuck_tasks('a', include_errors=False) == []


def test_account_cache_does_not_follow_list_indices(monkeypatch):
    monkeypatch.setattr(pk, 'USER', ['b', 'a'])
    monkeypatch.setattr(pk, 'pikpak_headers', {'a': {'token': 'a'}, 'b': {'token': 'b'}})
    monkeypatch.setattr(pk, 'pikpak_clients', {'a': 'client-a', 'b': 'client-b'})
    assert pk.get_clients('a') == 'client-a'
    pk.USER.pop(0)
    pk.forget_account('b')
    assert pk.get_headers('a') == {'token': 'a'}
    with pytest.raises(pk.PikPakError):
        pk.get_clients('b')


def test_http_retries_transient_read_and_auth_has_separate_budget(monkeypatch):
    transport = Mock()
    transport.request.side_effect = [response({}, 401), response({}, 503), response({'tasks': []})]
    monkeypatch.setattr(pk, 'session', lambda: transport)
    monkeypatch.setattr(pk, 'get_headers', lambda _: {})
    login = Mock()
    monkeypatch.setattr(pk, 'login', login)
    assert pk._pikpak_request('GET', 'url', 'a', max_attempts=2)[1] == {'tasks': []}
    assert transport.request.call_count == 3
    login.assert_called_once_with('a')


def test_ambiguous_post_timeout_not_blindly_duplicated(monkeypatch):
    transport = Mock(request=Mock(side_effect=requests.Timeout()))
    monkeypatch.setattr(pk, 'session', lambda: transport)
    monkeypatch.setattr(pk, 'get_headers', lambda _: {})
    with pytest.raises(requests.Timeout):
        pk._pikpak_request('POST', 'url', 'a')
    assert transport.request.call_count == 1


def test_http_provider_error_preserved(monkeypatch):
    transport = Mock(request=Mock(return_value=response({'error_code': 42, 'error_description': 'quota'}, 403)))
    monkeypatch.setattr(pk, 'session', lambda: transport)
    monkeypatch.setattr(pk, 'get_headers', lambda _: {})
    with pytest.raises(pk.PikPakError, match='42.*quota'):
        pk._pikpak_request('GET', 'url', 'a')


def test_multicall_auth_per_entry_and_per_file_errors(monkeypatch):
    transport = Mock(post=Mock(return_value=response({'result': [[{'status': 'active'}],
                                  {'code': 1, 'message': 'GID not found'}]})))
    monkeypatch.setattr(aria, 'session', lambda: transport)
    statuses = aria.tell_status_many(['one', 'two'])
    assert statuses['one']['status'] == 'active'
    assert statuses['two']['rpc_error'] == 'GID not found'
    payload = transport.post.call_args.kwargs['json']
    assert payload['method'] == 'system.multicall'
    assert len(payload['params']) == 1
    assert payload['params'][0][0]['params'][0].startswith('token:')


def test_aria_submission_uses_idempotent_gid_and_resume_options(monkeypatch):
    rpc = Mock(return_value={'result': 'gid'})
    monkeypatch.setattr(aria, '_rpc', rpc)
    aria.add_uri('url', '/downloads', 'file', {'User-Agent': 'test'}, gid='gid')
    options = rpc.call_args.args[1][1]
    assert options['gid'] == 'gid' and options['continue'] == 'true'
    assert options['auto-file-renaming'] == options['allow-overwrite'] == 'false'
    assert options['header'] == ['User-Agent: test']


@pytest.mark.parametrize('mode', ['503', 'invalid_json', 'missing_task_id'])
def test_uncertain_upload_result_is_explicit_and_not_replayed(monkeypatch, magnet, mode):
    reply = response({}, 503 if mode == '503' else 200)
    if mode == 'invalid_json':
        reply.json.side_effect = ValueError('truncated response')
    transport = Mock(request=Mock(return_value=reply))
    monkeypatch.setattr(pk, 'session', lambda: transport)
    monkeypatch.setattr(pk, 'get_headers', lambda _: {})
    with pytest.raises(pk.AmbiguousMutation):
        pk.magnet_upload(magnet, 'a')
    assert transport.request.call_count == 1


def test_retry_query_outage_reports_failure_not_empty_success(monkeypatch):
    monkeypatch.setattr(pk, 'get_offline_list', Mock(side_effect=pk.PikPakError('503')))
    success, failed, results = pk.retry_stuck_tasks('a')
    assert (success, failed) == (0, 1)
    assert '503' in results[0]['message']
    assert '未重試' in results[0]['message']
