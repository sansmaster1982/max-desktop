"""Current public Info Sound contracts. Pure parsing; no requests or defaults."""
from __future__ import annotations

import math
import copy

from .settings_contracts import chat_notification_values, user_setting_values
from .search_contracts import _chat, _contact, _integer, _number
from .chat_record_maps import normalize_chat_id_maps


MUTE_DURATIONS = frozenset({-1, 0, 3_600_000, 14_400_000, 86_400_000})


def _timestamp(value):
    try:
        return type(value) in (int, float) and math.isfinite(value) and value >= -1
    except (ValueError, OverflowError):
        return False


def mute_deadline(duration_ms, now_ms):
    """Use exact established durations and Date.now-style absolute milliseconds."""
    if type(duration_ms) is not int or duration_ms not in MUTE_DURATIONS:
        raise ValueError('Unsupported mute duration')
    if duration_ms <= 0:
        return duration_ms
    if not _timestamp(now_ms) or now_ms < 0:
        raise ValueError('A finite current timestamp is required')
    value = now_ms + duration_ms
    if not _timestamp(value):
        raise ValueError('Invalid mute deadline')
    return value


def build_chat_mute(chat_id, dont_disturb_until):
    if type(chat_id) is not int or not -(2**63) <= chat_id < 2**63:
        raise ValueError('A typed chat ID is required')
    if not _timestamp(dont_disturb_until) or -1 < dont_disturb_until < 0:
        raise ValueError('A finite mute timestamp is required')
    return {'settings': {'chats': {str(chat_id): {'dontDisturbUntil': dont_disturb_until}}}}


def parse_chat_mute_ack(data):
    """Optional CONFIG is authoritative only for fields actually supplied."""
    if data is None or data == {}:
        return None
    if not isinstance(data, dict) or not isinstance(data.get('hash'), str):
        raise ValueError('Unconfirmed settings response')
    result = {'hash': data['hash']}
    if 'user' in data:
        result['user'] = user_setting_values(data['user'])
    if 'chats' in data:
        result['chats'] = chat_notification_values(data['chats'])
    return result


def supplied_mute(config, chat_id):
    """Do not convert an omitted CONFIG value into the WEB model's default0."""
    if not isinstance(config, dict) or not isinstance(config.get('chats'), dict):
        return None
    row = config['chats'].get(str(chat_id))
    if not isinstance(row, dict) or 'dontDisturbUntil' not in row:
        return None
    value = row['dontDisturbUntil']
    return value if _timestamp(value) else None


def mute_is_active(value, now_ms):
    if value is None:
        return None
    if not _timestamp(value) or not _timestamp(now_ms):
        raise ValueError('Invalid mute timestamp')
    return value == -1 or value != 0 and value > now_ms


def build_story_visibility(contact_id, hidden):
    if not _integer(contact_id, 64) or type(hidden) is not bool:
        raise ValueError('A typed recipient and story visibility are required')
    return {'contactId': contact_id, 'action': 'HIDE_STORIES' if hidden else 'SHOW_STORIES'}


def parse_story_visibility_ack(data, contact_id):
    if not isinstance(data, dict):
        raise ValueError('Unconfirmed contact action response')
    if 'contact' not in data:
        return {}
    contact = _contact(data['contact'])
    if contact is None or contact['id'] != contact_id:
        raise ValueError('The contact action returned another recipient')
    return {'contact': contact}


def parse_action_contact_readback(data, contact_id):
    if not isinstance(data, dict) or not isinstance(data.get('contacts'), list):
        raise ValueError('No typed contact readback')
    if len(data['contacts']) != 1 or (contact := _contact(data['contacts'][0])) is None or contact['id'] != contact_id:
        raise ValueError('No unique matching contact readback')
    return contact


def story_hidden_from_contact(contact):
    flags = contact.get('flags') if isinstance(contact, dict) else None
    return bool(flags & 1024) if _integer(flags, 32) else None


def build_stop_bot(bot_id):
    if not _integer(bot_id, 64):
        raise ValueError('A typed bot recipient is required')
    return {'botId': bot_id, 'value': True}


def parse_stop_bot_ack(data):
    # The public mapping is ZodUndefined/VOID, not an empty-object schema.
    if data is not None:
        raise ValueError('Unconfirmed bot-stop response')
    return None


def parse_action_chat_readback(data, chat_id):
    if not isinstance(data, dict) or not isinstance(data.get('chats'), list):
        raise ValueError('No typed chat readback')
    if len(data['chats']) != 1 or not isinstance(data['chats'][0], dict):
        raise ValueError('No unique matching chat readback')
    raw = normalize_chat_id_maps(data['chats'][0])
    if _chat(raw) is None or raw['id'] != chat_id:
        raise ValueError('The bot action returned another chat')
    for field in ('isSuspended', 'hasBots'):
        if field in raw and type(raw[field]) is not bool:
            raise ValueError('Unconfirmed bot state')
    return copy.deepcopy(raw)


def bot_suspended_from_chat(raw):
    if not isinstance(raw, dict) or raw.get('hasBots') is not True:
        return None
    event = raw.get('lastEventTime')
    suspended = raw.get('isSuspended')
    if 'isSuspended' in raw and type(suspended) is not bool:
        return None
    if _number(event):
        return event == 0 or suspended is True
    return suspended if type(suspended) is bool else None
