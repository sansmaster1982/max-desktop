"""Утилиты UI: инициалы, цвета аватаров, форматирование времени и размера."""
from __future__ import annotations

import datetime
import hashlib

# Приятная палитра для аватаров (как в мессенджерах — цвет от хэша).
_AVATAR_COLORS = [
    ("#FF8A65", "#FF7043"),
    ("#4FC3F7", "#2B7FFF"),
    ("#81C784", "#43A047"),
    ("#BA68C8", "#8E24AA"),
    ("#FFD54F", "#FFB300"),
    ("#4DB6AC", "#00897B"),
    ("#F06292", "#E91E63"),
    ("#7986CB", "#3F51B5"),
    ("#A1887F", "#6D4C41"),
    ("#9575CD", "#5E35B1"),
]


def initials(name: str | None) -> str:
    if not name:
        return "#"
    parts = [p for p in name.strip().split() if p]
    if not parts:
        return "#"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[1][0]).upper()


def avatar_colors(key) -> tuple[str, str]:
    # Стабильный хэш (не hash()): цвет не меняется между запусками.
    digest = hashlib.md5(str(key).encode("utf-8")).digest()
    return _AVATAR_COLORS[digest[0] % len(_AVATAR_COLORS)]


def fmt_time_hm(ms: int | None) -> str:
    if not ms:
        return ""
    try:
        dt = datetime.datetime.fromtimestamp(ms / 1000)
        return dt.strftime("%H:%M")
    except (ValueError, OSError, OverflowError):
        return ""


_WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


def fmt_chat_time(ms: int | None) -> str:
    if not ms:
        return ""
    try:
        dt = datetime.datetime.fromtimestamp(ms / 1000)
    except (ValueError, OSError, OverflowError):
        return ""
    now = datetime.datetime.now()
    if dt.date() == now.date():
        return dt.strftime("%H:%M")
    delta = (now.date() - dt.date()).days
    if delta == 1:
        return "Вчера"
    if delta < 7:
        return _WEEKDAYS[dt.weekday()]
    if dt.year == now.year:
        return dt.strftime("%d.%m")
    return dt.strftime("%d.%m.%y")


def fmt_day_divider(ms: int | None) -> str:
    if not ms:
        return ""
    try:
        dt = datetime.datetime.fromtimestamp(ms / 1000)
    except (ValueError, OSError, OverflowError):
        return ""
    now = datetime.datetime.now()
    if dt.date() == now.date():
        return "Сегодня"
    if (now.date() - dt.date()).days == 1:
        return "Вчера"
    months = [
        "января", "февраля", "марта", "апреля", "мая", "июня",
        "июля", "августа", "сентября", "октября", "ноября", "декабря",
    ]
    s = f"{dt.day} {months[dt.month - 1]}"
    if dt.year != now.year:
        s += f" {dt.year}"
    return s


def human_size(num: int | None) -> str:
    if not num:
        return ""
    size = float(num)
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if size < 1024 or unit == "ГБ":
            return f"{size:.0f} {unit}" if unit == "Б" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} ГБ"


def fmt_duration(ms: int | None) -> str:
    if not ms:
        return "0:00"
    total = int(ms / 1000)
    return f"{total // 60}:{total % 60:02d}"
