"""Доступ к ресурсам приложения (иконка, логотип)."""
from __future__ import annotations

import os

from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon, QPixmap

_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "assets"))
_BRAND_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "resources"))


def icon_file() -> str | None:
    for name in ("maxim.ico", "maxim_icon.png"):
        path = os.path.join(_BRAND_DIR, name)
        if os.path.isfile(path):
            return path
    for name in ("icon.ico", "icon.png"):
        path = os.path.join(_DIR, name)
        if os.path.isfile(path):
            return path
    return None


def app_icon() -> QIcon:
    path = icon_file()
    return QIcon(path) if path else QIcon()


def logo_pixmap(size: int) -> QPixmap | None:
    paths = [os.path.join(_BRAND_DIR, "maxim_icon.png")]
    paths.extend(os.path.join(_DIR, name) for name in ("icon_256.png", "icon.png"))
    for path in paths:
        if os.path.isfile(path):
            pix = QPixmap(path)
            if not pix.isNull():
                return pix.scaled(
                    size, size,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
    return None
