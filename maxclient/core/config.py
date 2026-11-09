"""Пути, настройки и сессия (хранилище токена).

Минимум настроек повторяет оригинальное приложение MAX: тема, аккаунт/сессии,
версия протокола, выход. Токен лежит отдельным файлом (как max_login_token.txt
в test5.py), на POSIX — с правами 600.
"""
from __future__ import annotations

import json
import os
import tempfile
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from .appearance import chat_font_scale, chat_theme, theme_scheme
from .i18n import normalize_language

APP_DIR_NAME = "MAX Desktop"


class AppPaths:
    def __init__(self) -> None:
        override = os.environ.get("MAX_DESKTOP_DATA_DIR")
        if override:
            # A separate working copy can keep its data inside the workspace
            # without changing the original installation's APPDATA files.
            self.dir = Path(override).expanduser()
        else:
            base = os.environ.get("APPDATA") or os.path.expanduser("~/.config")
            self.dir = Path(base) / APP_DIR_NAME
        self.dir.mkdir(parents=True, exist_ok=True)
        self.settings_file = self.dir / "settings.json"
        self.session_file = self.dir / "session.json"
        self.token_file = self.dir / "max_auth_token.txt"
        # Persist the installation UUID across launches. Its server-side
        # fingerprint/anti-abuse interpretation has not been established.
        self.device_file = self.dir / "device_id.txt"
        # Historical Android compatibility profile, deterministic per UUID.
        # This is not evidence of native desktop identity parity.
        self.device_profile_file = self.dir / "device_profile.json"
        self.db_file = self.dir / "cache.db"
        self.media_dir = self.dir / "media"
        self.media_dir.mkdir(parents=True, exist_ok=True)


@dataclass
class Settings:
    theme: str = "system"               # system | light | dark (local preference)
    chat_theme: str = "space"
    chat_font_scale: str = "large"
    accent: str = "#2B7FFF"             # фирменный синий
    app_version: str = "26.15.0"
    web_app_version: str = "26.10.11"
    locale: str = "ru"
    ui_language: str = "ru"            # local interface only; independent of transport locale
    send_on_enter: bool = True
    notifications: bool = True
    download_dir: str = ""              # пусто -> media_dir
    window_w: int = 1180
    window_h: int = 760

    _path: Optional[Path] = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        self.theme = theme_scheme(self.theme)
        self.chat_theme = chat_theme(self.chat_theme)
        self.chat_font_scale = chat_font_scale(self.chat_font_scale)
        self.ui_language = normalize_language(self.ui_language)

    @classmethod
    def load(cls, paths: AppPaths) -> "Settings":
        data = _read_json(paths.settings_file)
        if not isinstance(data, dict):
            data = {}
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

    def save_ui_language(self, language: str) -> None:
        """Atomically persist only the UI language; expose failure to its picker.

        The generic legacy save helper intentionally remains unchanged. Existing
        settings, including unknown future fields and transport locale, survive.
        """
        language = normalize_language(language)
        if self._path is None:
            self.ui_language = language
            return
        path = Path(self._path)
        if path.exists():
            data = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(data, dict):
                raise ValueError('Invalid local settings document')
        else:
            data = {key: value for key, value in asdict(self).items() if not key.startswith('_')}
        data['ui_language'] = language
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                             prefix=path.name + '.ui-', suffix='.tmp', delete=False) as handle:
                temporary = Path(handle.name)
                json.dump(data, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(path)
            temporary = None
        finally:
            if temporary is not None:
                try:
                    temporary.unlink()
                except OSError:
                    pass
        self.ui_language = language

    def save_notifications(self, enabled: bool) -> None:
        """Commit local delivery atomically before changing its runtime value.

        Unlike legacy save(), errors propagate to the notification switch. Only
        this field is changed in an existing settings file, including unknown
        future fields. A missing storage binding cannot claim persistence.
        """
        if type(enabled) is not bool:
            raise ValueError('Notification delivery must be a boolean')
        if self._path is None:
            raise ValueError('Local settings storage is not configured')
        path = Path(self._path)
        if path.exists():
            data = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(data, dict):
                raise ValueError('Invalid local settings document')
        else:
            data = {key: value for key, value in asdict(self).items() if not key.startswith('_')}
        data['notifications'] = enabled
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                             prefix=path.name + '.notifications-', suffix='.tmp',
                                             delete=False) as handle:
                temporary = Path(handle.name)
                json.dump(data, handle, ensure_ascii=False, indent=2, allow_nan=False)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(path)
            temporary = None
        finally:
            if temporary is not None:
                try:
                    temporary.unlink()
                except OSError:
                    pass
        self.notifications = enabled


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

    def set_device_id(self, device_id: str) -> None:
        """Persist an imported UUID only after the caller confirmed its login."""
        canonical = str(uuid.UUID(device_id))
        if self._paths is not None:
            temporary = self._paths.device_file.with_suffix(".tmp")
            temporary.write_text(canonical, encoding="utf-8")
            temporary.replace(self._paths.device_file)
        self.device_id = canonical

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


# Historical Android model/screen profiles, stable per installation UUID.
# Their effect on server-side identification has not been validated.
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
