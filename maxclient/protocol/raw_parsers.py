"""Байтовые парсеры msgpack-полей по сырому ответу.

LOGIN (opcode 19) и CHAT_HISTORY (49) сервер MAX отдаёт в payload, который
обычный msgpack не распаковывает целиком (compact/ref encoding). Тогда decoded
из codec.unpack_payload == None, и приходится вытаскивать поля по байтовым
маркерам ключей: msgpack fixstr пишет ключ как [0xA0|len] + utf8, например
b"\\xa2id", b"\\xa4text", b"\\xa6sender".

Полностью портировано из рабочего test5.py.
"""
from __future__ import annotations

import datetime
import re

__all__ = [
    "read_int_after_key",
    "read_str_after_key",
    "find_long_token",
    "find_uuid",
    "extract_visible_utf8_strings",
    "extract_chat_ids_from_login_raw",
    "login_chats_count",
    "find_message_chunks",
    "parse_history_messages",
    "msgpack_array_len_after_key",
    "parse_sessions",
    "session_times",
    "ts_ms_to_iso",
]


def read_int_after_key(data: bytes, key: bytes) -> int | None:
    pos = data.find(key)
    if pos == -1:
        return None
    p = pos + len(key)
    if p >= len(data):
        return None

    typ = data[p]
    p += 1

    if typ == 0xD2 and p + 4 <= len(data):           # int32
        return int.from_bytes(data[p:p + 4], "big", signed=True)
    if typ == 0xD3 and p + 8 <= len(data):           # int64
        return int.from_bytes(data[p:p + 8], "big", signed=True)
    if typ == 0xCC and p + 1 <= len(data):           # uint8
        return data[p]
    if typ == 0xCD and p + 2 <= len(data):           # uint16
        return int.from_bytes(data[p:p + 2], "big")
    if typ == 0xCE and p + 4 <= len(data):           # uint32
        return int.from_bytes(data[p:p + 4], "big")
    if typ == 0xCF and p + 8 <= len(data):           # uint64
        return int.from_bytes(data[p:p + 8], "big")
    if 0x00 <= typ <= 0x7F:                          # positive fixint
        return typ
    if 0xE0 <= typ <= 0xFF:                          # negative fixint
        return typ - 256
    return None


def read_str_after_key(data: bytes, key: bytes) -> str | None:
    pos = data.find(key)
    if pos == -1:
        return None
    p = pos + len(key)
    if p >= len(data):
        return None

    typ = data[p]
    p += 1

    if 0xA0 <= typ <= 0xBF:                          # fixstr
        n = typ & 0x1F
    elif typ == 0xD9 and p < len(data):              # str8
        n = data[p]
        p += 1
    elif typ == 0xDA and p + 2 <= len(data):         # str16
        n = int.from_bytes(data[p:p + 2], "big")
        p += 2
    elif typ == 0xDB and p + 4 <= len(data):         # str32
        n = int.from_bytes(data[p:p + 4], "big")
        p += 4
    else:
        return None

    return data[p:p + n].decode("utf-8", errors="ignore").strip()


def find_long_token(data: bytes) -> str | None:
    """Самая длинная последовательность токен-символов (>100) — это auth/verify token."""
    valid = set(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-+.~="
    )
    best = None
    cur: list[str] = []

    for b in data:
        c = chr(b)
        if c in valid:
            cur.append(c)
        else:
            if len(cur) > 100:
                token = "".join(cur)
                if best is None or len(token) > len(best):
                    best = token
            cur = []

    if len(cur) > 100:
        token = "".join(cur)
        if best is None or len(token) > len(best):
            best = token
    return best


def find_uuid(data: bytes) -> str | None:
    m = re.search(
        rb"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        data,
    )
    return m.group(0).decode("utf-8") if m else None


