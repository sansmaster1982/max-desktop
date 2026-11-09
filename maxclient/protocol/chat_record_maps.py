"""Normalize known int64-keyed chat records at the binary-to-WEB boundary.

The binary decoder preserves int64 extension map keys; WEB object records use
decimal string keys. Unrelated fields and full DTO validation are unchanged.
"""
from __future__ import annotations

import copy

from .folder_contracts import int64


def normalize_chat_id_maps(raw):
    if not isinstance(raw, dict):
        raise ValueError('Нет типизированных сведений о чате')
    result = copy.deepcopy(raw)
    for name in ('participants', 'adminParticipants'):
        if name not in raw:
            continue
        rows = raw[name]
        if not isinstance(rows, dict) or len(rows) > 100000:
            raise ValueError('MAX вернул некорректный состав чата')
        normalized = {}
        for key, value in rows.items():
            if type(key) is int:
                identity = key
            elif (isinstance(key, str) and key.isascii() and key.isdecimal()
                  and len(key) <= 19):
                identity = int(key)
                if str(identity) != key:
                    raise ValueError('MAX вернул неоднозначного участника чата')
            else:
                raise ValueError('MAX вернул некорректного участника чата')
            if not int64(identity) or identity < 0 or str(identity) in normalized:
                raise ValueError('MAX вернул неоднозначного участника чата')
            normalized[str(identity)] = copy.deepcopy(value)
        result[name] = normalized
    return result
