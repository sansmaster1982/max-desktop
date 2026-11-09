"""Pinned WEB55 text edit: theme/description only, JS trim and UTF16 limits."""
from __future__ import annotations

from .search_contracts import _integer
from .conversation_actions_contracts import parse_action_chat_readback

_JS_WHITESPACE = '\u0009\u000a\u000b\u000c\u000d\u0020\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff'


def normalize_conversation_edit_text(value):
    if not isinstance(value, str):
        raise ValueError('Название и описание должны быть текстом')
    return value.strip(_JS_WHITESPACE)


def utf16_length(value):
    if not isinstance(value, str):
        raise ValueError('Ожидался текст')
    return len(value.encode('utf-16-le', errors='surrogatepass')) // 2


def conversation_edit_limits(server, experiments=None):
    if not isinstance(server, dict) or experiments is not None and not isinstance(experiments, dict):
        raise ValueError('Не подтверждены ограничения MAX')
    values = []
    for key, default in (('max-cname-length', 200), ('max-description-length', 400)):
        value = (experiments or {}).get(key)
        if value is None:
            value = server.get(key)
        if value is None:
            value = default
        if type(value) is not int or not 1 <= value <= 100000:
            raise ValueError('MAX вернул некорректные ограничения редактирования')
        values.append(value)
    return values[0], min(values[1], 400)


def conversation_edit_values(title, description, limits):
    title = normalize_conversation_edit_text(title)
    description = normalize_conversation_edit_text(description)
    if not 1 <= utf16_length(title) <= limits[0]:
        raise ValueError(f'Название должно содержать от 1 до {limits[0]} символов')
    if utf16_length(description) > limits[1]:
        raise ValueError(f'Описание: не более {limits[1]} символов')
    return title, description


def build_conversation_edit(chat_id, patch):
    if (not _integer(chat_id, 64) or not isinstance(patch, dict) or not patch
            or set(patch) - {'theme', 'description'} or any(not isinstance(value, str) for value in patch.values())
            or 'theme' in patch and not normalize_conversation_edit_text(patch['theme'])):
        raise ValueError('Не подтверждено изменение сведений чата')
    return {'chatId': chat_id, **patch}


def edit_matching_fields(raw, patch):
    return [field for field, value in patch.items()
            if raw.get('title' if field == 'theme' else 'description', '') == value]


def parse_conversation_edit_ack(data, chat_id, chat_type, patch):
    if not isinstance(data, dict) or set(data) != {'chat'}:
        raise ValueError('MAX не подтвердил изменение сведений')
    raw = parse_action_chat_readback({'chats': [data['chat']]}, chat_id)
    if (chat_type not in ('CHAT', 'CHANNEL') or raw['type'] != chat_type
            or set(edit_matching_fields(raw, patch)) != set(patch)):
        raise ValueError('MAX вернул другие сведения чата')
    return raw
