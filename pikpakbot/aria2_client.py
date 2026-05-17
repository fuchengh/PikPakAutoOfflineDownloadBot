import json

import requests

from config import ARIA2_HTTPS, ARIA2_HOST, ARIA2_PORT, ARIA2_SECRET

SCHEMA = 'https' if ARIA2_HTTPS else 'http'
RPC_URL = f'{SCHEMA}://{ARIA2_HOST}:{ARIA2_PORT}/jsonrpc'


def _rpc(method, params, timeout=5):
    payload = json.dumps({
        'jsonrpc': '2.0',
        'id': 'qwer',
        'method': method,
        'params': [f"token:{ARIA2_SECRET}"] + params,
    })
    return requests.post(RPC_URL, data=payload, timeout=timeout).json()


def add_uri(url, dir_, name, headers, timeout=5):
    return _rpc('aria2.addUri', [[url], {'dir': dir_, 'out': name, 'header': headers}], timeout=timeout)


def tell_status(gid, keys, timeout=5):
    return _rpc('aria2.tellStatus', [gid, keys], timeout=timeout)
