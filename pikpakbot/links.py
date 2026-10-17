"""Parse pasted links without splitting HTTP tracker parameters in magnets."""
import base64
import re
from urllib.parse import parse_qs, urlsplit


def source_key(link):
    if not link.lower().startswith('magnet:?'):
        return link
    params = {key.lower(): value for key, value in parse_qs(urlsplit(link).query).items()}
    for xt in params.get('xt', []):
        if xt.lower().startswith('urn:btih:'):
            value = xt[9:]
            if re.fullmatch(r'[0-9a-fA-F]{40}', value):
                return 'btih:' + value.lower()
            if re.fullmatch(r'[A-Za-z2-7]{32}', value):
                return 'btih:' + base64.b32decode(value.upper()).hex()
        if re.fullmatch(r'urn:btmh:1220[0-9a-fA-F]{64}', xt, re.I):
            return xt.lower()
    raise ValueError('magnet 缺少有效的 xt/infohash')


def parse_links(text):
    # A literal magnet:? cannot be part of a properly percent-encoded value.
    text = re.sub(r'(?i)(magnet:\?)', r' \1', text)
    # Commas separate pasted links only before a new scheme, not inside URLs.
    text = re.sub(r'[,，;；](?=(?:https?://|ed2k://))', ' ', text, flags=re.I)
    links, seen = [], set()
    for item in text.split():
        item = item.strip('<>"\'`，,；;')
        if not item:
            continue
        if item.lower().startswith('magnet:?'):
            item = 'magnet:?' + item[len('magnet:?'):]
        elif not re.match(r'(?i)^(https?://|ed2k://)', item):
            raise ValueError('請提供 magnet、HTTP(S) 或 ed2k 連結')
        key = source_key(item)
        if key not in seen:
            seen.add(key)
            links.append(item)
    return links
