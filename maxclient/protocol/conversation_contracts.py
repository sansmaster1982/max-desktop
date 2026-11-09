"""Pure current-WEB198/51 contracts for detached conversation-info reads.

No transport, persistence, media download, URL opening or account actions.
The explicitly imported projections share the pinned ig/zh schema validation
with search_contracts; unsupported raw fields are never copied recursively.
"""
from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

from .search_contracts import _chat, _message, _integer, _number


COMMON_CHATS_OPCODE = 198
INFO_MEDIA_OPCODE = 51
INFO_MEDIA_TABS = frozenset({'PHOTO_VIDEO', 'AUDIO', 'FILE', 'SHARE'})
_TYPES = {'PHOTO_VIDEO': ('PHOTO', 'VIDEO'), 'AUDIO': ('AUDIO',),
          'FILE': ('FILE',), 'SHARE': ('SHARE',)}
_MAX_ITEMS = 1000  # Local resource policy, not a claimed server maximum.


class ConversationContractError(ValueError):
    pass


def explicit_media_url(value):
    """Keep a supplied HTTPS URL only; never build one from IDs or tokens.

    This is a syntax/origin policy, not a DNS lookup. A downloading consumer
    must independently bind the session and reject private resolved addresses.
    """
    if (not isinstance(value, str) or not value or len(value) > 8192
            or any(ord(char) <= 32 or ord(char) == 127 for char in value)
            or '\\' in value):
        return None
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        if (parsed.scheme != 'https' or not host or parsed.username is not None
                or parsed.password is not None or parsed.fragment or parsed.port not in (None, 443)):
            return None
        host = host.encode('idna').decode('ascii').lower()
        if ('.' not in host or host.endswith(('.localhost', '.local', '.internal', '.lan', '.home', '.'))
                or any(not part or len(part) > 63 or part.startswith('-') or part.endswith('-')
                       or any(not (char.isascii() and (char.isalnum() or char == '-')) for char in part)
                       for part in host.split('.'))):
            return None
        try:
            ipaddress.ip_address(host)
        except ValueError:
            return value
        return None  # Local policy: no literal IP media origins.
    except (ValueError, UnicodeError):
        return None


def _id(value):
    if not _integer(value, 64):
        raise ConversationContractError('Expected a confirmed int64 identity')
    return value


def _timestamp(value):
    if not _number(value):
        raise ConversationContractError('Expected an actual finite timestamp')
    return value


def build_common_chats(peer_id, marker=None, count=None):
    """198 uses the actual peer's userId and an optional timestamp cursor."""
    result = {'userIds': [_id(peer_id)]}
    if marker is not None:
        result['marker'] = _timestamp(marker)
    if count is not None:
        if not _integer(count, 32) or not 1 <= count <= _MAX_ITEMS:
            raise ConversationContractError('Invalid common-chat page size')
        result['count'] = count
    return result


def parse_common_chats(data):
    if (not isinstance(data, dict) or not isinstance(data.get('commonChats'), list)
            or len(data['commonChats']) > _MAX_ITEMS or type(data.get('hasMore')) is not bool):
        raise ConversationContractError('MAX did not return a structured common-chat page')
    chats = []
    for raw in data['commonChats']:
        chat = _chat(raw)
        if chat is None:
            raise ConversationContractError('MAX did not confirm common-chat metadata')
        chats.append(chat)
    result = {'commonChats': chats, 'hasMore': data['hasMore']}
    if 'marker' in data:
        result['marker'] = _timestamp(data['marker'])
    return result


def build_info_media(chat_id, tab, marker=None, *, initial=False):
    """51 messageId is an actual message ID, not a timestamp.

    First/no-anchor windows use25/25; a continuation at an actual backward
    marker uses25 backward. The caller must not fabricate a marker or infer
    server ordering from this bounded paging policy.
    """
    if not isinstance(tab, str) or tab not in INFO_MEDIA_TABS or type(initial) is not bool:
        raise ConversationContractError('Unsupported conversation-media tab')
    result = {'chatId': _id(chat_id), 'attachTypes': list(_TYPES[tab]),
              'backward': 25, 'forward': 25 if initial or marker is None else 0}
    if marker is not None:
        result['messageId'] = _id(marker)
    return result


