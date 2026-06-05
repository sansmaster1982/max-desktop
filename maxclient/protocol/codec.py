"""Упаковка/распаковка кадров протокола MAX.

Кадр (10-байтный заголовок + msgpack-тело):
    [0]   PROTO_VER  (10)
    [1]   cmd        (в запросе 0; в ответе: 1=ok, 3=invalid/expired, ...)
    [2-3] seq        big-endian uint16
    [4-5] opcode     big-endian uint16
    [6-9] длина тела big-endian uint32 (старший байт — флаги, длина в 3 младших)

Эвристика распаковки повторяет test5.py: MAX иногда кладёт 1-4 служебных
байта перед обычным msgpack, поэтому пробуем несколько смещений. Большие
LOGIN/HISTORY payload содержат compact/ref encoding и обычным msgpack не
парсятся — тогда decoded=None, и работают raw_parsers.
"""
from __future__ import annotations

import msgpack

PROTO_VER = 10
HEADER_LEN = 10


def pack_payload(obj: dict) -> bytes:
    return msgpack.packb(obj, use_bin_type=True)


def _pairs_hook(pairs):
    """Терпимый сборщик map: нехэшируемые ключи (dict/list/bytes) -> str.
    Повторяет устойчивость клиента MAX к нестандартным ключам."""
    out = {}
    for k, v in pairs:
        if isinstance(k, (dict, list, bytes, bytearray)):
            k = str(k)
        out[k] = v
    return out


def unpack_payload(data: bytes):
    """Вернуть распакованный msgpack или None, если тело не парсится целиком.

    Клиент MAX (класс kua) читает строки с CodingErrorAction.REPLACE — то есть
    НЕ падает на битом UTF-8. Стандартный Python-msgpack по умолчанию падает,
    поэтому маленькие ответы (push нового сообщения, профиль, контакты) раньше
    не декодировались. unicode_errors="replace" + терпимый object_pairs_hook
    воспроизводят поведение клиента.
    """
    if not data:
        return None
    for offset in (0, 1, 2, 3, 4):
        if offset >= len(data):
            break
        try:
            return msgpack.unpackb(
                data[offset:],
                raw=False,
                strict_map_key=False,
                unicode_errors="replace",
                object_pairs_hook=_pairs_hook,
            )
        except Exception:
            continue
    return None


def build_frame(seq: int, opcode: int, payload: dict, cmd: int = 0) -> bytes:
    """Собрать кадр для отправки."""
    body = pack_payload(payload)
    header = bytearray(HEADER_LEN)
    header[0] = PROTO_VER
    header[1] = cmd & 0xFF
    header[2] = (seq >> 8) & 0xFF
    header[3] = seq & 0xFF
    header[4] = (opcode >> 8) & 0xFF
    header[5] = opcode & 0xFF

    length = len(body)
    header[6] = (length >> 24) & 0xFF
    header[7] = (length >> 16) & 0xFF
    header[8] = (length >> 8) & 0xFF
    header[9] = length & 0xFF
    return bytes(header) + body


def parse_header(header: bytes) -> tuple[int, int, int, int, int, int]:
    """Разобрать 10-байтный заголовок.

    Возвращает (ver, cmd, seq, opcode, payload_len, comp).
    comp (старший байт поля длины, header[6]) — флаг сжатия тела:
      0 = без сжатия, 0xFF(-1) = zstd, >0 = LZ4 (см. lp.java:172-191 в декомпиле).
    """
    if len(header) < HEADER_LEN:
        raise ValueError("header too short")
    ver = header[0]
    cmd = header[1]
    seq = (header[2] << 8) | header[3]
    opcode = (header[4] << 8) | header[5]
    comp = header[6]
    payload_len = (header[7] << 16) | (header[8] << 8) | header[9]
    return ver, cmd, seq, opcode, payload_len, comp


def lz4_block_decompress(data: bytes) -> bytes:
    """Распаковка сырого LZ4-block (без framing/magic), как в net.jpountz.lz4.

    safeDecompressor у MAX вызывается с известным destLen, но потоковый разбор
    до конца входа даёт тот же результат и не требует знать размер заранее.
    Матчи могут перекрываться (offset < length) — копируем по байту.
    """
    out = bytearray()
    i = 0
    n = len(data)
    while i < n:
        token = data[i]
        i += 1
        lit = token >> 4
        if lit == 15:
            while i < n:
                b = data[i]
                i += 1
                lit += b
                if b != 255:
                    break
        out += data[i:i + lit]
        i += lit
        if i >= n:
            break
        if i + 2 > n:
            break
        offset = data[i] | (data[i + 1] << 8)  # little-endian
        i += 2
        if offset == 0 or offset > len(out):
            break
        mlen = (token & 0x0F) + 4
        if (token & 0x0F) == 15:
            while i < n:
                b = data[i]
                i += 1
                mlen += b
                if b != 255:
                    break
        start = len(out) - offset
        if offset >= mlen:
            # неперекрывающийся матч — копируем срезом (быстро)
            out += out[start:start + mlen]
        else:
            # перекрытие (offset < length) — только побайтно
            for j in range(mlen):
                out.append(out[start + j])
    return bytes(out)


def decompress_body(body: bytes, comp: int) -> bytes:
    """Распаковать тело кадра по флагу сжатия из заголовка."""
    if not body or comp == 0:
        return body
    if comp == 0xFF:  # zstd
        try:
            import zstandard
            return zstandard.ZstdDecompressor().decompress(body)
        except Exception:
            return body  # zstd недоступен — отдаём как есть (редкий путь)
    # comp > 0 -> LZ4
    try:
        result = lz4_block_decompress(body)
        return result if result else body
    except Exception:
        return body


def raw_ascii(data: bytes) -> str:
    """ASCII-дамп для отладки сырых ответов."""
    return "".join(chr(b) if 32 <= b <= 126 else "." for b in data)
