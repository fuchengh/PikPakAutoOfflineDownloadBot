import uuid
from pikpakbot.http import session

from config import ARIA2_HTTPS, ARIA2_HOST, ARIA2_PORT, ARIA2_SECRET

SCHEMA = 'https' if ARIA2_HTTPS else 'http'
RPC_URL = f'{SCHEMA}://{ARIA2_HOST}:{ARIA2_PORT}/jsonrpc'


class Aria2Error(RuntimeError):
    pass


def _rpc(method, params, timeout=10, *, authenticated=True):
    response = session().post(RPC_URL, json={
        'jsonrpc': '2.0',
        'id': uuid.uuid4().hex,
        'method': method,
        'params': ([f'token:{ARIA2_SECRET}'] if authenticated else []) + params,
    }, timeout=timeout)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict) or 'result' not in data:
        raise Aria2Error(str(data.get('error') if isinstance(data, dict) else 'Invalid RPC response'))
    return data


def add_uri(url, dir_, name, headers, timeout=10, *, gid=None):
    options = {'dir': dir_, 'out': name,
               'header': [f'{key}: {value}' for key, value in headers.items()],
               # Owned .aria2 control files resume without --continue. Never infer
               # progress from the length of an unrelated existing file.
               'continue': 'false', 'auto-file-renaming': 'false', 'allow-overwrite': 'false'}
    if gid:
        options['gid'] = gid
    return _rpc('aria2.addUri', [[url], options], timeout=timeout)


def tell_status(gid, keys, timeout=5):
    return _rpc('aria2.tellStatus', [gid, keys], timeout=timeout)


def tell_status_many(gids):
    keys = ['gid', 'status', 'errorCode', 'errorMessage', 'dir', 'completedLength', 'totalLength']
    result = {}
    for start in range(0, len(gids), 100):
        batch = gids[start:start + 100]
        calls = [{'methodName': 'aria2.tellStatus',
                  'params': [f'token:{ARIA2_SECRET}', gid, keys]} for gid in batch]
        rows = _rpc('system.multicall', [calls], authenticated=False)['result']
        if not isinstance(rows, list) or len(rows) != len(batch):
            raise Aria2Error('Incomplete multicall response')
        for gid, row in zip(batch, rows):
            if isinstance(row, list) and len(row) == 1 and isinstance(row[0], dict):
                result[gid] = row[0]
            elif isinstance(row, dict) and 'code' in row:
                result[gid] = {'rpc_error': str(row.get('message') or row)}
            else:
                raise Aria2Error('Invalid multicall entry')
    return result
