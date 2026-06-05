"""Загрузка медиа в MAX — двухступенчатая схема.

1. Опкод (PHOTO_UPLOAD 80 / VIDEO_UPLOAD 82 / FILE_UPLOAD 87) -> сервер
   возвращает HTTP upload-URL.
2. Клиент POST'ит файл (multipart) на этот URL -> сервер возвращает
   photoToken / token / fileId.
3. Токен кладётся в attach внутри MSG_SEND (опкод 64).

Точные имена полей в ответе опкода и в HTTP-ответе в декомпиле зафиксированы
неполностью (docs/MEDIA_OPCODES.md: «поля точно не зафиксированы»), поэтому
извлечение URL и токена перебирает известные варианты ключей — логика
портирована из upload_repository.dart. Если сервер сменит формат, правки нужны
только здесь.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Optional

import requests

from .client import MaxClient
from .errors import MaxUploadError


class AttachKind(str, Enum):
    PHOTO = "PHOTO"
    VIDEO = "VIDEO"
    AUDIO = "AUDIO"        # голосовое сообщение
    VIDEO_MSG = "VIDEO_MSG"
    FILE = "FILE"


@dataclass
class UploadResult:
    token: Optional[str] = None
    file_id: Optional[int] = None

    @property
    def ok(self) -> bool:
        return self.token is not None or self.file_id is not None


ProgressCb = Optional[Callable[[float], None]]


def upload_and_build_attach(
    client: MaxClient,
    path: str,
    kind: AttachKind,
    *,
    duration_ms: Optional[int] = None,
    on_progress: ProgressCb = None,
    timeout: float = 120.0,
) -> dict:
    """Залить файл и вернуть готовый attach-словарь для MSG_SEND.message.attaches.

    Бросает MaxUploadError на любой стадии сбоя.
    """
    if on_progress:
        on_progress(0.0)
    if not os.path.isfile(path):
        raise MaxUploadError(f"файл не найден: {path}")
    if os.path.getsize(path) == 0:
        raise MaxUploadError("файл нулевого размера")

    # 1. Запрос upload-URL нужным опкодом.
    if kind == AttachKind.PHOTO:
        op_resp = client.request_photo_upload(count=1, profile=False)
    elif kind in (AttachKind.VIDEO, AttachKind.VIDEO_MSG):
        op_resp = client.request_video_upload(type_="VIDEO", count=1, uploader_type="VIDEO")
    elif kind == AttachKind.AUDIO:
        op_resp = client.request_video_upload(type_="AUDIO", count=1, uploader_type="AUDIO")
    else:  # FILE
        op_resp = client.request_file_upload(count=1)

    url = _extract_upload_url(op_resp)
    if not url:
        raise MaxUploadError(f"upload URL не найден в ответе опкода: {op_resp!r}")
    if on_progress:
        on_progress(0.15)

    # 2. HTTP multipart POST.
    try:
        with open(path, "rb") as fh:
            files = {"file": (os.path.basename(path), fh)}
            resp = requests.post(url, files=files, timeout=timeout)
    except requests.RequestException as e:
        raise MaxUploadError(f"HTTP send failed: {e}") from e
    if on_progress:
        on_progress(0.7)

    if not (200 <= resp.status_code < 300):
        raise MaxUploadError(f"HTTP {resp.status_code}")

    parsed = _extract_token(resp.text)
    if not parsed.ok:
        raise MaxUploadError(f"токен не найден в ответе upload: {resp.text[:200]!r}")
    if on_progress:
        on_progress(1.0)

    return build_attach(kind, parsed, duration_ms=duration_ms)


def build_attach(kind: AttachKind, res: UploadResult, *, duration_ms: Optional[int] = None) -> dict:
    """Собрать attach-словарь под формат MSG_SEND (docs/MEDIA_OPCODES.md)."""
    if kind == AttachKind.PHOTO:
        attach: dict[str, Any] = {"_type": "PHOTO"}
        if res.token:
            attach["photoToken"] = res.token
        return attach

    if kind in (AttachKind.VIDEO, AttachKind.VIDEO_MSG):
        attach = {"_type": "VIDEO_MSG" if kind == AttachKind.VIDEO_MSG else "VIDEO"}
        if res.token:
            attach["token"] = res.token
        elif res.file_id is not None:
            attach["videoId"] = res.file_id
        if duration_ms is not None:
            attach["duration"] = duration_ms
        return attach

    if kind == AttachKind.AUDIO:
        attach = {"_type": "AUDIO"}
        if res.token:
            attach["token"] = res.token
        elif res.file_id is not None:
            attach["audioId"] = res.file_id
        if duration_ms is not None:
            attach["duration"] = duration_ms
        return attach

    # FILE
    attach = {"_type": "FILE"}
    if res.token:
        attach["token"] = res.token
    elif res.file_id is not None:
        attach["fileId"] = res.file_id
    return attach


def _extract_upload_url(resp: dict) -> Optional[str]:
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
    body = body.strip()
    try:
        decoded = json.loads(body)
    except (ValueError, TypeError):
        if body and not body.startswith("<"):
            return UploadResult(token=body)
        return UploadResult()

    if not isinstance(decoded, dict):
        return UploadResult()
    m = {str(k): v for k, v in decoded.items()}

    token: Optional[str] = None
    file_id: Optional[int] = None
    for k in ("photoToken", "token", "videoId", "fileId", "id"):
        v = m.get(k)
        if v is None:
            continue
        if token is None and k in ("photoToken", "token"):
            token = str(v)
        if file_id is None and k in ("videoId", "fileId", "id"):
            file_id = _as_int(v)
    if token is not None or file_id is not None:
        return UploadResult(token=token, file_id=file_id)

    result = m.get("result")
    if isinstance(result, dict):
        r = {str(k): v for k, v in result.items()}
        tokens = r.get("tokens")
        if isinstance(tokens, list) and tokens:
            return UploadResult(token=str(tokens[0]))
        rid = r.get("id")
        if isinstance(rid, (int, float)):
            return UploadResult(file_id=int(rid))
        uf = r.get("uploadedFiles")
        if isinstance(uf, list) and uf and isinstance(uf[0], dict):
            first = {str(k): v for k, v in uf[0].items()}
            t = first.get("token")
            fid = first.get("fileId", first.get("id"))
            return UploadResult(
                token=str(t) if t is not None else None,
                file_id=_as_int(fid),
            )
    return UploadResult()


def _as_int(v: Any) -> Optional[int]:
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, (int, float)):
        return int(v)
    try:
        return int(str(v))
    except (TypeError, ValueError):
        return None
