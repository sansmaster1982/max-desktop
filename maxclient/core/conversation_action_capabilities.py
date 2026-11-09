"""Info actions reconstructed from pinned WEB models and the Info-specific menu.

These are UI/protocol capability checks, not permission grants from a server.
Unknown or malformed input never authorizes a write. No chat ID is derived.
See research/CONVERSATION_ACTIONS_CAPABILITIES.md for source locations.
"""
from __future__ import annotations

import math

from ..protocol.folder_contracts import int64


def _finite(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except (ValueError, OverflowError):
        return False


def _bits(value):
    return type(value) is int and -(2 ** 31) <= value < 2 ** 31


def _ids(mapping):
    if not isinstance(mapping, dict):
        return None
    result = set()
    for key in mapping:
        if isinstance(key, str) and len(key) <= 20 and key.lstrip('-').isascii() and key.lstrip('-').isdigit():
            key = int(key)
        if not int64(key):
            return None
        result.add(key)
    return result


def _admin_ids(mapping):
    identities = _ids(mapping)
    if identities is None:
        return None
    for identity, row in mapping.items():
        if (not isinstance(row, dict) or not int64(row.get('id')) or row['id'] != int(identity)
                or 'permissions' in row and not _bits(row['permissions'])
                or 'inviterId' in row and not int64(row['inviterId'])
                or 'alias' in row and not isinstance(row['alias'], str)):
            return None
    return identities


def conversation_action_capabilities(raw, contact, owner_id, server_config, *, peer_id=None):
    result = {'supported': False, 'kind': 'unknown', 'mute': False, 'participant': False,
              'owner': False, 'service': False, 'suspended': False,
              'recipient_blocked': False, 'hide_stories': False,
              'last_message': False, 'more': [], 'clear_for_all': False,
              'clear_for_all_optional': False, 'leave_requires_transfer': False}
    result.update(delete_for_all=False, delete_for_all_optional=False)
    if (not isinstance(raw, dict) or not int64(raw.get('id')) or not int64(owner_id)
            or not isinstance(contact, dict) or not isinstance(server_config, dict)):
        return result
    kind = raw.get('type')
    restrictions = raw.get('restrictions', 0)
    options = raw.get('options', {})
    has_bots = raw.get('hasBots', False)
    if (kind not in ('DIALOG', 'CHAT', 'CHANNEL') or not _bits(restrictions)
            or not isinstance(options, dict) or type(has_bots) is not bool
            or ('SERVICE_CHAT' in options and type(options['SERVICE_CHAT']) is not bool)):
        return result
    if any(key in server_config and type(server_config[key]) is not bool for key in
           ('bots-mute-restriction', 'bot-complaint-enabled', 'delete-for-all')):
        return result
    participants = _ids(raw.get('participants', {}))
    admins = _admin_ids(raw.get('adminParticipants', {}))
    if (participants is None or admins is None or
            any(not _finite(stamp) for stamp in raw.get('participants', {}).values())):
        return result
    last = raw.get('lastMessage')
    last_message = isinstance(last, dict) and int64(last.get('id')) and last['id'] != 0
    status = raw.get('status')
    # Inline message pushes can contain only a partial chat DTO. Missing status
    # cannot establish that normal actions are allowed (it may be BLOCKED).
    if status not in ('ACTIVE', 'LEFT', 'REMOVED', 'CLOSED', 'HIDDEN', 'BLOCKED'):
        return result
    if 'lastEventTime' in raw and not _finite(raw['lastEventTime']):
        return result
    is_owner = int64(raw.get('owner')) and raw['owner'] != 0 and raw['owner'] == owner_id
    join_time = raw.get('joinTime')
    participant = (is_owner or owner_id in admins or
                   (_finite(join_time) and join_time > 0 and owner_id in participants))
    service = options.get('SERVICE_CHAT', False)
    result.update(owner=is_owner, participant=participant, service=service,
                  last_message=last_message)
    can_delete = can_leave = can_report = can_stop = False
    normal = bot = video = False
    if kind == 'DIALOG':
        result['participant'] = True  # uM's actual getter, not inferred membership.
        if raw['id'] == 0:
            result['kind'] = 'saved'
        else:
            if (not int64(contact.get('id')) or peer_id is not None and contact['id'] != peer_id
                    or ('status' in contact and contact['status'] not in ('REMOVED', 'BLOCKED', 'HIDDEN'))):
                return {**result, 'participant': False}
            flags = contact.get('flags', 0)
            if not _bits(flags):
                return {**result, 'participant': False}
            blocked = contact.get('status') == 'BLOCKED'
            result.update(recipient_blocked=blocked, hide_stories=bool(flags & 1024))
            if has_bots:
                bot = True
                result['kind'] = 'bot'
                suspended = raw.get('isSuspended', False)
                if type(suspended) is not bool:
                    return result
                suspended = raw.get('lastEventTime') == 0 or suspended
                result['suspended'] = suspended
                can_stop = not bool(restrictions & 128)
                can_delete = can_stop
                result['mute'] = (not (server_config.get('bots-mute-restriction', False)
                                       and restrictions & 8192)
                                  and not suspended and not blocked)
                can_report = not bool(restrictions & 256) and server_config.get('bot-complaint-enabled', False)
            else:
                normal = True
                result['kind'] = 'dialog'
                result['mute'] = not blocked
                can_delete = True
    else:
        # Post-discussion contexts carry a separate model argument in WEB. Do
        # not reconstruct its permissions from a guessed ID or ordinary group.
        if raw.get('postId') is not None:
            return result
        video_data = raw.get('videoConversation')
        if 'videoConversation' in raw and not isinstance(video_data, dict):
            return result
        if isinstance(video_data, dict) and 'type' in video_data and (
                type(video_data['type']) is not int or video_data['type'] not in (0, 1)):
            return result
        video = kind == 'CHAT' and isinstance(video_data, dict) and video_data.get('type') != 1
        result['kind'] = 'video' if video else ('group' if kind == 'CHAT' else 'channel')
        result['mute'] = not video
        can_delete = is_owner
        can_leave = not bool(restrictions & 8) and participant
        can_report = kind == 'CHANNEL' and not bool(restrictions & 256) and not is_owner
        count = raw.get('participantsCount')
        # The original owner path opens transfer-and-leave when count>1. An
        # unknown count must not silently choose the destructive simple path.
        result['leave_requires_transfer'] = bool(is_owner and (type(count) is not int or count > 1))
    result['supported'] = True
    more = []
    if video:
        more = ['search', 'clear'] if last_message else []
    else:
        if normal:
            more.append('share_contact')
        if result['participant']:
            more.append('folder')
        if not bot and last_message:
            more.append('search')
        if last_message and not (bot and service) and (kind != 'CHANNEL' or is_owner):
            more.append('clear')
        if normal:
            more.append('show_stories' if result['hide_stories'] else 'hide_stories')
        if can_leave:
            more.append('leave')
        if can_report:
            more.append('report')
        if bot and can_stop and not service and not result['suspended']:
            more.append('stop_bot')
        if normal:
            more.append('unblock' if result['recipient_blocked'] else 'block')
        if can_delete and not (bot and service):
            more.append('delete')
    if status == 'BLOCKED':
        more = [action for action in more if action in ('leave', 'delete')]
    if bot and result['suspended'] and not last_message:
        more = []
    result['more'] = more
    result['clear_for_all'] = kind == 'CHANNEL'
    result['clear_for_all_optional'] = ((normal and server_config.get('delete-for-all', False))
                                        or (kind == 'CHAT' and is_owner))
    result['delete_for_all'] = kind in ('CHAT', 'CHANNEL')
    result['delete_for_all_optional'] = normal and server_config.get('delete-for-all', False)
    return result