def extract_visible_utf8_strings(data: bytes) -> list[str]:
    strings: list[str] = []

    for m in re.finditer(rb"[ -~]{4,}", data):
        s = m.group(0).decode("utf-8", errors="ignore").strip()
        if s:
            strings.append(s)

    buf = bytearray()
    for b in data:
        if b >= 0x80 or b in (0x0A, 0x0D, 0x20) or 48 <= b <= 57:
            buf.append(b)
        else:
            if len(buf) >= 6:
                s = bytes(buf).decode("utf-8", errors="ignore").strip()
                if len(s) >= 4:
                    strings.append(s)
            buf.clear()
    if len(buf) >= 6:
        s = bytes(buf).decode("utf-8", errors="ignore").strip()
        if len(s) >= 4:
            strings.append(s)

    out: list[str] = []
    seen: set[str] = set()
    for s in strings:
        s = re.sub(r"\s+", " ", s).strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def extract_chat_ids_from_login_raw(data: bytes) -> list[int]:
    ids: list[int] = []

    for m in re.finditer(
        rb"\xa2id([\xd2\xd3])(.{4,8}).{0,120}?(DIALOG|CHAT|CHANNEL)",
        data,
        re.DOTALL,
    ):
        typ = m.group(1)
        raw_num = m.group(2)
        try:
            if typ == b"\xd2":
                ids.append(int.from_bytes(raw_num[:4], "big", signed=True))
            else:
                ids.append(int.from_bytes(raw_num[:8], "big", signed=True))
        except Exception:
            pass

    for m in re.finditer(rb"\xa6chatId([\xd2\xd3])(.{4,8})", data, re.DOTALL):
        typ = m.group(1)
        raw_num = m.group(2)
        try:
            if typ == b"\xd2":
                ids.append(int.from_bytes(raw_num[:4], "big", signed=True))
            else:
                ids.append(int.from_bytes(raw_num[:8], "big", signed=True))
        except Exception:
            pass

    return list(dict.fromkeys(ids))


def login_chats_count(raw: bytes) -> int | None:
    marker = b"\xa5chats\xdc"
    pos = raw.find(marker)
    if pos != -1 and pos + len(marker) + 2 <= len(raw):
        return int.from_bytes(raw[pos + len(marker):pos + len(marker) + 2], "big")
    return None


def msgpack_array_len_after_key(raw: bytes, key: bytes) -> int | None:
    pos = raw.find(key)
    if pos == -1:
        return None
    p = pos + len(key)
    if p >= len(raw):
        return None
    typ = raw[p]
    if 0x90 <= typ <= 0x9F:                          # fixarray
        return typ & 0x0F
    if typ == 0xDC and p + 2 < len(raw):             # array16
        return int.from_bytes(raw[p + 1:p + 3], "big")
    if typ == 0xDD and p + 4 < len(raw):             # array32
        return int.from_bytes(raw[p + 1:p + 5], "big")
    return None


def find_message_chunks(raw: bytes) -> list[bytes]:
    start = raw.find(b"\xa8messages")
    if start == -1:
        return []
    region = raw[start:]

    patterns = [
        rb"\xde\x00[\x04-\x40]\xa2id",
        rb"\xde\x00[\x04-\x40].{0,12}\xa2id",
        rb"\xa2id[\xd2\xd3].{4,8}.{0,80}\xa4time",
    ]
    starts: list[int] = []
    for pat in patterns:
        for m in re.finditer(pat, region, re.DOTALL):
            starts.append(m.start())
    starts = sorted(set(starts))

    filtered: list[int] = []
    for s in starts:
        if not filtered or s - filtered[-1] > 20:
            filtered.append(s)

    chunks: list[bytes] = []
    for i, s in enumerate(filtered):
        e = filtered[i + 1] if i + 1 < len(filtered) else len(region)
        chunks.append(region[s:e])
    return chunks


_NOISE_EXACT = {
    "USER", "PHOTO", "CONTROL", "FORWARD", "LINK", "SHARE", "INLINE_KEYBOARD",
    "CLIPBOARD", "RIFF", "WEBPVP8", "reactionInfo", "previewData", "baseUrl",
    "photoToken",
}


def _looks_like_noise(s: str) -> bool:
    if not s:
        return True
    if s in _NOISE_EXACT:
        return True
    if s.startswith("https://i.oneme.ru") or s.startswith("BTEx"):
        return True
    if any(ord(ch) < 32 and ch not in "\n\r\t" for ch in s):
        return True
    return False


def _best_text_from_chunk(chunk: bytes) -> str | None:
    text = read_str_after_key(chunk, b"\xa4text")
    if text:
        return text
    title = read_str_after_key(chunk, b"\xa5title")
    if title:
        return title

    useful: list[str] = []
    for s in extract_visible_utf8_strings(chunk):
        s = re.sub(r"\s+", " ", s).strip()
        if len(s) < 3 or _looks_like_noise(s):
            continue
        if len(s) > 800 and not any("а" <= c.lower() <= "я" for c in s):
            continue
        useful.append(s)
    if not useful:
        return None
    useful.sort(key=len, reverse=True)
    return useful[0]


