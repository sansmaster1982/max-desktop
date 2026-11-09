"""Область переписки: шапка чата, лента сообщений, панель ввода.

Сетевые операции — через run_async. Локальные перечитывания истории из
SQLite быстрые и идут синхронно. Голос/просмотр медиа подключаются лениво из
media.py (QtMultimedia), при его отсутствии деградируют мягко.
"""
from __future__ import annotations

import datetime
from dataclasses import dataclass
import math
import os
import weakref
from typing import Optional

from PySide6.QtCore import Qt, QTimer, Signal, QSize, QEvent
from PySide6.QtGui import QKeyEvent, QKeySequence, QShortcut
from shiboken6 import isValid
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QInputDialog,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ..core.models import Attach, Chat, Message
from ..core.service import MaxService
from ..core.config import Settings
from ..core.i18n import normalize_language, tr
from ..protocol.uploader import AttachKind
from ..protocol.client import (AuthenticatedSession, ConnectionState, MaxClient,
                               MaxError, MaxWriteNotStarted, MaxWriteOutcomeUnknown)
from ..protocol.errors import MaxNotConnected, MaxTimeout
from .async_runner import run_async
from .theme import palette_for
from .widgets.avatar import Avatar
from .widgets.common import fmt_day_divider
from .widgets.message_bubble import MessageBubble
from .widgets.chat_wallpaper import ChatWallpaper
from .widgets.line_icon import line_icon


_UNCAPTURED_ACTION = object()


@dataclass(frozen=True)
class _MessageActionFailure:
    """Only an allowlisted category crosses the worker/UI boundary."""
    kind: str


_MESSAGE_ACTION_FAILURE_TEXT = {
    'session': ('Действие не отправлено', 'Сессия изменилась. Откройте сообщение заново.'),
    'not_started': ('Действие не отправлено', 'Отправка действия не началась. Откройте сообщение заново.'),
    'rejected': ('Действие отклонено', 'MAX отклонил действие. Обновите чат и проверьте доступные действия.'),
    'unknown': ('Результат не подтверждён', 'Результат действия неизвестен. Проверьте сообщение перед повторной попыткой.'),
    'validation': ('Действие не подтверждено', 'Не удалось проверить параметры действия. Обновите чат перед повторной попыткой.'),
    'unexpected': ('Действие не подтверждено', 'Не удалось установить результат действия. Проверьте сообщение перед повторной попыткой.'),
    'saved_scope': ('Удаление в Избранном', 'В Избранном доступно только удаление у себя. Откройте подтверждение заново.'),
}


def _bound_message_action(service, context, method, chat_id, args, kwargs):
    """The worker may start after logout; never capture a replacement session."""
    try:
        current = isinstance(context, AuthenticatedSession) and service.search_context() is context
    except Exception:
        current = False
    if not current:
        return _MessageActionFailure('session')  # No service mutation was invoked.
    if (chat_id == 0 and method == getattr(service, 'delete_messages', None)
            and kwargs.get('for_all') is True):
        return _MessageActionFailure('saved_scope')
    try:
        return method(chat_id, *args, context=context, **kwargs)
    except MaxWriteNotStarted:
        return _MessageActionFailure('not_started')
    except (MaxWriteOutcomeUnknown, MaxTimeout, MaxNotConnected):
        return _MessageActionFailure('unknown')
    except MaxError as error:
        # These ordinary message wrappers use exact MaxError for a negative
        # server reply. Other protocol failures must not be called a rejection.
        return _MessageActionFailure('rejected' if type(error) is MaxError else 'unexpected')
    except ValueError:
        return _MessageActionFailure('validation')  # Does not prove no packet was sent.
    except Exception:
        return _MessageActionFailure('unexpected')


