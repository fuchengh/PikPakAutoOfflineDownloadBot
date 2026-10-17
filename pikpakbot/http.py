"""One connection pool per worker, never shared mutable Session headers."""
import threading

import requests

_local = threading.local()


def session():
    if not hasattr(_local, 'session'):
        _local.session = requests.Session()
    return _local.session
