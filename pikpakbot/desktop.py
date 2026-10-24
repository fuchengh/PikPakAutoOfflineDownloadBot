"""Optional adapter owned by pikpak-next; absent in Discord-only installations."""
from importlib import import_module
import logging


def _adapter():
    try:
        return import_module('desktop_api')
    except ModuleNotFoundError as exc:
        if exc.name != 'desktop_api':
            raise  # Do not mistake a broken adapter dependency for no adapter.
        return None


def start_api():
    try:
        adapter = _adapter()
        if adapter is not None:
            adapter.start_api()
    except Exception:
        logging.exception('Desktop API unavailable; existing bot continues')


def restore_notifier(row):
    adapter = _adapter()
    return adapter.restore_notifier(row) if adapter is not None else None
