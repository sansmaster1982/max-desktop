"""Detached history preview. No ChatView, composer, receipts, or media RPCs."""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QAbstractButton, QDialog, QHBoxLayout, QLabel,
                              QLayout, QPushButton, QScrollArea, QVBoxLayout)
from shiboken6 import isValid

from ..core.i18n import tr
from ..core.models import Message
from ..core.conversation_preview_service import ConversationPreviewResult
from .async_runner import run_async
from .chat_view import _DayDivider
from .theme import palette_for
from .timeline_photos import PhotoPreview
from .widgets.chat_wallpaper import ChatWallpaper
from .widgets.common import fmt_day_divider
from .widgets.message_bubble import MessageBubble


def _passive_bubble(bubble):
    """Keep actual message presentation, revoke all actions owned by the renderer."""
    for label in bubble.findChildren(QLabel):
        # A QLabel can open URLs independently of a ChatView callback.
        if label.receivers('2linkActivated(QString)'):
            try:
                label.linkActivated.disconnect()
            except (RuntimeError, TypeError):
                pass
        label.setOpenExternalLinks(False)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse
                                      | Qt.TextInteractionFlag.TextSelectableByKeyboard)
    for button in bubble.findChildren(QAbstractButton):
        button.setEnabled(False)
        button.setAutoRepeat(False)
    for photo in bubble.findChildren(PhotoPreview):
        photo.set_unavailable()  # This view does not launch TimelinePhotos.
    for widget in (bubble, *bubble.findChildren(QLabel), *bubble.findChildren(PhotoPreview)):
        widget.setCursor(Qt.CursorShape.ArrowCursor)
    bubble.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)


class ChatPreviewDialog(QDialog):
    """A single bounded read tied to the exact account/session/conversation."""
    def __init__(self, service, settings, context, title='', parent=None, *, context_is_current=None):
        super().__init__(parent)
        self.service, self.settings, self.context = service, settings, context
        self._context_is_current = context_is_current
        self._revoked = False
        self._request = None
        self._bubbles = []
        self.setWindowTitle(tr('Предпросмотр', settings.ui_language))
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.resize(560, 700)
        self.setMinimumSize(360, 360)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 10)
        root.setSpacing(0)
        self.title = QLabel(title if isinstance(title, str) else '')
        self.title.setObjectName('ChatTitle')
        self.title.setTextFormat(Qt.TextFormat.PlainText)
        self.title.setWordWrap(True)
        self.title.setContentsMargins(16, 12, 16, 6)
        root.addWidget(self.title)
        self.status = QLabel(tr('Без отметки о прочтении', settings.ui_language))
        self.status.setObjectName('ChatSubtitle')
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setContentsMargins(16, 0, 16, 10)
        root.addWidget(self.status)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setObjectName('MessageScroll')
        self.container = ChatWallpaper(settings.theme, preset=settings.chat_theme)
        self.messages = QVBoxLayout(self.container)
        self.messages.setSizeConstraint(QLayout.SizeConstraint.SetMinAndMaxSize)
        self.messages.setContentsMargins(0, 12, 0, 12)
        self.messages.setSpacing(4)
        self.scroll.setWidget(self.container)
        root.addWidget(self.scroll, 1)
        self.notice = QLabel()
        self.notice.setTextFormat(Qt.TextFormat.PlainText)
        self.notice.setWordWrap(True)
        self.notice.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.notice.setContentsMargins(20, 20, 20, 20)
        self.messages.addStretch(1)
        self.messages.addWidget(self.notice)
        footer = QHBoxLayout()
        footer.setContentsMargins(12, 8, 12, 0)
        self.retry = QPushButton(tr('Повторить', settings.ui_language))
        self.retry.setAutoDefault(False)
        self.retry.clicked.connect(self.start_loading)
        self.retry.hide()
        footer.addWidget(self.retry)
        footer.addStretch(1)
        close = QPushButton(tr('Закрыть', settings.ui_language))
        close.clicked.connect(self.reject)
        close.setDefault(True)
        footer.addWidget(close)
        root.addLayout(footer)
        self._scope_timer = QTimer(self)
        self._scope_timer.setInterval(200)
        self._scope_timer.timeout.connect(self._check_scope)

    def _current(self):
        try:
            return (isValid(self) and not self._revoked
                    and self.service.is_current_conversation(self.context)
                    and (self._context_is_current is None or self._context_is_current()))
        except (AttributeError, RuntimeError, ValueError):
            return False

    def _check_scope(self):
        if not self._current():
            self.reject()

    def start_loading(self):
        if not self.isVisible():
            return
        if not self._current():
            self.reject()
            return
        if self._request is not None:
            return
        self._scope_timer.start()
        request = self._request = object()
        self.retry.hide()
        self.notice.setText(tr('Загрузка…', self.settings.ui_language))
        context = self.context

        def work():
            # Worker never touches QWidget state and never captures a newer session.
            if self._revoked or self._request is not request:
                return None
            try:
                return self.service.load_conversation_preview(context)
            except Exception:
                # Raw exceptions can contain account data; the UI needs no payload.
                raise RuntimeError('Conversation preview read failed') from None

        def finished(result=None):
            if not self._current() or self._request is not request or not self.isVisible():
                return
            self._request = None
            valid = (isinstance(result, ConversationPreviewResult) and result.context is context
                     and isinstance(result.title, str) and isinstance(result.messages, tuple)
                     and all(isinstance(message, Message) and type(message.chat_id) is int
                             and message.chat_id == context.chat_id for message in result.messages))
            if not valid:
                self.notice.setText(tr('Не удалось загрузить предпросмотр.', self.settings.ui_language))
                self.retry.show()
                return
            self.title.setText(result.title)
            self._render(result.messages)

        run_async(work, on_done=finished, on_error=lambda _error: finished())

    def _render(self, messages):
        self.notice.setVisible(not messages)
        self.notice.setText(tr('Сообщений пока нет', self.settings.ui_language))
        palette = palette_for(self.settings.theme)
        previous_day = None
        for message in messages:
            day = fmt_day_divider(message.time_ms)
            if day != previous_day:
                label = _DayDivider(day)
                label.setObjectName('DayDivider')
                self.messages.insertWidget(self.messages.count() - 1, label)
                previous_day = day
            bubble = MessageBubble(message, self.settings.accent, palette,
                                   font_scale=self.settings.chat_font_scale,
                                   chat_preset=self.settings.chat_theme, language=self.settings.ui_language)
            _passive_bubble(bubble)
            self._bubbles.append(bubble)
            self.messages.insertWidget(self.messages.count() - 1, bubble)
        QTimer.singleShot(0, self._scroll_bottom)

    def _scroll_bottom(self):
        if self._current() and self.isVisible():
            bar = self.scroll.verticalScrollBar()
            bar.setValue(bar.maximum())

    def done(self, result):
        self._revoked = True
        self._request = None
        self._scope_timer.stop()
        # Detached messages are not retained by a closed dialog or callback.
        self._bubbles.clear()
        while self.messages.count():
            item = self.messages.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        super().done(result)

