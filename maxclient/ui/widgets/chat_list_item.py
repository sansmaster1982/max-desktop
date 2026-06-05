"""Строка чата в боковой ленте: аватар, заголовок, превью, время, бейдж непрочитанных."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from ...core.models import Chat
from .avatar import Avatar
from .common import fmt_chat_time


class ChatListItem(QWidget):
    def __init__(self, chat: Chat, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.chat_id = chat.id

        root = QHBoxLayout(self)
        root.setContentsMargins(10, 8, 10, 8)
        root.setSpacing(12)

        self.avatar = Avatar(48)
        root.addWidget(self.avatar)

        center = QVBoxLayout()
        center.setSpacing(2)
        center.setContentsMargins(0, 0, 0, 0)
        self.title = QLabel()
        self.title.setStyleSheet("font-weight:600; font-size:14.5px;")
        self.preview = QLabel()
        self.preview.setObjectName("ChatSubtitle")
        self.preview.setStyleSheet("color: palette(mid); font-size:13px;")
        center.addWidget(self.title)
        center.addWidget(self.preview)
        root.addLayout(center, 1)

        right = QVBoxLayout()
        right.setSpacing(4)
        right.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignRight)
        self.time = QLabel()
        self.time.setStyleSheet("color: palette(mid); font-size:11.5px;")
        self.time.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.badge = QLabel()
        self.badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.badge.setFixedHeight(20)
        self.badge.setMinimumWidth(20)
        right.addWidget(self.time)
        right.addWidget(self.badge, 0, Qt.AlignmentFlag.AlignRight)
        right.addStretch(1)
        root.addLayout(right)

        self.update_from(chat)

    def update_from(self, chat: Chat) -> None:
        self.chat_id = chat.id
        self.avatar.set_identity(chat.display, chat.id)
        self.title.setText(_elide(chat.display, 26))
        self.preview.setText(_elide(chat.last_preview or "", 34))
        self.time.setText(fmt_chat_time(chat.last_time_ms))
        if chat.unread > 0:
            self.badge.setText(str(chat.unread if chat.unread < 100 else "99+"))
            self.badge.setStyleSheet(
                "background:#2B7FFF; color:white; border-radius:10px;"
                "padding:0 6px; font-size:11.5px; font-weight:700;"
            )
            self.badge.show()
        else:
            self.badge.hide()


def _elide(text: str, limit: int) -> str:
    text = (text or "").replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 1] + "…"
