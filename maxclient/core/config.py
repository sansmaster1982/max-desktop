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
        if not obj.device_id:
            obj.device_id = str(uuid.uuid4())
            obj.save()
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
