"""Доменные модели клиента MAX (без зависимости от GUI)."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional

# Значения _type из x70.java (docs/MEDIA_OPCODES.md).
A_PHOTO = "PHOTO"
A_VIDEO = "VIDEO"
A_AUDIO = "AUDIO"
A_VIDEO_MSG = "VIDEO_MSG"
A_FILE = "FILE"
A_STICKER = "STICKER"
A_CONTROL = "CONTROL"


def _to_int(v: Any) -> Optional[int]:
    if v is None or isinstance(v, bool):
        return int(v) if isinstance(v, bool) else None
    if isinstance(v, (int, float)):
        return int(v)
    try:
        return int(str(v))
    except (TypeError, ValueError):
        return None


@dataclass
class Profile:
    id: Optional[int] = None
    name: Optional[str] = None
    phone: Optional[str] = None


@dataclass
class Contact:
    id: int
    name: Optional[str] = None
    phone: Optional[str] = None

    @property
    def display(self) -> str:
        return self.name or self.phone or f"Контакт {self.id}"


@dataclass
class Chat:
    id: int
    title: Optional[str] = None
    is_group: bool = False
    last_time_ms: Optional[int] = None
    last_preview: Optional[str] = None
    unread: int = 0
    avatar_url: Optional[str] = None

    @property
    def display(self) -> str:
        return self.title or f"Чат {self.id}"


@dataclass
class Attach:
    """Вложение сообщения. Поля собраны из server payload и docs/MEDIA_OPCODES.md."""
    type: str = A_FILE
    token: Optional[str] = None
    file_id: Optional[int] = None
    url: Optional[str] = None
    base_url: Optional[str] = None
    file_name: Optional[str] = None
    mime_type: Optional[str] = None
    size: Optional[int] = None
    width: Optional[int] = None
    height: Optional[int] = None
    duration_ms: Optional[int] = None
    local_path: Optional[str] = None

    @classmethod
    def from_server(cls, m: dict) -> "Attach":
        t = str(m.get("_type") or m.get("type") or A_FILE).upper()
        return cls(
            type=t,
            token=_str_or_none(m.get("token") or m.get("photoToken")),
            file_id=_to_int(
                m.get("fileId") or m.get("videoId") or m.get("audioId") or m.get("id")
            ),
            url=_str_or_none(m.get("url")),
            base_url=_str_or_none(m.get("baseUrl")),
            file_name=_str_or_none(m.get("name") or m.get("fileName")),
            mime_type=_str_or_none(m.get("mime") or m.get("mimeType")),
            size=_to_int(m.get("size")),
            width=_to_int(m.get("width")),
            height=_to_int(m.get("height")),
            duration_ms=_to_int(m.get("duration") or m.get("durationMs")),
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
        }.get(self.type, self.file_name or "Вложение")


@dataclass
class Message:
    chat_id: int
    id: Optional[int] = None
    sender: Optional[int] = None
    text: str = ""
    time_ms: int = field(default_factory=lambda: int(time.time() * 1000))
    outgoing: bool = False
    status: str = "sent"  # pending | sent | failed
    local_id: Optional[str] = None
    attaches: list[Attach] = field(default_factory=list)
    error: Optional[str] = None  # причина неудачной отправки (не хранится в БД)

    @property
    def has_attaches(self) -> bool:
        return bool(self.attaches)

    @property
    def preview(self) -> str:
        if self.text:
            return self.text
        if self.attaches:
            return f"[{self.attaches[0].label}]"
        return ""

    @classmethod
    def from_server(cls, chat_id: int, m: dict, my_id: Optional[int]) -> "Message":
        sender = _to_int(m.get("sender"))
        raw_attaches = m.get("attaches") or m.get("attachments") or []
        attaches = [
            Attach.from_server(a) for a in raw_attaches if isinstance(a, dict)
        ]
        return cls(
            chat_id=chat_id,
            id=_to_int(m.get("id")),
            sender=sender,
            text=clean_message_text(m.get("text")),
            time_ms=_sanitize_time(_to_int(m.get("time"))),
            outgoing=(my_id is not None and sender == my_id),
            status="sent",
            attaches=attaches,
        )


# Правдоподобный диапазон ms-таймстампа: ~2001..2033. Байтовый парс компактного
# формата MAX иногда даёт «время» с лишними разрядами — нормализуем, чтобы не
# ломать порядок сообщений. Совсем неправдоподобные -> 0 (сортируются как старые).
def _sanitize_time(t: Optional[int]) -> int:
    if not t or t < 0:
        return 0
    while t > 2_000_000_000_000:
        t //= 1000
    if t < 1_000_000_000_000:
        return 0
    return t


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
