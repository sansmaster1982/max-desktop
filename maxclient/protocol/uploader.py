"""Загрузка медиа в MAX.

Порядок действий сверен с официальным веб-клиентом web.max.ru (сборки 26.10.11
build 20935 и 26.11.8 build 22163; код загрузки в чанке CebawyPl.js, SHA256
4778811bafa55370db7186794b843b0ae5227ad6ad9d96f7a65e4115bd86a71b):

1. Опкод PHOTO_UPLOAD 80 отвечает {url}. VIDEO_UPLOAD 82 и FILE_UPLOAD 87 отвечают
   {info: [{url, videoId | fileId, token?}]}.
2. Фото уходит POST multipart/form-data с одним полем file. Ответ HTTP имеет вид
   {photos: {<id>: {token}}}; токен первой записи идёт в attach как photoToken.
3. Файл, видео и голосовое уходят POST сырым телом с заголовками Content-Type,
   Content-Disposition и Content-Range. Тело ответа не используется: идентификатор
   и токен уже пришли в ответе опкода.
4. Для файла, видео и голосового сообщение отправляется только после серверного
   уведомления NOTIF_ATTACH 136 с тем же идентификатором. Ожидание регистрируется
   до начала HTTP-загрузки, иначе быстрое уведомление можно пропустить.
5. attach в SEND_MESSAGE 64: PHOTO {photoToken}; FILE {fileId}; VIDEO {videoId,
   token}; AUDIO {audioId, token, duration}.
6. Временный обрыв основного соединения загрузку не отменяет: веб-клиент при
   потере сокета свои ожидания тоже не сбрасывает. Закрытие клиента, конец сессии
   и новый вход меняют поколение загрузок (MaxClient.upload_epoch), и начатая
   загрузка останавливается на ближайшем блоке тела.
"""
from __future__ import annotations

import io
import json
import mimetypes
import os
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, BinaryIO, Callable, NoReturn, Optional
from urllib.parse import quote

import requests

from .client import MaxClient
from .conversation_contracts import explicit_media_url
from .errors import MaxError, MaxNotConnected, MaxTimeout, MaxUploadError


class AttachKind(str, Enum):
    PHOTO = "PHOTO"
    VIDEO = "VIDEO"
    AUDIO = "AUDIO"        # голосовое сообщение
    VIDEO_MSG = "VIDEO_MSG"
    FILE = "FILE"


# Сколько секунд ждать NOTIF_ATTACH после успешной HTTP-загрузки. У официального
# веб-клиента предела нет; здесь он нужен, чтобы рабочий поток не ждал вечно.
ATTACH_READY_TIMEOUT = 90.0
# Видео сервер обрабатывает уже после приёма файла. Сколько это длится для больших
# роликов, не измерено, поэтому запас растёт с размером и ограничен сверху.
VIDEO_READY_SECONDS_PER_MB = 0.5
VIDEO_READY_TIMEOUT_MAX = 1800.0

# Поле идентификатора в ответе опкода и поле того же идентификатора в NOTIF_ATTACH.
# У голосового идентификатор приходит как videoId, а уведомление несёт audioId.
_ID_FIELD = {
    AttachKind.FILE: ("fileId", "fileId"),
    AttachKind.VIDEO: ("videoId", "videoId"),
    AttachKind.VIDEO_MSG: ("videoId", "videoId"),
    AttachKind.AUDIO: ("videoId", "audioId"),
}

# Числовые type и uploaderType опкода 82 из схемы веб-клиента. Видеофайл идёт без них.
_VIDEO_UPLOAD_ARGS = {
    AttachKind.VIDEO: {},
    AttachKind.VIDEO_MSG: {"media_type": 1, "uploader_type": 0},
    AttachKind.AUDIO: {"media_type": 2, "uploader_type": 0},
}

# Content-Type для расширений, которые клиент сам создаёт (голосовое .m4a) или
# предлагает в диалоге выбора видео. Без таблицы значение берётся из реестра Windows,
# а встроенного типа у .m4a и .mkv в Python 3.11 нет.
_CONTENT_TYPES = {
    ".m4a": "audio/mp4",
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".mkv": "video/x-matroska",
    ".webm": "video/webm",
}

_BLOCK = 64 * 1024
_STOPPED = "Загрузка остановлена: соединение с MAX закрыто."


