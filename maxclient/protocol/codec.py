"""Упаковка/распаковка кадров протокола MAX.

Кадр (10-байтный заголовок + msgpack-тело):
    [0]   PROTO_VER  (10)
    [1]   cmd        (в запросе 0; в ответе: 1=ok, 3=invalid/expired, ...)
    [2-3] seq        big-endian uint16
    [4-5] opcode     big-endian uint16
    [6-9] длина тела big-endian uint32 (старший байт — флаги, длина в 3 младших)

Тело WEB/native binary10 начинается непосредственно с MessagePack и содержит
ровно одно значение. Основание: WEB_BINARY_ENVELOPE_EVIDENCE.md и сохранённые
native reader/decoder Evidence. Неподтверждённые prefix-эвристики не применяются.
"""
from __future__ import annotations

import msgpack

PROTO_VER = 10
HEADER_LEN = 10
MAX_PAYLOAD_BYTES = 4 * 1024 * 1024


def _ext_hook(code, data):
    """MAX's code-1 integer wrapper contains one complete MsgPack scalar."""
    if code == 1 and len(data) <= 16:
        try:
            number = msgpack.unpackb(data, raw=False, strict_map_key=False)
            if type(number) is int:
                return number
        except (ValueError, TypeError, msgpack.UnpackException):
            pass
    return msgpack.ExtType(code, data)


def pack_payload(obj: dict) -> bytes:
    return msgpack.packb(obj, use_bin_type=True)


def _pairs_hook(pairs):
    """Preserve scalar keys, including MAX's decoded int64 extension keys."""
    out = {}
    for k, v in pairs:
        if isinstance(k, (dict, list, bytearray)):
            raise ValueError('Unsupported MessagePack map key')
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
    if not data or len(data) > MAX_PAYLOAD_BYTES:
        return None
    try:
        return msgpack.unpackb(
                data,
                raw=False,
                strict_map_key=False,
                unicode_errors="replace",
                object_pairs_hook=_pairs_hook,
                ext_hook=_ext_hook,
                max_str_len=MAX_PAYLOAD_BYTES,
                max_bin_len=MAX_PAYLOAD_BYTES,
                max_array_len=100000,
                max_map_len=100000,
                max_ext_len=MAX_PAYLOAD_BYTES,
        )
    except (ValueError, TypeError, msgpack.UnpackException):
        return None
    return None


def build_frame(seq: int, opcode: int, payload: dict | None, cmd: int = 0) -> bytes:
    """Собрать кадр для отправки."""
    # Official SS omits the body for an undefined/null payload (e.g. QR_GET288).
    body = b"" if payload is None else pack_payload(payload)
    if len(body) > MAX_PAYLOAD_BYTES:
        raise ValueError("Request payload exceeds the size limit")
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
      0 = без сжатия, 1..255 = LZ4 expansion budget в WEB/native binary10.
    Старый Android Zstd-вариант выбирается отдельно, а не по одному байту.
    """
    if len(header) < HEADER_LEN:
        raise ValueError("header too short")
    ver = header[0]
    cmd = header[1]
    seq = (header[2] << 8) | header[3]
    opcode = (header[4] << 8) | header[5]
    comp = header[6]
    payload_len = (header[7] << 16) | (header[8] << 8) | header[9]
    if ver != PROTO_VER or payload_len > MAX_PAYLOAD_BYTES:
        raise ValueError("Unsupported protocol header or payload size")
    return ver, cmd, seq, opcode, payload_len, comp


def lz4_block_decompress(data: bytes, *, max_output_size: int | None = None) -> bytes:
    """Распаковка сырого LZ4-block (без framing/magic), как в net.jpountz.lz4.

    safeDecompressor у MAX вызывается с известным destLen, но потоковый разбор
    до конца входа даёт тот же результат и не требует знать размер заранее.
    Матчи могут перекрываться (offset < length) — копируем по байту.
    """
    if len(data) > MAX_PAYLOAD_BYTES:
        raise ValueError("Compressed payload exceeds the size limit")
    limit = MAX_PAYLOAD_BYTES if max_output_size is None else min(MAX_PAYLOAD_BYTES, max_output_size)
    out = bytearray()
    i = 0
    n = len(data)
    while i < n:
        token = data[i]
        i += 1
        lit = token >> 4
        if lit == 15:
            while True:
                if i >= n:
                    raise ValueError("Truncated LZ4 literal length")
                b = data[i]
                i += 1
                lit += b
                if b != 255:
                    break
        if lit > n - i or len(out) + lit > limit:
            raise ValueError("Invalid or oversized LZ4 literal")
        out += data[i:i + lit]
        i += lit
        if i >= n:
            break
        if i + 2 > n:
            raise ValueError("Truncated LZ4 offset")
        offset = data[i] | (data[i + 1] << 8)  # little-endian
        i += 2
        if offset == 0 or offset > len(out):
            raise ValueError("Invalid LZ4 offset")
        mlen = (token & 0x0F) + 4
        if (token & 0x0F) == 15:
            while True:
                if i >= n:
                    raise ValueError("Truncated LZ4 match length")
                b = data[i]
                i += 1
                mlen += b
                if b != 255:
                    break
        start = len(out) - offset
        if len(out) + mlen > limit:
            raise ValueError("Decompressed payload exceeds the size limit")
        if offset >= mlen:
            # неперекрывающийся матч — копируем срезом (быстро)
            out += out[start:start + mlen]
        else:
            # перекрытие (offset < length) — только побайтно
            for j in range(mlen):
                out.append(out[start + j])
    return bytes(out)


def decompress_body(body: bytes, comp: int, *, legacy_android_zstd: bool = False) -> bytes:
    """Распаковать тело кадра по флагу сжатия из заголовка."""
    if len(body) > MAX_PAYLOAD_BYTES:
        raise ValueError("Payload exceeds the size limit")
    if type(comp) is not int or not 0 <= comp <= 255:
        raise ValueError('Invalid compression coefficient')
    if not body or comp == 0:
        return body
    if comp == 0xFF and legacy_android_zstd:
        import zstandard
        size = zstandard.frame_content_size(body)
        if size not in (zstandard.CONTENTSIZE_UNKNOWN, zstandard.CONTENTSIZE_ERROR) and size > MAX_PAYLOAD_BYTES:
            raise ValueError("Decompressed payload exceeds the size limit")
        result = zstandard.ZstdDecompressor().decompress(body, max_output_size=MAX_PAYLOAD_BYTES)
        if len(result) > MAX_PAYLOAD_BYTES:
            raise ValueError("Decompressed payload exceeds the size limit")
        return result
    # Coefficient is an upper allocation bound, not an exact output length.
    return lz4_block_decompress(body, max_output_size=len(body) * comp)


def raw_ascii(data: bytes) -> str:
    """ASCII-дамп для отладки сырых ответов."""
    return "".join(chr(b) if 32 <= b <= 126 else "." for b in data)
