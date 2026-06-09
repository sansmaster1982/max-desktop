"""Пути, настройки и сессия (хранилище токена).

Минимум настроек повторяет оригинальное приложение MAX: тема, аккаунт/сессии,
версия протокола, выход. Токен лежит отдельным файлом (как max_login_token.txt
в test5.py), на POSIX — с правами 600.
"""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

APP_DIR_NAME = "MAX Desktop"


class AppPaths:
    def __init__(self) -> None:
        base = os.environ.get("APPDATA") or os.path.expanduser("~/.config")
        self.dir = Path(base) / APP_DIR_NAME
        self.dir.mkdir(parents=True, exist_ok=True)
        self.settings_file = self.dir / "settings.json"
        self.session_file = self.dir / "session.json"
        self.token_file = self.dir / "max_auth_token.txt"
        # deviceId — отдельный файл: пишется один раз и НЕ удаляется при выходе,
        # чтобы для сервера это всегда было одно и то же устройство (смена
        # deviceId = сигнал «новое устройство» для антифрода).
        self.device_file = self.dir / "device_id.txt"
        # Профиль устройства для userAgent (модель/экран/osVersion). Если он
        # захардкожен одинаково у всех инсталляций — антифрод кластеризует все
        # номера этого клиента в один отпечаток. Поэтому фиксируем стабильный,
        # но варьирующийся на установку профиль (детерминирован из deviceId).
        self.device_profile_file = self.dir / "device_profile.json"
        self.db_file = self.dir / "cache.db"
        self.media_dir = self.dir / "media"
        self.media_dir.mkdir(parents=True, exist_ok=True)


@dataclass
class Settings:
    theme: str = "dark"                 # dark | light
    accent: str = "#2B7FFF"             # фирменный синий
    app_version: str = "26.15.0"
    locale: str = "ru"
    send_on_enter: bool = True
    notifications: bool = True
    download_dir: str = ""              # пусто -> media_dir
    window_w: int = 1180
    window_h: int = 760

    _path: Optional[Path] = field(default=None, repr=False, compare=False)

    @classmethod
    def load(cls, paths: AppPaths) -> "Settings":
        data = _read_json(paths.settings_file)
        known = {f for f in cls.__dataclass_fields__ if not f.startswith("_")}
        clean = {k: v for k, v in data.items() if k in known}
        obj = cls(**clean)
        obj._path = paths.settings_file
        if not obj.download_dir:
            obj.download_dir = str(paths.media_dir)
        return obj

    def save(self) -> None:
        if self._path is None:
            return
        data = {k: v for k, v in asdict(self).items() if not k.startswith("_")}
        _write_json(self._path, data)


@dataclass
class Session:
    """Сохранённая сессия: токен + его тип + мой профиль."""
    token: Optional[str] = None
    token_kind: str = "android"         # android | web (web => deviceType=WEB)
    my_user_id: Optional[int] = None
    my_name: Optional[str] = None
    phone: Optional[str] = None
    device_id: str = ""

    _paths: Optional[AppPaths] = field(default=None, repr=False, compare=False)

    @classmethod
    def load(cls, paths: AppPaths) -> "Session":
        data = _read_json(paths.session_file)
        known = {f for f in cls.__dataclass_fields__ if not f.startswith("_")}
        clean = {k: v for k, v in data.items() if k in known}
        obj = cls(**clean)
        obj._paths = paths
        # Токен хранится отдельным файлом.
        if paths.token_file.exists():
            tok = paths.token_file.read_text(encoding="utf-8").strip()
            obj.token = tok or None
        # deviceId — из выделенного файла (источник истины). Если его нет:
        # мигрируем из session.json, иначе генерируем один раз и фиксируем.
        obj.device_id = _load_or_create_device_id(paths.device_file, obj.device_id)
        return obj

    @property
    def device_type(self) -> str:
        return "WEB" if self.token_kind == "web" else "ANDROID"

    def set_token(self, token: str, kind: str) -> None:
        self.token = token
        self.token_kind = kind
        self.save()

    def clear_token(self) -> None:
        self.token = None
        self.my_user_id = None
        if self._paths and self._paths.token_file.exists():
            try:
                self._paths.token_file.unlink()
            except OSError:
                pass
        self.save()

    def save(self) -> None:
        if self._paths is None:
            return
        # Токен — отдельным файлом с урезанными правами.
        if self.token:
            self._paths.token_file.write_text(self.token, encoding="utf-8")
            try:
                os.chmod(self._paths.token_file, 0o600)
            except OSError:
                pass
        meta = {
            "token_kind": self.token_kind,
            "my_user_id": self.my_user_id,
            "my_name": self.my_name,
            "phone": self.phone,
            "device_id": self.device_id,
        }
        _write_json(self._paths.session_file, meta)