@dataclass
class UploadResult:
    token: Optional[str] = None
    file_id: Optional[int] = None

    @property
    def ok(self) -> bool:
        return self.token is not None or self.file_id is not None


ProgressCb = Optional[Callable[[float], None]]


class _NoCredentials(requests.auth.AuthBase):
    """Без явного auth requests подставляет Authorization из netrc пользователя.

    Адрес загрузки подписан сам, посторонние учётные данные хосту загрузки не нужны.
    """
    def __call__(self, request):
        request.headers.pop("Authorization", None)
        request.headers.pop("Cookie", None)
        return request


class _Body:
    """Тело запроса: ровно size байт открытого файла.

    requests берёт длину из __len__ и читает тело блоками через read. На каждом
    блоке проверяется, не пора ли остановиться; исключение из read выходит из
    requests.post без обёртки.
    """
    def __init__(self, handle: BinaryIO, size: int, stopped: Callable[[], bool]) -> None:
        self._handle, self._size, self._left, self._stopped = handle, size, size, stopped

    def __len__(self) -> int:
        return self._size

    def __iter__(self):
        return iter(lambda: self.read(_BLOCK), b"")

    def read(self, amount: Optional[int] = -1) -> bytes:
        if self._stopped():
            raise MaxUploadError(_STOPPED)
        if self._left <= 0:
            return b""
        want = self._left if amount is None or amount < 0 else min(amount, self._left)
        try:
            block = self._handle.read(want)
        except OSError:
            raise MaxUploadError("Не удалось прочитать файл.") from None
        if not block:
            raise MaxUploadError("Файл изменился во время отправки. Повторите отправку.")
        self._left -= len(block)
        return block


def upload_and_build_attach(
    client: MaxClient,
    path: str,
    kind: AttachKind,
    *,
    duration_ms: Optional[int] = None,
    on_progress: ProgressCb = None,
    timeout: float = 120.0,
    profile: bool = False,
) -> dict:
    """Залить файл и вернуть готовый attach-словарь для SEND_MESSAGE.message.attaches.

    Бросает MaxUploadError на любой стадии сбоя.
    """
    if type(profile) is not bool or (profile and kind != AttachKind.PHOTO):
        raise ValueError("Profile upload is supported only for photos")
    if on_progress:
        on_progress(0.0)
    epoch = _epoch(client)
    # Файл открывается один раз и до запроса опкода: размер, Content-Range и тело
    # берутся из одного дескриптора, а недоступный файл не расходует адрес загрузки.
    try:
        handle = open(path, "rb")
    except (OSError, TypeError, ValueError):
        _fail(client, kind, "local", "Файл не найден или недоступен.")
    with handle:
        try:
            size = os.fstat(handle.fileno()).st_size
        except OSError:
            _fail(client, kind, "local", "Не удалось прочитать файл.")
        if size == 0:
            _fail(client, kind, "local", "Файл пустой, отправить его нельзя.")
        if kind == AttachKind.PHOTO:
            result = _upload_photo(client, handle, path, epoch, on_progress, timeout, profile)
        else:
            result = _upload_raw(client, handle, path, size, kind, epoch, on_progress, timeout)
    if _epoch(client) != epoch:
        # Вложение загружено в сессии, которой уже нет: в другую оно уйти не должно.
        _fail(client, kind, "stopped", _STOPPED)
    if on_progress:
        on_progress(1.0)
    return build_attach(kind, result, duration_ms=duration_ms)


