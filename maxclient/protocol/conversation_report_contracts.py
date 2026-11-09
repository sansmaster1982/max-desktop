"""Pinned WEB 162 reasons and 161 channel/bot report contracts; no transport.

V.int8() in the captured schema is an integer, without a signed-byte bound.
The safe integer and collection bounds below are local resource policies.
"""
from __future__ import annotations

import copy
from .search_contracts import _integer, _number


def reason_id_valid(value):
    return type(value) is int and -(2**53 - 1) <= value <= 2**53 - 1


def build_report_reasons(sync):
    if not _number(sync) or sync < 0:
        raise ValueError('Не подтверждена версия списка причин')
    return {'complainSync': sync}


def parse_report_reasons(data):
    if not isinstance(data, dict):
        raise ValueError('MAX не подтвердил список причин')
    build_report_reasons(data.get('complainSync'))
    groups = data.get('complains')
    if not isinstance(groups, list) or len(groups) > 9:
        raise ValueError('MAX вернул неоднозначный список причин')
    seen_types = set()
    for group in groups:
        if (not isinstance(group, dict) or type(group.get('typeId')) is not int
                or group['typeId'] not in range(1, 10) or group['typeId'] in seen_types):
            raise ValueError('MAX вернул неоднозначный тип жалобы')
        seen_types.add(group['typeId'])
        reasons = group.get('reasons')
        if not isinstance(reasons, list) or len(reasons) > 1024:
            raise ValueError('MAX вернул неоднозначные причины')
        seen = set()
        for reason in reasons:
            if (not isinstance(reason, dict) or not reason_id_valid(reason.get('reasonId'))
                    or reason['reasonId'] in seen or not isinstance(reason.get('reasonTitle'), str)):
                raise ValueError('MAX вернул неподтверждённую причину')
            seen.add(reason['reasonId'])
    return copy.deepcopy({'complainSync': data['complainSync'], 'complains': groups})


def build_conversation_report(type_id, target_id, reason_id):
    if type(type_id) is not int or type_id not in (2, 7) or not _integer(target_id, 64):
        raise ValueError('Не подтверждён получатель жалобы')
    if not reason_id_valid(reason_id) or reason_id == 0:
        raise ValueError('Выберите подтверждённую причину жалобы')
    return {'reasonId': reason_id, 'typeId': type_id, 'ids': [target_id]}


def parse_report_ack(data):
    if not isinstance(data, dict) or type(data.get('success')) is not bool:
        raise ValueError('MAX не подтвердил результат жалобы')
    return {'success': data['success']}
