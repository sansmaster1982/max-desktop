"""Read local session credentials as data; never execute exported JavaScript."""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path

_TOKEN = re.compile(r'[A-Za-z0-9_+~.=/\-]{100,16384}', re.ASCII)


@dataclass
class WebSessionImport:
    token: str = field(repr=False)
    device_id: str


def read_web_session(path: str | Path) -> WebSessionImport:
    selected = Path(path).resolve()
    if not selected.is_file() or selected.stat().st_size > 128 * 1024:
        raise ValueError('Выберите небольшой локальный файл сессии JSON.')
    text = selected.read_text(encoding='utf-8-sig').strip()
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        raise ValueError('Нужен JSON с токеном и deviceId. JavaScript не выполняется.') from None
    if not isinstance(data, dict):
        raise ValueError('Файл сессии должен содержать объект JSON.')
    token = data.get('token') or data.get('authToken')
    if token is None and isinstance(data.get('auth'), dict):
        token = data['auth'].get('token')
    if token is None and isinstance(data.get('tokenFile'), str):
        token_file = (selected.parent / data['tokenFile']).resolve()
        if not token_file.is_relative_to(selected.parent) or not token_file.is_file():
            raise ValueError('Файл токена должен находиться в папке выбранной сессии.')
        if token_file.stat().st_size > 32768:
            raise ValueError('Файл токена слишком большой.')
        token = token_file.read_text(encoding='utf-8-sig').strip()
    if not isinstance(token, str) or _TOKEN.fullmatch(token) is None:
        raise ValueError('В файле не найден один корректный токен MAX.')
    device_id = data.get('deviceId') or data.get('device_id')
    try:
        device_id = str(uuid.UUID(device_id))
    except (ValueError, TypeError, AttributeError):
        raise ValueError('В файле отсутствует корректный deviceId исходной WEB-сессии.') from None
    if data.get('deviceType', 'WEB') != 'WEB' or data.get('transport', 'wss') != 'wss':
        raise ValueError('Выберите экспорт WEB-сессии MAX.')
    return WebSessionImport(token=token, device_id=device_id)