def _upload_photo(client: MaxClient, handle: BinaryIO, path: str, epoch: Any, on_progress: ProgressCb,
                  timeout: float, profile: bool) -> UploadResult:
    kind = AttachKind.PHOTO
    op_resp = _ask(client, kind, lambda: client.request_photo_upload(count=1, profile=profile))
    url = (op_resp.get("url") if isinstance(op_resp, dict) else None) if profile else _extract_upload_url(op_resp)
    if not url:
        _fail(client, kind, "target", "Сервер не выдал адрес загрузки.", op_resp)
    _require_https(client, kind, url)
    if _epoch(client) != epoch:
        _fail(client, kind, "stopped", _STOPPED)
    if on_progress:
        on_progress(0.15)

    # Имя части служебное: сервер его не использует, а кириллица в заголовке
    # multipart кодируется по-разному в разных версиях urllib3. Тело собирает сам
    # requests, как и раньше; отдельная подготовка нужна, чтобы отправка шла через
    # _Body и останавливалась вместе с сессией.
    try:
        prepared = requests.Request("POST", url, files={"file": (_photo_part_name(path), handle)}).prepare()
        body = prepared.body if isinstance(prepared.body, bytes) else b""
        content_type = prepared.headers.get("Content-Type")
    except requests.RequestException:
        _fail(client, kind, "target", "Сервер не выдал защищённый адрес загрузки.")
    except OSError:
        _fail(client, kind, "local", "Не удалось прочитать файл.")
    if not body or not content_type:
        _fail(client, kind, "local", "Не удалось подготовить фото к отправке.")
    try:
        resp = requests.post(url, data=_Body(io.BytesIO(body), len(body), lambda: _epoch(client) != epoch),
                             headers={"Content-Type": content_type}, timeout=timeout,
                             allow_redirects=False, auth=_NoCredentials())
    except requests.RequestException:
        _fail(client, kind, "http", "Сетевая ошибка загрузки файла.")
    except MaxUploadError:
        _log(client, "upload PHOTO failed: stage=body")
        raise
    if on_progress:
        on_progress(0.7)

    _log(client, f"upload PHOTO: http {resp.status_code}")
    if not (200 <= resp.status_code < 300):
        _fail(client, kind, "http", f"HTTP {resp.status_code}")
    parsed = _extract_profile_photo_token(resp.text) if profile else _extract_token(resp.text)
    # В сообщение фото идёт только с photoToken, поэтому идентификатора без токена мало.
    if not (isinstance(parsed.token, str) and parsed.token):
        _fail(client, kind, "answer", "Сервер не подтвердил результат загрузки.", _json_or_none(resp.text))
    return UploadResult(token=parsed.token)


def _upload_raw(client: MaxClient, handle: BinaryIO, path: str, size: int, kind: AttachKind, epoch: Any,
                on_progress: ProgressCb, timeout: float) -> UploadResult:
    """Файл, видео, голосовое: сырое тело и ожидание NOTIF_ATTACH."""
    # Заголовки собираются до запроса опкода: первый вызов mimetypes на Windows читает
    # реестр, а между ответом опкода и регистрацией ожидания пауз быть не должно.
    headers = {
        "Content-Type": _content_type(path),
        "Content-Disposition": "attachment; filename=" + _encode_uri_component(os.path.basename(path)),
        "Content-Range": f"bytes 0-{size - 1}/{size}",
    }
    if kind == AttachKind.FILE:
        op_resp = _ask(client, kind, lambda: client.request_file_upload(count=1))
    else:
        op_resp = _ask(client, kind, lambda: client.request_video_upload(count=1, **_VIDEO_UPLOAD_ARGS[kind]))
    answer_field, ready_field = _ID_FIELD[kind]
    target = _upload_target(op_resp, answer_field)
    if target is None or (kind != AttachKind.FILE and target[2] is None):
        _fail(client, kind, "target", "Сервер не выдал адрес загрузки.", op_resp)
    url, ident, token = target
    _require_https(client, kind, url)

    def stopped() -> bool:
        return _epoch(client) != epoch

    # Регистрация до HTTP: уведомление может прийти раньше ответа на загрузку.
    try:
        waiter = client.expect_attach_ready(ready_field, ident, epoch=epoch)
    except MaxUploadError:
        _log(client, f"upload {kind.value} failed: stage=stopped")
        raise
    try:
        if on_progress:
            on_progress(0.15)
        try:
            resp = requests.post(url, data=_Body(handle, size, stopped), headers=headers, timeout=timeout,
                                 allow_redirects=False, auth=_NoCredentials())
        except requests.RequestException:
            _fail(client, kind, "http", "Сетевая ошибка загрузки файла.")
        except MaxUploadError:
            _log(client, f"upload {kind.value} failed: stage=body")
            raise
        # Тело ушло: файл пользователя больше не нужен. Иначе он оставался бы занят на
        # всё ожидание уведомления, и его нельзя было бы переименовать или удалить.
        handle.close()
        if on_progress:
            on_progress(0.7)
        _log(client, f"upload {kind.value}: http {resp.status_code}")
        _check_raw_answer(client, kind, resp, size)
        try:
            waiter.wait(_ready_timeout(kind, size))
        except MaxUploadError:
            _log(client, f"upload {kind.value} failed: stage=ready")
            raise
    finally:
        waiter.cancel()
    _log(client, f"upload {kind.value}: ready")
    return UploadResult(token=token, file_id=ident)