def _metadata_attachment(raw):
    if not isinstance(raw, dict) or not isinstance(raw.get('_type'), str):
        raise ConversationContractError('MAX did not return typed attachment metadata')
    kind = raw['_type']
    result = {'_type': kind}
    # Unknown kinds remain identified only by the actual type. Opaque tokens,
    # ID-derived URLs and unrelated nested payloads never enter Info rows.
    fields = {
        'PHOTO': {'deleted': lambda value: type(value) is bool,
                  'width': lambda value: _integer(value, 32), 'height': lambda value: _integer(value, 32),
                  'gif': lambda value: type(value) is bool},
        'VIDEO': {'duration': _number, 'size': lambda value: _integer(value, 64),
                  'width': lambda value: _integer(value, 32), 'height': lambda value: _integer(value, 32),
                  'deleted': lambda value: type(value) is bool},
        'AUDIO': {'duration': lambda value: _integer(value, 32),
                  'deleted': lambda value: type(value) is bool},
        'FILE': {'name': lambda value: isinstance(value, str), 'size': _number, 'fileId': _number,
                 'deleted': lambda value: type(value) is bool},
        'SHARE': {'title': lambda value: isinstance(value, str),
                  'description': lambda value: isinstance(value, str)},
    }
    if kind == 'AUDIO' and (not _number(raw.get('audioId')) or not _integer(raw.get('duration'), 32)):
        raise ConversationContractError('MAX did not confirm audio metadata')
    if kind == 'SHARE' and not _number(raw.get('shareId')):
        raise ConversationContractError('MAX did not confirm shared-link metadata')
    for name, valid in fields.get(kind, {}).items():
        if name in raw:
            if not valid(raw[name]):
                raise ConversationContractError('MAX returned invalid attachment metadata')
            result[name] = raw[name]
    url_field = {'PHOTO': 'url', 'VIDEO': 'thumbnail'}.get(kind)
    if url_field is not None and url_field in raw:
        if not isinstance(raw[url_field], str):
            raise ConversationContractError('MAX returned invalid media URL metadata')
        if result.get('deleted') is not True and (url := explicit_media_url(raw[url_field])) is not None:
            result[url_field] = url
    preview_field = {'FILE': 'preview', 'SHARE': 'image'}.get(kind)
    if preview_field is not None and preview_field in raw:
        preview = raw[preview_field]
        if not isinstance(preview, dict):
            raise ConversationContractError('MAX returned invalid preview metadata')
        if preview.get('_type') in ('PHOTO', 'VIDEO') and (kind != 'SHARE' or preview['_type'] == 'PHOTO'):
            result[preview_field] = _metadata_attachment(preview)
    return result


def parse_info_media(data):
    """Validate51 and preserve actual message order/IDs/times as safe metadata."""
    if (not isinstance(data, dict) or not isinstance(data.get('messages'), list)
            or len(data['messages']) > _MAX_ITEMS or not _integer(data.get('total'), 32)):
        raise ConversationContractError('MAX did not return a structured media page')
    messages = []
    for raw in data['messages']:
        message = _message(raw)
        if message is None:
            raise ConversationContractError('MAX did not confirm media message identity')
        if 'delayedAttributes' in raw:
            attributes = raw['delayedAttributes']
            if not isinstance(attributes, dict) or not _number(attributes.get('timeToFire')):
                raise ConversationContractError('MAX returned an invalid actual message schedule')
            message['delayedAttributes'] = ({'timeToFire': attributes['timeToFire']}
                                             if 'timeToFire' in attributes else {})
        if 'attaches' in raw:
            attaches = raw['attaches']
            if not isinstance(attaches, list) or len(attaches) > _MAX_ITEMS:
                raise ConversationContractError('MAX returned invalid attachment metadata')
            message['attaches'] = [_metadata_attachment(item) for item in attaches]
        link = raw.get('link')
        if isinstance(link, dict) and link.get('type') == 'FORWARD':
            nested = _message(link.get('message'))
            if nested is None or not _integer(link.get('chatId'), 64):
                raise ConversationContractError('MAX did not confirm forwarded media metadata')
            if 'attaches' in link['message']:
                attaches = link['message']['attaches']
                if not isinstance(attaches, list) or len(attaches) > _MAX_ITEMS:
                    raise ConversationContractError('MAX returned invalid forwarded attachments')
                nested['attaches'] = [_metadata_attachment(item) for item in attaches]
            # Exactly the verified first-level NA message attachment source.
            # Deeper links are not recursively interpreted or opened.
            message['link'] = {'type': 'FORWARD', 'chatId': link['chatId'], 'message': nested}
        messages.append(message)
    result = {'messages': messages, 'total': data['total']}
    if 'pos' in data:
        if not _integer(data['pos'], 32):
            raise ConversationContractError('MAX returned an invalid media position')
        result['pos'] = data['pos']
    for field in ('forward', 'backward'):
        if field in data:
            if not _number(data[field]):
                raise ConversationContractError('MAX returned an invalid media boundary')
            result[field] = data[field]
    return result
