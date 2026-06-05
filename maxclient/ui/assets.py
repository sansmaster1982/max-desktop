"""Доступ к ресурсам приложения (иконка, логотип)."""
from __future__ import annotations

import os

from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon, QPixmap

_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "assets"))


def icon_file() -> str | None:
    for name in ("icon.ico", "icon.png"):
        path = os.path.join(_DIR, name)
        if os.path.isfile(path):
            return path
    return None


def app_icon() -> QIcon:
    path = icon_file()
    return QIcon(path) if path else QIcon()


def logo_pixmap(size: int) -> QPixmap | None:
    for name in ("icon_256.png", "icon.png"):
        path = os.path.join(_DIR, name)
        if os.path.isfile(path):
            pix = QPixmap(path)
            if not pix.isNull():
                return pix.scaled(
                    size, size,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
    return None