def _ready_timeout(kind: AttachKind, size: int) -> float:
    if kind in (AttachKind.VIDEO, AttachKind.VIDEO_MSG):
        megabytes = size / (1024 * 1024)
        return min(VIDEO_READY_TIMEOUT_MAX, ATTACH_READY_TIMEOUT + megabytes * VIDEO_READY_SECONDS_PER_MB)
    return ATTACH_READY_TIMEOUT


def _check_raw_answer(client: MaxClient, kind: AttachKind, resp, size: int) -> None:
    """Разбор статуса по образцу веб-клиента.

    200 и 201 годятся, если сервер не сообщил, что принял меньше байт, чем было в
    файле; 201 без названного последнего байта веб-клиент считает сорванной докачкой.
    """
    status = resp.status_code
    if status not in (200, 201):
        _fail(client, kind, "http", f"HTTP {status}")
    headers = getattr(resp, "headers", None)
    last = _last_known_byte(headers)
    if last is None and status == 201:
        # Прежняя терпимость к Range вида "bytes 0-N/M" сохранена только здесь: при 200
        # веб-клиент такой заголовок не читает и считает загрузку завершённой.
        last = _last_known_byte(headers, bytes_prefix=True)
    if (last is not None and last + 1 < size) or (last is None and status == 201):
        _fail(client, kind, "http", "Сервер принял файл не полностью. Повторите отправку.")
    answer = _json_or_none(getattr(resp, "text", ""))
    if isinstance(answer, dict) and "error_code" in answer:
        _fail(client, kind, "answer", "Сервер отклонил загрузку.", answer)


def build_attach(kind: AttachKind, res: UploadResult, *, duration_ms: Optional[int] = None) -> dict:
    """Собрать attach-словарь в том виде, в каком его шлёт официальный веб-клиент."""
    if kind == AttachKind.PHOTO:
        attach: dict[str, Any] = {"_type": "PHOTO"}
        if res.token:
            attach["photoToken"] = res.token
        return attach

    if kind in (AttachKind.VIDEO, AttachKind.VIDEO_MSG):
        # Видеосообщение в схеме запроса тоже VIDEO, его отличает videoType 1.
        attach = {"_type": "VIDEO"}
        if res.file_id is not None:
            attach["videoId"] = res.file_id
        if res.token:
            attach["token"] = res.token
        if kind == AttachKind.VIDEO_MSG:
            attach["videoType"] = 1
        if duration_ms is not None:
            attach["duration"] = duration_ms
        return attach

    if kind == AttachKind.AUDIO:
        attach = {"_type": "AUDIO"}
        if res.file_id is not None:
            attach["audioId"] = res.file_id
        if res.token:
            attach["token"] = res.token
        if duration_ms is not None:
            attach["duration"] = duration_ms
        return attach

    # FILE: новый файл идёт одним fileId, токен в сообщение не кладётся.
    attach = {"_type": "FILE"}
    if res.file_id is not None:
        attach["fileId"] = res.file_id
    elif res.token:
        attach["token"] = res.token
    return attach


def _ask(client: MaxClient, kind: AttachKind, request: Callable[[], Any]) -> Any:
    """Запрос адреса загрузки. Сбой связи превращается в понятный отказ загрузки."""
    try:
        return request()
    except MaxNotConnected:
        _fail(client, kind, "request", "Нет соединения с MAX. Повторите отправку, когда связь восстановится.")
    except MaxTimeout:
        _fail(client, kind, "request", "Сервер не ответил на запрос загрузки. Повторите отправку.")
    except MaxUploadError:
        raise
    except MaxError as error:
        # Текст отказа сервера уже ограничен набором [A-Za-z0-9_.-] в MaxFrame.error_text.
        _log(client, f"upload {kind.value} failed: stage=request")
        raise MaxUploadError(f"Сервер отклонил запрос загрузки ({error}).") from None


