"""Literal incoming message presentation, without protocol actions or I/O.

Incoming INLINE_KEYBOARD is keyboard.buttons (not outgoing buttons). The
retained official schema and renderer are recorded in DISPLAY_BUTTONS_20261103.md.
"""
from __future__ import annotations

import re
from html import escape
from urllib.parse import urlsplit

_BUTTON_TYPES = frozenset({'LINK', 'CLIPBOARD', 'CALLBACK', 'CHAT', 'MESSAGE',
                           'REQUEST_GEO_LOCATION', 'REQUEST_CONTACT', 'OPEN_APP'})
_URL = re.compile(r'https?://[^\s<>"\x00-\x1f\x7f]+', re.IGNORECASE)


def safe_web_url(value):
    """Allow only ordinary absolute web URLs, never credentials or app commands."""
    if (not isinstance(value, str) or not value or len(value) > 16384
            or any(ord(char) < 33 or ord(char) == 127 for char in value)
            or '\\' in value):
        return None
    try:
        parsed = urlsplit(value)
        if (parsed.scheme.lower() not in {'http', 'https'} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None):
            return None
        _ = parsed.port  # Reject malformed/out-of-range ports.
        if any(char in parsed.hostname for char in '<>"{}|^`'):
            return None
    except (ValueError, TypeError):
        return None
    return value


def literal_link_html(text):
    """Return escaped rich text only when an actual safe web URL is present."""
    fragments, offset, found = [], 0, False
    for match in _URL.finditer(text):
        # Closing typographic quotes/ellipsis after a URL belong to the sentence.
        candidate = match.group(0).rstrip('.,;:!?»“”’…')
        # A balanced parenthesis can be part of the URL; sentence punctuation isn't.
        while candidate.endswith(')') and candidate.count(')') > candidate.count('('):
            candidate = candidate[:-1]
        if safe_web_url(candidate) is None:
            continue
        end = match.start() + len(candidate)
        fragments.extend((escape(text[offset:match.start()]),
                          '<a style="color:#0085FF;text-decoration:underline" href="'
                          + escape(candidate, quote=True) + '">' + escape(candidate) + '</a>'))
        offset, found = end, True
    if not found:
        return None
    fragments.append(escape(text[offset:]))
    return '<p style="white-space:pre-wrap;margin:0">' + ''.join(fragments) + '</p>'


def normalized_button_rows(rows):
    """Bound cached/incoming display fields; discard all unsupported action data."""
    if not isinstance(rows, list) or len(rows) > 50:
        return None
    result, count = [], 0
    for row in rows:
        if not isinstance(row, list) or len(row) > 20:
            return None
        projected = []
        for button in row:
            count += 1
            if (count > 200 or not isinstance(button, dict)
                    or button.get('type') not in _BUTTON_TYPES
                    or not isinstance(button.get('text'), str) or len(button['text']) > 4096):
                return None
            item = {'type': button['type'], 'text': button['text']}
            if item['type'] == 'CLIPBOARD':
                if not isinstance(button.get('payload'), str) or len(button['payload']) > 16384:
                    return None
                item['payload'] = button['payload']
            elif item['type'] == 'LINK':
                if not isinstance(button.get('url'), str) or len(button['url']) > 16384:
                    return None
                item['url'] = button['url']
            elif item['type'] == 'OPEN_APP':
                if 'contactId' in button:
                    identity = button['contactId']
                    if type(identity) is not int or not -(2**63) <= identity < 2**63:
                        return None
                    item['contactId'] = identity
                if 'payload' in button:
                    if not isinstance(button['payload'], str) or len(button['payload']) > 16384:
                        return None
                    item['payload'] = button['payload']
            projected.append(item)
        result.append(projected)
    return result


def incoming_button_rows(attach):
    if (not isinstance(attach, dict) or attach.get('_type') != 'INLINE_KEYBOARD'
            or not isinstance(attach.get('callbackId'), str)
            or not isinstance(attach.get('keyboard'), dict)):
        return None
    return normalized_button_rows(attach['keyboard'].get('buttons'))


def visible_clipboard_text(message):
    """Literal copy-button text supplied to this session, not arbitrary code fields."""
    parts = []
    attaches = message.get('attaches') if isinstance(message, dict) else None
    if not isinstance(attaches, list):
        return ''
    for attach in attaches:
        rows = incoming_button_rows(attach)
        if rows is None:
            continue
        for row in rows:
            for button in row:
                if button['type'] != 'CLIPBOARD' or not button['payload']:
                    continue
                label, payload = button['text'], button['payload']
                parts.append(payload if label == payload or not label else label + '\n' + payload)
    return '\n\n'.join(parts)
