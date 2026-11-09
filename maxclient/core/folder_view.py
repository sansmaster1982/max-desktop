"""Pure qA/JA folder projection over an already ordered viewer-chat list.

No RPC, persistence, guessed membership or timestamp/ID decoding. Missing data
needed by a predicate is three-valued and returned as unresolved, not false.
"""
from __future__ import annotations

import copy

from ..protocol.folder_contracts import ALL_FOLDER, folder_dto, int64

_TYPE_FILTERS = {2, 3, 4, 8, 9, 10}
_REQUIRED_FILTERS = {0, 1, 5, 6, 7, 11, 12, 13}


def _and(values):
    values = list(values)
    return False if False in values else None if None in values else True


def _or(values):
    values = list(values)
    return True if not values or True in values else None if None in values else False


def _lookup(mapping, identity):
    return mapping.get(identity, mapping.get(str(identity))) if isinstance(mapping, dict) else None


def _peer(chat, raw, viewer_id):
    if int64(chat.peer_user_id):
        return chat.peer_user_id
    participants = raw.get('participants') if raw else None
    if not isinstance(participants, dict) or not int64(viewer_id):
        return None
    identities = []
    for value in participants:
        if isinstance(value, str) and value.lstrip('-').isdigit():
            value = int(value)
        if int64(value) and value != viewer_id:
            identities.append(value)
    return identities[0] if len(identities) == 1 else viewer_id if not identities else None


def _properties(chat, raw, contacts, viewer_id, chat_settings, now_ms, folder):
    raw = raw if isinstance(raw, dict) else {}
    known = (all(int64(raw.get(key)) for key in ('id', 'owner', 'created', 'lastEventTime'))
             and raw['id'] == chat.id and isinstance(raw.get('participants'), dict)
             and raw.get('type') in ('DIALOG', 'CHAT', 'CHANNEL')
             and raw.get('status') in ('ACTIVE', 'BLOCKED', 'HIDDEN', 'REMOVED'))
    kind = raw.get('type', chat.chat_type)
    marked = raw.get('markedAsUnread', False if known else None)
    marked = marked if type(marked) is bool else None
    count = raw.get('newMessages')
    if known and 'lastMessage' not in raw:
        read = not marked if marked is not None else None
    elif type(count) is int and count >= 0:
        read = False if count else (not marked if marked is not None else None)
    elif known and isinstance(raw.get('lastMessage'), dict):
        # W.newMessages defaults to +marked with lastMessage, otherwise zero.
        read = not marked if marked is not None else None
    else:
        read = False if type(chat.unread) is int and chat.unread > 0 else None
    muted = None
    if chat_settings is not None:
        settings = _lookup(chat_settings, chat.id)
        dnd = settings.get('dontDisturbUntil', 0) if isinstance(settings, dict) else 0
        if int64(dnd):
            if dnd in (0, -1):
                muted = dnd == -1
            elif int64(now_ms):
                muted = dnd > now_ms
    elif chat.server_fields and 'is_muted' in chat.server_fields and type(chat.is_muted) is bool:
        muted = chat.is_muted
    flags = {0: not read if read is not None else None, 1: read, 7: muted,
             11: not muted if muted is not None else None, 12: marked}
    for value in (2, 3, 4, 5, 6, 8, 9, 10, 13):
        flags[value] = False if kind in ('DIALOG', 'CHAT', 'CHANNEL') else None
    if kind == 'DIALOG':
        flags[4] = True
        if chat.id != 0:
            bot = raw.get('hasBots', False if known else None)
            bot = bot if type(bot) is bool else None
            flags[10] = bot
            if bot is None:
                flags[8] = flags[9] = None
            elif not bot:
                contact = _lookup(contacts, _peer(chat, raw, viewer_id))
                mask = contact.get('flags', 0) if isinstance(contact, dict) else None
                added = bool(mask & 512) if type(mask) is int and 0 <= mask < 2 ** 31 else None
                flags[8], flags[9] = added, not added if added is not None else None
    elif kind in ('CHAT', 'CHANNEL'):
        flags[3 if kind == 'CHAT' else 2] = True
        owner = raw.get('owner')
        owned = False if owner == 0 else owner == viewer_id if int64(owner) and int64(viewer_id) else None
        admins = raw.get('adminParticipants', {} if known else None)
        member = (str(viewer_id) in admins or viewer_id in admins) if isinstance(admins, dict) and int64(viewer_id) else None
        flags[5], flags[6] = owned, _or([owned, member])
        if kind == 'CHAT':
            subjects = folder.get('filterSubjects', {})
            wanted = _lookup(subjects, 13) or []
            subject = raw.get('subject', {} if known else None)
            actual = subject.get('organizationIds', []) if isinstance(subject, dict) else None
            flags[13] = (bool(set(wanted) & set(actual)) if isinstance(wanted, list)
                         and isinstance(actual, list) and all(int64(value) for value in wanted + actual) else None)
    return flags, read, muted