def _upload_target(resp: Any, id_field: str) -> Optional[tuple[str, int, Optional[str]]]:
    """Адрес, идентификатор и токен из ответа опкодов 82 и 87: info[0]."""
    info = resp.get("info") if isinstance(resp, dict) else None
    first = info[0] if isinstance(info, list) and info else None
    if not isinstance(first, dict):
        return None
    url, ident, token = first.get("url"), _as_ident(first.get(id_field)), first.get("token")
    if not isinstance(url, str) or not url or ident is None:
        return None
    return url, ident, token if isinstance(token, str) and token else None


def _extract_upload_url(resp: Any) -> Optional[str]:
    """Адрес загрузки фото: {url}. Остальные формы оставлены как запасной разбор."""
    if not isinstance(resp, dict):
        return None
    for key in ("url", "uploadUrl", "endpoint"):
        v = resp.get(key)
        if isinstance(v, str) and v:
            return v
    for list_key in ("urls", "upload", "uploads"):
        lst = resp.get(list_key)
        if isinstance(lst, list) and lst:
            first = lst[0]
            if isinstance(first, str) and first:
                return first
            if isinstance(first, dict):
                for k in ("url", "uploadUrl", "endpoint"):
                    v = first.get(k)
                    if isinstance(v, str) and v:
                        return v
    for wrap_key in ("result", "data", "response"):
        w = resp.get(wrap_key)
        if isinstance(w, dict):
            nested = _extract_upload_url({str(k): v for k, v in w.items()})
            if nested:
                return nested
    return None


def _extract_token(body: str) -> UploadResult:
    """Разобрать ответ HTTP-загрузки фото.

    Основная форма: {photos: {<id>: {token}}}; список записей принимается так же.
    Тело не в JSON токеном не считается: веб-клиент называет это unknown response.
    Токеном считается только непустая строка.
    """
    decoded = _json_or_none(body)
    if not isinstance(decoded, dict) or "error_code" in decoded:
        return UploadResult()
    m = {str(k): v for k, v in decoded.items()}

    photo_token = _photos_token(m.get("photos"))
    if photo_token is not None:
        return UploadResult(token=photo_token)

    token: Optional[str] = None
    file_id: Optional[int] = None
    for k in ("photoToken", "token", "videoId", "fileId", "id"):
        v = m.get(k)
        if v is None:
            continue
        if token is None and k in ("photoToken", "token"):
            token = _text_token(v)
        if file_id is None and k in ("videoId", "fileId", "id"):
            file_id = _as_int(v)
    if token is not None or file_id is not None:
        return UploadResult(token=token, file_id=file_id)

    result = m.get("result")
    if isinstance(result, dict):
        r = {str(k): v for k, v in result.items()}
        tokens = r.get("tokens")
        if isinstance(tokens, list) and tokens:
            return UploadResult(token=_text_token(tokens[0]))
        rid = r.get("id")
        if isinstance(rid, (int, float)):
            return UploadResult(file_id=int(rid))
        uf = r.get("uploadedFiles")
        if isinstance(uf, list) and uf and isinstance(uf[0], dict):
            first = {str(k): v for k, v in uf[0].items()}
            fid = first.get("fileId", first.get("id"))
            return UploadResult(token=_text_token(first.get("token")), file_id=_as_int(fid))
    return UploadResult()


