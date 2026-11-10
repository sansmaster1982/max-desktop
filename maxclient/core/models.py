"""Доменные модели клиента MAX (без зависимости от GUI)."""
from __future__ import annotations

import time
import math
import copy
from dataclasses import dataclass, field
from typing import Any, Optional
from .message_presentation import incoming_button_rows

# Значения _type из x70.java (docs/MEDIA_OPCODES.md).
A_PHOTO = "PHOTO"
A_VIDEO = "VIDEO"
A_AUDIO = "AUDIO"
A_VIDEO_MSG = "VIDEO_MSG"
A_FILE = "FILE"
A_STICKER = "STICKER"
A_CONTROL = "CONTROL"

# Captions for CONTROL events whose name alone states what happened. The names
# follow the attachment schema family and are NOT confirmed against a live MAX
# session; any other event (including 'new'/'hello') keeps the generic caption.
_CONTROL_EVENT_CAPTIONS = {
    'add': 'Добавлены участники',
    'remove': 'Участник удалён из чата',
    'leave': 'Участник покинул чат',
    'joinByLink': 'Участник присоединился по ссылке',
    'title': 'Название чата изменено',
    'icon': 'Фото чата изменено',
}


def control_event_caption(event) -> str:
    """Plain caption of one CONTROL event; never raw type names or user text."""
    return _CONTROL_EVENT_CAPTIONS.get(event, 'Системное сообщение') if isinstance(event, str) else 'Системное сообщение'


def _to_int(v: Any) -> Optional[int]:
    if v is None or isinstance(v, bool):
        return int(v) if isinstance(v, bool) else None
    if isinstance(v, (int, float)):
        return int(v)
    try:
        return int(str(v))
    except (TypeError, ValueError):
        return None