class ChatView(QWidget):
    chat_touched = Signal(int)  # чат изменился — обновить ленту слева
    call_requested = Signal(object, bool)
    info_requested = Signal(object)
    file_save_requested = Signal(object)  # Bound descriptor, never a guessed URL.
    file_scope_closed = Signal(object)

    def __init__(self, service: MaxService, settings: Settings) -> None:
        super().__init__()
        self.service = service
        self.settings = settings
        self.chat: Optional[Chat] = None
        self._metadata_owner = None
        self._history_generation = 0
        self._history_cache_revision = 0
        self._history_target = None
        self._history_target_pending = False
        self._pending_target_changes = {}
        self._change_scroll_pending = None
        self._change_scroll_timer = QTimer(self)
        self._change_scroll_timer.setSingleShot(True)
        self._change_scroll_timer.timeout.connect(self._restore_change_scroll)
        self._spotlight_id = None
        self._recorder = None
        self._rows: list[dict] = []  # [{key, msg, bubble, divider, day}] — для инкрем. рендера
        self._search_matches: list[int] = []
        self._search_index = -1
        self._drafts: dict[int, str] = {}
        self._reply_targets: dict[int, Message] = {}
        self._reply_target: Optional[Message] = None
        self._file_context = None
        self._message_action_request = None
        self._message_menu_generation = 0
        self._message_action_menus = weakref.WeakSet()
        self._message_dialog_retranslations = {}
        self._build()
        self._reply_last_shortcut = QShortcut(QKeySequence('Ctrl+R'), self)
        self._reply_last_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self._reply_last_shortcut.setAutoRepeat(False)
        self._reply_last_shortcut.activated.connect(self.reply_to_last_loaded)
        from .timeline_photos import TimelinePhotos
        self._photos = TimelinePhotos(self)
        from .timeline_miniapps import TimelineMiniApps
        self._miniapps = TimelineMiniApps(self)
        self.scroll.viewport().installEventFilter(self)
        self.scroll.verticalScrollBar().installEventFilter(self)
        self.show_placeholder()
        from .read_receipts import VisibleReadReceipts
        self.read_receipts = VisibleReadReceipts(self)
        self.destroyed.connect(lambda: self.close_file_scope())

    # ───────────────────────── layout ─────────────────────────

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Шапка.
        self.header = QFrame()
        self.header.setObjectName("ChatHeader")
        self.header.setFixedHeight(64)
        h = QHBoxLayout(self.header)
        h.setContentsMargins(16, 8, 16, 8)
        h.setSpacing(12)
        self.h_avatar = Avatar(42)
        h.addWidget(self.h_avatar)
        title_col = QVBoxLayout()
        title_col.setSpacing(0)
        self.h_title = QLabel()
        self.h_title.setTextFormat(Qt.TextFormat.PlainText)
        self.h_title.setObjectName("ChatTitle")
        self.h_sub = QLabel()
        self.h_sub.setTextFormat(Qt.TextFormat.PlainText)
        self.h_sub.setObjectName("ChatSubtitle")
        title_col.addWidget(self.h_title)
        title_col.addWidget(self.h_sub)
        for target in (self.h_avatar, self.h_title, self.h_sub):
            target.setCursor(Qt.CursorShape.PointingHandCursor)
            target.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            target.setToolTip('Информация о собеседнике или чате')
            target.setAccessibleDescription('Открыть информацию о собеседнике или чате')
            target.installEventFilter(self)
        h.addLayout(title_col, 1)
        self.call_button = self._header_button('phone', 'Голосовые звонки пока недоступны')
        self.video_button = self._header_button('video', 'Видеозвонки пока недоступны')
        self.call_button.setEnabled(False)
        self.video_button.setEnabled(False)
        self.call_button.clicked.connect(lambda: self._request_call(False))
        self.video_button.clicked.connect(lambda: self._request_call(True))
        self.search_button = self._header_button('search', 'Найти в загруженных сообщениях')
        self.search_button.setCheckable(True)
        self.search_button.toggled.connect(self._toggle_search)
        for button in (self.call_button, self.video_button, self.search_button):
            h.addWidget(button)
        root.addWidget(self.header)

        self.search_bar = QFrame()
        self.search_bar.setObjectName('ChatSearchBar')
        search_layout = QHBoxLayout(self.search_bar)
        search_layout.setContentsMargins(16, 6, 16, 6)
        self.history_search = QLineEdit()
        self.history_search.setPlaceholderText('Поиск в загруженных сообщениях')
        self.history_search.textChanged.connect(self._find_messages)
        self.history_search.returnPressed.connect(self._next_match)
        search_layout.addWidget(self.history_search, 1)
        self.search_result = QLabel()
        search_layout.addWidget(self.search_result)
        self.next_match = QPushButton('Далее')
        self.next_match.clicked.connect(self._next_match)
        search_layout.addWidget(self.next_match)
        self.search_bar.hide()
        root.addWidget(self.search_bar)

        # Лента сообщений.
        self.scroll = QScrollArea()
        self.scroll.setObjectName("MessageScroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.container = ChatWallpaper(self.settings.theme, preset=self.settings.chat_theme)
        self.container.setObjectName("ChatArea")
        self.msg_layout = QVBoxLayout(self.container)
        self.msg_layout.setSizeConstraint(QLayout.SizeConstraint.SetMinAndMaxSize)
        self.msg_layout.setContentsMargins(0, 12, 0, 12)
        self.msg_layout.setSpacing(2)
        self.msg_layout.addStretch(1)
        self.scroll.setWidget(self.container)
        root.addWidget(self.scroll, 1)

        self.reply_bar = QFrame()
        self.reply_bar.setObjectName('ReplyBar')
        reply_layout = QHBoxLayout(self.reply_bar)
        reply_layout.setContentsMargins(18, 7, 12, 7)
        self.reply_label = QLabel()
        self.reply_label.setTextFormat(Qt.TextFormat.PlainText)
        self.reply_label.setWordWrap(True)
        reply_layout.addWidget(self.reply_label, 1)
        cancel_reply = self._header_button('close', 'Отменить ответ')
        cancel_reply.clicked.connect(lambda: self._set_reply_target(None))
        reply_layout.addWidget(cancel_reply)
        self.reply_bar.hide()
        root.addWidget(self.reply_bar)

        # Панель ввода.
        self.input_bar = QFrame()
        self.input_bar.setObjectName("InputBar")
        bar = QHBoxLayout(self.input_bar)
        bar.setContentsMargins(12, 10, 12, 10)
        bar.setSpacing(8)

        self.attach_btn = QPushButton()
        self.attach_btn.setIcon(line_icon('attach', palette_for(self.settings.theme).text_dim))
        self.attach_btn.setObjectName("IconButton")
        self.attach_btn.setToolTip("Прикрепить фото, видео или файл")
        self.attach_btn.clicked.connect(self._attach_menu)
        bar.addWidget(self.attach_btn)

        self.input = _InputEdit(self.settings)
        self.input.setPlaceholderText("Написать сообщение…")
        self.input.send_requested.connect(self._send_text)
        bar.addWidget(self.input, 1)

        self.voice_btn = QPushButton()
        self.voice_btn.setIcon(line_icon('microphone', palette_for(self.settings.theme).text_dim))
        self.voice_btn.setObjectName("IconButton")
        self.voice_btn.setToolTip("Записать голосовое сообщение")
        self.voice_btn.clicked.connect(self._toggle_voice)
        bar.addWidget(self.voice_btn)

        self.send_btn = QPushButton()
        self.send_btn.setIcon(line_icon('send', self.settings.accent))
        self.send_btn.setObjectName("IconButton")
        self.send_btn.setToolTip("Отправить")
        self.send_btn.clicked.connect(self._send_text)
        bar.addWidget(self.send_btn)
        for button, label in ((self.attach_btn, 'Прикрепить файл'),
                              (self.voice_btn, 'Записать голосовое сообщение'),
                              (self.send_btn, 'Отправить сообщение')):
            button.setIconSize(QSize(24, 24))
            button.setAccessibleName(label)

        root.addWidget(self.input_bar)
        self._set_composer_visible(False)

    def _header_button(self, icon: str, label: str) -> QPushButton:
        button = QPushButton()
        button.setObjectName('IconButton')
        button.setIcon(line_icon(icon, palette_for(self.settings.theme).text_dim, 24))
        button.setIconSize(QSize(24, 24))
        button.setFixedSize(36, 36)
        button.setToolTip(label)
        button.setAccessibleName(label)
        button.setProperty('appearanceIcon', icon)
        return button

    def _toggle_search(self, visible: bool) -> None:
        self.search_bar.setVisible(visible and self.chat is not None)
        if visible:
            self.history_search.setFocus()
        else:
            self.history_search.clear()

    def _find_messages(self, *_args) -> None:
        query = self.history_search.text().strip().casefold()
        self._search_matches = [i for i, row in enumerate(self._rows)
                                if query and query in (row['msg'].display_text or '').casefold()]
        self._search_index = -1
        self.next_match.setEnabled(bool(self._search_matches))
        self.search_result.setText('' if not query else f'Найдено: {len(self._search_matches)}')
        if self._search_matches:
            self._next_match()

    def _next_match(self) -> None:
        if not self._search_matches:
            return
        self._search_index = (self._search_index + 1) % len(self._search_matches)
        row = self._rows[self._search_matches[self._search_index]]
        self.scroll.ensureWidgetVisible(row['bubble'], 0, 24)
        self.search_result.setText(f'{self._search_index + 1} / {len(self._search_matches)}')

    def _set_composer_visible(self, visible: bool) -> None:
        self.input_bar.setVisible(visible)
        self.header.setVisible(visible)
        self.reply_bar.setVisible(visible and self._reply_target is not None)
        if not visible:
            self.search_button.setChecked(False)
            self.search_bar.hide()

    # ───────────────────────── open / render ─────────────────────────

    def show_placeholder(self) -> None:
        self._cancel_change_scroll()
        self.close_file_scope()
        self._history_generation += 1
        self._history_target = None
        self._history_target_pending = False
        self._pending_target_changes = {}
        self._spotlight_id = None
        self.chat = None
        self._metadata_owner = None
        self._clear_all()
        hint = QLabel("Выберите чат, чтобы начать переписку")
        hint.setObjectName("EmptyHint")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint.setStyleSheet('background:transparent;')
        self.msg_layout.insertStretch(0, 1)
        self.msg_layout.insertWidget(self.msg_layout.count() - 1, hint)
        self._set_composer_visible(False)

    def eventFilter(self, watched, event):  # noqa: N802
        if not isValid(self):
            return False
        if event.type() == QEvent.Type.ContextMenu and isinstance(watched, QLabel):
            message = getattr(watched, '_message_menu_message', None)
            if isinstance(message, Message):
                self._message_text_menu(message, watched, event.pos())
                event.accept()
                return True
        scroll = getattr(self, 'scroll', None)
        if isinstance(scroll, QScrollArea) and isValid(scroll) and watched in (scroll.viewport(), scroll.verticalScrollBar()) and event.type() in (
                QEvent.Type.Wheel, QEvent.Type.MouseButtonPress, QEvent.Type.KeyPress):
            self._cancel_change_scroll()
        if watched in (self.h_avatar, self.h_title, self.h_sub) and self.chat is not None:
            mouse = event.type() == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton
            keyboard = event.type() == QEvent.Type.KeyPress and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space)
            if mouse or keyboard:
                self.info_requested.emit(self.chat)
                return True
        return super().eventFilter(watched, event)

    def open_chat(self, chat: Chat, *, message_id=None, time_ms=None) -> None:
        """Open latest history, or an explicit source-timestamp message window."""
        self._cancel_change_scroll()
        self._history_target_pending = False
        self._pending_target_changes = {}
        self.close_file_scope()
        same = self.chat is not None and self.chat.id == chat.id
        if not same and self.chat is not None:
            self._drafts[self.chat.id] = self.input.toPlainText()
        self.chat = chat
        # Metadata may follow a same-owner reconnect, but must not relabel an
        # old owner's body using the replacement account's coincident chat ID.
        self._metadata_owner = (getattr(self.service, 'client', None), getattr(self.service, 'my_id', None))
        try:
            self._file_context = self.service.timeline_file_context(chat.id)
        except (AttributeError, ValueError):
            self._file_context = None
        self._history_generation += 1
        generation = self._history_generation
        target_requested = message_id is not None or time_ms is not None
        self._history_target = (chat.id, generation, message_id) if target_requested else None
        self._spotlight_id = None
        self._refresh_spotlight()
        self.update_call_availability()
        if not same:
            self.input.setPlainText(self._drafts.get(chat.id, ''))
            self._set_reply_target(self._reply_targets.get(chat.id))
        self._set_composer_visible(True)
        self.h_avatar.set_identity(chat.display, chat.id)
        self.h_avatar.set_photo(chat.avatar_url)
        self.h_title.setText(chat.display)
        self.h_sub.setText("Обновление сообщений…")
        self.input.setFocus()
        if not same or target_requested:
            self.history_search.clear()
        if not same:
            # Смена чата — полный рендер из кэша (мгновенно).
            self._full_render(self.service.local_history(chat.id))
        else:
            # Every open gets a fresh scope, including a jump within this chat.
            # Replace old popup bindings before an asynchronous history reply.
            for index, row in enumerate(list(self._rows)):
                self._replace_row(index, row['msg'])
        chat_id = chat.id
        context = self._capture_history_context()
        if context is not None and not self._history_context_current(context):
            self.h_sub.setText('Нет подтверждённого соединения для загрузки сообщений')
            return
        if target_requested:
            if (type(message_id) is not int or not -(2**63) <= message_id < 2**63 or
                    type(time_ms) not in (int, float) or
                    (type(time_ms) is float and not math.isfinite(time_ms))):
                self.h_sub.setText('Для перехода нужны подтверждённые ID и время сообщения')
                return
            self.h_sub.setText('Загрузка найденного сообщения…')
            self._history_target_pending = True
            # Actual public target-open path uses from:message.time. Its normal
            # window is backward15/forward30; do not substitute ID/now/latest.
            run_async(
                self.service.open_chat_window, chat.id,
                from_cursor=time_ms, backward=15, forward=30,
                on_done=lambda messages: self._on_target_history(
                    chat_id, generation, context, message_id, messages),
                on_error=lambda _error: self._on_history_error(
                    chat_id, generation, context, target=True),
            )
            return
        reload_revision = self._history_cache_revision
        run_async(
            self.service.open_chat, chat.id,
            on_done=lambda messages: self._on_history(chat_id, generation, messages, context, reload_revision),
            on_error=lambda _error: self._on_history_error(chat_id, generation, context),
        )

    def _capture_history_context(self):
        client = getattr(self.service, 'client', None)
        context = getattr(client, 'authenticated_session', None)
        if isinstance(client, MaxClient) or isinstance(context, AuthenticatedSession):
            return client, context, getattr(self.service, 'my_id', None)
        # Old offline widget fixtures have no real typed client/auth context.
        return None

    def _history_context_current(self, binding):
        if binding is None:
            return True
        client, context, owner = binding
        return (getattr(self.service, 'client', None) is client and
                isinstance(context, AuthenticatedSession) and
                getattr(client, 'authenticated_session', None) is context and
                getattr(client, 'state', None) is ConnectionState.CONNECTED and
                type(owner) is int and context.owner_id == owner and
                getattr(self.service, 'my_id', None) == owner)

    def _history_current(self, chat_id, generation, context):
        return (isValid(self) and isValid(self.scroll) and
                self.chat is not None and self.chat.id == chat_id and
                generation == self._history_generation and
                self._history_context_current(context))

    def chat_metadata_binding(self):
        """Capture a fresh authenticated local-read context, never reconnect."""
        if self.chat is None:
            return None
        binding = (self.chat.id, self._history_generation, self._capture_history_context())
        return binding if self.chat_metadata_binding_current(binding) else None

    def chat_metadata_binding_current(self, binding):
        if (not isinstance(binding, tuple) or len(binding) != 3 or self._metadata_owner is None
                or not self._history_current(*binding)):
            return False
        client, owner = self._metadata_owner
        if (getattr(self.service, 'client', None) is not client
                or getattr(self.service, 'my_id', None) != owner):
            return False
        if isinstance(self.service, MaxService) and getattr(self.service.store, 'cache_owner', None) != owner:
            return False
        return True

    def refresh_chat_metadata(self, chat, binding):
        """Refresh only header metadata; preserve history, composer and scope."""
        if (not isinstance(chat, Chat) or not self.chat_metadata_binding_current(binding)
                or chat.id != binding[0] or not isValid(self.h_title) or not isValid(self.h_avatar)):
            return False
        self.chat = chat
        self.h_title.setText(chat.display)
        self.h_avatar.set_identity(chat.display, chat.id)
        self.h_avatar.set_photo(chat.avatar_url)
        self.update_call_availability()
        self.read_receipts.schedule()
        return True

    def _on_target_history(self, chat_id, generation, context, message_id, messages):
        if not self._history_current(chat_id, generation, context):
            return
        if (not isinstance(messages, list) or
                any(not isinstance(message, Message) or message.chat_id != chat_id for message in messages)):
            self._on_history_error(chat_id, generation, context, target=True)
            return
        if self._pending_target_changes:
            # Keep the requested window membership; overlay only IDs whose
            # owner-bound cache changed while this window waited in Qt's queue.
            messages = [self._pending_target_changes.get(message.id, message) for message in messages]
            messages = [message for message in messages if message is not None and message.status != 'removed']
        self._history_target_pending = False
        self._pending_target_changes = {}
        self._cancel_change_scroll()
        self._spotlight_id = message_id
        self._set_messages(messages)
        found = self._refresh_spotlight()
        if not found:
            self._spotlight_id = None
        self.h_sub.setText('Найденное сообщение' if found else 'Сообщение не найдено в полученной истории')
        self.chat_touched.emit(chat_id)
        if found:
            QTimer.singleShot(0, lambda: self._scroll_to_search_target(
                chat_id, generation, context, message_id))

    def _scroll_to_search_target(self, chat_id, generation, context, message_id):
        if not self._history_current(chat_id, generation, context):
            return
        for row in self._rows:
            message = row['msg']
            if (type(message.id) is int and message.id == message_id and
                    message.chat_id == chat_id and self._spotlight_id == message_id and
                    isValid(row['bubble'])):
                self.scroll.ensureWidgetVisible(row['bubble'], 0, 24)
                return

    def _apply_spotlight(self, bubble, message):
        active = (self.chat is not None and message.chat_id == self.chat.id and
                  type(message.id) is int and message.id == self._spotlight_id)
        bubble.setProperty('searchSpotlight', active)
        frame = bubble.findChild(QFrame, 'Bubble')
        if frame is not None:
            setter = getattr(frame, 'set_spotlight', None)
            if callable(setter):
                setter(active)
                return active
            base = frame.property('searchBaseStyle')
            if base is None:
                base = frame.styleSheet()
                frame.setProperty('searchBaseStyle', base)
            frame.setStyleSheet(base + ('\n#Bubble { border:2px solid #0085FF; }' if active else ''))
        return active

    def _refresh_spotlight(self):
        found = False
        for row in self._rows:
            found = self._apply_spotlight(row['bubble'], row['msg']) or found
        return found

    def update_call_availability(self):
        checker = getattr(self.service, 'calls_available', None)
        available = bool(callable(checker) and checker(self.chat))
        for button, title in ((self.call_button, 'Позвонить'), (self.video_button, 'Видеозвонок')):
            button.setEnabled(available)
            button.setToolTip(title if available else 'Звонок этому контакту сейчас недоступен')

    def _request_call(self, is_video):
        if self.chat is not None:
            self.call_requested.emit(self.chat, is_video)

    def _on_history(self, chat_id: int, generation: int, messages: list[Message], context=None,
                    reload_revision=None) -> None:
        if not self._history_current(chat_id, generation, context):
            return
        if reload_revision is not None and reload_revision != self._history_cache_revision:
            # A push/send reload has already rendered a newer local state while
            # this latest-history result waited in Qt's queue. The read merges
            # into the store; present its current state rather than its old list.
            messages = self.service.local_history(chat_id)
            if not self._history_current(chat_id, generation, context):
                return
            if (not isinstance(messages, list) or any(not isinstance(message, Message)
                    or message.chat_id != chat_id for message in messages)):
                self._on_history_error(chat_id, generation, context)
                return
        self._set_messages(messages)
        self.h_sub.setText("Сообщения обновлены")
        self.chat_touched.emit(self.chat.id)

    def _on_history_error(self, chat_id: int, generation: int, context=None, *, target=False) -> None:
        if not self._history_current(chat_id, generation, context):
            return
        if target:
            self._history_target_pending = False
            self._pending_target_changes = {}
        # Preserve the current cache. A failed refresh is not an empty history.
        self.h_sub.setText('Не удалось загрузить найденное сообщение' if target else 'Не удалось обновить сообщения')

    def reload(self) -> None:
        if self.chat is not None:
            messages = self.service.local_history(self.chat.id)
            self._history_cache_revision += 1
            self._set_messages(messages)

    def message_change_binding(self, chat_id):
        context = self._capture_history_context()
        generation = self._history_generation
        return (chat_id, generation, context) if self._history_current(chat_id, generation, context) else None

    def _change_current(self, event, binding):
        if binding is None or not self._history_current(*binding) or event.chat_id != binding[0]:
            return False
        checker = getattr(self.service, 'is_current_message_change', None)
        try:
            return callable(checker) and checker(event) is True
        except (AttributeError, ValueError):
            return False

    def apply_message_change(self, event, changes, binding):
        """Patch current row IDs only; never load latest or extend a target window."""
        if not self._change_current(event, binding):
            return False
        ids = event.message_ids
        if (not isinstance(changes, dict) or set(changes) != set(ids)
                or any(type(identity) is not int for identity in changes)
                or any(message is not None and (not isinstance(message, Message)
                       or type(message.id) is not int or message.id != identity
                       or type(message.chat_id) is not int or message.chat_id != event.chat_id)
                       for identity, message in changes.items())):
            return False
        if self._history_target_pending:
            self._pending_target_changes.update(changes)
        self._history_cache_revision += 1
        affected = [index for index, row in enumerate(self._rows) if row['msg'].id in changes]
        if not affected:
            return True
        scroll_state = self._change_scroll_pending
        if scroll_state is not None and not self._change_current(scroll_state['event'], scroll_state['binding']):
            scroll_state = None
        if scroll_state is None:
            anchors = []
            for row in self._rows:
                if isValid(row['bubble']):
                    y = row['bubble'].mapTo(self.scroll.viewport(), row['bubble'].rect().topLeft()).y()
                    if y + row['bubble'].height() > 0:
                        anchors.append((row['key'], y))
            scroll_state = {'event': event, 'binding': binding, 'anchors': anchors,
                            'value': self.scroll.verticalScrollBar().value()}
        selected_key = None
        if 0 <= self._search_index < len(self._search_matches):
            index = self._search_matches[self._search_index]
            if 0 <= index < len(self._rows):
                selected_key = self._rows[index]['key']
        previous_search_index = self._search_index
        for index in reversed(affected):
            row = self._rows[index]
            message = changes[row['msg'].id]
            if message is None or message.status == 'removed':
                for widget in (row['bubble'], row['divider']):
                    if widget is not None and isValid(widget):
                        self.msg_layout.removeWidget(widget)
                        widget.hide()
                        widget.deleteLater()
                self._rows.pop(index)
            else:
                self._replace_row(index, message)
        self._sync_change_dividers()
        if self._spotlight_id is not None and not any(row['msg'].id == self._spotlight_id for row in self._rows):
            self._spotlight_id = None
            if self._history_target is not None and not self._history_target_pending:
                self.h_sub.setText('Сообщение больше недоступно')
        self._refresh_spotlight()
        self._search_after_change(selected_key, previous_search_index)
        self._change_scroll_pending = scroll_state
        self._change_scroll_timer.start(0)
        return True

    def _sync_change_dividers(self):
        previous = None
        for row in self._rows:
            day = _day_key(row['msg'].time_ms)
            wanted = day != previous
            divider = row['divider']
            if divider is not None and (not wanted or row['day'] != day):
                self.msg_layout.removeWidget(divider)
                divider.hide()
                divider.deleteLater()
                row['divider'] = None
            if wanted and row['divider'] is None:
                divider = _DayDivider(fmt_day_divider(row['msg'].time_ms))
                self.msg_layout.insertWidget(self.msg_layout.indexOf(row['bubble']), divider)
                row['divider'] = divider
            row['day'] = day
            previous = day

    def _search_after_change(self, selected_key, previous_index):
        query = self.history_search.text().strip().casefold()
        self._search_matches = [index for index, row in enumerate(self._rows)
                                if query and query in (row['msg'].display_text or '').casefold()]
        self._search_index = next((position for position, index in enumerate(self._search_matches)
                                   if self._rows[index]['key'] == selected_key), -1)
        if self._search_index < 0 and self._search_matches:
            self._search_index = min(max(0, previous_index), len(self._search_matches) - 1)
        self.next_match.setEnabled(bool(self._search_matches))
        self.search_result.setText('' if not query else
                                  f'{self._search_index + 1} / {len(self._search_matches)}' if self._search_matches
                                  else 'Найдено: 0')

    def _cancel_change_scroll(self):
        self._change_scroll_pending = None
        if isValid(self._change_scroll_timer):
            self._change_scroll_timer.stop()

    def _restore_change_scroll(self):
        state, self._change_scroll_pending = self._change_scroll_pending, None
        if state is None or not self._change_current(state['event'], state['binding']):
            return
        self.msg_layout.activate()
        bar = self.scroll.verticalScrollBar()
        for key, old_y in state['anchors']:
            row = next((row for row in self._rows if row['key'] == key and isValid(row['bubble'])), None)
            if row is not None:
                y = row['bubble'].mapTo(self.scroll.viewport(), row['bubble'].rect().topLeft()).y()
                bar.setValue(bar.value() + y - old_y)
                return
        bar.setValue(min(state['value'], bar.maximum()))

    def apply_appearance(self) -> None:
        """Restyle existing local rows without refetching history or drafts."""
        self.container.set_appearance(self.settings.theme, self.settings.chat_theme)
        color = palette_for(self.settings.theme).text_dim
        self.attach_btn.setIcon(line_icon('attach', color))
        if self._recorder is None:
            self.voice_btn.setIcon(line_icon('microphone', color))
        for button in self.findChildren(QPushButton):
            icon = button.property('appearanceIcon')
            if icon:
                button.setIcon(line_icon(icon, color, 24))
        for index, row in enumerate(list(self._rows)):
            self._replace_row(index, row['msg'])

    # ── инкрементальный рендер: не сносим всю ленту на каждый push/отправку ──

    @staticmethod
    def _key(m: Message) -> str:
        if m.local_id:
            return "l" + m.local_id
        if m.id is not None:
            return "s" + str(m.id)
        return "t" + str(m.time_ms)

    def _set_messages(self, messages: list[Message]) -> None:
        old_keys = [r["key"] for r in self._rows]
        new_keys = [self._key(m) for m in messages]
        # Общий случай: старое — префикс нового (дописали в конец) ± смена статуса.
        if old_keys and new_keys[: len(old_keys)] == old_keys:
            bar = self.scroll.verticalScrollBar()
            was_bottom = bar.maximum() - bar.value() < 140
            for i, r in enumerate(self._rows):
                if messages[i] != r["msg"]:
                    self._replace_row(i, messages[i])
            appended = False
            for m in messages[len(self._rows):]:
                self._append_row(m)
                appended = True
            if appended and was_bottom:
                QTimer.singleShot(0, self._scroll_to_bottom)
        else:
            self._full_render(messages)
        self._find_messages()
        self.read_receipts.schedule()

    def _full_render(self, messages: list[Message]) -> None:
        self._clear_all()
        for m in messages:
            self._append_row(m)
        QTimer.singleShot(0, self._scroll_to_bottom)
        receipts = getattr(self, 'read_receipts', None)
        if receipts is not None:
            receipts.schedule()

    def _make_bubble(self, m: Message) -> MessageBubble:
        context = self._file_context
        bubble = MessageBubble(
            m, self.settings.accent, palette_for(self.settings.theme),
            on_open_media=self._open_media, on_play_voice=self._play_voice,
            on_reaction=self._react_to,
            on_open_app=lambda message, index, row, button, owner=context:
                self._miniapps.open(message, index, row, button, owner),
            font_scale=self.settings.chat_font_scale,
            chat_preset=self.settings.chat_theme,
            file_actions=self._file_actions_for(m),
            language=self._message_action_language(),
        )
        bubble.file_action_requested.connect(
            lambda action, message, index, owner=context, widget=bubble:
                self._file_action(action, message, index, owner, widget))
        bubble.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        bubble.customContextMenuRequested.connect(
            lambda position, message=m, widget=bubble: self._message_menu(message).exec(widget.mapToGlobal(position)))
        for text in bubble.findChildren(QLabel, 'MessageText'):
            # Selectable QLabel owns its native Copy/Select All popup, so the
            # event otherwise never reaches the message bubble's menu. Filter
            # only ContextMenu; normal mouse/key/link events remain untouched.
            text._message_menu_message = m
            text.installEventFilter(self)
        self._apply_spotlight(bubble, m)
        return bubble

    def file_context_current(self, context):
        if (not isValid(self) or context is None or context is not self._file_context
                or self.chat is None or getattr(context, 'chat_id', None) != self.chat.id):
            return False
        try:
            return self.service.is_current_timeline_file_context(context) is True
        except (AttributeError, ValueError):
            return False

    def close_file_scope(self):
        if hasattr(self, '_miniapps') and isValid(self._miniapps):
            self._miniapps.reset()
        if hasattr(self, '_photos') and isValid(self._photos):
            self._photos.reset()
        context, self._file_context = self._file_context, None
        if context is None:
            return
        clear = getattr(self.service, 'clear_timeline_files', None)
        if callable(clear):
            clear(context)
        if isValid(self):
            self.file_scope_closed.emit(context)

    def _file_actions_for(self, message):
        if not self.file_context_current(self._file_context):
            return {}
        try:
            actions = self.service.timeline_file_actions(self._file_context, message)
            return actions if isinstance(actions, dict) else {}
        except (AttributeError, ValueError):
            return {}

    def _file_action(self, action, message, index, context, bubble):
        if (not self.file_context_current(context) or not isinstance(message, Message) or not isValid(bubble)
                or type(index) is not int or not 0 <= index < len(message.display_attaches)
                or message.display_attaches[index].type != 'FILE'
                or not any(row['msg'] is message and row['bubble'] is bubble for row in self._rows)):
            return
        capabilities = self._file_actions_for(message).get(index, {})
        if not isinstance(capabilities, dict):
            return
        if action == 'copy_name':
            name = capabilities.get('file_name')
            if isinstance(name, str) and name:
                from PySide6.QtWidgets import QApplication
                QApplication.clipboard().setText(name)
            return
        if action != 'save' or capabilities.get('download_available') is not True:
            return
        try:
            descriptor = self.service.get_timeline_file_descriptor(context, message, index)
        except (AttributeError, ValueError):
            self.h_sub.setText('Файл больше недоступен. Обновите переписку.')
            return
        if self.file_context_current(context):
            self.file_save_requested.emit(descriptor)

    def show_file_message(self, descriptor):
        if not self.file_context_current(getattr(descriptor, 'context', None)):
            return
        try:
            if self.service.is_current_conversation_file(descriptor.context, descriptor) is not True:
                return
        except (AttributeError, ValueError):
            return
        for row in self._rows:
            if row['msg'].id == descriptor.message_id:
                self._spotlight_id = descriptor.message_id
                self._refresh_spotlight()
                self.scroll.ensureWidgetVisible(row['bubble'], 0, 70)
                return

    def closeEvent(self, event):  # noqa: N802
        self.close_file_scope()
        super().closeEvent(event)

    def _message_action_language(self):
        language = getattr(self.settings, 'ui_language', 'ru')
        return normalize_language(language if isinstance(language, str) else 'ru')

    def _message_tr(self, source):
        return tr(source, self._message_action_language())

    def retranslate_media(self, language=None):
        """Relabel existing media controls without rebuilding or fetching."""
        if not isValid(self):
            return
        language = self._message_action_language() if language is None else language
        photos = getattr(self, '_photos', None)
        if photos is not None and isValid(photos):
            photos.retranslate(language)
        for row in self._rows:
            bubble = row['bubble']
            if isValid(bubble):
                bubble.retranslate_file_actions(language)

    def retranslate_message_actions(self, _language=None):
        """Update only owned action controls; never touch message text or drafts."""
        if not isValid(self):
            return
        self._message_menu_generation += 1
        for menu in tuple(self._message_action_menus):
            if isValid(menu):
                menu.close()
        for dialog, update in tuple(self._message_dialog_retranslations.items()):
            if isValid(dialog):
                update()

    def _message_menu(self, msg: Message) -> QMenu:
        menu = QMenu(self)
        self._message_action_menus.add(menu)
        binding = self.chat_metadata_binding()
        generation, language = self._message_menu_generation, self._message_action_language()
        def current():
            return (isValid(self) and generation == self._message_menu_generation
                    and language == self._message_action_language()
                    and self._local_message_action_current(msg, binding))
        menu._message_action_current = current
        if msg.display_text:
            menu.addAction(self._message_tr('Копировать текст'), lambda: self._copy_message(msg)
                           if current() else None)
        if type(msg.id) is not int or msg.id <= 0:
            return menu
        menu.addAction(self._message_tr('Ответить'), lambda: self._reply_to(msg) if current() else None)
        write_allowed = self._message_action_current(msg, binding)
        forward = menu.addAction(self._message_tr('Переслать…'), lambda: self._forward_message(msg, binding) if current() else None)
        forward.setEnabled(write_allowed)
        if msg.outgoing:
            edit = menu.addAction(self._message_tr('Изменить…'), lambda: self._edit_message(msg, binding) if current() else None)
            edit.setEnabled(write_allowed)
        reactions = menu.addMenu(self._message_tr('Реакция'))
        # Keep the Python wrapper alive as long as its native parent popup.
        menu._message_submenus = [reactions]
        reactions.setEnabled(write_allowed)
        for emoji in ('👍', '❤️', '🔥', '😂', '😮', '😢'):
            reactions.addAction(emoji, lambda checked=False, value=emoji: self._react_to(msg, value, binding) if current() else None)
        reactions.addAction(self._message_tr('Убрать мою реакцию'), lambda: self._react_to(msg, None, binding) if current() else None)
        menu.addAction(self._message_tr('Отметить прочитанным'), lambda: self._mark_read(msg, binding) if current() else None)
        menu.addSeparator()
        delete = menu.addAction(self._message_tr('Удалить…'), lambda: self._delete_message(msg, binding) if current() else None)
        delete.setEnabled(write_allowed)
        return menu

    def _message_text_menu(self, msg: Message, label: QLabel, position) -> None:
        """Keep text selection/link interaction while exposing message actions."""
        if not isValid(label):
            return
        menu = self._message_menu(msg)
        current = menu._message_action_current
        selected = label.selectedText()
        menu.addSeparator()
        if selected:
            menu.addAction(self._message_tr('Копировать выделенное'), lambda: QApplication.clipboard().setText(selected)
                if isValid(label) and current() else None)
        length = len((msg.display_text or '').encode('utf-16-le', errors='surrogatepass')) // 2
        menu.addAction(self._message_tr('Выделить всё'), lambda: label.setSelection(0, length)
            if isValid(label) and current() else None)
        try:
            menu.exec(label.mapToGlobal(position))
        finally:
            menu.deleteLater()

    @staticmethod
    def _copy_message(msg: Message) -> None:
        from PySide6.QtWidgets import QApplication
        QApplication.clipboard().setText(msg.display_text or '')

    def _set_reply_target(self, msg: Optional[Message]) -> None:
        self._reply_target = msg
        if self.chat is not None:
            if msg is None:
                self._reply_targets.pop(self.chat.id, None)
            else:
                self._reply_targets[self.chat.id] = msg
        self.reply_label.setText('Ответ на сообщение\n' + (msg.preview[:180] if msg else ''))
        self.reply_bar.setVisible(msg is not None and self.chat is not None)

    def _reply_to(self, msg: Message) -> None:
        if self.chat is None or self.chat.id != msg.chat_id:
            return
        self._set_reply_target(msg)
        self.input.setFocus()

    def reply_to_last_loaded(self) -> None:
        """Prepare a local reply; never load history, send, or change a draft."""
        if (not isValid(self) or not self.isVisible() or self.chat is None
                or QApplication.activeModalWidget() is not None
                or QApplication.activePopupWidget() is not None
                or not self.input.isVisible() or not self.input.isEnabled()
                or self.input.isReadOnly() or self.chat_metadata_binding() is None):
            return
        for row in reversed(self._rows):
            msg = row.get('msg')
            if (isinstance(msg, Message) and msg.chat_id == self.chat.id
                    and type(msg.id) is int and msg.id > 0
                    and msg.status not in ('pending', 'failed', 'unknown', 'removed')
                    and msg.control_display_text is None and msg.has_display_content):
                self._reply_to(msg)
                return

    def _local_message_action_current(self, msg, binding):
        return (isValid(self) and isinstance(msg, Message) and binding is not None
                and self.chat_metadata_binding_current(binding) and binding[0] == msg.chat_id)

    def _message_action_current(self, msg, binding):
        if (not self._local_message_action_current(msg, binding)
                or type(msg.id) is not int or msg.id <= 0
                or msg.status in ('pending', 'failed', 'unknown', 'removed')
                or binding[2] is None or not isinstance(binding[2][1], AuthenticatedSession)):
            return False
        try:
            return self.service.search_context() is binding[2][1]
        except (AttributeError, ValueError):
            return False

    def _run_message_action(self, method, msg, binding, *args, affected_chat_id=None, **kwargs) -> None:
        if not self._message_action_current(msg, binding) or self._message_action_request is not None:
            return
        request = self._message_action_request = object()
        context = binding[2][1]
        def finish(result=None):
            if not isValid(self) or self._message_action_request is not request:
                return
            self._message_action_request = None
            if not self._message_action_current(msg, binding):
                return
            if isinstance(result, _MessageActionFailure):
                title, text = _MESSAGE_ACTION_FAILURE_TEXT.get(result.kind, _MESSAGE_ACTION_FAILURE_TEXT['unexpected'])
                QMessageBox.warning(self, self._message_tr(title), self._message_tr(text))
            else:
                self._after_send(msg.chat_id if affected_chat_id is None else affected_chat_id)
        try:
            run_async(_bound_message_action, self.service, context, method, msg.chat_id, args, kwargs,
                      on_done=finish, on_error=lambda _error: finish(_MessageActionFailure('unexpected')))
        except Exception:
            finish(_MessageActionFailure('unexpected'))

    def _edit_message_text(self, text):
        dialog = QInputDialog(self)
        dialog.setInputMode(QInputDialog.InputMode.TextInput)
        dialog.setOption(QInputDialog.InputDialogOption.UsePlainTextEditForTextInput, True)
        dialog.setTextValue(text)
        def update():
            dialog.setWindowTitle(self._message_tr('Изменить сообщение'))
            dialog.setLabelText(self._message_tr('Текст сообщения:'))
            dialog.setOkButtonText(self._message_tr('Сохранить'))
            dialog.setCancelButtonText(self._message_tr('Отменить'))
        self._message_dialog_retranslations[dialog] = update
        update()
        try:
            accepted = dialog.exec() == QDialog.DialogCode.Accepted
            return (dialog.textValue(), accepted) if isValid(dialog) else (text, False)
        finally:
            self._message_dialog_retranslations.pop(dialog, None)
            if isValid(dialog):
                dialog.deleteLater()

    def _edit_message(self, msg: Message, binding=_UNCAPTURED_ACTION) -> None:
        if binding is _UNCAPTURED_ACTION:
            binding = self.chat_metadata_binding()
        if not msg.outgoing or not self._message_action_current(msg, binding):
            return
        text, ok = self._edit_message_text(msg.text or '')
        if self._message_action_current(msg, binding) and ok and text.strip() and text != msg.text:
            self._run_message_action(self.service.edit_message, msg, binding, msg.id, text)

    def _delete_message(self, msg: Message, binding=_UNCAPTURED_ACTION) -> None:
        if binding is _UNCAPTURED_ACTION:
            binding = self.chat_metadata_binding()
        if not self._message_action_current(msg, binding):
            return
        box = QMessageBox(self)
        local = box.addButton(self._message_tr('Удалить у меня'), QMessageBox.ButtonRole.DestructiveRole)
        everyone = (box.addButton(self._message_tr('Удалить у всех'), QMessageBox.ButtonRole.DestructiveRole)
                    if msg.outgoing and msg.chat_id != 0 else None)
        cancel = box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(cancel)
        def update():
            box.setWindowTitle(self._message_tr('Удалить сообщение'))
            box.setText(self._message_tr('Удалить выбранное сообщение?'))
            local.setText(self._message_tr('Удалить у меня'))
            if everyone is not None:
                everyone.setText(self._message_tr('Удалить у всех'))
            cancel.setText(self._message_tr('Отменить'))
        self._message_dialog_retranslations[box] = update
        update()
        try:
            box.exec()
            if not isValid(box) or not self._message_action_current(msg, binding):
                return
            selected = box.clickedButton()
            if selected is local or (everyone is not None and selected is everyone):
                self._run_message_action(self.service.delete_messages, msg, binding, [msg.id], for_all=selected is everyone)
        finally:
            self._message_dialog_retranslations.pop(box, None)
            if isValid(box):
                box.deleteLater()

    def _forward_message(self, msg: Message, binding=_UNCAPTURED_ACTION) -> None:
        if binding is _UNCAPTURED_ACTION:
            binding = self.chat_metadata_binding()
        if not self._message_action_current(msg, binding):
            return
        target = _ForwardChatDialog(self.service.list_chats(), self, language=self._message_action_language())
        self._message_dialog_retranslations[target] = lambda: target.retranslate(self._message_action_language())
        try:
            accepted = target.exec() == QDialog.DialogCode.Accepted
            if not isValid(target) or not self._message_action_current(msg, binding):
                return
            target_id = target.selected_chat_id
            if accepted and type(target_id) is int:
                self._run_message_action(self.service.forward_messages, msg, binding, [msg.id], target_id,
                                         affected_chat_id=target_id)
        finally:
            self._message_dialog_retranslations.pop(target, None)
            if isValid(target):
                target.deleteLater()

    def _react_to(self, msg: Message, emoji: Optional[str], binding=_UNCAPTURED_ACTION) -> None:
        if binding is _UNCAPTURED_ACTION:
            binding = self.chat_metadata_binding()
        if not self._message_action_current(msg, binding):
            return
        if emoji is None:
            self._run_message_action(self.service.remove_message_reaction, msg, binding, msg.id)
        else:
            self._run_message_action(self.service.set_message_reaction, msg, binding, msg.id, emoji)

    def _mark_read(self, msg: Message, binding=None) -> None:
        if (binding is None or not self.chat_metadata_binding_current(binding)
                or binding[0] != msg.chat_id or binding[2] is None):
            return
        def failed(error):
            if self.chat_metadata_binding_current(binding):
                QMessageBox.warning(self, self._message_tr('Прочтение не подтверждено'), error)
        run_async(self.service.mark_chat_read, msg.chat_id, message_id=msg.id,
                  mark=msg.time_ms, context=binding[2][1], on_error=failed)

    def _append_row(self, m: Message) -> None:
        prev_day = self._rows[-1]["day"] if self._rows else None
        day = _day_key(m.time_ms)
        divider = None
        if day != prev_day:
            divider = _DayDivider(fmt_day_divider(m.time_ms))
            self.msg_layout.insertWidget(self.msg_layout.count() - 1, divider)
        bubble = self._make_bubble(m)
        self.msg_layout.insertWidget(self.msg_layout.count() - 1, bubble)
        self._rows.append(
            {"key": self._key(m), "msg": m, "bubble": bubble, "divider": divider, "day": day}
        )

    def _replace_row(self, i: int, m: Message) -> None:
        r = self._rows[i]
        idx = self.msg_layout.indexOf(r["bubble"])
        if idx < 0:
            return
        previous = r["bubble"]
        self.msg_layout.removeWidget(previous)
        previous.hide()
        previous.deleteLater()
        bubble = self._make_bubble(m)
        self.msg_layout.insertWidget(idx, bubble)
        r["bubble"] = bubble
        r["msg"] = m
        r["key"] = self._key(m)

    def _clear_all(self) -> None:
        self._search_matches = []
        self._search_index = -1
        while self.msg_layout.count() > 1:
            item = self.msg_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.hide()
                w.deleteLater()
        self._rows = []

    def _scroll_to_bottom(self) -> None:
        # A singleShot callback can outlive this Python wrapper's C++ widget
        # tree. Validate the actual Qt receivers before touching a scrollbar.
        if not isValid(self) or not isValid(self.scroll) or self._history_target is not None:
            return
        bar = self.scroll.verticalScrollBar()
        bar.setValue(bar.maximum())

    # ───────────────────────── sending ─────────────────────────

    def _send_text(self) -> None:
        if self.chat is None:
            return
        selected = self.chat_metadata_binding()
        if selected is None:
            return
        binding = selected[2]
        text = self.input.toPlainText().strip()
        if not text:
            return
        self.input.clear()
        chat_id = self.chat.id
        self._drafts.pop(chat_id, None)
        reply = self._reply_target
        self._set_reply_target(None)
        method, args = ((self.service.reply_to_message, (chat_id, reply.id, text))
                        if reply is not None else (self.service.send_text, (chat_id, text)))
        options = {'context': binding[1]} if binding is not None else {}

        def complete(message=None, error=None):
            if isValid(self) and self._history_context_current(binding):
                self._on_sent(chat_id, message, error)

        run_async(
            method, *args, **options,
            on_done=complete,
            on_error=lambda error: complete(error=error),
        )

    def _after_send(self, chat_id: int) -> None:
        if self.chat and self.chat.id == chat_id:
            self.reload()
        self.chat_touched.emit(chat_id)

    def _on_sent(self, chat_id: int, msg, err: str | None = None) -> None:
        self._after_send(chat_id)
        reason = err or (msg.error if (msg is not None and msg.status in {"failed", "unknown"}) else None)
        if reason:
            QMessageBox.warning(self, "Сообщение не отправлено", str(reason))

    def _attach_menu(self) -> None:
        if self.chat is None:
            return
        menu = QMenu(self)
        menu.addAction("🖼  Фото", lambda: self._pick_and_send(
            AttachKind.PHOTO, "Изображения (*.png *.jpg *.jpeg *.gif *.webp *.bmp)"))
        menu.addAction("🎬  Видео", lambda: self._pick_and_send(
            AttachKind.VIDEO, "Видео (*.mp4 *.mov *.mkv *.avi *.webm)"))
        menu.addAction("📎  Файл", lambda: self._pick_and_send(AttachKind.FILE, "Все файлы (*.*)"))
        menu.exec(self.attach_btn.mapToGlobal(self.attach_btn.rect().topRight()))

    def _pick_and_send(self, kind: AttachKind, file_filter: str) -> None:
        if self.chat is None:
            return
        path, _ = QFileDialog.getOpenFileName(self, "Выберите файл", "", file_filter)
        if not path:
            return
        chat_id = self.chat.id
        text = self.input.toPlainText().strip()
        self.input.clear()
        run_async(
            self.service.send_media, chat_id, path, kind,
            text=text,
            on_done=lambda _m: self._after_send(chat_id),
            on_error=lambda e: self._media_error(chat_id, e),
        )
        self.reload()

    def _media_error(self, chat_id: int, message: str) -> None:
        self._after_send(chat_id)
        QMessageBox.warning(self, "Не удалось отправить", f"Ошибка загрузки: {message}")

    # ───────────────────────── voice ─────────────────────────

    def _toggle_voice(self) -> None:
        if self.chat is None:
            return
        try:
            from .media import MULTIMEDIA_AVAILABLE, VoiceRecorder
        except Exception:
            MULTIMEDIA_AVAILABLE = False
            VoiceRecorder = None
        if not MULTIMEDIA_AVAILABLE:
            QMessageBox.information(
                self, "Голосовые недоступны",
                "Модуль QtMultimedia не установлен. Установите PySide6-Addons "
                "для записи голосовых сообщений.",
            )
            return
        if self._recorder is None:
            self._recorder = VoiceRecorder(self.settings)
            self._recorder.recorded.connect(self._on_voice_recorded)
            self._recorder.cancelled.connect(self._on_voice_cancelled)
        if self._recorder.is_recording():
            self._recorder.stop()
        else:
            self.voice_btn.setIcon(line_icon('stop', self.settings.accent))
            self.voice_btn.setToolTip("Остановить и отправить")
            self._recorder.start()

    def _on_voice_recorded(self, path: str, duration_ms: int) -> None:
        self.voice_btn.setIcon(line_icon('microphone', palette_for(self.settings.theme).text_dim))
        self.voice_btn.setToolTip("Записать голосовое сообщение")
        if self.chat is None or not path:
            return
        chat_id = self.chat.id
        run_async(
            self.service.send_voice, chat_id, path, duration_ms,
            on_done=lambda _m: self._after_send(chat_id),
            on_error=lambda e: self._media_error(chat_id, e),
        )
        self.reload()

    def _on_voice_cancelled(self) -> None:
        self.voice_btn.setIcon(line_icon('microphone', palette_for(self.settings.theme).text_dim))
        self.voice_btn.setToolTip("Записать голосовое сообщение")

    # ───────────────────────── media open / play ─────────────────────────

    def _open_media(self, msg: Message, attach: Attach) -> None:
        if attach.is_image and attach.deleted:
            return
        if attach.is_image and not (attach.local_path and os.path.isfile(attach.local_path)):
            self._photos.open_photo(msg, attach)
            return
        if attach.type == 'FILE':
            # Old callback callers must also pass through the bound save flow.
            row = next((row for row in self._rows if row['msg'] is msg), None)
            index = next((i for i, value in enumerate(msg.display_attaches) if value is attach), None)
            if row is not None and index is not None:
                self._file_action('save', msg, index, self._file_context, row['bubble'])
            return
        if msg.display_source != 'outer':
            # Nested media cannot reuse the ordinary current-chat resolver.
            # FILE above has its own exact selected-attachment descriptor.
            return
        # Локальный файл (например, только что отправленный) открываем сразу.
        if attach.local_path and os.path.isfile(attach.local_path):
            self._show_media(attach.local_path, attach)
            return
        if msg.id is None:
            QMessageBox.information(self, "Медиа", "Файл ещё не доступен на сервере.")
            return
        chat_id = self.chat.id if self.chat else msg.chat_id
        run_async(
            self.service.resolve_attach_url, attach, chat_id, msg.id,
            on_done=lambda url: self._on_media_url(url, attach),
            on_error=lambda e: QMessageBox.warning(self, "Медиа", str(e)),
        )

    def _on_media_url(self, url: Optional[str], attach: Attach) -> None:
        if not url:
            QMessageBox.information(self, "Медиа", "Не удалось получить ссылку на файл.")
            return
        self._show_media(url, attach)

    def _show_media(self, url_or_path: str, attach: Attach) -> None:
        try:
            from .media import MediaViewer
        except Exception:
            self._open_externally(url_or_path)
            return
        viewer = MediaViewer(url_or_path, attach, self.settings, self)
        viewer.exec()

    def _play_voice(self, msg: Message, attach: Attach) -> None:
        self._open_media(msg, attach)

    def _open_externally(self, url_or_path: str) -> None:
        from PySide6.QtGui import QDesktopServices
        from PySide6.QtCore import QUrl
        if os.path.isfile(url_or_path):
            QDesktopServices.openUrl(QUrl.fromLocalFile(url_or_path))
        else:
            QDesktopServices.openUrl(QUrl(url_or_path))

    # ───────────────────────── push ─────────────────────────

    def on_incoming(self, msg: Message) -> None:
        if self.chat and msg.chat_id == self.chat.id:
            self.reload()


