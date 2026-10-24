import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
from email.utils import formatdate
import json
import logging
from pathlib import Path
import tempfile
import threading
import types
import unittest
from unittest.mock import Mock, patch

import httpx
from pikpakapi import PikPakApi

from pikpakbot.cloud_guard import CloudGuard, CloudBlocked, rate_limited
from pikpakbot import pikpak_client as pk


class PikPakError(Exception):
    pass


class AmbiguousMutation(PikPakError):
    pass


class RequestError(Exception):
    pass


class CloudPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'status.json'
        self.now = 10000
        self.guard = CloudGuard(self.path, clock=lambda: self.now)
        self.session = Mock()
        self.ctx = dict(guard=self.guard, CloudBlocked=CloudBlocked, rate_limited=rate_limited,
            PikPakError=PikPakError, AmbiguousMutation=AmbiguousMutation, DEFAULT_TIMEOUT=30,
            DEFAULT_MAX_ATTEMPTS=3, USER=['account'], PASSWORD=['secret'], session=lambda: self.session,
            pikpak_headers={'account': {'Authorization': 'test'}}, pikpak_clients={},
            requests=types.SimpleNamespace(RequestException=RequestError), invalidate_offline=Mock(),
            time=types.SimpleNamespace(time=lambda: self.now), asyncio=asyncio, logging=logging,
            login_lock=threading.RLock(), PIKPAK_API_URL='https://mock.invalid')
        self.patch = patch.dict(pk.__dict__, self.ctx)
        self.patch.start()
        self.ctx = pk.__dict__

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    def response(self, status=200, body=None, retry=None):
        return types.SimpleNamespace(status_code=status, content=b'json',
            json=lambda: body if body is not None else {'tasks': []},
            headers={'Retry-After': retry} if retry else {})

    def request(self, method='GET'):
        return self.ctx['_pikpak_request'](method, 'https://mock.invalid', 'account')

    def test_concurrent_callers_make_only_one_request_on_rate_limit(self):
        self.session.request.return_value = self.response(500, {'error_description': 'Aborted - Your operation is too frequent'})
        def run(_):
            try:
                self.request()
            except PikPakError:
                return True
        with ThreadPoolExecutor(max_workers=12) as pool:
            self.assertTrue(all(pool.map(run, range(30))))
        self.assertEqual(self.session.request.call_count, 1)
        self.assertEqual(self.guard.account_status('account')['retry_at'], self.now + 900)
        self.assertEqual(self.guard.account_status('account')['status'], 'cooldown')

    def test_restart_preserves_deadline_backoff_and_no_credentials(self):
        self.guard.fail('account', 'rate_limit')
        raw = self.path.read_text()
        self.assertNotIn('secret', raw)
        self.assertNotIn('Authorization', raw)
        guard = CloudGuard(self.path, clock=lambda: self.now)
        with self.assertRaises(CloudBlocked):
            guard.check('account')
        self.now += 901
        self.assertEqual(guard.account_status('account')['status'], 'recovering')
        guard.fail('account', 'rate_limit')
        self.assertEqual(guard.account_status('account')['retry_after_seconds'], 1800)
        guard.success('account')
        self.assertEqual(guard.account_status('account')['status'], 'ok')
        self.now += 601
        self.assertEqual(guard.account_status('account')['status'], 'unknown')

    def test_retry_after_seconds_and_http_date_are_honored_without_short_cap(self):
        for value in ('10000', formatdate(self.now + 10000, usegmt=True)):
            with self.subTest(value=value):
                guard = CloudGuard(Path(self.temp.name) / ('s' + str(len(value))), clock=lambda: self.now)
                guard.fail('account', 'rate_limit', value)
                self.assertEqual(guard.account_status('account')['retry_after_seconds'], 10000)

    def test_post_timeout_and_server_error_are_not_replayed(self):
        for failure in ('timeout', '500', 'rate500'):
            with self.subTest(failure=failure):
                self.now += 10000
                self.session.reset_mock()
                self.session.request.side_effect = RequestError('timeout') if failure == 'timeout' else None
                self.session.request.return_value = self.response(500, {'error': 'too frequent' if failure == 'rate500' else 'Internal'})
                with self.assertRaises((RequestError, AmbiguousMutation)):
                    self.request('POST')
                self.assertEqual(self.session.request.call_count, 1)

    def test_local_cooldown_prevents_login_and_post(self):
        self.guard.fail('account', 'rate_limit')
        self.ctx['pikpak_headers'].clear()
        self.ctx['PikPakApi'] = Mock()
        with self.assertRaises(PikPakError):
            self.ctx['prepare_submission']('account')
        self.ctx['PikPakApi'].assert_not_called()
        self.session.request.assert_not_called()

    def test_post_server_error_with_auth_code_remains_uncertain(self):
        self.session.request.return_value = self.response(500, {'error': 'internal', 'error_code': 16})
        self.ctx['login'] = Mock()
        with self.assertRaises(AmbiguousMutation):
            self.request('POST')
        self.assertEqual(self.session.request.call_count, 1)
        self.ctx['login'].assert_not_called()

    def sdk_transport(self, handler, with_token=False):
        def build(**kwargs):
            kwargs['httpx_client_args']['transport'] = httpx.MockTransport(handler)
            return PikPakApi(**kwargs)
        self.ctx['PikPakApi'] = build
        self.ctx['pikpak_headers'].clear()
        if with_token:
            encoded = base64.b64encode(json.dumps({'access_token': 'fixture-access',
                'refresh_token': 'fixture-refresh'}).encode()).decode()
            self.ctx['pikpak_clients']['account'] = types.SimpleNamespace(encoded_token=encoded)

    def test_real_sdk_rate_response_honors_retry_after_without_extra_calls(self):
        calls = []
        def provider(request):
            calls.append(request.url.path)
            if request.url.path.endswith('/captcha/init'):
                return httpx.Response(200, json={'captcha_token': 'fixture-captcha'})
            return httpx.Response(429, json={'error': 'captcha_invalid',
                'error_description': 'Aborted - Your operation is too frequent'}, headers={'Retry-After': '3600'})
        self.sdk_transport(provider)
        with self.assertRaises(PikPakError):
            self.ctx['login']('account')
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.guard.account_status('account')['retry_after_seconds'], 3600)
        with self.assertRaises(PikPakError):
            self.ctx['login']('account')
        self.assertEqual(len(calls), 2)

    def test_real_sdk_expired_refresh_cannot_recursively_refresh(self):
        calls = []
        def provider(request):
            calls.append(request.url.path)
            if len(calls) > 6:
                raise RuntimeError('Fixture stopped recursive SDK network calls')
            if request.url.path.endswith('/auth/token'):
                return httpx.Response(401, json={'error': 'unauthenticated', 'error_code': 16})
            if request.url.path.endswith('/captcha/init'):
                return httpx.Response(200, json={'captcha_token': 'fixture-captcha'})
            return httpx.Response(200, json={'access_token': 'fixture-new',
                'refresh_token': 'fixture-next', 'sub': 'fixture-user'})
        self.sdk_transport(provider, with_token=True)
        self.ctx['login']('account')
        self.assertEqual(calls, ['/v1/auth/token', '/v1/shield/captcha/init', '/v1/auth/signin'])
        self.assertTrue(self.ctx['pikpak_clients']['account'].httpx_client.is_closed)

    def test_real_sdk_refresh_5xx_does_not_recurse_or_fall_back_to_signin(self):
        calls = []
        def provider(request):
            calls.append(request.url.path)
            if len(calls) > 6:
                raise RuntimeError('Fixture stopped recursive SDK network calls')
            return httpx.Response(500, json={'error': 'internal', 'error_code': 16})
        self.sdk_transport(provider, with_token=True)
        with self.assertRaises(PikPakError):
            self.ctx['login']('account')
        self.assertEqual(calls, ['/v1/auth/token'])

    def test_probe_is_read_only_coalesced_and_healthy_only_after_success(self):
        self.session.request.return_value = self.response()
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda _: self.ctx['probe_cloud']('account'), range(20)))
        self.assertEqual(self.session.request.call_count, 1)
        self.assertEqual(self.session.request.call_args.args[0], 'GET')
        self.assertEqual(self.guard.account_status('account')['status'], 'ok')
        self.now += 301
        self.ctx['probe_cloud']('account')
        self.assertEqual(self.session.request.call_count, 2)

    def test_business_error_probe_also_backs_off(self):
        self.session.request.return_value = self.response(403, {'error': 'access_denied'})
        for _ in range(4):
            self.ctx['probe_cloud']('account')
        self.assertEqual(self.session.request.call_count, 1)

    def test_corrupt_or_unwritable_state_fails_closed_and_keeps_post_uncertain(self):
        self.path.write_text('broken')
        guard = CloudGuard(self.path)
        with self.assertRaises(CloudBlocked):
            guard.check('account')
        self.session.request.return_value = self.response()
        self.guard._save = Mock(side_effect=CloudBlocked('disk unavailable'))
        with self.assertRaises(AmbiguousMutation):
            self.request('POST')
        self.assertEqual(self.session.request.call_count, 1)

    def fake_sdk(self, refresh_fail=False, rate_fail=False):
        client = types.SimpleNamespace(refresh_token='refresh', encoded_token='encoded',
                                     get_headers=lambda: {'Authorization': 'new'})
        counts = {'login': 0, 'refresh': 0, 'close': 0}
        async def login():
            counts['login'] += 1
            if rate_fail:
                raise ValueError('Aborted - Your operation is too frequent, please try again later')
        async def refresh():
            counts['refresh'] += 1
            if refresh_fail:
                raise ValueError('Your operation is too frequent')
        async def close():
            counts['close'] += 1
        client.login, client.refresh_access_token = login, refresh
        client.httpx_client = types.SimpleNamespace(aclose=close)
        self.ctx['PikPakApi'] = Mock(return_value=client)
        return client, counts

    def test_auth_uses_refresh_first_without_extra_signin_or_refresh(self):
        client, counts = self.fake_sdk()
        self.ctx['pikpak_clients']['account'] = client
        self.ctx['login']('account')
        self.assertEqual(counts, {'login': 0, 'refresh': 1, 'close': 1})
        self.assertEqual(self.ctx['PikPakApi'].call_args.kwargs['request_max_retries'], 1)
        self.assertNotEqual(self.guard.account_status('account')['status'], 'ok')

    def test_refresh_rate_limit_does_not_fall_back_to_password(self):
        client, counts = self.fake_sdk(refresh_fail=True)
        self.ctx['pikpak_clients']['account'] = client
        with self.assertRaises(PikPakError):
            self.ctx['login']('account')
        self.assertEqual(counts, {'login': 0, 'refresh': 1, 'close': 1})
        with self.assertRaises(PikPakError):
            self.ctx['login']('account')
        self.assertEqual(counts['refresh'], 1)

    def test_fresh_login_does_not_immediately_refresh(self):
        client, counts = self.fake_sdk()
        client.refresh_token = None
        self.ctx['login']('account')
        self.assertEqual(counts, {'login': 1, 'refresh': 0, 'close': 1})



if __name__ == '__main__':
    unittest.main()
