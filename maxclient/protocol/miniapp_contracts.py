"""Pinned incoming OPEN_APP / authenticated webapp launch160 contracts."""
from dataclasses import dataclass, field
import ipaddress
from urllib.parse import urlsplit

from .search_contracts import _integer


def miniapp_origin(url):
    """Validate supplied HTTPS URL, preserving its signed fragment unchanged."""
    error = 'MAX did not supply a supported HTTPS mini-app address'
    if (not isinstance(url, str) or not url or len(url) > 65536
            or any(ord(c) <= 32 or ord(c) == 127 for c in url) or '\\' in url):
        raise ValueError(error)
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.port not in (None, 443)):
            raise ValueError(error)
        host = parsed.hostname.encode('idna').decode('ascii').lower()
        last = host.rsplit('.', 1)[-1]
        # Chromium's WHATWG parser accepts legacy IPv4 spellings that Python's
        # ipaddress rejects (127.1, octal and hexadecimal components).
        numeric_tail = last.isdigit() or (last.startswith('0x') and len(last) > 2
                                          and all(c in '0123456789abcdef' for c in last[2:]))
        if ('.' not in host or host.endswith(('.localhost', '.local', '.internal', '.lan', '.home', '.'))
                or numeric_tail
                or any(not p or len(p) > 63 or p.startswith('-') or p.endswith('-')
                       or any(not (c.isascii() and (c.isalnum() or c == '-')) for c in p)
                       for p in host.split('.'))):
            raise ValueError(error)
        try:
            ipaddress.ip_address(host)
        except ValueError:
            return 'https://' + host
    except (ValueError, UnicodeError):
        pass
    raise ValueError(error)


@dataclass(frozen=True)
class MiniAppReply:
    url: str = field(repr=False)
    origin: str
    query_id: str = field(repr=False)


def build_miniapp_launch(bot_id, chat_id, start_param=None):
    if not _integer(bot_id, 64) or bot_id == 0 or not _integer(chat_id, 64):
        raise ValueError('Actual mini-app bot and dialog identifiers are required')
    if start_param is not None and (not isinstance(start_param, str) or len(start_param) > 16384):
        raise ValueError('Invalid supplied mini-app start parameter')
    payload = {'botId': bot_id, 'chatId': chat_id}
    if start_param:  # Exact native startParam:t||undefined behavior.
        payload['startParam'] = start_param
    return payload


def parse_miniapp_launch(data):
    if (not isinstance(data, dict) or not isinstance(data.get('query_id'), str)
            or len(data['query_id']) > 16384):
        raise ValueError('MAX did not confirm the mini-app launch response')
    origin = miniapp_origin(data.get('url'))
    return MiniAppReply(data['url'], origin, data['query_id'])
