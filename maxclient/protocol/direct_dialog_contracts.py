"""Observed WEB Contact.chatId / DM(48) read-only navigation contracts.

See research/CONTACT_DIRECT_DIALOG_20261104.md. A missing DTO means a local
draft only, never proof that a server conversation was created.
"""
from __future__ import annotations

import copy

from .folder_contracts import int64
from .search_contracts import _chat
from .chat_record_maps import normalize_chat_id_maps


def direct_dialog_id(owner_id, contact_id):
    if not int64(owner_id) or not int64(contact_id) or owner_id <= 0 or contact_id <= 0:
        raise ValueError('Не подтверждены участники личного диалога')
    # Exact BigInt producer U.resolveChatId(viewer.contactId, contact.id).
    # Both operands are confirmed actual IDs; no float/equal-ID fallback.
    return owner_id ^ contact_id


def direct_dialog_record(raw, owner_id, contact_id, chat_id):
    raw = normalize_chat_id_maps(raw)
    if _chat(raw) is None or raw['id'] != chat_id or raw['type'] != 'DIALOG':
        raise ValueError('MAX не подтвердил личный диалог выбранного контакта')
    # uM.recipientId uses participants, not the optional ownership semantics
    # used for groups/channels. A typed owner=0 is valid for a direct dialog.
    expected = {str(owner_id), str(contact_id)}
    if set(raw['participants']) != expected:
        raise ValueError('MAX вернул диалог с другими участниками')
    if 'participantsCount' in raw and raw['participantsCount'] != len(expected):
        raise ValueError('MAX вернул неоднозначный состав диалога')
    return copy.deepcopy(raw)


def direct_dialog_reply(data, owner_id, contact_id, chat_id):
    if not isinstance(data, dict):
        raise ValueError('MAX не вернул сведения диалога')
    # Hie allows chats and chat. Do not interpret an untyped body/foreign record
    # as an empty chat; absence must come from an explicitly empty list.
    if 'chat' in data:
        if 'chats' in data and data['chats'] != []:
            raise ValueError('MAX вернул неоднозначные сведения диалога')
        return direct_dialog_record(data['chat'], owner_id, contact_id, chat_id)
    rows = data.get('chats')
    if not isinstance(rows, list) or len(rows) > 1:
        raise ValueError('MAX не подтвердил сведения диалога')
    return direct_dialog_record(rows[0], owner_id, contact_id, chat_id) if rows else None