def _finite_file_number(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except (ValueError, OverflowError):
        return False


def file_message_schedule(message):
    attributes = message.get('delayedAttributes', {})
    if (not isinstance(attributes, dict)
            or 'delayedAttributes' in message and 'timeToFire' not in attributes):
        return 0, False, False
    supplied = 'timeToFire' in attributes
    value = attributes.get('timeToFire', 0)
    return value, _finite_file_number(value), supplied


@dataclass
class Profile:
    id: Optional[int] = None
    name: Optional[str] = None
    phone: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    description: Optional[str] = None
    avatar_url: Optional[str] = None
    photo_id: Optional[int] = None
    link: Optional[str] = None


@dataclass
class Contact:
    id: int
    name: Optional[str] = None
    phone: Optional[str] = None
    avatar_url: Optional[str] = None
    is_verified: bool = False
    server_fields: Optional[frozenset[str]] = field(default=None, repr=False, compare=False)

    @property
    def display(self) -> str:
        return self.name or self.phone or f"Контакт {self.id}"


@dataclass
class Chat:
    id: int
    title: Optional[str] = None
    is_group: bool = False
    last_time_ms: Optional[int | float] = None
    last_preview: Optional[str] = None
    unread: int = 0
    avatar_url: Optional[str] = None
    title_locked: bool = False  # локальное переименование — sync не затирает
    peer_user_id: Optional[int] = None  # для 1:1 диалога — id собеседника
    chat_type: Optional[str] = None
    is_channel: bool = False
    is_pinned: bool = False
    is_muted: bool = False
    is_verified: bool = False
    pin_order: Optional[int] = None
    # Presence matters for partial DTOs: a missing counter is not a server zero.
    server_fields: Optional[frozenset[str]] = field(default=None, repr=False, compare=False)

    @property
    def display(self) -> str:
        return self.title or f"Чат {self.id}"


@dataclass
class Attach:
    """Вложение сообщения. Поля собраны из server payload и docs/MEDIA_OPCODES.md."""
    type: str = A_FILE
    token: Optional[str] = None
    file_id: int | float | None = None
    url: Optional[str] = None
    base_url: Optional[str] = None
    file_name: Optional[str] = None
    mime_type: Optional[str] = None
    size: int | float | None = None
    width: Optional[int] = None
    height: Optional[int] = None
    duration_ms: Optional[int] = None
    local_path: Optional[str] = None
    file_metadata_confirmed: bool = False
    deleted: bool = False
    file_item_type: str = 'REGULAR'
    file_update_time: int | float | None = None
    control_metadata_confirmed: bool = False
    control_event: Optional[str] = None
    control_message: Optional[str] = None
    control_short_message: Optional[str] = None
    control_update_time: int | float | None = None
    inline_metadata_confirmed: bool = False
    inline_open_app_metadata_confirmed: bool = False
    inline_buttons: list[list[dict]] = field(default_factory=list)
    share_metadata_confirmed: bool = False
    share_title: Optional[str] = None
    share_description: Optional[str] = None
    share_url: Optional[str] = None

    @classmethod
    def from_server(cls, m: dict, *, file_item_type='REGULAR', file_context_confirmed=True,
                    file_update_time=None) -> "Attach":
        t = str(m.get("_type") or m.get("type") or A_FILE).upper()
        actual_file = m.get('_type') == A_FILE
        file_valid = (actual_file and file_context_confirmed is True
                      and file_item_type in ('REGULAR', 'DELAYED')
                      and ('fileId' not in m or _finite_file_number(m['fileId']))
                      and ('name' not in m or isinstance(m['name'], str))
                      and ('size' not in m or _finite_file_number(m['size']) and m['size'] >= 0)
                      and ('deleted' not in m or type(m['deleted']) is bool))
        control_valid = (m.get('_type') == A_CONTROL and m.get('event') == 'system'
                         and all(key not in m or isinstance(m[key], str)
                                 for key in ('message', 'shortMessage')))
        buttons = incoming_button_rows(m)
        share_valid = (m.get('_type') == 'SHARE' and _finite_file_number(m.get('shareId'))
                       and all(key not in m or isinstance(m[key], str)
                               for key in ('title', 'description', 'url', 'host')))
        return cls(
            type=t,
            token=_str_or_none(m.get("token") or m.get("photoToken")),
            file_id=(m.get('fileId') if _finite_file_number(m.get('fileId')) else None) if t == A_FILE else _to_int(
                m.get("fileId") or m.get("videoId") or m.get("audioId") or m.get("id")
            ),
            url=_str_or_none(m.get("url")),
            base_url=_str_or_none(m.get("baseUrl")),
            file_name=(m.get('name') if isinstance(m.get('name'), str) else None) if t == A_FILE
                      else _str_or_none(m.get("name") or m.get("fileName")),
            mime_type=_str_or_none(m.get("mime") or m.get("mimeType")),
            size=(m.get('size') if _finite_file_number(m.get('size')) and m['size'] >= 0 else None)
                 if t == A_FILE else _to_int(m.get("size")),
            width=_to_int(m.get("width")),
            height=_to_int(m.get("height")),
            duration_ms=_to_int(m.get("duration") or m.get("durationMs")),
            file_metadata_confirmed=file_valid,
            deleted=(m.get('deleted') is True if t == A_FILE else
                     ('deleted' in m and m['deleted'] is not False) if t == A_PHOTO else False),
            file_item_type=file_item_type if file_item_type in ('REGULAR', 'DELAYED') else 'REGULAR',
            file_update_time=file_update_time if _finite_file_number(file_update_time) else None,
            control_metadata_confirmed=control_valid,
            control_event=m.get('event') if isinstance(m.get('event'), str) else None,
            control_message=m.get('message') if isinstance(m.get('message'), str) else None,
            control_short_message=m.get('shortMessage') if isinstance(m.get('shortMessage'), str) else None,
            control_update_time=(file_update_time if m.get('_type') == A_CONTROL
                                 and file_context_confirmed is True
                                 and _finite_file_number(file_update_time) else None),
            inline_metadata_confirmed=buttons is not None,
            inline_open_app_metadata_confirmed=buttons is not None,
            inline_buttons=buttons or [],
            share_metadata_confirmed=share_valid,
            share_title=m.get('title') if share_valid else None,
            share_description=m.get('description') if share_valid else None,
            share_url=m.get('url') if share_valid else None,
        )

    @property
    def is_image(self) -> bool:
        return self.type == A_PHOTO

    @property
    def is_video(self) -> bool:
        return self.type in (A_VIDEO, A_VIDEO_MSG)

    @property
    def is_voice(self) -> bool:
        return self.type in (A_AUDIO, A_VIDEO_MSG)

    @property
    def label(self) -> str:
        return {
            A_PHOTO: "Фото",
            A_VIDEO: "Видео",
            A_VIDEO_MSG: "Видеосообщение",
            A_AUDIO: "Голосовое сообщение",
            A_FILE: self.file_name or "Файл",
            A_STICKER: "Стикер",
            A_CONTROL: control_event_caption(self.control_event),
            'INLINE_KEYBOARD': "Кнопки сообщения",
            'SHARE': self.share_title or "Ссылка",
            'CONTACT': f"Контакт: {self.file_name}" if self.file_name else "Контакт",
            'LOCATION': "Геопозиция",
            'CALL': "Звонок",
            'MUSIC': "Аудиозапись",
        }.get(self.type, self.file_name or "Вложение")


def server_attaches(message, *, previous=None):
    """Normalize attachment indices once; FILE proof survives cache roundtrip."""
    raw = message.get('attaches', message.get('attachments', []))
    if not isinstance(raw, list):
        return []
    schedule, schedule_valid, schedule_supplied = file_message_schedule(message)
    version = message.get('updateTime')
    version_valid = 'updateTime' not in message or _finite_file_number(version)
    confirmed = (isinstance(message.get('attaches'), list)
                 and type(message.get('id')) is int and -(2**63) <= message['id'] < 2**63
                 and _finite_file_number(message.get('time'))
                 and message.get('type') in ('USER', 'CHANNEL', 'CHANNEL_ADMIN')
                 and ('status' not in message or message['status'] in ('EDITED', 'SPAM', 'DELAYED_FIRE_ERROR'))
                 and ('sender' not in message or type(message['sender']) is int and -(2**63) <= message['sender'] < 2**63)
                 and schedule_valid and version_valid)
    partial_context = (isinstance(message.get('attaches'), list) and schedule_valid and version_valid
                       and type(message.get('id')) is int and -(2**63) <= message['id'] < 2**63
                       and ('status' not in message or message['status'] in ('EDITED', 'SPAM', 'DELAYED_FIRE_ERROR'))
                       and ('type' not in message or message['type'] in ('USER', 'CHANNEL', 'CHANNEL_ADMIN'))
                       and ('time' not in message or _finite_file_number(message['time']))
                       and ('sender' not in message or type(message['sender']) is int
                            and -(2**63) <= message['sender'] < 2**63))
    item_type = 'DELAYED' if schedule_valid and schedule > 0 else 'REGULAR'
    result = []
    for item in (item for item in raw if isinstance(item, dict)):
        index = len(result)
        old = previous[index] if isinstance(previous, list) and index < len(previous) else None
        chosen_type = (old.file_item_type if not confirmed and not schedule_supplied and isinstance(old, Attach)
                       and old.file_metadata_confirmed is True else item_type)
        chosen_version = (old.file_update_time if not confirmed and 'updateTime' not in message
                          and isinstance(old, Attach) and old.file_metadata_confirmed is True else version)
        attach = Attach.from_server(item, file_item_type=chosen_type, file_context_confirmed=confirmed,
                                    file_update_time=chosen_version)
        if (not confirmed and partial_context and isinstance(old, Attach)
                and old.type == A_FILE and old.file_metadata_confirmed is True
                and _finite_file_number(old.file_id) and attach.file_id == old.file_id):
            # Partial ACKs can retain an already proven resource, never promote
            # a legacy fallback ID or invent an omitted actual fileId.
            supplied = dict(item)
            for key, value in (('name', old.file_name), ('size', old.size), ('deleted', old.deleted)):
                if key not in supplied and value is not None:
                    supplied[key] = value
            attach = Attach.from_server(supplied, file_item_type=chosen_type, file_context_confirmed=True,
                                        file_update_time=chosen_version)
        if attach.type == A_FILE and message.get('status') == 'REMOVED':
            attach.deleted, attach.file_metadata_confirmed = True, False
        result.append(attach)
    return result


@dataclass
class ForwardedMessage:
    """Bounded incoming Jte content, distinct from outgoing Kte references."""
    available: bool = False
    source_chat_id: Optional[int] = None
    source_message_id: Optional[int] = None
    source_sender_id: Optional[int] = None
    source_time_ms: int | float | None = None
    source_type: Optional[str] = None
    text: str = ''
    chat_name: Optional[str] = None
    attaches: list[Attach] = field(default_factory=list)


def _forward_id(value):
    return type(value) is int and -(2**63) <= value < 2**63


def parse_forwarded_message(message: dict, *, previous=None) -> Optional[ForwardedMessage]:
    """Project one incoming FORWARD level; never descend into another link."""
    if 'link' not in message:
        return copy.deepcopy(previous)
    link = message.get('link')
    if not isinstance(link, dict) or link.get('type') != 'FORWARD':
        return None
    nested = link.get('message')
    chat_id = link.get('chatId')
    chat_name = link.get('chatName') if isinstance(link.get('chatName'), str) else None
    unavailable = ForwardedMessage(source_chat_id=chat_id if _forward_id(chat_id) else None,
                                   chat_name=chat_name)
    # A reference-only partial ACK cannot replace already proven content.
    if ('message' not in link and isinstance(previous, ForwardedMessage)
            and previous.available and _forward_id(link.get('messageId'))
            and link['messageId'] == previous.source_message_id and chat_id == previous.source_chat_id
            and 'postId' not in link):
        return copy.deepcopy(previous)
    if (not isinstance(nested, dict) or not _forward_id(chat_id) or 'postId' in link
            or not _forward_id(nested.get('id')) or not _finite_file_number(nested.get('time'))
            or nested.get('type') not in ('USER', 'CHANNEL', 'CHANNEL_ADMIN')
            or ('sender' in nested and not _forward_id(nested['sender']))
            or ('text' in nested and not isinstance(nested['text'], str))
            or ('updateTime' in nested and not _finite_file_number(nested['updateTime']))
            or ('status' in nested and nested['status'] not in ('EDITED', 'SPAM', 'DELAYED_FIRE_ERROR'))
            or nested.get('link') is not None
            or not isinstance(nested.get('attaches', []), list)
            or len(nested.get('attaches', [])) > 1000
            or any(not isinstance(item, dict) for item in nested.get('attaches', []))):
        return unavailable
    schedule, valid_schedule, _supplied = file_message_schedule(message)
    version = message.get('updateTime')
    outer_confirmed = (_forward_id(message.get('id')) and _finite_file_number(message.get('time'))
        and message.get('type') in ('USER', 'CHANNEL', 'CHANNEL_ADMIN') and valid_schedule
        and ('sender' not in message or _forward_id(message['sender']))
        and ('updateTime' not in message or _finite_file_number(version))
        and ('status' not in message or message['status'] in ('EDITED', 'SPAM', 'DELAYED_FIRE_ERROR')))
    selected = []
    for item in nested.get('attaches', []):
        kind = item.get('_type') if isinstance(item.get('_type'), str) else 'UNSUPPORTED'
        if kind == 'FILE':
            # Strip opaque data before parsing: FILE saves use only the real88
            # selected file ID and OUTER message context, never a nested URL.
            supplied = {key: item[key] for key in ('_type', 'fileId', 'name', 'size', 'deleted') if key in item}
            attach = Attach.from_server(supplied, file_context_confirmed=outer_confirmed,
                file_item_type='DELAYED' if valid_schedule and schedule > 0 else 'REGULAR', file_update_time=version)
        else:
            attach = Attach(type=kind)  # Other media are display-only in this increment.
        selected.append(attach)
    return ForwardedMessage(True, chat_id, nested['id'], nested.get('sender'), nested['time'],
                            nested['type'], nested.get('text', ''), chat_name, selected)


def forwarded_to_data(value: Optional[ForwardedMessage]):
    if value is None:
        return None
    return {'version': 1, 'available': value.available, 'source_chat_id': value.source_chat_id,
            'source_message_id': value.source_message_id, 'source_sender_id': value.source_sender_id,
            'source_time_ms': value.source_time_ms, 'source_type': value.source_type,
            'text': value.text, 'chat_name': value.chat_name,
            'attaches': [{key: getattr(attach, key) for key in ('type', 'file_id', 'file_name', 'size',
                'file_metadata_confirmed', 'deleted', 'file_item_type', 'file_update_time')}
                        for attach in value.attaches]}


def forwarded_from_data(value) -> Optional[ForwardedMessage]:
    """Read only the new typed cache format; legacy references grant no proof."""
    if (not isinstance(value, dict) or type(value.get('version')) is not int or value['version'] != 1
            or type(value.get('available')) is not bool or not isinstance(value.get('text'), str)
            or value.get('chat_name') is not None and not isinstance(value['chat_name'], str)
            or not isinstance(value.get('attaches'), list) or len(value['attaches']) > 1000):
        return None
    if any(value.get(key) is not None and not _forward_id(value[key])
           for key in ('source_chat_id', 'source_message_id', 'source_sender_id')):
        return None
    if value['available'] and (not all(_forward_id(value.get(key)) for key in ('source_chat_id', 'source_message_id'))
            or not _finite_file_number(value.get('source_time_ms'))
            or value.get('source_type') not in ('USER', 'CHANNEL', 'CHANNEL_ADMIN')):
        return None
    attaches = []
    for item in value['attaches']:
        if (not isinstance(item, dict) or not isinstance(item.get('type'), str)
                or type(item.get('file_metadata_confirmed')) is not bool or type(item.get('deleted')) is not bool
                or item.get('file_item_type') not in ('REGULAR', 'DELAYED')
                or any(item.get(key) is not None and not _finite_file_number(item[key])
                       for key in ('file_id', 'size', 'file_update_time'))
                or item.get('file_name') is not None and not isinstance(item['file_name'], str)):
            return None
        attaches.append(Attach(**{key: item.get(key) for key in ('type', 'file_id', 'file_name', 'size',
            'file_metadata_confirmed', 'deleted', 'file_item_type', 'file_update_time')}))
    return ForwardedMessage(value['available'], value.get('source_chat_id'), value.get('source_message_id'),
        value.get('source_sender_id'), value.get('source_time_ms'), value.get('source_type'),
        value['text'], value.get('chat_name'), attaches)


@dataclass
class Message:
    chat_id: int
    id: Optional[int] = None
    sender: Optional[int] = None
    text: str = ""
    time_ms: int | float = field(default_factory=lambda: int(time.time() * 1000))
    outgoing: bool = False
    status: str = "sent"  # pending | sent | failed | unknown
    local_id: Optional[str] = None
    attaches: list[Attach] = field(default_factory=list)
    error: Optional[str] = None  # причина неудачной отправки (не хранится в БД)
    reaction_info: dict = field(default_factory=dict)
    link: Optional[dict] = None
    update_time_ms: int | float | None = None
    forwarded: Optional[ForwardedMessage] = None

    @property
    def display_source(self) -> str:
        if self.forwarded is not None:
            return 'forward' if self.forwarded.available else 'unavailable_forward'
        if isinstance(self.link, dict) and self.link.get('type') == 'FORWARD':
            return 'unavailable_forward'
        return 'outer'

    @property
    def display_text(self) -> str:
        if self.display_source == 'forward':
            return self.forwarded.text
        return self.text if self.display_source == 'outer' else ''

    @property
    def display_attaches(self) -> list[Attach]:
        if self.display_source == 'forward':
            return self.forwarded.attaches
        return self.attaches if self.display_source == 'outer' else []

    @property
    def has_display_content(self) -> bool:
        return bool(self.display_text or self.display_attaches or self.display_source != 'outer')

    @property
    def has_attaches(self) -> bool:
        return bool(self.attaches)

    @property
    def control_display_text(self) -> Optional[str]:
        """Local label for the first proven system welcome, never user text."""
        if self.display_source != 'outer':
            return None
        first = next((attach for attach in self.attaches if attach.type == A_CONTROL), None)
        key = 'welcome.saved.dialog.message'
        if (first is not None and first.control_metadata_confirmed is True
                and first.control_event == 'system' and self.text in ('', key)):
            if first.control_message == key:
                return 'Здесь можно сохранять сообщения и файлы.'
            # Saved welcome DTOs can carry the literal caption in CONTROL.message
            # and the untranslated key in outer text. Prefer that actual caption,
            # never shortMessage or an inferred translation of ordinary user text.
            if (self.chat_id == 0 and self.text == key
                    and isinstance(first.control_message, str) and first.control_message.strip()):
                return first.control_message
        return None

    @property
    def preview(self) -> str:
        if self.control_display_text is not None:
            return self.control_display_text
        if self.display_text:
            return self.display_text
        if self.display_attaches:
            return f"[{self.display_attaches[0].label}]"
        if self.display_source != 'outer':
            return 'Пересланное сообщение'
        return ""

    @classmethod
    def from_server(cls, chat_id: int, m: dict, my_id: Optional[int]) -> "Message":
        sender = _to_int(m.get("sender"))
        attaches = server_attaches(m)
        return cls(
            chat_id=chat_id,
            id=_to_int(m.get("id")),
            sender=sender,
            text=m["text"] if isinstance(m.get("text"), str) else "",
            time_ms=_sanitize_time(m["time"]) if "time" in m else 0,
            outgoing=(my_id is not None and sender == my_id),
            status="sent",
            attaches=attaches,
            reaction_info=parse_reaction_info(m.get("reactionInfo")),
            link=parse_message_link(m.get("link")),
            update_time_ms=_sanitize_time(m['updateTime']) if 'updateTime' in m else None,
            forwarded=parse_forwarded_message(m),
        )


def parse_reaction_info(value: Any) -> dict:
    """Retain only supplied fields from the documented reaction-info DTO."""
    if not isinstance(value, dict):
        return {}
    result = {}
    counters = value.get("counters")
    if isinstance(counters, list):
        parsed = []
        valid = True
        for counter in counters:
            if (not isinstance(counter, dict) or not isinstance(counter.get("reaction"), str)
                    or type(counter.get("count")) is not int or counter["count"] < 0):
                valid = False
                break
            parsed.append({"reaction": counter["reaction"], "count": counter["count"]})
        if valid:
            result["counters"] = parsed
    if type(value.get("totalCount")) is int and value["totalCount"] >= 0:
        result["totalCount"] = value["totalCount"]
    if isinstance(value.get("yourReaction"), str):
        result["yourReaction"] = value["yourReaction"]
    return result


def parse_message_link(value: Any) -> Optional[dict]:
    """Preserve the proven reply/forward reference fields, without hidden data."""
    if (not isinstance(value, dict) or value.get("type") not in {"REPLY", "FORWARD"}
            or type(value.get("messageId")) is not int):
        return None
    result = {"type": value["type"], "messageId": value["messageId"]}
    if value["type"] == "FORWARD":
        if type(value.get("chatId")) is not int:
            return None
        result["chatId"] = value["chatId"]
    if type(value.get("postId")) is int:
        result["postId"] = value["postId"]
    return result


def _sanitize_time(t: Any) -> int | float:
    """Preserve the source finite timestamp without rounding or epoch guesses.

    The integer bound is SQLite's storage limit, not a server date restriction.
    Missing time is handled separately by partial-message callers.
    """
    if type(t) is int and -(2 ** 63) <= t < 2 ** 63:
        return t
    if type(t) is float and math.isfinite(t):
        return t
    raise ValueError("Message timestamp is not a supported finite number")


def _str_or_none(v: Any) -> Optional[str]:
    if v is None:
        return None
    s = str(v)
    return s if s else None


# Имена msgpack-полей, которые «протекают» в текст при разборе компактной
# истории (английские ключи — в русском тексте не встречаются, обрезать безопасно).
_LEAK_TOKENS = (
    "attaches", "reactionInfo", "previewData", "baseUrl", "photoToken",
    "delayedAttributes", "detectShare", "isLive", "_type", "stickerId",
)


def clean_message_text(text: Optional[str]) -> str:
    """Подчистить текст сообщения от артефактов компактного формата:
    обрезать утёкшие имена полей и хвостовой мусор. Середину (символы □ от
    повреждённых байт) не трогаем — её восстановит только полный декодер."""
    if not text:
        return ""
    s = str(text)
    cut = len(s)
    for tok in _LEAK_TOKENS:
        idx = s.find(tok)
        if idx > 0:
            cut = min(cut, idx)
    s = s[:cut]
    return s.rstrip(" \t\r\n,;:{}[]�").strip()