# Реальные, самосогласованные Android-устройства (model code / экран / API).
# Все современные — arm64-v8a. Захардкоженный одинаковый UA (deviceName="Android",
# screen=1080x2340 у ВСЕХ) — кластеризуемый отпечаток «один сторонний клиент».
# На установку выбираем СТАБИЛЬНЫЙ профиль детерминированно из deviceId.
_ANDROID_DEVICES = [
    {"deviceName": "SM-G991B", "screen": "1080x2400", "osVersion": "34"},   # Galaxy S21
    {"deviceName": "SM-G996B", "screen": "1080x2400", "osVersion": "34"},   # Galaxy S21+
    {"deviceName": "SM-S911B", "screen": "1080x2340", "osVersion": "34"},   # Galaxy S23
    {"deviceName": "SM-A536B", "screen": "1080x2400", "osVersion": "34"},   # Galaxy A53
    {"deviceName": "SM-A525F", "screen": "1080x2400", "osVersion": "33"},   # Galaxy A52
    {"deviceName": "SM-A346B", "screen": "1080x2340", "osVersion": "34"},   # Galaxy A34
    {"deviceName": "Pixel 6", "screen": "1080x2400", "osVersion": "34"},
    {"deviceName": "Pixel 7", "screen": "1080x2400", "osVersion": "34"},
    {"deviceName": "2201117TG", "screen": "1080x2400", "osVersion": "33"},  # Redmi Note 11
    {"deviceName": "M2101K6G", "screen": "1080x2400", "osVersion": "33"},   # Redmi Note 10 Pro
]


def _load_or_create_device_profile(path: Path, device_id: str) -> dict:
    """Стабильный per-install профиль устройства для userAgent. Детерминирован
    из deviceId (один deviceId -> один профиль), сохраняется в файл, чтобы не
    «прыгать» при обновлении таблицы (смена профиля = сигнал «новое устройство»)."""
    data = _read_json(path)
    if all(data.get(k) for k in ("deviceName", "screen", "osVersion")):
        return {k: data[k] for k in ("deviceName", "screen", "osVersion")}
    import hashlib

    h = int(hashlib.sha256((device_id or "x").encode("utf-8")).hexdigest(), 16)
    prof = dict(_ANDROID_DEVICES[h % len(_ANDROID_DEVICES)])
    _write_json(path, prof)
    return prof


def _load_or_create_device_id(path: Path, migrated: str = "") -> str:
    """Стабильный deviceId: один раз создаём и фиксируем в отдельном файле.
    При выходе из аккаунта файл не трогается — устройство остаётся тем же."""
    try:
        if path.exists():
            existing = path.read_text(encoding="utf-8").strip()
            if existing:
                return existing
    except OSError:
        pass
    did = (migrated or "").strip() or str(uuid.uuid4())
    try:
        path.write_text(did, encoding="utf-8")
    except OSError:
        pass
    return did


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_json(path: Path, data: dict) -> None:
    try:
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError:
        pass
