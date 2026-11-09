"""Pure WEB viewerCanWrite projection for explicit conversation destinations.

No owner/session inference, reads, mutations or guessed peers. Source offsets
are retained in research/CONVERSATION_WRITE_CAPABILITIES_20261104.md.
"""
from __future__ import annotations

import math

from ..protocol.folder_contracts import int64


def _int32(value):
    return type(value) is int and -(2**31) <= value < 2**31


def _number(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except (OverflowError, ValueError):
        return False


def _joined(value):
    return _number(value) and value > 0


def _id_rows(rows):
    """Normalize nested MessagePack int keys and JSON decimal string keys.

    The transport normalizes only top-level keys. Reject conflicting spellings
    instead of allowing map iteration order to decide membership or rights.
    """
    if not isinstance(rows, dict) or len(rows) > 100000:
        return None
    result = {}
    for key, value in rows.items():
        if type(key) is int:
            identity = key
        elif (isinstance(key, str) and key.isascii() and key.isdecimal()
              and len(key) <= 19):
            identity = int(key)
            if str(identity) != key:
                return None
        else:
            return None
        if not int64(identity) or identity < 0 or identity in result:
            return None
        result[identity] = value
    return result


def _participants(rows):
    result = _id_rows(rows)
    if result is None or not all(_number(value) for value in result.values()):
        return None
    return set(result)


def _admins(rows):
    result = _id_rows(rows)
    if result is None:
        return None
    for identity, value in result.items():
        if (not isinstance(value, dict) or not int64(value.get('id')) or value['id'] != identity
                or not _int32(value.get('permissions', 0))):
            return None
    return result


def conversation_can_write(raw, contact, owner_id):
    """Project only identity and permissions used by WEB viewerCanWrite.

    Callers still capture and revalidate account/client/session and must obtain
    fresh metadata before the one explicit write. This is not server authority.
    Cached partial messages and unrelated timestamps are not permission fields.
    """
    if (not int64(owner_id) or owner_id <= 0 or not isinstance(raw, dict)
            or not int64(raw.get('id')) or raw.get('type') not in ('DIALOG', 'CHAT', 'CHANNEL')):
        return False
    restrictions = raw.get('restrictions', 0)
    if not _int32(restrictions):
        return False
    participants = _participants(raw.get('participants'))
    if participants is None:
        return False
    if raw.get('postId') is not None:
        return False  # Thread/comment permission depends on its parent channel.
    kind = raw['type']
    if kind == 'DIALOG':
        others = participants - {owner_id, 0}
        if raw['id'] == 0:
            # dM overrides viewerCanWrite to true. Never apply that override to
            # a malformed or foreign conversation pretending to be Saved.
            return not others and owner_id in participants
        if (not isinstance(contact, dict) or not int64(contact.get('id')) or contact['id'] <= 0
                or contact['id'] == owner_id or others != {contact['id']}):
            return False
        state = contact.get('accountStatus', 0)
        if type(state) is not int or state != 0:
            return False
        if contact.get('status') not in (None, 'REMOVED', 'HIDDEN'):
            return False
        bots = raw.get('hasBots', False)
        if type(bots) is not bool:
            return False
        if bots:
            suspended = raw.get('isSuspended', False)
            event_time = raw.get('lastEventTime')
            if (type(suspended) is not bool or suspended or not _number(event_time)
                    or event_time == 0):
                return False
        # uM declares isViewerParticipant=true. A normal dialog can be locally
        # REMOVED before its first message; that is not a write prohibition.
        return not restrictions & 64
    if raw.get('status') not in ('ACTIVE', 'HIDDEN') or restrictions & 64:
        return False
    admins = _admins(raw.get('adminParticipants', {}))
    if not int64(raw.get('owner')) or admins is None:
        return False
    video = raw.get('videoConversation')
    if video is not None and (not isinstance(video, dict) or type(video.get('type')) is not int
                              or video['type'] != 1):
        return False  # Group-call lifecycle is outside this implementation.
    joined = _joined(raw.get('joinTime'))
    member = raw['owner'] == owner_id or owner_id in admins or joined and owner_id in participants
    if not member:
        return False
    if kind == 'CHANNEL':
        # _M does not grant an owner-only bypass: a matching typed admin entry
        # must carry POST_MESSAGE(256) or POST_EDIT_DELETE_MESSAGE(1).
        record = admins.get(owner_id)
        return bool(record and record.get('permissions', 0) & (256 | 1))
    return kind == 'CHAT'