def build_folder_view(folder, chats, *, raw_chats=None, contacts=None, viewer_id=None,
                      chat_settings=None, now_ms=None):
    """Favorites first; remaining matching rows retain caller viewer order.

    `chats` must be the caller's ordered viewer-chat list, not arbitrary history
    or thread rows. This function cannot recover draft/pending sortTime/marker
    from Chat.last_time_ms. CONFIG.chats can be supplied for mute predicates;
    None explicitly means that configuration is unavailable.
    """
    folder = folder_dto(folder)
    values = list(chats)
    by_id = {chat.id: chat for chat in values}
    if len(by_id) != len(values):
        raise ValueError('Список чатов содержит повторяющиеся идентификаторы')
    filters = set(folder.get('filters', []))
    include = set(folder.get('include', []))
    favorites = folder.get('favorites', [])
    selected, unresolved, missing = [], [], []
    properties = {}
    for identity in favorites:
        if identity in by_id:
            selected.append(identity)
        else:
            missing.append(identity)
    for chat in values:
        raw = _lookup(raw_chats, chat.id)
        flags, read, muted = _properties(chat, raw, contacts, viewer_id, chat_settings, now_ms, folder)
        properties[chat.id] = (read, muted)
        if chat.id in selected:
            continue
        # Favorite insertion precedes viewer-list filtering in the actual model.
        if isinstance(raw, dict) and raw.get('status') in ('HIDDEN', 'REMOVED'):
            continue
        if folder['id'] == ALL_FOLDER or chat.id in include:
            match = True
        elif not filters:
            match = False
        else:
            match = _and((_and(flags[key] for key in filters & _REQUIRED_FILTERS),
                          _or(flags[key] for key in filters & _TYPE_FILTERS)))
        if match is True:
            selected.append(chat.id)
        elif match is None:
            unresolved.append(chat.id)
    complete = not unresolved and not missing
    unread = 0 if complete else None
    if unread is not None:
        for identity in selected:
            read, muted = properties[identity]
            if folder['id'] == ALL_FOLDER:
                if identity == 0 or read is True or muted is True:
                    continue
                if read is None or muted is None:
                    unread = None
                    break
            elif read is None:
                unread = None
                break
            if read is False:
                unread += 1
    hidden = False
    if 0 in folder.get('options', []) and not selected:
        hidden = True if complete else None
    if folder['id'] == 'chat.channel.folder':
        hidden = False  # KA.visible keeps this special folder visible.
    unsupported = [key for key in ('exclude', 'allFilterExcludeFolders') if key in folder]
    return {'folderId': folder['id'], 'title': folder['title'], 'chatIds': selected,
            'unresolvedChatIds': unresolved, 'unavailableChatIds': missing, 'complete': complete,
            'unreadCount': unread, 'hidden': hidden, 'ignoredFilterIds': [14] if 14 in filters else [],
            'unsupportedFields': unsupported, 'widgets': copy.deepcopy(folder.get('widgets', [])),
            'elements': copy.deepcopy(folder.get('elements', []))}
