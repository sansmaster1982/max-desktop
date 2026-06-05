"""Область переписки: шапка чата, лента сообщений, панель ввода.

Сетевые операции — через run_async. Локальные перечитывания истории из
SQLite быстрые и идут синхронно. Голос/просмотр медиа подключаются лениво из
media.py (QtMultimedia), при его отсутствии деградируют мягко.
"""
from __future__ import annotations

import datetime
import os
from typing import Optional

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
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
from ..protocol.uploader import AttachKind
from .async_runner import run_async
from .theme import palette_for
from .widgets.avatar import Avatar
from .widgets.common import fmt_day_divider
from .widgets.message_bubble import MessageBubble


class ChatView(QWidget):
    chat_touched = Signal(int)  # чат изменился — обновить ленту слева

    def __init__(self, service: MaxService, settings: Settings) -> None:
        super().__init__()
        self.service = service
        self.settings = settings
        self.chat: Optional[Chat] = None
        self._recorder = None
        self._rows: list[dict] = []  # [{key, msg, bubble, divider, day}] — для инкрем. рендера
        self._build()
        self.show_placeholder()

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
        self.h_title.setObjectName("ChatTitle")
        self.h_sub = QLabel()
        self.h_sub.setObjectName("ChatSubtitle")
        title_col.addWidget(self.h_title)
        title_col.addWidget(self.h_sub)
        h.addLayout(title_col, 1)
        root.addWidget(self.header)

        # Лента сообщений.
        self.scroll = QScrollArea()
        self.scroll.setObjectName("MessageScroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.container = QWidget()
        self.container.setObjectName("ChatArea")
        self.msg_layout = QVBoxLayout(self.container)
        self.msg_layout.setContentsMargins(0, 12, 0, 12)
        self.msg_layout.setSpacing(2)
        self.msg_layout.addStretch(1)
        self.scroll.setWidget(self.container)
        root.addWidget(self.scroll, 1)

        # Панель ввода.
        self.input_bar = QFrame()
        self.input_bar.setObjectName("InputBar")
        bar = QHBoxLayout(self.input_bar)
        bar.setContentsMargins(12, 10, 12, 10)
        bar.setSpacing(8)

        self.attach_btn = QPushButton("📎")
        self.attach_btn.setObjectName("IconButton")
        self.attach_btn.setToolTip("Прикрепить фото, видео или файл")
        self.attach_btn.clicked.connect(self._attach_menu)
        bar.addWidget(self.attach_btn)

        self.input = _InputEdit(self.settings)
        self.input.setPlaceholderText("Написать сообщение…")
        self.input.send_requested.connect(self._send_text)
        bar.addWidget(self.input, 1)

        self.voice_btn = QPushButton("🎤")
        self.voice_btn.setObjectName("IconButton")
        self.voice_btn.setToolTip("Записать голосовое сообщение")
        self.voice_btn.clicked.connect(self._toggle_voice)
        bar.addWidget(self.voice_btn)

        self.send_btn = QPushButton("➤")
        self.send_btn.setObjectName("IconButton")
        self.send_btn.setToolTip("Отправить")
        self.send_btn.clicked.connect(self._send_text)
        bar.addWidget(self.send_btn)

        root.addWidget(self.input_bar)
        self._set_composer_visible(False)

    def _set_composer_visible(self, visible: bool) -> None:
        self.input_bar.setVisible(visible)
        self.header.setVisible(visible)

    # ───────────────────────── open / render ─────────────────────────

    def show_placeholder(self) -> None:
        self.chat = None
        self._clear_all()
        hint = QLabel("Выберите чат, чтобы начать переписку")
        hint.setObjectName("EmptyHint")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.msg_layout.insertWidget(self.msg_layout.count() - 1, hint)
        self._set_composer_visible(False)

    def open_chat(self, chat: Chat) -> None:
        same = self.chat is not None and self.chat.id == chat.id
        self.chat = chat
        self._set_composer_visible(True)
        self.h_avatar.set_identity(chat.display, chat.id)
        self.h_title.setText(chat.display)
        self.h_sub.setText("онлайн" if not chat.is_group else "группа")
        self.input.setFocus()
        if not same:
            # Смена чата — полный рендер из кэша (мгновенно).
            self._full_render(self.service.local_history(chat.id))
        run_async(
            self.service.open_chat, chat.id,
            on_done=self._on_history, on_error=lambda _e: None,
        )

    def _on_history(self, messages: list[Message]) -> None:
        if self.chat is None:
            return
        self._set_messages(messages)
        self.chat_touched.emit(self.chat.id)

    def reload(self) -> None:
        if self.chat is not None:
            self._set_messages(self.service.local_history(self.chat.id))

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

    def _full_render(self, messages: list[Message]) -> None:
        self._clear_all()
        for m in messages:
            self._append_row(m)
        QTimer.singleShot(0, self._scroll_to_bottom)

    def _make_bubble(self, m: Message) -> MessageBubble:
        return MessageBubble(
            m, self.settings.accent, palette_for(self.settings.theme),
            on_open_media=self._open_media, on_play_voice=self._play_voice,
        )

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
        r["bubble"].deleteLater()
        bubble = self._make_bubble(m)
        self.msg_layout.insertWidget(idx, bubble)
        r["bubble"] = bubble
        r["msg"] = m
        r["key"] = self._key(m)

    def _clear_all(self) -> None:
        while self.msg_layout.count() > 1:
            item = self.msg_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._rows = []

    def _scroll_to_bottom(self) -> None:
        bar = self.scroll.verticalScrollBar()
        bar.setValue(bar.maximum())

    # ───────────────────────── sending ─────────────────────────

    def _send_text(self) -> None:
        if self.chat is None:
            return
        text = self.input.toPlainText().strip()
        if not text:
            return
        self.input.clear()
        chat_id = self.chat.id
        run_async(
            self.service.send_text, chat_id, text,
            on_done=lambda m: self._on_sent(chat_id, m),
            on_error=lambda e: self._on_sent(chat_id, None, e),
        )

    def _after_send(self, chat_id: int) -> None:
        if self.chat and self.chat.id == chat_id:
            self.reload()
        self.chat_touched.emit(chat_id)

    def _on_sent(self, chat_id: int, msg, err: str | None = None) -> None:
        self._after_send(chat_id)
        reason = err or (msg.error if (msg is not None and msg.status == "failed") else None)
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
            self.voice_btn.setText("⏹")
            self.voice_btn.setToolTip("Остановить и отправить")
            self._recorder.start()

    def _on_voice_recorded(self, path: str, duration_ms: int) -> None:
        self.voice_btn.setText("🎤")
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
        self.voice_btn.setText("🎤")
        self.voice_btn.setToolTip("Записать голосовое сообщение")

    # ───────────────────────── media open / play ─────────────────────────

    def _open_media(self, msg: Message, attach: Attach) -> None:
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
            mods = event.modifiers()
            shift = bool(mods & Qt.KeyboardModifier.ShiftModifier)
            ctrl = bool(mods & Qt.KeyboardModifier.ControlModifier)
            send_on_enter = getattr(self._settings, "send_on_enter", True)
            should_send = (send_on_enter and not shift) or (not send_on_enter and ctrl)
            if should_send:
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
