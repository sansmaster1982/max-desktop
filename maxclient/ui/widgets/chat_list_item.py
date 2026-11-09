"""Строка чата в боковой ленте: аватар, заголовок, превью, время, бейдж непрочитанных."""
from __future__ import annotations

from PySide6.QtCore import Qt, QSize
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
    QSizePolicy,
)

from ...core.models import Chat
from .avatar import Avatar
from .common import fmt_chat_time
from .line_icon import line_icon


class _ElidedLabel(QLabel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._full_text = ''
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setTextFormat(Qt.TextFormat.PlainText)

    def setText(self, text):  # noqa: N802
        self._full_text = str(text or '').replace('\n', ' ')
        self._fit()

    def _fit(self):
        super().setText(QFontMetrics(self.font()).elidedText(self._full_text, Qt.TextElideMode.ElideRight, max(0, self.width())))

    def resizeEvent(self, event):  # noqa: N802
        self._fit()
        super().resizeEvent(event)


class ChatListItem(QWidget):
    """A real chat row: title, photo, preview and only server-backed badges."""

    def __init__(self, chat: Chat, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.chat_id = chat.id
        self.setObjectName('ChatRow')
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setMinimumWidth(0)
        self.setFixedHeight(80)
        root = QHBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(12)
        self.avatar = Avatar(54)
        root.addWidget(self.avatar)

        center = QVBoxLayout()
        center.setContentsMargins(0, 0, 0, 0)
        center.setSpacing(3)
        heading = QHBoxLayout()
        heading.setSpacing(4)
        self.title = _ElidedLabel()
        self.title.setObjectName('ChatRowTitle')
        self.verified = QLabel()
        self.verified.setFixedSize(14, 14)
        self.verified.setPixmap(line_icon('verified', '#0085FF', 14).pixmap(14, 14))
        self.muted = QLabel()
        self.muted.setFixedSize(14, 14)
        self.muted.setPixmap(line_icon('muted', '#A2A7AE', 14).pixmap(14, 14))
        heading.addWidget(self.title, 1)
        heading.addWidget(self.verified)
        heading.addWidget(self.muted)
        self.preview = _ElidedLabel()
        self.preview.setObjectName('ChatRowPreview')
        self.preview.setFixedHeight(20)
        center.addLayout(heading)
        center.addWidget(self.preview)
        center.addStretch(1)
        root.addLayout(center, 1)

        right = QVBoxLayout()
        right.setSpacing(9)
        right.setContentsMargins(0, 2, 0, 0)
        self.time = QLabel()
        self.time.setObjectName('ChatRowTime')
        self.time.setAlignment(Qt.AlignmentFlag.AlignRight)
        right.addWidget(self.time, 0, Qt.AlignmentFlag.AlignRight)
        bottom = QHBoxLayout()
        bottom.setSpacing(5)
        self.pin = QLabel()
        self.pin.setFixedSize(14, 14)
        self.pin.setPixmap(line_icon('pin', '#A7ACB3', 14).pixmap(14, 14))
        self.badge = QLabel()
        self.badge.setObjectName('UnreadBadge')
        self.badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.badge.setFixedHeight(20)
        bottom.addStretch(1)
        bottom.addWidget(self.pin)
        bottom.addWidget(self.badge)
        right.addLayout(bottom)
        right.addStretch(1)
        root.addLayout(right)
        self.update_from(chat)

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(355, 80)

    def update_from(self, chat: Chat) -> None:
        self.chat_id = chat.id
        self.avatar.set_identity(chat.display, chat.id)
        self.avatar.set_photo(getattr(chat, 'avatar_url', None))
        self.avatar.set_online(getattr(chat, 'is_online', None))
        self.title.setText(chat.display)
        self.preview.setText(chat.last_preview or '')
        self.time.setText(fmt_chat_time(chat.last_time_ms))
        self.verified.setVisible(getattr(chat, 'is_verified', False) is True)
        self.muted.setVisible(getattr(chat, 'is_muted', False) is True)
        self.pin.setVisible(getattr(chat, 'is_pinned', False) is True)
        count = max(0, int(chat.unread or 0))
        self.badge.setVisible(count > 0)
        if count:
            value = str(count) if count < 1000 else '999+'
            self.badge.setText(value)
            self.badge.setFixedWidth(max(20, self.badge.fontMetrics().horizontalAdvance(value) + 10))
        self.badge.setProperty('muted', getattr(chat, 'is_muted', False) is True)
        self.badge.style().unpolish(self.badge)
        self.badge.style().polish(self.badge)
        self.setAccessibleName(f'{chat.display}, непрочитанных: {count}')
