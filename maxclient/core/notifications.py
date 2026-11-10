"""Pure, bounded notification eligibility. No account or OS side effects."""
from collections import OrderedDict
from dataclasses import dataclass


@dataclass(frozen=True)
class Notice:
    chat_id: int
    title: str
    body: str
    sound: bool


def _plain(text, limit):
    # OS/tool windows must never interpret a sender's text as rich markup.
    return ' '.join(str(text).split())[:limit]


class NotificationPolicy:
    def __init__(self, started_ms):
        self.started_ms = started_ms
        self._seen = OrderedDict()

    def decide(self, message, chat, preferences, *, now_ms, local_enabled=True,
               opened_chat=None, window_active=False, viewer_id=None):
        key = (message.chat_id, message.id)
        if message.id is None or key in self._seen:
            return None
        self._seen[key] = None
        if len(self._seen) > 2048:
            self._seen.popitem(last=False)
        if (not local_enabled or chat is None or message.outgoing or
                message.sender == viewer_id or message.time_ms < self.started_ms or
                message.time_ms > now_ms + 60_000 or chat.unread <= 0 or chat.is_muted or
                window_active and opened_chat == chat.id or not preferences.get('available') or
                preferences.get('pending')):
            return None
        values = preferences['settings']
        dnd = values.get('DONT_DISTURB_UNTIL', -1)
        if dnd == -1 or dnd > now_ms:
            return None
        if chat.is_group:
            # REPLY needs verified mention/reply metadata not present in this model.
            if values.get('CHATS_PUSH_NOTIFICATION') != 'ON':
                return None
        sound = values.get('PUSH_SOUND', '_NONE_') != '_NONE_'
        if chat.is_group:
            sound = sound and values.get('CHATS_PUSH_SOUND', '_NONE_') != '_NONE_'
        if chat.is_channel:
            sound = False
        details = values.get('PUSH_DETAILS') is True
        # Until the sender's identity is hydrated the row title is only the
        # local placeholder; a chat number is not a name worth announcing.
        named = bool(chat.title) and chat.title != f'Чат {chat.id}'
        return Notice(chat.id, _plain(chat.display, 100) if details and named else 'MAX',
                      _plain(message.preview, 240) if details else 'Новое сообщение', sound)
