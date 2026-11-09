"""Pure projections of current public WEB68/60 search contracts.

No transport, account state, HTML parsing or dialog-ID derivation lives here.
Source: DPMqYNCb.js SHA256 5c95e802f2e95ba9ed83e1435b4553e56616266a089e0b7cf5a17b66479ea2ed.
The service/UI owns observed trim/debounce, context guards and pagination state.
Count/collection limits below are local resource policy, not claimed server limits.
"""
from __future__ import annotations

import math

MESSAGE_SEARCH_OPCODE = 68
GLOBAL_SEARCH_OPCODE = 60
SEARCH_SECTIONS = frozenset({'CHATS', 'MESSAGES', 'PUBLIC_CHATS', 'BOTS'})
GLOBAL_SEARCH_TYPES = frozenset({'ALL', 'CHANNELS', 'PUBLIC_CHATS'})
MAX_RESULTS = 1000


class SearchContractError(ValueError):
    pass


def _integer(value, bits):
    return type(value) is int and -(2**(bits - 1)) <= value < 2**(bits - 1)


def _number(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except (ValueError, OverflowError):
        return False


def _request(query, count):
    if not isinstance(query, str) or not query:
        raise SearchContractError('Введите поисковый запрос.')
    if type(count) is not int or not 1 <= count <= MAX_RESULTS:
        raise SearchContractError('Некорректный размер страницы поиска.')
    return {'query': query, 'count': count}


def build_message_search(query, *, count=30, marker=None):
    """68 marker is an opaque string; empty query must never cause an RPC."""
    payload = _request(query, count)
    if marker is not None:
        if not isinstance(marker, str):
            raise SearchContractError('Некорректный курсор поиска сообщений.')
        payload['marker'] = marker
    return payload


def build_global_search(query, *, count=40, marker=None, search_type='ALL'):
    """60 marker is int64. The inspected UI uses only the first ALL page."""
    payload = _request(query, count)
    if not isinstance(search_type, str) or search_type not in GLOBAL_SEARCH_TYPES:
        raise SearchContractError('Некорректный тип глобального поиска.')
    payload['type'] = search_type
    if marker is not None:
        if not _integer(marker, 64):
            raise SearchContractError('Некорректный курсор глобального поиска.')
        payload['marker'] = marker
    return payload


def _project(data, required, optional):
    if not isinstance(data, dict):
        return None
    output = {}
    for name, valid in required.items():
        if name not in data or not valid(data[name]):
            return None
        output[name] = data[name]
    for name, valid in optional.items():
        if name in data:
            if not valid(data[name]):
                return None
            output[name] = data[name]
    return output


_str = lambda value: isinstance(value, str)
_i64 = lambda value: _integer(value, 64)
_i32 = lambda value: _integer(value, 32)


def _enum(*values):
    return lambda value: isinstance(value, str) and value in values


def _message(data):
    # zh requires id,time,type. Sender is optional in this exact source.
    return _project(data, {'id': _i64, 'time': _number, 'type': _enum('USER', 'CHANNEL', 'CHANNEL_ADMIN')},
                    {'sender': _i64, 'text': _str, 'updateTime': _number, 'cid': _i64,
                     'status': _enum('EDITED', 'REMOVED', 'SPAM', 'DELAYED_FIRE_ERROR')})


def _participants(value):
    return (isinstance(value, dict) and len(value) <= 100000
            and all(isinstance(key, str) and _number(item) for key, item in value.items()))


def _chat(data):
    result = _project(data, {'id': _i64, 'type': _enum('DIALOG', 'CHAT', 'CHANNEL'),
                            'status': _enum('ACTIVE', 'LEFT', 'REMOVED', 'CLOSED', 'HIDDEN', 'BLOCKED'),
                            'owner': _i64, 'participants': _participants,
                            'lastEventTime': _number, 'created': _number},
                      {'title': _str, 'baseIconUrl': _str, 'baseRawIconUrl': _str,
                       'newMessages': _i32, 'participantsCount': _i32, 'description': _str,
                       'link': _str, 'access': _enum('PUBLIC', 'PRIVATE')})
    if result is None:
        return None
    result['participants'] = dict(result['participants'])
    if 'lastMessage' in data:
        message = _message(data['lastMessage'])
        if message is None:
            return None
        result['lastMessage'] = message
    return result


def _contact(data):
    result = _project(data, {'id': _i64, 'updateTime': _number,
                            'names': lambda value: isinstance(value, list) and len(value) <= 256},
                      {'baseUrl': _str, 'baseRawUrl': _str, 'description': _str, 'link': _str,
                       'flags': _i32, 'status': _enum('REMOVED', 'BLOCKED', 'HIDDEN'),
                       'accountStatus': lambda value: type(value) is int and value in (0, 1, 2)})
    if result is None:
        return None
    names = []
    for raw in result['names']:
        name = _project(raw, {}, {'name': _str, 'firstName': _str, 'lastName': _str,
                                 'type': _enum('ONEME', 'CUSTOM')})
        if name is None:
            return None
        names.append(name)
    result['names'] = names
    return result


def _directory_contact(data):
    # dg is a wrapper, not a naked Vh or a derived chat identity.
    result = _project(data, {'summary': _str}, {'feedback': _str, 'friendsCount': _i32,
                     'restricted': lambda value: type(value) is bool})
    if result is None or (contact := _contact(data.get('contact'))) is None:
        return None
    result['contact'] = contact
    if 'presence' in data:
        presence = _project(data['presence'], {'seen': _i32},
                            {'on': _enum('ON', 'WAS_RECENTLY', 'WAS_LONG_AGO'),
                             'status': lambda value: type(value) is int and value in (0, 1, 2, 3)})
        if presence is None:
            return None
        result['presence'] = presence
    return result


def _row(raw, *, messages):
    if not isinstance(raw, dict):
        return {'unavailable': ('result',)}
    result, unavailable = {}, []
    fields = {'highlights': lambda value: isinstance(value, list) and len(value) <= 256 and all(_str(v) for v in value)}
    if messages:
        fields['chatId'] = _i64
    for field, valid in fields.items():
        if field not in raw or not valid(raw[field]):
            unavailable.append(field)
        else:
            result[field] = list(raw[field]) if field == 'highlights' else raw[field]
    if messages and 'section' in raw:
        if _str(raw['section']) and raw['section'] in SEARCH_SECTIONS:
            result['section'] = raw['section']
        else:
            unavailable.append('section')
    if messages and 'count' in raw:
        if _i64(raw['count']):
            result['count'] = raw['count']
        else:
            unavailable.append('count')
    nested = {'chat': _chat, 'message': _message}
    if not messages:
        nested['contact'] = _directory_contact
    for field, project in nested.items():
        if field in raw:
            value = project(raw[field])
            if value is None:
                unavailable.append(field)
            else:
                result[field] = value
    if result.get('section') == 'MESSAGES' and 'message' not in result and 'message' not in unavailable:
        unavailable.append('message')
    if messages and 'chat' in result and 'chatId' in result and result['chat']['id'] != result['chatId']:
        result.pop('chat')
        unavailable.append('chat')
    if not messages and not any(name in result for name in ('chat', 'contact')):
        unavailable.append('identity')  # Message alone has no source chatId in Rg.
    if unavailable:
        result['unavailable'] = tuple(dict.fromkeys(unavailable))
    return result


def _parse(data, *, messages):
    if not isinstance(data, dict) or not isinstance(data.get('result'), list) or len(data['result']) > MAX_RESULTS:
        raise SearchContractError('MAX вернул некорректную страницу поиска.')
    total = data.get('total', 0) if messages else data.get('total')
    if not _i32(total):
        raise SearchContractError('MAX не подтвердил количество результатов поиска.')
    result = {'result': [_row(row, messages=messages) for row in data['result']], 'total': total}
    if 'marker' in data:
        if not (_str(data['marker']) if messages else _i64(data['marker'])):
            raise SearchContractError('MAX вернул некорректный курсор поиска.')
        result['marker'] = data['marker']
    if 'ucpQId' in data:
        if not _str(data['ucpQId']):
            raise SearchContractError('MAX вернул некорректные метаданные поиска.')
        result['ucpQId'] = data['ucpQId']
    if messages and 'counters' in data:
        raw = data['counters']
        if not isinstance(raw, dict):
            raise SearchContractError('MAX вернул некорректные счётчики поиска.')
        counters = {}
        for key in ('CHATS', 'MESSAGES', 'PUBLIC_CHATS'):
            if key in raw:
                if not _i32(raw[key]):
                    raise SearchContractError('MAX вернул некорректные счётчики поиска.')
                counters[key] = raw[key]
        result['counters'] = counters
    return result


def parse_message_search(data):
    """Validate68 ACK and project display/identity fields, not arbitrary rich media."""
    return _parse(data, messages=True)


def parse_global_search(data):
    """Validate60 ACK; preserve contacts as contacts without fabricating chat IDs."""
    return _parse(data, messages=False)
