"""Pure contracts from the retained official WEB64/new and55 producers."""
from __future__ import annotations

import copy

from .chat_record_maps import normalize_chat_id_maps
from .folder_contracts import int64
from .search_contracts import _chat, _message

CONVERSATION_CREATE = 64
CONVERSATION_UPDATE = 55
DEFAULT_LIMITS = {'max-cname-length': 200, 'max-description-length': 400,
                  'max-added-participants': 100, 'max-participants': 20000}


def conversation_creation_limits(server=None):
    """Use shipped defaults unless the server supplied a typed positive limit."""
    result = dict(DEFAULT_LIMITS)
    if server is not None and not isinstance(server, dict):
        raise ValueError('Неверные ограничения MAX')
    for name in result:
        if server is not None and name in server:
            value = server[name]
            if type(value) is not int or not 1 <= value <= 100000:
                raise ValueError('MAX вернул неверные ограничения создания')
            result[name] = value
    return result


def _length(value):
    return len(value.encode('utf-16-le', errors='surrogatepass')) // 2


def conversation_create_values(kind, title, member_ids=(), description='', *, limits=None):
    limits = conversation_creation_limits(limits)
    if kind not in ('group', 'channel') or not isinstance(title, str) or not isinstance(description, str):
        raise ValueError('Укажите тип, название и описание')
    title, description = title.strip(), description.strip()
    if not 1 <= _length(title) <= limits['max-cname-length']:
        raise ValueError(f"Название должно содержать от 1 до {limits['max-cname-length']} символов")
    if _length(description) > limits['max-description-length']:
        raise ValueError(f"Описание: не более {limits['max-description-length']} символов")
    if (not isinstance(member_ids, (tuple, list))
            or any(not int64(identity) or identity <= 0 for identity in member_ids)
            or len(member_ids) != len(set(member_ids))):
        raise ValueError('Выберите уникальные контакты MAX')
    maximum = min(limits['max-added-participants'], limits['max-participants'] - 1)
    if len(member_ids) > maximum:
        raise ValueError(f'Можно выбрать не более {maximum} участников')
    if kind == 'channel' and member_ids:
        raise ValueError('Создание канала не добавляет подписчиков')
    if kind == 'group' and description:
        raise ValueError('Описание доступно при создании канала')
    return {'kind': kind, 'title': title, 'member_ids': list(member_ids), 'description': description}


def conversation_create_payload(values, cid):
    if not int64(cid) or cid <= 0:
        raise ValueError('Нужен идентификатор операции')
    return {'message': {'cid': cid, 'attaches': [{'_type': 'CONTROL', 'event': 'new',
        'chatType': 'CHAT' if values['kind'] == 'group' else 'CHANNEL',
        'title': values['title'], 'userIds': list(values['member_ids'])}]}, 'notify': True}


def _created_chat(raw, values, owner_id, *, chat_id=None):
    # The binary decoder keeps int64 participant keys; the WEB schema used by
    # _chat expects decimal strings. Conflicting or malformed keys still fail.
    try:
        raw = normalize_chat_id_maps(raw)
    except ValueError:
        raise ValueError('MAX не подтвердил созданный чат') from None
    if (_chat(raw) is None or raw['id'] == 0 or raw['owner'] != owner_id
            or raw['type'] != ('CHAT' if values['kind'] == 'group' else 'CHANNEL')
            or raw['status'] != 'ACTIVE' or raw.get('title') != values['title']
            or (chat_id is not None and raw['id'] != chat_id)):
        raise ValueError('MAX не подтвердил созданный чат')
    return copy.deepcopy(raw)


def conversation_create_reply(data, values, owner_id, cid):
    # Sv requires chatId/message; fR additionally requires the supplied ChatDTO.
    if (not isinstance(data, dict) or not int64(data.get('chatId'))
            or _message(data.get('message')) is None
            or ('cid' in data['message'] and data['message']['cid'] != cid)):
        raise ValueError('MAX не подтвердил создание')
    return _created_chat(data.get('chat'), values, owner_id, chat_id=data['chatId'])


def conversation_create_readback(data, values, owner_id, chat_id, *, description=False):
    if (not isinstance(data, dict) or not isinstance(data.get('chats'), list)
            or len(data['chats']) != 1):
        raise ValueError('MAX не вернул созданный чат')
    raw = _created_chat(data['chats'][0], values, owner_id, chat_id=chat_id)
    if description and raw.get('description', '') != values['description']:
        raise ValueError('Описание канала ещё не подтверждено')
    return raw


def conversation_description_reply(data, values, owner_id, chat_id):
    if not isinstance(data, dict):
        raise ValueError('MAX не подтвердил описание')
    raw = _created_chat(data.get('chat'), values, owner_id, chat_id=chat_id)
    if raw.get('description', '') != values['description']:
        raise ValueError('MAX не подтвердил описание')
    return raw