class _ForwardChatDialog(QDialog):
    """Require an explicit destination; display labels never determine its ID."""

    def __init__(self, chats: list[Chat], parent=None, *, language='ru') -> None:
        super().__init__(parent)
        self.selected_chat_id = None
        self.resize(380, 440)
        layout = QVBoxLayout(self)
        self.prompt = QLabel()
        self.prompt.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.prompt)
        self.chats = QListWidget()
        for chat in chats:
            item = QListWidgetItem(chat.display)
            item.setData(Qt.ItemDataRole.UserRole, chat.id)
            self.chats.addItem(item)
        layout.addWidget(self.chats, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.send_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.cancel_button = buttons.button(QDialogButtonBox.StandardButton.Cancel)
        self.send_button.setEnabled(False)
        self.chats.itemSelectionChanged.connect(lambda: self.send_button.setEnabled(self.chats.currentItem() is not None))
        buttons.accepted.connect(self._choose)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.retranslate(language)

    def retranslate(self, language):
        self.setWindowTitle(tr('Переслать сообщение', language))
        self.prompt.setText(tr('Выберите чат:', language))
        self.chats.setAccessibleName(tr('Выберите чат:', language))
        self.send_button.setText(tr('Переслать', language))
        self.cancel_button.setText(tr('Отменить', language))

    def _choose(self) -> None:
        item = self.chats.currentItem()
        if item is None:
            return
        self.selected_chat_id = item.data(Qt.ItemDataRole.UserRole)
        self.accept()


class _InputEdit(QPlainTextEdit):
    """Поле ввода. send_on_enter=True: Enter отправляет, Shift+Enter — перенос.
    send_on_enter=False: Enter — перенос, Ctrl+Enter отправляет."""

    send_requested = Signal()

    def __init__(self, settings: Settings) -> None:
        super().__init__()
        self._settings = settings
        self.setFixedHeight(44)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            # Numpad Enter has the same meaning as Return. Other modifiers must
            # match exactly: Alt/Meta and Ctrl+Shift must never become Send.
            mods = event.modifiers() & ~Qt.KeyboardModifier.KeypadModifier
            send_on_enter = getattr(self._settings, "send_on_enter", True)
            send_mods = (Qt.KeyboardModifier.NoModifier if send_on_enter
                         else Qt.KeyboardModifier.ControlModifier)
            should_send = mods == send_mods
            if should_send:
                if not event.isAutoRepeat() and self.isEnabled() and not self.isReadOnly():
                    self.send_requested.emit()
                event.accept()
            else:
                super().keyPressEvent(event)
            return
        super().keyPressEvent(event)


class _DayDivider(QWidget):
    def __init__(self, text: str) -> None:
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 8, 0, 8)
        lay.addStretch(1)
        pill = QLabel(text)
        pill.setStyleSheet(
            "background: rgba(127,127,127,0.18); color: palette(mid);"
            "border-radius:10px; padding:3px 12px; font-size:12px;"
        )
        lay.addWidget(pill)
        lay.addStretch(1)


def _day_key(ms: int | None) -> str:
    if not ms:
        return ""
    try:
        return datetime.datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d")
    except (ValueError, OSError, OverflowError):
        return ""
