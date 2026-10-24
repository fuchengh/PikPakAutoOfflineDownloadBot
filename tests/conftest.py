from concurrent.futures import Future
from pathlib import Path
import sys
from unittest.mock import Mock

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config
from pikpakbot import pipeline, pikpak_client, state, cloud_guard


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(state, 'DB_PATH', str(tmp_path / 'state.db'))
    monkeypatch.setattr(state, '_conn', None)
    monkeypatch.setattr(pipeline, 'ARIA2_DOWNLOAD_PATH', str(tmp_path / 'downloads'))
    (tmp_path / 'downloads').mkdir()
    monkeypatch.setattr(requests.sessions.Session, 'request', Mock(side_effect=AssertionError('Network forbidden')))
    import httpx
    import aiohttp
    from unittest.mock import AsyncMock
    monkeypatch.setattr(httpx.Client, 'send', Mock(side_effect=AssertionError('Network forbidden')))
    original_send = httpx.AsyncClient.send
    async def send_mock_transport(client, *args, **kwargs):
        if not isinstance(client._transport, httpx.MockTransport):
            raise AssertionError('Network forbidden')
        return await original_send(client, *args, **kwargs)
    monkeypatch.setattr(httpx.AsyncClient, 'send', send_mock_transport)
    monkeypatch.setattr(aiohttp.ClientSession, '_request', AsyncMock(side_effect=AssertionError('Network forbidden')))
    monkeypatch.setattr(pikpak_client, '_snapshots', {})
    monkeypatch.setattr(pikpak_client, '_snapshot_locks', {})
    guard = cloud_guard.CloudGuard(tmp_path / 'cloud-status.json')
    for account in ('a', 'b'):
        guard.success(account)
    monkeypatch.setattr(cloud_guard, 'guard', guard)
    monkeypatch.setattr(pikpak_client, 'guard', guard)
    monkeypatch.setattr(pikpak_client, 'USER', ['a', 'b'])
    monkeypatch.setattr(pikpak_client, 'pikpak_headers', {'a': {}, 'b': {}})
    monkeypatch.setattr(pikpak_client, 'pikpak_clients', {})
    monkeypatch.setattr(config, 'AUTO_DELETE', {})
    monkeypatch.setattr(pikpak_client, 'AUTO_DELETE', {})
    monkeypatch.setattr(pipeline, 'batch_results', {})
    monkeypatch.setattr(pipeline, '_jobs', set())
    # Capture submissions by default. Individual tests exercise real workers explicitly.
    executor = Mock()
    executor.submit.side_effect = lambda *a, **kw: Future()
    monkeypatch.setattr(pipeline, '_executor', executor)
    import threading
    monkeypatch.setattr(pipeline, '_slots', threading.BoundedSemaphore(128))
    clock = [0.0]
    monkeypatch.setattr(pipeline, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(pipeline, 'sleep', lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    monkeypatch.setattr(pikpak_client, 'sleep', lambda seconds: None)
    yield
    if state._conn is not None:
        state._conn.close()


@pytest.fixture
def magnet():
    return 'magnet:?xt=urn:btih:' + 'a' * 40
