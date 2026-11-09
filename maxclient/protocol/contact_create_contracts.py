"""Bounded WEB41 AddContact contract and authoritative CONTACT_INFO32 checks.

Source: research/SELF_CONTACT_SOURCE_EVIDENCE.md and MENU_NESTED_SOURCE_20261101.md.
The name limit is the captured form's UTF-16 maxlength, not a server-wide limit.
"""
from __future__ import annotations

import math
import re

from .folder_contracts import contact_dto, int64


CONTACT_CREATE = 41
CONTACT_ADDED_FLAG = 512


def contact_phone(value: str) -> str:
    # Match the existing Find form's supported input, without importing Qt.
    if (not isinstance(value, str) or len(value) > 64
            or not re.fullmatch(r'\+?[0-9 ()\-.]+', value.strip(), flags=re.ASCII)):
        raise ValueError('Введите номер телефона с кодом страны')
    from ..core.service import normalize_phone
    phone = normalize_phone(value)
    digits = phone[1:]
    if not 7 <= len(digits) <= 15 or digits[0] == '0':
        raise ValueError('Введите номер телефона с кодом страны')
    return phone


def contact_name(value: str, *, required: bool) -> str:
    if not isinstance(value, str):
        raise ValueError('Введите имя контакта')
    value = value.strip()
    if ((required and not value) or len(value.encode('utf-16-le', errors='surrogatepass')) // 2 > 60
            or any(ord(character) < 32 or 0xD800 <= ord(character) <= 0xDFFF for character in value)):
        raise ValueError('Имя и фамилия должны содержать не более 60 символов')
    return value


def contact_create_payload(phone: str, first_name: str, last_name: str = '') -> dict:
    result = {'phone': contact_phone(phone), 'firstName': contact_name(first_name, required=True)}
    last = contact_name(last_name, required=False)
    if last:
        result['lastName'] = last
    return result


def created_contact(data) -> dict:
    record = contact_dto(data)
    if 'flags' in record and (type(record['flags']) is not int or not -(2 ** 31) <= record['flags'] < 2 ** 31):
        raise ValueError('MAX вернул неверные признаки контакта')
    if 'phone' in record:
        phone = record['phone']
        if (type(phone) not in (int, float) or not 0 < phone < 10 ** 15
                or not math.isfinite(phone) or phone != int(phone)):
            raise ValueError('MAX вернул неверный телефон контакта')
    return record


def contact_create_reply(data) -> tuple[dict, bool]:
    if not isinstance(data, dict) or type(data.get('new')) is not bool:
        raise ValueError('MAX не подтвердил сохранение контакта')
    return created_contact(data.get('contact')), data['new']


def contact_create_name_matches(contact: dict, payload: dict) -> bool:
    """Whether actual CUSTOM fields confirm the requested name, not just ADD."""
    names = contact.get('names')
    custom = [name for name in names if isinstance(name, dict) and name.get('type') == 'CUSTOM'] if isinstance(names, list) else []
    return (len(custom) == 1 and custom[0].get('firstName') == payload['firstName']
            and custom[0].get('lastName', '') == payload.get('lastName', ''))


def contact_create_readback(data, identity: int, payload: dict, *, require_name_match=True) -> dict:
    if type(require_name_match) is not bool:
        raise ValueError('Не подтверждён режим проверки имени')
    if not int64(identity) or not isinstance(data, dict) or not isinstance(data.get('contacts'), list):
        raise ValueError('MAX не вернул сохранённый контакт')
    matches = [row for row in data['contacts'] if isinstance(row, dict)
               and type(row.get('id')) is int and row['id'] == identity]
    if len(matches) != 1:
        raise ValueError('MAX не вернул однозначное подтверждение контакта')
    contact = created_contact(matches[0])
    if not contact.get('flags', 0) & CONTACT_ADDED_FLAG or contact.get('status') in ('REMOVED', 'HIDDEN'):
        raise ValueError('MAX пока не подтвердил добавление в контакты')
    if 'phone' in contact and '+' + str(int(contact['phone'])) != payload['phone']:
        raise ValueError('MAX вернул контакт с другим номером')
    if require_name_match and not contact_create_name_matches(contact, payload):
        raise ValueError('MAX пока не подтвердил сохранённое имя контакта')
    return contact