def parse_history_messages(chat_id: int, raw: bytes) -> list[dict]:
    """Грубый парс истории из сырого payload, когда msgpack не сработал."""
    messages: list[dict] = []
    for i, chunk in enumerate(find_message_chunks(raw), 1):
        messages.append(
            {
                "index": i,
                "chatId": chat_id,
                "id": read_int_after_key(chunk, b"\xa2id"),
                "cid": read_int_after_key(chunk, b"\xa3cid"),
                "time": read_int_after_key(chunk, b"\xa4time"),
                "type": read_str_after_key(chunk, b"\xa4type"),
                "sender": read_int_after_key(chunk, b"\xa6sender"),
                "event": read_str_after_key(chunk, b"\xa5event"),
                "text": _best_text_from_chunk(chunk),
                "attachmentType": read_str_after_key(chunk, b"\xa5_type"),
                "baseUrl": read_str_after_key(chunk, b"\xa7baseUrl"),
            }
        )
    return messages


def parse_sessions(raw: bytes) -> list[dict]:
    """Список сессий из SESSIONS_INFO (decoded обычно None).

    Сессия = {client, info, time, current, location}, id у сессии в протоколе
    НЕТ. Второе+ сессии используют ref-кодирование ключей, поэтому парсим
    best-effort: число из заголовка массива + видимые строки устройств/локаций +
    флаг current.
    """
    count = msgpack_array_len_after_key(raw, b"\xa8sessions") or 0
    cpos = raw.find(b"\xa7current\xc3")  # позиция флага текущей сессии

    # Клиент сессии всегда вида "MAX <платформа>"; запоминаем позиции.
    clients = [(m.start(), m.group(0).decode("ascii", "ignore"))
               for m in re.finditer(rb"MAX [A-Za-z]+", raw)]
    model_hits = [(m.start(), m.group(0).decode("utf-8", "ignore").strip(" ,"))
                  for m in re.finditer(
                      rb"(?:samsung|iPhone|iPad|Xiaomi|Redmi|Huawei|Honor|Pixel|"
                      rb"Windows|Mac|Linux)[^\x00-\x1f,]{0,32}", raw)]
    loc_hits = [(m.start(), m.group(0).decode("ascii", "ignore"))
                for m in re.finditer(rb"IP [0-9.]+", raw)]

    # Индекс текущей сессии = клиент с наибольшей позицией ДО флага current.
    current_idx = -1
    if cpos != -1:
        before = [i for i, (p, _) in enumerate(clients) if p < cpos]
        if before:
            current_idx = before[-1]

    n = count or max(len(clients), 1)
    out: list[dict] = []
    for i in range(n):
        client = clients[i][1] if i < len(clients) else f"Сессия {i + 1}"
        start = clients[i][0] if i < len(clients) else 0
        end = clients[i + 1][0] if i + 1 < len(clients) else len(raw)
        # Модель/локацию относим к сессии по позиции в её диапазоне.
        model = next((t for p, t in model_hits if start <= p < end), "")
        loc = next((t for p, t in loc_hits if start <= p < end), "")
        label = f"{client} · {model}" if model else client
        out.append({"label": label, "location": loc, "current": i == current_idx})
    return out


def session_times(raw: bytes, only_other: bool = True) -> list[int]:
    """Времена старта сессий — единственный идентификатор сессии (id в протоколе
    нет; закрытие в test5 шло по этому time через SESSIONS_CLOSE 97).

    only_other=True исключает ВРЕМЯ ТЕКУЩЕЙ сессии (флаг current:true), чтобы
    случайно не закрыть собственную сессию. Из-за ref-кодирования время чужих
    устройств литерально обычно НЕ читается, поэтому список часто пустой —
    тогда надёжный способ выкинуть другие устройства это смена пароля 2FA.
    """
    times: list[int] = []
    for m in re.finditer(b"\xa4time", raw):
        v = read_int_after_key(raw[m.start():m.start() + 20], b"\xa4time")
        if v:
            times.append(v)
    times = list(dict.fromkeys(times))
    if not only_other:
        return times

    # Время текущей сессии = ближайшее \xa4time ПЕРЕД флагом current:true.
    current_time = None
    cpos = raw.find(b"\xa7current\xc3")
    if cpos != -1:
        tpos = raw.rfind(b"\xa4time", 0, cpos)
        if tpos != -1:
            current_time = read_int_after_key(raw[tpos:tpos + 20], b"\xa4time")
    return [t for t in times if t != current_time]


def ts_ms_to_iso(value: int | None) -> str | None:
    if not value:
        return None
    try:
        if value > 10_000_000_000_000:
            value = value // 1000
        elif value > 10_000_000_000:
            pass
        else:
            return None
        return datetime.datetime.fromtimestamp(
            value / 1000, tz=datetime.timezone.utc
        ).isoformat()
    except Exception:
        return None
