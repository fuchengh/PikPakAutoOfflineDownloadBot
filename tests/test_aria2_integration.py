"""Real aria2 + synthetic localhost HTTP; PikPak and cleanup stay mocked."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import shutil
import socket
import subprocess
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import urlsplit

import pytest
import requests

import config
from pikpakbot import aria2_client as aria, pipeline as p, state
from pikpakbot.notifier import NullNotifier

# Captured during collection, before the per-test network-blocking fixture.
_real_request = requests.sessions.Session.request


def eventually(check, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError('isolated aria2 operation did not finish')


@pytest.fixture
def engine(monkeypatch, tmp_path):
    binary = os.getenv('ARIA2_TEST_BINARY') or shutil.which('aria2c')
    if not binary:
        if os.getenv('REQUIRE_ARIA2_TESTS') == '1':
            pytest.fail('CI requires aria2c for integration tests')
        pytest.skip('Set ARIA2_TEST_BINARY or install aria2c for localhost integration tests')
    data = SimpleNamespace(payload=b'new-cloud-content', ranges=[], proc=None, rpc_url=None)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            offset = int(self.headers.get('Range', 'bytes=0-').split('=')[1].split('-')[0])
            data.ranges.append(offset)
            content = data.payload[offset:]
            self.send_response(206 if offset else 200)
            self.send_header('Content-Type', 'application/octet-stream')
            self.send_header('Content-Length', str(len(content)))
            self.send_header('Accept-Ranges', 'bytes')
            if offset:
                self.send_header('Content-Range', f'bytes {offset}-{len(data.payload) - 1}/{len(data.payload)}')
            self.end_headers()
            try:
                self.wfile.write(content)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass  # Expected when the isolated download is paused/stopped.

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    transport = requests.Session()
    transport.trust_env = False

    def localhost_request(method, url, **kwargs):
        assert url == data.rpc_url and urlsplit(url).hostname == '127.0.0.1'
        kwargs['allow_redirects'] = False
        return _real_request(transport, method, url, **kwargs)

    # Exempt only this fixture's loopback RPC endpoint from the network guard.
    transport.request = localhost_request
    monkeypatch.setattr(aria, 'session', lambda: transport)
    monkeypatch.setattr(aria, 'ARIA2_SECRET', 'isolated-regression-only')
    monkeypatch.setattr(p, 'sleep', lambda seconds: time.sleep(min(0.02, seconds)))
    monkeypatch.setattr(p, 'monotonic', time.monotonic)
    monkeypatch.setattr(config, 'ARIA2_STALL_SECONDS', 5)
    url = f'http://127.0.0.1:{server.server_port}/file'
    monkeypatch.setattr(p, 'get_download_url', lambda *args: ('same.bin', url))
    monkeypatch.setattr(p, 'auto_delete_judge', lambda *args: 'on')
    data.trash, data.purge = Mock(return_value=True), Mock(return_value=True)
    monkeypatch.setattr(p, 'delete_files', data.trash)
    monkeypatch.setattr(p, 'delete_trash', data.purge)
    data.url = url

    def stop():
        if data.proc is not None and data.proc.poll() is None:
            data.proc.terminate()
            try:
                data.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                data.proc.kill()
                data.proc.wait(timeout=5)

    def start(*, rate='0'):
        stop()
        with socket.socket() as finder:
            finder.bind(('127.0.0.1', 0))
            port = finder.getsockname()[1]
        data.rpc_url = f'http://127.0.0.1:{port}/jsonrpc'
        monkeypatch.setattr(aria, 'RPC_URL', data.rpc_url)
        data.proc = subprocess.Popen([
            binary, '--no-conf=true', '--enable-rpc=true', '--rpc-listen-all=false',
            f'--rpc-listen-port={port}', '--rpc-secret=isolated-regression-only',
            '--enable-dht=false', '--enable-dht6=false', '--enable-peer-exchange=false',
            '--file-allocation=none', '--max-tries=1', '--all-proxy=',
            '--connect-timeout=2', '--timeout=2', f'--max-download-limit={rate}',
            '--console-log-level=error', '--summary-interval=0', '--auto-save-interval=1',
        ], cwd=tmp_path, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))

        def ready():
            try:
                return aria._rpc('aria2.getVersion', [], timeout=0.2)['result']
            except requests.RequestException:
                return False
        eventually(ready)

    data.start = start
    try:
        start()
        yield data
    finally:
        stop()
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)
        transport.close()


def test_real_engine_does_not_accept_same_size_unrelated_file(engine):
    path = Path(p.ARIA2_DOWNLOAD_PATH) / 'same.bin'
    path.write_bytes(b'old-local-content')
    assert path.stat().st_size == len(engine.payload)
    gid = '1234567890abcdef'
    aria.add_uri(engine.url, str(path.parent), path.name, {}, gid=gid)
    def terminal():
        status = aria.tell_status_many([gid])[gid]
        return status if status.get('status') in ('error', 'complete') else None
    assert eventually(terminal)['status'] == 'error'
    assert path.read_bytes() == b'old-local-content'


def test_real_pipeline_preserves_unrelated_local_and_cloud_files(engine):
    path = Path(p.ARIA2_DOWNLOAD_PATH) / 'same.bin'
    path.write_bytes(b'old-local-content')
    task_id = p.submit_cloud_file(NullNotifier(), 'file', 'a')
    p._run_task(task_id, NullNotifier())
    assert state.get_task(task_id)['stage'] == state.STAGE_FAILED
    assert path.read_bytes() == b'old-local-content'
    assert engine.ranges == []
    engine.trash.assert_not_called()
    engine.purge.assert_not_called()


def test_real_fresh_download_succeeds_before_cloud_cleanup(engine):
    task_id = p.submit_cloud_file(NullNotifier(), 'file', 'a')
    p._run_task(task_id, NullNotifier())
    assert state.get_task(task_id)['stage'] == state.STAGE_COMPLETE
    assert (Path(p.ARIA2_DOWNLOAD_PATH) / 'same.bin').read_bytes() == engine.payload
    engine.trash.assert_called_once_with(['file'], 'a')
    engine.purge.assert_called_once_with(['file'], 'a')


def test_real_partial_download_resumes_after_bot_and_engine_restart(engine, monkeypatch):
    engine.payload = bytes(range(256)) * 16384
    engine.start(rate='64K')
    tid = p.submit_cloud_file(NullNotifier(), 'file', 'a')
    state.set_manifest(tid, 'same.bin', 'file', [('file', 'same.bin', p.ARIA2_DOWNLOAD_PATH)])
    state.update_task(tid, stage=state.STAGE_DOWNLOAD)
    pushes = Mock(wraps=aria.add_uri)
    monkeypatch.setattr(aria, 'add_uri', pushes)
    p._push(tid, state.files(tid)[0], 'a')
    saved_gid = state.files(tid)[0]['gid']

    def partially_downloaded():
        done = int(aria.tell_status_many([saved_gid])[saved_gid].get('completedLength', 0))
        return 0 < done < len(engine.payload)
    eventually(partially_downloaded)
    aria._rpc('aria2.forcePause', [saved_gid])
    control = Path(p.ARIA2_DOWNLOAD_PATH) / 'same.bin.aria2'
    eventually(lambda: aria.tell_status_many([saved_gid])[saved_gid].get('status') == 'paused' and control.exists())
    engine.start()  # Old GID is absent; the owned control file remains on disk.
    state._conn.close()
    state._conn = None
    state.sweep_interrupted()
    p.startup_recovery(NullNotifier())
    child = state.get_task(tid)['retry_child']
    assert state.files(child)[0]['destination_owned'] == 1
    p._run_task(child, NullNotifier())
    assert state.get_task(child)['stage'] == state.STAGE_COMPLETE
    assert pushes.call_count == 2  # One submission per engine, same reserved GID.
    assert [call.kwargs['gid'] for call in pushes.call_args_list] == [saved_gid, saved_gid]
    assert any(offset > 0 for offset in engine.ranges)
    assert (Path(p.ARIA2_DOWNLOAD_PATH) / 'same.bin').read_bytes() == engine.payload
    engine.trash.assert_called_once_with(['file'], 'a')
    engine.purge.assert_called_once_with(['file'], 'a')
