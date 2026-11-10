"""Selected current WEB folder DTOs; no transport or account state."""
from __future__ import annotations

import copy

ALL_FOLDER = 'all.chat.folder'
# WEB 26.11.3 (build 21379) added FILTER_ARCHIVE=15, OPTION_NO_EXCLUDE=5 and
# OPTION_HIDDEN_FOLDER=6. They are accepted so such folders still load and can
# be edited; folder_view reports them as unsupported instead of evaluating them.
FOLDER_FILTERS = tuple(range(16))
FOLDER_OPTIONS = tuple(range(7))
UNEVALUATED_FILTERS = frozenset({15})
UNEVALUATED_OPTIONS = frozenset({5, 6})


def int64(value):
    return type(value) is int and -(2 ** 63) <= value < 2 ** 63


def folder_ids(value, *, allow_empty=False):
    if (not isinstance(value, list) or len(value) > 1000 or (not value and not allow_empty)
            or any(not isinstance(item, str) or not item or len(item) > 256 for item in value)
            or len(value) != len(set(value))):
        raise ValueError('Нужен список уникальных идентификаторов папок')
    return list(value)


def folder_title(value):
    if not isinstance(value, str):
        raise ValueError('Введите название папки')
    value = value.strip()
    if not 1 <= len(value.encode('utf-16-le', errors='surrogatepass')) // 2 <= 20:
        raise ValueError('Название папки должно содержать от 1 до 20 символов')
    return value


def integer_list(value, *, enum=None):
    if (not isinstance(value, list) or len(value) > 1000
            or any(not int64(item) or (enum is not None and item not in enum) for item in value)
            or len(value) != len(set(value))):
        raise ValueError('MAX требует список уникальных целочисленных значений')
    return list(value)


def folder_payload(data):
    if not isinstance(data, dict) or set(data) - {'id', 'title', 'include', 'favorites', 'filters', 'options'}:
        raise ValueError('Неподтверждённые поля изменения папки')
    result = {'id': folder_ids([data.get('id')])[0], 'title': folder_title(data.get('title'))}
    for key, enum in (('include', None), ('favorites', None), ('filters', FOLDER_FILTERS), ('options', FOLDER_OPTIONS)):
        if key in data:
            result[key] = integer_list(data[key], enum=enum)
    return result


def folder_dto(data):
    if not isinstance(data, dict) or not isinstance(data.get('id'), str) or not isinstance(data.get('title'), str):
        raise ValueError('MAX не вернул папку')
    folder_ids([data['id']])
    for key, enum in (('include', None), ('favorites', None), ('filters', FOLDER_FILTERS), ('options', FOLDER_OPTIONS)):
        if key in data:
            integer_list(data[key], enum=enum)
    if 'updateTime' in data and not int64(data['updateTime']):
        raise ValueError('MAX вернул неверное время папки')
    return copy.deepcopy(data)


def contact_dto(data):
    # Vh: these fields are required; status is optional and distinct from accountStatus.
    if (not isinstance(data, dict) or not int64(data.get('id')) or not int64(data.get('updateTime'))
            or not isinstance(data.get('names'), list)):
        raise ValueError('MAX не вернул подтверждённый контакт')
    for name in data['names']:
        if (not isinstance(name, dict) or any(key in name and not isinstance(name[key], str)
                for key in ('name', 'firstName', 'lastName'))
                or ('type' in name and name['type'] not in ('ONEME', 'CUSTOM'))):
            raise ValueError('MAX вернул неверное имя контакта')
    if 'status' in data and data['status'] not in ('REMOVED', 'BLOCKED', 'HIDDEN'):
        raise ValueError('MAX вернул неизвестный статус контакта')
    return copy.deepcopy(data)
