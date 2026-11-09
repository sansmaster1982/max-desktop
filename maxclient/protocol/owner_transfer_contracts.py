"""Pinned WEB 55 ownership and 59 member contracts; no inferred commands."""
from __future__ import annotations

import copy

from .search_contracts import _integer, _number, _contact
from .conversation_actions_contracts import parse_action_chat_readback


def build_owner_transfer(chat_id, successor_id):
    if not _integer(chat_id, 64) or not _integer(successor_id, 64) or successor_id <= 0:
        raise ValueError('Не подтверждён чат или новый владелец')
    return {'chatId': chat_id, 'changeOwnerId': successor_id}


def parse_owner_transfer_ack(data, chat_id, successor_id):
    # Schema chat? is weaker than the shipped producer: actual chat is required.
    if not isinstance(data, dict) or set(data) != {'chat'}:
        raise ValueError('MAX не подтвердил передачу прав')
    raw = parse_action_chat_readback({'chats': [data['chat']]}, chat_id)
    if raw['type'] not in ('CHAT', 'CHANNEL') or raw['owner'] != successor_id:
        raise ValueError('MAX вернул другого владельца')
    return raw


def build_transfer_members(chat_id, member_type, *, query='', marker=None):
    if (not _integer(chat_id, 64) or member_type not in ('ADMIN', 'MEMBER')
            or not isinstance(query, str) or len(query) > 1024
            or marker is not None and (not _number(marker) or marker < 0)):
        raise ValueError('Некорректный запрос участников')
    if query and marker is not None or member_type == 'ADMIN' and marker is not None:
        raise ValueError('Эта страница поиска не поддерживается')
    result = {'chatId': chat_id, 'type': member_type}
    if query:
        result['query'] = query
    else:
        result['count'] = 50
        if marker is not None:
            result['marker'] = marker
    return result


def parse_transfer_members(data):
    if not isinstance(data, dict) or set(data) - {'members', 'marker'}:
        raise ValueError('MAX вернул неоднозначный список участников')
    rows = data.get('members', [])
    if not isinstance(rows, list) or len(rows) > 1000:
        raise ValueError('MAX вернул некорректный список участников')
    if 'marker' in data and (not _number(data['marker']) or data['marker'] < 0):
        raise ValueError('MAX вернул некорректный курсор участников')
    contacts, identities = [], set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('MAX вернул некорректного участника')
        if 'contact' not in row:
            continue  # Shipped producer also skips rows without a contact.
        contact = _contact(row['contact'])
        if contact is None or contact['id'] <= 0 or contact['id'] in identities:
            raise ValueError('MAX вернул неоднозначного участника')
        identities.add(contact['id'])
        contacts.append(copy.deepcopy(contact))
    return {'contacts': contacts, 'marker': data.get('marker'), 'complete': 'marker' not in data}