def _text_token(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and value else None


def _extract_profile_photo_token(body: str) -> UploadResult:
    """Фото профиля: только строковый токен из photos, плоские формы не принимаются."""
    decoded = _json_or_none(body)
    if not isinstance(decoded, dict) or "error_code" in decoded:
        return UploadResult()
    return UploadResult(token=_photos_token(decoded.get("photos")))


def _photos_token(photos: Any) -> Optional[str]:
    """Токен первой записи photos. Веб-клиент обходит photos как пары ключ-значение,
    поэтому годятся и объект с ключом-идентификатором, и список."""
    if isinstance(photos, dict):
        items = list(photos.values())
    elif isinstance(photos, list):
        items = photos
    else:
        return None
    for item in items:
        token = item.get("token") if isinstance(item, dict) else None
        if isinstance(token, str) and token:
            return token
    return None


def _require_https(client: MaxClient, kind: AttachKind, url: Any) -> None:
    """Та же проверка вида адреса, что и для скачивания: только https на порт 443,
    имя хоста с точкой, без логина, IP-адреса, пробелов и управляющих символов."""
    if explicit_media_url(url) is None:
        _fail(client, kind, "target", "Сервер не выдал защищённый адрес загрузки.")


def _photo_part_name(path: str) -> str:
    extension = os.path.splitext(path)[1].lower()
    return "image" + extension if re.fullmatch(r"\.[a-z0-9]{1,5}", extension) else "image.jpg"


def _content_type(path: str) -> str:
    known = _CONTENT_TYPES.get(os.path.splitext(path)[1].lower())
    return known or mimetypes.guess_type(path)[0] or "application/octet-stream"


def _encode_uri_component(text: str) -> str:
    """То же, что encodeURIComponent в браузере: имя файла в Content-Disposition."""
    return quote(text, safe="-_.!~*'()", errors="replace")


def _last_known_byte(headers: Any, *, bytes_prefix: bool = False) -> Optional[int]:
    """Последний принятый байт, как его читает веб-клиент (функция nE бандла):
    X-Last-Known-Byte, иначе первая часть Range, которая начинается с нуля: 0-N,
    0-N/M, список через запятую. bytes_prefix допускает ещё и "bytes 0-N/M"."""
    getter = getattr(headers, "get", None)
    if not callable(getter):
        return None
    known = _decimal(getter("X-Last-Known-Byte"))
    if known is not None:
        return known
    ranges = getter("Range")
    if not isinstance(ranges, str):
        return None
    for part in ranges.split(","):
        pieces = part.split("/")[0].split("-")
        start = pieces[0].strip()
        if bytes_prefix and start.lower().startswith("bytes"):
            start = start[5:]
        if _decimal(start) == 0:
            return _decimal(pieces[1]) if len(pieces) > 1 else None
    return None


def _decimal(value: Any) -> Optional[int]:
    if isinstance(value, str) and re.fullmatch(r"\d{1,18}", value.strip(), re.ASCII):
        return int(value.strip())
    return None


def _json_or_none(body: Any) -> Any:
    try:
        return json.loads(body.strip())
    except (ValueError, TypeError, AttributeError, RecursionError):
        return None


def _as_ident(value: Any) -> Optional[int]:
    """Идентификатор вложения: целое число, без приведения строк и логических значений."""
    if type(value) is int:
        return value
    if type(value) is float and value.is_integer():
        return int(value)
    return None


def _as_int(v: Any) -> Optional[int]:
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, (int, float)):
        return int(v)
    try:
        return int(str(v))
    except (TypeError, ValueError):
        return None


def _epoch(client: Any) -> Any:
    """Поколение загрузок клиента. У простых двойников клиента его нет."""
    return getattr(client, "upload_epoch", 0)


_SHAPE_LIMIT = 300


def _shape(value: Any, depth: int = 0, *, keys_are_data: bool = False) -> str:
    """Ключи и типы ответа без значений: адреса, токены и имена в журнал не попадают.

    Имена полей печатаются как есть: по ним видно, что сервер сменил формат ответа.
    Ключи словаря photos имён не несут, там ключом служит идентификатор фото.
    """
    if isinstance(value, dict):
        if depth >= 3:
            return "{...}"
        parts = []
        for key in list(value)[:8]:
            named = isinstance(key, str) and not keys_are_data and re.fullmatch(r"[A-Za-z_]{1,24}", key)
            parts.append(f"{key if named else '<key>'}:{_shape(value[key], depth + 1, keys_are_data=key == 'photos')}")
        return "{" + ",".join(parts) + "}"
    if isinstance(value, list):
        if depth >= 3:
            return "[...]"
        return "[" + (_shape(value[0], depth + 1) if value else "") + "]"
    return type(value).__name__


def _log(client: Any, message: str) -> None:
    debug = getattr(client, "_debug", None)
    if callable(debug):
        try:
            debug(message)
        except Exception:
            pass


def _fail(client: Any, kind: AttachKind, stage: str, message: str, answer: Any = None) -> NoReturn:
    """Записать в журнал стадию отказа (и форму ответа, если она есть) и бросить ошибку."""
    shape = f" shape={_shape(answer)[:_SHAPE_LIMIT]}" if answer is not None else ""
    _log(client, f"upload {kind.value} failed: stage={stage}{shape}")
    raise MaxUploadError(message) from None
