"""Whole-chat and contact-share contracts from the pinned public WEB producer.

52 deletes a conversation; 54 clears history. Legacy opcode names are reversed.
See research/CONVERSATION_ACTIONS_CAPABILITIES.md (DPM hR/gR and node0).
"""
from __future__ import annotations

import copy
from .search_contracts import _integer, _number, _message
from .conversation_actions_contracts import parse_action_chat_readback


def chat_sync_time(raw):
    if not isinstance(raw, dict):
        raise ValueError('Нет подтверждённых данных чата')
    values = [raw.get('created'), raw.get('lastEventTime'), raw.get('modified', 0)]
    if any(not _number(value) or value < 0 for value in values):
        raise ValueError('Не подтверждено время состояния чата')
    return max(values)


def build_chat_lifecycle(chat_id, last_event_time, for_all):
    if (not _integer(chat_id, 64) or not _number(last_event_time)
            or last_event_time < 0 or type(for_all) is not bool):
        raise ValueError('Неверные параметры действия с чатом')
    return {'chatId': chat_id, 'lastEventTime': last_event_time, 'forAll': for_all}


def parse_lifecycle_ack(data):
    # Both public contracts map to VOID. {} is not a confirmed VOID frame.
    if data is not None:
        raise ValueError('MAX не подтвердил действие с чатом')


def build_chat_leave(chat_id):
    if not _integer(chat_id, 64):
        raise ValueError('Не подтверждён выбранный чат')
    return {'chatId': chat_id}


def parse_leave_ack(data):
    """58 is an object with optional zh message, unlike the VOID 52/54 ACK."""
    if not isinstance(data, dict) or set(data) - {'message'}:
        raise ValueError('MAX не подтвердил выход из чата')
    if 'message' in data and _message(data['message']) is None:
        raise ValueError('MAX вернул неподтверждённое сообщение о выходе')
    # This service message is not a readback of membership; do not cache it here.
    return copy.deepcopy(data)


def lifecycle_readback(data, chat_id):
    if not isinstance(data, dict) or any(key in data for key in ('chat', 'chatId', 'message')):
        raise ValueError('Неоднозначные сведения о чате')
    if isinstance(data, dict) and data.get('chats') == []:
        return None
    return parse_action_chat_readback(data, chat_id)


def build_contact_share(chat_id, contact_id, cid):
    if any(not _integer(value, 64) for value in (chat_id, contact_id, cid)):
        raise ValueError('Не подтверждён получатель или контакт')
    return {'chatId': chat_id, 'message': {'cid': cid, 'text': '',
            'attaches': [{'_type': 'CONTACT', 'contactId': contact_id}]}, 'notify': True}


def parse_contact_share_ack(data, chat_id, cid):
    if (not isinstance(data, dict) or type(data.get('chatId')) is not int
            or data['chatId'] != chat_id or _message(data.get('message')) is None):
        raise ValueError('MAX не подтвердил передачу контакта получателю')
    if 'cid' in data['message'] and data['message']['cid'] != cid:
        raise ValueError('MAX вернул другое сообщение')
    return copy.deepcopy(data)
