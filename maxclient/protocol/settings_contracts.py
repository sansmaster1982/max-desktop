"""Selected current public user-config fields; no effective defaults."""
import math

PRIVACY_ENUMS = {
    "SEARCH_BY_PHONE": frozenset({"ALL", "CONTACTS"}),
    "INCOMING_CALL": frozenset({"ALL", "CONTACTS", "NOBODY"}),
    "CHATS_INVITE": frozenset({"ALL", "CONTACTS", "NOBODY"}),
    "PHONE_NUMBER_PRIVACY": frozenset({"ALL", "CONTACTS", "NOBODY"}),
    "FAMILY_PROTECTION": frozenset({"OFF", "ADMIN", "MANAGEABLE"}),
}
PRIVACY_BOOLEANS = frozenset({"HIDDEN", "CONTENT_LEVEL_ACCESS", "SAFE_MODE"})
READONLY_PRIVACY = frozenset({"FAMILY_PROTECTION", "SAFE_MODE"})
EDITABLE_PRIVACY = (frozenset(PRIVACY_ENUMS) | PRIVACY_BOOLEANS) - READONLY_PRIVACY

NOTIFICATION_FIELDS = frozenset({
    'DONT_DISTURB_UNTIL', 'PUSH_DETAILS', 'PUSH_SOUND',
    'CHATS_PUSH_NOTIFICATION', 'CHATS_PUSH_SOUND',
})


def chat_notification_values(data):
    """Only the established CONFIG.chats mute timestamp; preserve partial maps."""
    if not isinstance(data, dict):
        raise ValueError('Неверные настройки чатов')
    output = {}
    identities = set()
    for identity, fields in data.items():
        if type(identity) is int:
            number = identity
        elif isinstance(identity, str):
            digits = identity[1:] if identity.startswith('-') else identity
            if not digits.isascii() or not digits.isdigit():
                raise ValueError('Неверные настройки чата')
            try:
                number = int(identity)
            except ValueError:
                raise ValueError('Неверные настройки чата') from None
        else:
            raise ValueError('Неверные настройки чата')
        if not -(2**63) <= number < 2**63 or not isinstance(fields, dict):
            raise ValueError('Неверные настройки чата')
        canonical = str(number)
        if canonical in identities:
            raise ValueError('Повторяющийся идентификатор настроек чата')
        identities.add(canonical)
        if 'dontDisturbUntil' in fields:
            value = fields['dontDisturbUntil']
            try:
                valid = type(value) in (int, float) and math.isfinite(value) and -1 <= value
            except (ValueError, OverflowError):
                valid = False
            if not valid:
                raise ValueError('Неверное время отключения уведомлений чата')
            output[canonical] = {'dontDisturbUntil': value}
    return output


def notification_values(data, *, writable=False):
    """RPC22 fields observed in the current WEB producer; no guessed writes."""
    if not isinstance(data, dict) or writable and not data:
        raise ValueError('Нужен непустой набор настроек уведомлений')
    if writable and set(data) - NOTIFICATION_FIELDS:
        raise ValueError('Неизвестная настройка уведомлений')
    result = {}
    for key, value in data.items():
        if key not in NOTIFICATION_FIELDS:
            continue
        valid = False
        if key == 'DONT_DISTURB_UNTIL':
            valid = type(value) is int and -1 <= value <= 2**63 - 1
            if writable:
                valid = valid and value in (-1, 0)
        elif key == 'PUSH_DETAILS':
            valid = type(value) is bool
        elif key == 'CHATS_PUSH_NOTIFICATION':
            valid = isinstance(value, str) and value in ('ON', 'OFF', 'REPLY')
            if writable:
                valid = valid and value in ('ON', 'OFF')
        else:
            valid = isinstance(value, str)
            if writable:
                valid = valid and value in ('DEFAULT', '_NONE_')
        if not valid:
            raise ValueError('MAX вернул неподтверждённую настройку уведомлений')
        result[key] = value
    return result


def user_setting_values(data, *, writable=False):
    """Union validator; privacy callers still use their narrower validator."""
    if not isinstance(data, dict) or writable and not data:
        raise ValueError('Нужен непустой набор настроек MAX')
    if writable and set(data) - EDITABLE_PRIVACY - NOTIFICATION_FIELDS:
        raise ValueError('Изменение этой настройки MAX не поддерживается')
    privacy = {key: value for key, value in data.items()
               if key in PRIVACY_ENUMS or key in PRIVACY_BOOLEANS}
    notifications = {key: value for key, value in data.items() if key in NOTIFICATION_FIELDS}
    result = privacy_values(privacy, writable=writable) if privacy else {}
    if notifications:
        result.update(notification_values(notifications, writable=writable))
    return result


def privacy_values(data, *, writable: bool = False) -> dict:
    """Validate supplied fields, rejecting unknown/read-only writes."""
    if not isinstance(data, dict) or writable and not data:
        raise ValueError("Нужен непустой набор настроек MAX")
    if writable and any(key not in EDITABLE_PRIVACY for key in data):
        raise ValueError("Изменение этой настройки MAX не поддерживается")
    output = {}
    for key, value in data.items():
        if key in PRIVACY_ENUMS:
            if not isinstance(value, str) or value not in PRIVACY_ENUMS[key]:
                raise ValueError("MAX вернул неподтверждённое значение настройки")
            output[key] = value
        elif key in PRIVACY_BOOLEANS:
            if type(value) is not bool:
                raise ValueError("Настройка MAX должна быть логическим значением")
            output[key] = value
    return output
