"""Круглый аватар с инициалами и цветом-градиентом от ключа."""
from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QLinearGradient, QPainter, QPen
from PySide6.QtWidgets import QWidget

from .common import avatar_colors, initials


class Avatar(QWidget):
    def __init__(self, size: int = 46, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._size = size
        self._initials = "#"
        self._c1 = "#4FC3F7"
        self._c2 = "#2B7FFF"
        self.setFixedSize(size, size)

    def set_identity(self, name: str | None, key=None) -> None:
        self._initials = initials(name)
        self._c1, self._c2 = avatar_colors(key if key is not None else name)
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt API)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(0, 0, self._size, self._size)

        grad = QLinearGradient(0, 0, 0, self._size)
        grad.setColorAt(0, QColor(self._c1))
        grad.setColorAt(1, QColor(self._c2))
        painter.setBrush(QBrush(grad))
        painter.setPen(QPen(Qt.PenStyle.NoPen))
        painter.drawEllipse(rect)

        painter.setPen(QPen(QColor("#FFFFFF")))
        font = QFont(self.font())
        font.setPixelSize(int(self._size * 0.4))
        font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(font)
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, self._initials)
        painter.end()
