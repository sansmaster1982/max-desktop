"""Главное окно: лента чатов слева, переписка справа, контакты и настройки.

Push с сервера приходит в колбэки сервиса (из читающего потока) и
проксируется в GUI-тред через Qt-сигналы.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QInputDialog,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from ..core.config import Settings
from ..core.models import Chat, Message
from ..core.service import MaxService
from ..protocol.client import ConnectionState
from .async_runner import run_async
from .chat_view import ChatView
from .contacts_dialog import ContactsDialog
from .settings_dialog import SettingsDialog
from .widgets.chat_list_item import ChatListItem


class MainWindow(QMainWindow):
    # Кросс-потоковые сигналы для push (emit из читающего потока — безопасно).
    sig_message = Signal(object)
    sig_chat_changed = Signal(int)
    sig_state = Signal(object)

    theme_changed = Signal()
    logged_out = Signal()
    sig_auth_invalid = Signal()  # сервер отверг токен -> на экран входа

    def __init__(self, service: MaxService, settings: Settings) -> None:
        super().__init__()
        self.service = service
        self.settings = settings
        self._current_chat_id: Optional[int] = None
        self._chat_order: list[int] = []  # для инкрементального обновления списка

        self.setWindowTitle("MAX Desktop")
        self.resize(settings.window_w, settings.window_h)
        self.setMinimumSize(760, 520)
        self._build()
        self._wire_push()

        self._reload_chats()
        run_async(self.service.sync_chats, on_done=lambda _c: self._reload_chats(),
                  on_error=lambda _e: None)

    # ───────────────────────── layout ─────────────────────────

    def _build(self) -> None:
        splitter = QSplitter(Qt.Orientation.Horizontal)

        # Боковая лента.
        sidebar = QWidget()
        sidebar.setObjectName("Sidebar")
        sidebar.setMinimumWidth(280)
        sb = QVBoxLayout(sidebar)
        sb.setContentsMargins(0, 0, 0, 0)
        sb.setSpacing(0)

        header = QFrame()
        header.setObjectName("SidebarHeader")
        header.setFixedHeight(64)
        hb = QHBoxLayout(header)
        hb.setContentsMargins(16, 8, 12, 8)
        app_title = QLabel("MAX")
        app_title.setObjectName("AppTitle")
        hb.addWidget(app_title)
        self.state_dot = QLabel("●")
        self.state_dot.setStyleSheet("color:#23B26D; font-size:11px;")
        hb.addWidget(self.state_dot)
        hb.addStretch(1)

        contacts_btn = QPushButton("👥")
        contacts_btn.setObjectName("IconButton")
        contacts_btn.setToolTip("Контакты")
        contacts_btn.clicked.connect(self._open_contacts)
        hb.addWidget(contacts_btn)

        settings_btn = QPushButton("⚙")
        settings_btn.setObjectName("IconButton")
        settings_btn.setToolTip("Настройки")
        settings_btn.clicked.connect(self._open_settings)
        hb.addWidget(settings_btn)
        sb.addWidget(header)

        search_wrap = QWidget()
        sw = QVBoxLayout(search_wrap)
        sw.setContentsMargins(10, 8, 10, 4)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Поиск чатов")
        self.search.textChanged.connect(self._filter_chats)
        sw.addWidget(self.search)
        sb.addWidget(search_wrap)

        self.chat_list = QListWidget()
        self.chat_list.itemClicked.connect(self._on_chat_clicked)
        self.chat_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.chat_list.customContextMenuRequested.connect(self._chat_context_menu)
        sb.addWidget(self.chat_list, 1)

        splitter.addWidget(sidebar)

        # Область переписки.
        self.chat_view = ChatView(self.service, self.settings)
        self.chat_view.chat_touched.connect(self._on_chat_touched)
        splitter.addWidget(self.chat_view)

        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([330, max(440, self.settings.window_w - 330)])
        self.setCentralWidget(splitter)

    # ───────────────────────── push wiring ─────────────────────────

    def _wire_push(self) -> None:
        self.service.on_message = self.sig_message.emit
        self.service.on_chat_changed = self.sig_chat_changed.emit
        self.service.on_state_changed = self.sig_state.emit
        self.service.on_auth_invalid = self.sig_auth_invalid.emit
        self.sig_message.connect(self._on_message)
        self.sig_chat_changed.connect(lambda _cid: self._schedule_reload())
        self.sig_state.connect(self._on_state)
        self.sig_auth_invalid.connect(self._on_auth_invalid)

    def _on_message(self, msg: Message) -> None:
        self.chat_view.on_incoming(msg)
        self._schedule_reload()

    def _on_state(self, state: ConnectionState) -> None:
        colors = {
            ConnectionState.CONNECTED: ("#23B26D", "онлайн"),
            ConnectionState.CONNECTING: ("#FFB300", "подключение…"),
            ConnectionState.RECONNECTING: ("#FFB300", "переподключение…"),
            ConnectionState.DISCONNECTED: ("#E5484D", "не в сети"),
        }
        color, tip = colors.get(state, ("#8A93A6", ""))
        self.state_dot.setStyleSheet(f"color:{color}; font-size:11px;")
        self.state_dot.setToolTip(tip)

    def _on_auth_invalid(self) -> None:
        """Сервер отверг сохранённый токен. Сессия уже очищена сервисом —
        сообщаем и уводим на экран входа, а не виснем в «не в сети»."""
        QMessageBox.information(
            self,
            "Сессия завершена",
            "Токен больше не действителен (истёк или сессия закрыта). "
            "Войдите снова.",
        )
        self.logged_out.emit()

    # ───────────────────────── chat list ─────────────────────────

    def _schedule_reload(self) -> None:
        QTimer.singleShot(80, self._reload_chats)

    def _reload_chats(self) -> None:
        chats = self.service.list_chats()
        query = self.search.text().strip().lower()
        if query:
            chats = [c for c in chats if query in (c.display or "").lower()]
        new_order = [c.id for c in chats]

        # Порядок не изменился — обновляем существующие строки на месте,
        # без пересоздания виджетов (частый случай: активный чат получил
        # сообщение и остался наверху).
        if new_order == self._chat_order and self.chat_list.count() == len(chats):
            for i, chat in enumerate(chats):
                w = self.chat_list.itemWidget(self.chat_list.item(i))
                if isinstance(w, ChatListItem):
                    w.update_from(chat)
            return

        self.chat_list.clear()
        for chat in chats:
            item = QListWidgetItem(self.chat_list)
            item.setData(Qt.ItemDataRole.UserRole, chat.id)
            row = ChatListItem(chat)
            item.setSizeHint(row.sizeHint())
            self.chat_list.addItem(item)
            self.chat_list.setItemWidget(item, row)
            if chat.id == self._current_chat_id:
                item.setSelected(True)
        self._chat_order = new_order

    def _filter_chats(self) -> None:
        self._reload_chats()

    def _on_chat_clicked(self, item: QListWidgetItem) -> None:
        chat_id = item.data(Qt.ItemDataRole.UserRole)
        chat = self.service.store.get_chat(chat_id)
        if chat is None:
            return
        self._current_chat_id = chat_id
        self.chat_view.open_chat(chat)

    def _chat_context_menu(self, pos) -> None:
        item = self.chat_list.itemAt(pos)
        if item is None:
            return
        chat_id = item.data(Qt.ItemDataRole.UserRole)
        menu = QMenu(self)
        menu.addAction("Переименовать", lambda: self._rename_chat(chat_id))
        menu.exec(self.chat_list.mapToGlobal(pos))

    def _rename_chat(self, chat_id: int) -> None:
        chat = self.service.store.get_chat(chat_id)
        current = chat.title if chat else ""
        new_name, ok = QInputDialog.getText(
            self, "Переименовать", "Имя собеседника / чата:", text=current or ""
        )
        if not ok or not new_name.strip():
            return
        try:
            self.service.rename_conversation(chat_id, new_name.strip())
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "Не удалось", str(e))
            return
        self._reload_chats()

    def _on_chat_touched(self, _chat_id: int) -> None:
        self._schedule_reload()

    def _open_chat_by_id(self, chat_id: int, title: str) -> None:
        self.service.store.ensure_chat(chat_id, title)
        self._current_chat_id = chat_id
        self._reload_chats()
        chat = self.service.store.get_chat(chat_id) or Chat(id=chat_id, title=title)
        self.chat_view.open_chat(chat)

    # ───────────────────────── dialogs ─────────────────────────

    def _open_contacts(self) -> None:
        dlg = ContactsDialog(self.service, self)
        dlg.open_chat.connect(self._open_chat_by_id)
        dlg.exec()

    def _open_settings(self) -> None:
        dlg = SettingsDialog(self.service, self.settings, self)
        dlg.theme_changed.connect(self.theme_changed.emit)
        dlg.logout_requested.connect(self._handle_logout)
        dlg.exec()

    def _handle_logout(self) -> None:
        run_async(self.service.logout, on_done=lambda _r: self.logged_out.emit(),
                  on_error=lambda _e: self.logged_out.emit())

    # ───────────────────────── window ─────────────────────────

    def closeEvent(self, event) -> None:  # noqa: N802
        self.settings.window_w = self.width()
        self.settings.window_h = self.height()
        self.settings.save()
        super().closeEvent(event)
