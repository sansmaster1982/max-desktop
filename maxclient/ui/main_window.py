"""Главное окно: лента чатов слева, переписка справа, контакты и настройки.

Push с сервера приходит в колбэки сервиса (из читающего потока) и
проксируется в GUI-тред через Qt-сигналы.
"""
from __future__ import annotations

import copy
from collections import deque
from .public_build import web_features_available
from typing import Optional

from shiboken6 import isValid

from PySide6.QtCore import Qt, QTimer, Signal, QSize, QPoint
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
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
    QScrollArea,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .. import __app_name__
from ..core.config import Settings
from ..core.i18n import tr
from ..core.models import Chat, Message
from ..core.service import MaxService
from ..protocol.client import ConnectionState
from ..protocol.folder_contracts import int64
from .async_runner import run_async
from .chat_view import ChatView
from .call_history_dialog import CallHistoryDialog
from .contacts_dialog import ContactsDialog
from .desktop_notifications import DesktopNotifications
from .settings_workspace import SettingsWorkspace
from .search_panel import SearchPanel
from .theme import resolve_theme
from .verification_codes_dialog import VerificationCodesDialog
from .widgets.chat_list_item import ChatListItem
from .widgets.line_icon import RailButton, line_icon
from .management_widgets import confirm_action
from .contact_profile import ContactProfileMixin
from .conversation_management_dialogs import (ShareContactDialog, confirm_conversation_removal,
                                               confirmed_lifecycle_action, accepted_lifecycle_pending_notice)
from .conversation_leave_dialog import confirm_conversation_leave, simple_leave_available
from .conversation_bot_delete_dialog import (bot_delete_available, bot_delete_result,
                                              bot_delete_state, bot_delete_notice,
                                              confirm_bot_conversation_delete)
from .conversation_owner_transfer_dialog import (ConversationOwnerTransferDialog,
    owner_transfer_available, owner_transfer_result, owner_transfer_state, owner_transfer_notice,
    owner_transfer_notice_parts)
from .conversation_edit_dialog import (ConversationEditDialog, conversation_edit_available,
    conversation_edit_state, conversation_edit_notice)


def _localized_info_status(source, language, values=None, values_sources=None):
    """Keep owned templates separate from names when relabelling a live card."""
    values = dict(values or {})
    values_sources = dict(values_sources or {})
    formatted = {key: tr(value, language) for key, value in values_sources.items()}
    formatted.update(values)
    return {'action_status': tr(source, language, **formatted),
            'action_status_source': source, 'action_status_values': values,
            'action_status_values_sources': values_sources}


class MainWindow(ContactProfileMixin, QMainWindow):
    # Кросс-потоковые сигналы для push (emit из читающего потока — безопасно).
    sig_message = Signal(object)
    sig_message_changed = Signal(object)
    sig_chat_changed = Signal(int)
    sig_state = Signal(object)
    sig_config_changed = Signal()
    sig_incoming_call = Signal(object)

    theme_changed = Signal()
    logged_out = Signal()
    sig_auth_invalid = Signal()  # сервер отверг токен -> на экран входа

    def __init__(self, service: MaxService, settings: Settings) -> None:
        super().__init__()
        self.service = service
        self.settings = settings
        self._current_chat_id: Optional[int] = None
        self._chat_order: list[int] = []  # для инкрементального обновления списка
        self._code_dialog = None
        self._call_history_dialog = None
        self._call_window = None
        self._incoming_enabled = True
        self._search_enabled = True
        self._search_context = None
        self._incoming_context = None
        self._incoming_seen = set()
        self._incoming_order = deque()
        self._settings_workspace = None
        self._contacts_workspace = None
        self._init_contact_profile()
        self._phone_lookup_dialog = None
        self._contact_create_dialog = None
        self._conversation_create_dialog = None
        self._new_menu = None
        self._new_menu_epoch = 0
        self._row_menu = None
        self._chat_preview_dialog = None
        self._report_dialog = None
        self._owner_transfer_dialog = None
        self._conversation_edit_dialog = None
        self._row_menu_context = None
        self._row_menu_section = None
        self._row_menu_folder_id = None
        self._row_read_context = None
        self._row_pin_context = None
        self._row_pin_state = None
        self._row_pin_job = None
        self._row_action_job = None
        self._row_folder_job = None
        self._row_folder_rows = None
        self._row_metadata_job = None
        self._row_metadata_failed = False
        self._info_panel = None
        self._info_context = None
        self._info_pages = {}
        self._info_snapshot = {}
        self._info_action_job = None
        self._info_folder_rows = None
        self._info_folder_job = None
        from .conversation_media_loader import ConversationMediaLoader
        self._info_media_loader = ConversationMediaLoader(self._info_current, self)
        self._info_media_loader.ready.connect(self._info_thumbnail_ready)
        self._info_photo_viewer = None
        self._info_file_controller = None
        self._info_clock = QTimer(self)
        self._info_clock.setInterval(1000)
        self._info_clock.timeout.connect(self._poll_info_action_state)
        self._section = 'all'
        self._folders = {}
        self._reload_timer = QTimer(self)
        self._reload_timer.setSingleShot(True)
        self._reload_timer.timeout.connect(self._reload_chats)

        self.setWindowTitle(__app_name__)
        self.resize(settings.window_w, settings.window_h)
        self.setMinimumSize(880, 560)
        self._build()
        from .keyboard_shortcuts import install_navigation_shortcuts
        self._navigation_shortcuts = install_navigation_shortcuts(
            self, search_chats=self._focus_chat_search, search_in_chat=self._focus_history_search,
            close_chat=self._close_current_chat)
        self._notifications = DesktopNotifications(self, service, settings)
        self._wire_push()

        self._reload_chats()
        run_async(self.service.sync_chats, on_done=lambda _c: self._reload_chats(),
                  on_error=lambda _e: None)
        if callable(getattr(self.service, 'get_folders', None)):
            initial_context = self._bind_search_context()
            run_async(self.service.get_folders,
                      on_done=lambda value: self._on_folders(value, context=initial_context),
                      on_error=lambda _e: None)

    # ───────────────────────── layout ─────────────────────────

    def _build(self) -> None:
        shell = QWidget()
        layout = QHBoxLayout(shell)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        rail = QFrame()
        rail.setObjectName('NavigationRail')
        rail.setFixedWidth(64)
        navigation = QVBoxLayout(rail)
        navigation.setContentsMargins(0, 7, 0, 8)
        navigation.setSpacing(1)
        self.nav_buttons = {}
        for key, icon, label in [('all', 'chats', 'Все'), ('new', 'unread', 'Новые'), ('channels', 'channels', 'Каналы')]:
            button = RailButton(icon, label, checkable=True)
            button.clicked.connect(lambda _checked=False, section=key: self._select_section(section))
            self.nav_buttons[key] = button
            navigation.addWidget(button)
        self.nav_buttons['all'].setChecked(True)
        separator = QFrame()
        separator.setObjectName('RailSeparator')
        self.folder_rail = QScrollArea()
        self.folder_rail.setWidgetResizable(True)
        self.folder_rail.setFrameShape(QFrame.Shape.NoFrame)
        self.folder_rail.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.folder_rail.setMaximumHeight(220)
        self.folder_rail.hide()
        folder_host = QWidget()
        self._folder_buttons_layout = QVBoxLayout(folder_host)
        self._folder_buttons_layout.setContentsMargins(0, 0, 0, 0)
        self._folder_buttons_layout.setSpacing(0)
        self.folder_rail.setWidget(folder_host)
        navigation.addWidget(self.folder_rail)
        navigation.addWidget(separator)
        self.contacts_button = RailButton('contacts', 'Контакты', checkable=True)
        self.contacts_button.clicked.connect(self._open_contacts)
        navigation.addWidget(self.contacts_button)
        self.calls_button = RailButton('phone', 'Звонки')
        self.calls_button.setToolTip('История звонков')
        self.calls_button.clicked.connect(self._open_call_history)
        navigation.addWidget(self.calls_button)
        navigation.addStretch(1)
        self.settings_button = RailButton('settings', 'Настройки', checkable=True)
        self.settings_button.clicked.connect(self._open_settings)
        navigation.addWidget(self.settings_button)
        self._rail_widgets = list(self.nav_buttons.values()) + [self.contacts_button, self.calls_button, self.settings_button]
        for button in self._rail_widgets:
            button.set_dark(resolve_theme(self.settings.theme) == 'dark')
        layout.addWidget(rail)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setHandleWidth(1)
        self.splitter = splitter
        sidebar = QWidget()
        sidebar.setObjectName("Sidebar")
        sidebar.setMinimumWidth(290)
        sidebar.setMaximumWidth(520)
        self.sidebar = sidebar
        sb = QVBoxLayout(sidebar)
        sb.setContentsMargins(0, 0, 0, 0)
        sb.setSpacing(0)

        header = QFrame()
        header.setObjectName("SidebarHeader")
        header.setFixedHeight(64)
        hb = QHBoxLayout(header)
        hb.setContentsMargins(16, 12, 14, 8)
        hb.setSpacing(8)
        self.section_title = QLabel('Чаты')
        self.section_title.setObjectName('SidebarTitle')
        hb.addWidget(self.section_title)
        self.state_dot = QLabel("●")
        self.state_dot.setStyleSheet("background:transparent;color:#9AA2AD;font-size:8px;")
        self.state_dot.setToolTip('Состояние соединения')
        hb.addWidget(self.state_dot)
        hb.addStretch(1)
        self.codes_button = QPushButton()
        self.codes_button.setObjectName('SidebarIcon')
        self.codes_button.setIcon(line_icon('codes', '#90959B', 20))
        self.codes_button.setIconSize(QSize(20, 20))
        self.codes_button.setFixedSize(32, 34)
        self.codes_button.setToolTip('Коды подтверждения')
        self.codes_button.setAccessibleName('Коды подтверждения')
        self.codes_button.clicked.connect(self._open_verification_codes)
        hb.addWidget(self.codes_button)
        self.new_chat_button = QPushButton()
        self.new_chat_button.setObjectName('NewChatButton')
        self.new_chat_button.setIcon(line_icon('plus', '#FFFFFF', 21))
        self.new_chat_button.setIconSize(QSize(21, 21))
        self.new_chat_button.setFixedSize(34, 34)
        self.new_chat_button.setToolTip('Новое сообщение')
        self.new_chat_button.setAccessibleName('Новое сообщение')
        self.new_chat_button.clicked.connect(self._open_new_menu)
        hb.addWidget(self.new_chat_button)
        sb.addWidget(header)

        search_wrap = QWidget()
        sw = QVBoxLayout(search_wrap)
        sw.setContentsMargins(15, 0, 15, 8)
        self.search = QLineEdit()
        self.search.setObjectName('ChatSearch')
        self.search.setPlaceholderText('Найти')
        self.search.setClearButtonEnabled(True)
        self.search.setFixedHeight(36)
        self.search.addAction(line_icon('search', '#AFB4BC', 18), QLineEdit.ActionPosition.LeadingPosition)
        self.search.textChanged.connect(self._filter_chats)
        sw.addWidget(self.search)
        sb.addWidget(search_wrap)
        self.folder_status = QLabel()
        self.folder_status.setWordWrap(True)
        self.folder_status.setTextFormat(Qt.TextFormat.PlainText)
        self.folder_status.setContentsMargins(15, 0, 15, 8)
        self.folder_status.hide()
        sb.addWidget(self.folder_status)

        self.chat_list = QListWidget()
        self.chat_list.setObjectName('ChatList')
        self.chat_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.chat_list.setSpacing(0)
        self.chat_list.itemClicked.connect(self._on_chat_clicked)
        self.chat_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.chat_list.customContextMenuRequested.connect(self._chat_context_menu)
        self._chat_pages = QStackedWidget()
        self._chat_pages.addWidget(self.chat_list)
        self.empty_list_hint = QLabel('Пока нет чатов')
        self.empty_list_hint.setObjectName('NoChatsHint')
        self.empty_list_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_list_hint.setWordWrap(True)
        self._chat_pages.addWidget(self.empty_list_hint)
        self.search_panel = SearchPanel(theme=self.settings.theme, language=getattr(self.settings, 'ui_language', 'ru'))
        self.search_panel.search_requested.connect(self._request_search)
        self.search_panel.result_activated.connect(self._open_search_result)
        self.search_panel.clear_requested.connect(self.search.clear)
        self._chat_pages.addWidget(self.search_panel)
        sb.addWidget(self._chat_pages, 1)

        self._sidebar_pages = QStackedWidget()
        self._sidebar_pages.setMinimumWidth(290)
        self._sidebar_pages.setMaximumWidth(520)
        self._sidebar_pages.addWidget(sidebar)
        splitter.addWidget(self._sidebar_pages)

        # Область переписки.
        self.chat_view = ChatView(self.service, self.settings)
        self.chat_view.chat_touched.connect(self._on_chat_touched)
        self.chat_view.call_requested.connect(self._open_call)
        self.chat_view.info_requested.connect(self._open_chat_header_info)
        self.chat_view.file_save_requested.connect(self._timeline_file_save)
        self.chat_view.file_scope_closed.connect(self._file_scope_closed)
        self._conversation_pages = QStackedWidget()
        self._conversation_pages.addWidget(self.chat_view)
        splitter.addWidget(self._conversation_pages)

        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([360, max(460, self.settings.window_w - 424)])
        self._workspace_pages = QStackedWidget()
        self._workspace_pages.addWidget(splitter)
        layout.addWidget(self._workspace_pages, 1)
        self.setCentralWidget(shell)
        self._apply_local_language()

    def _section_caption(self, section):
        source = {'all': 'Чаты', 'new': 'Новые', 'channels': 'Каналы'}.get(section)
        if source:
            return tr(source, getattr(self.settings, 'ui_language', 'ru'))
        # Custom folder titles are user data, not translation keys.
        return self._folders.get(section, {}).get('title', tr('Папка', getattr(self.settings, 'ui_language', 'ru')))

    def _apply_local_language(self, _language=None):
        language = getattr(self.settings, 'ui_language', 'ru')
        for key, source in (('all', 'Все'), ('new', 'Новые'), ('channels', 'Каналы')):
            button = self.nav_buttons[key]
            caption = tr(source, language)
            button.label.setText(caption)
            button.setAccessibleName(caption)
            button.setToolTip(caption)
        for button, source in ((self.contacts_button, 'Контакты'), (self.calls_button, 'Звонки'),
                               (self.settings_button, 'Настройки')):
            caption = tr(source, language)
            button.label.setText(caption)
            button.setAccessibleName(caption)
            button.setToolTip(caption)
        self.section_title.setText(self._section_caption(self._section))
        self.search.setPlaceholderText(tr('Найти', language))
        self.search_panel.retranslate(language)
        codes_dialog = self._code_dialog
        if codes_dialog is not None and isValid(codes_dialog):
            codes_dialog.retranslate(language)
        for button, source in ((self.codes_button, 'Коды подтверждения'), (self.new_chat_button, 'Новое сообщение')):
            button.setToolTip(tr(source, language))
            button.setAccessibleName(tr(source, language))
        self.chat_view.retranslate_message_actions(language)
        self.chat_view.retranslate_media(language)
        controller = self._info_file_controller
        if controller is not None and isValid(controller):
            controller.retranslate(language)
        viewer = self._info_photo_viewer
        if viewer is not None and isValid(viewer):
            viewer.retranslate(language)
        # Relabel existing presenters only. Do not reload data or restart requests.
        self._cancel_new_menu()
        self._cancel_chat_row_menu()
        contact_menu = self._contact_row_menu
        if contact_menu is not None and isValid(contact_menu):
            contact_menu.cancel()
            contact_menu.deleteLater()
        self._contact_row_menu = None
        for panel in (self._contacts_workspace, self._info_panel, self._contact_panel, self._contact_edit_panel):
            if panel is not None and isValid(panel):
                panel.retranslate(language)
        dialog = self._conversation_create_dialog
        if dialog is not None and isValid(dialog):
            dialog.set_language(language)

    def _select_section(self, section: str) -> None:
        section = self._resolve_folder_section(section)
        if section not in self.nav_buttons:
            return
        if section != self._section:
            self._cancel_chat_row_menu()
        self._section = section
        self._show_chat_workspace()
        for key, button in self.nav_buttons.items():
            button.setChecked(key == section)
        self.section_title.setText(self._section_caption(section))
        self._reload_chats()

    def _resolve_folder_section(self, section: str) -> str:
        if not self._folders:
            return 'all' if section.startswith('folder:') else section
        canonical = {'all': 'folder:all.chat.folder',
                     'channels': 'folder:chat.channel.folder'}.get(section)
        if canonical in self._folders:
            return canonical
        if section in self._folders:
            return section
        # The unread folder has no reserved ID. Never identify it by its title
        # or keep a competing local "New" tab after a complete server snapshot.
        return ('folder:all.chat.folder' if 'folder:all.chat.folder' in self._folders
                else next(iter(self._folders)))

    def _on_folders(self, snapshot, *, context=None):
        if not isValid(self) or not self._search_enabled:
            return
        if context is not None:
            try:
                if self.service.search_context() is not context:
                    return
            except (AttributeError, ValueError):
                return
        if (not isinstance(snapshot, dict) or not int64(snapshot.get('folderSync')) or
                not isinstance(snapshot.get('orderedFolders'), list)):
            return
        order = snapshot.get('foldersOrder')
        records = snapshot['orderedFolders']
        if (not isinstance(order, list) or not order or len(order) != len(records) or
                any(not isinstance(identity, str) or not identity for identity in order) or
                len(set(order)) != len(order) or
                any(record is None for record in records)):
            return  # A partial delta cannot replace the complete navigation.
        folders = {}
        for identity, record in zip(order, records):
            if (not isinstance(record, dict) or record.get('id') != identity or
                    not isinstance(record.get('title'), str)):
                return
            folders['folder:' + identity] = record
        for key in list(self._folders):
            button = self.nav_buttons.pop(key, None)
            if button is not None:
                self._folder_buttons_layout.removeWidget(button)
                self._rail_widgets.remove(button)
                button.hide()
                button.deleteLater()
        self._folders = folders
        for key, record in folders.items():
            icon = {'all.chat.folder': 'chats', 'chat.channel.folder': 'channels'}.get(
                record['id'], 'unread' if record.get('filters') == [0] else 'folder')
            button = RailButton(icon, record['title'], checkable=True)
            button.setToolTip(record['title'])
            button.set_dark(resolve_theme(self.settings.theme) == 'dark')
            button.clicked.connect(lambda _checked=False, section=key: self._select_section(section))
            self.nav_buttons[key] = button
            self._rail_widgets.append(button)
            self._folder_buttons_layout.addWidget(button)
        self.folder_rail.setVisible(bool(folders))
        for key in ('all', 'new', 'channels'):
            self.nav_buttons[key].setVisible(not folders)
        self._section = self._resolve_folder_section(self._section)
        if self._row_menu_context is not None and not self._chat_row_current(self._row_menu_context):
            self._cancel_chat_row_menu()
        showing_chats = (self._workspace_pages.currentWidget() == self.splitter
                         and self._sidebar_pages.currentWidget() == self.sidebar)
        for key, button in self.nav_buttons.items():
            button.setChecked(showing_chats and key == self._section)
        self.section_title.setText(self._section_caption(self._section))
        self._reload_chats()
        context = getattr(self, '_info_context', None)
        if self._info_current(context):
            # A Settings change or complete folder push invalidates both cached
            # choices and any older read still in flight for this card.
            self._info_folder_rows = self._info_folder_job = None
            self._info_panel.set_conversation(self._info_with_avatar(self._info_snapshot))
            self._load_info_folders(context)

    # ───────────────────────── push wiring ─────────────────────────

    def _wire_push(self) -> None:
        self.service.on_message = self.sig_message.emit
        if callable(getattr(self.service, 'is_current_message_change', None)):
            self.service.on_message_changed = self.sig_message_changed.emit
        self.service.on_chat_changed = self.sig_chat_changed.emit
        self.service.on_state_changed = self.sig_state.emit
        self.service.on_auth_invalid = self.sig_auth_invalid.emit
        self.service.on_config_changed = self.sig_config_changed.emit
        if callable(getattr(self.service, 'is_current_incoming_call', None)):
            self.service.on_incoming_call = self.sig_incoming_call.emit
        self.sig_message.connect(self._on_message)
        self.sig_message_changed.connect(self._on_message_changed, Qt.ConnectionType.QueuedConnection)
        self.sig_chat_changed.connect(lambda _cid: self._schedule_reload())
        self.sig_state.connect(self._on_state)
        self.sig_auth_invalid.connect(self._on_auth_invalid)
        self.sig_config_changed.connect(self._on_config_changed)
        self.sig_incoming_call.connect(self._on_incoming_call)
        state = getattr(getattr(self.service, 'client', None), 'state', None)
        if isinstance(state, ConnectionState):
            self._on_state(state)

    def _on_config_changed(self):
        self._notifications.clear()
        self.chat_view.update_call_availability()
        if self._settings_workspace is not None:
            self._settings_workspace.on_config_changed()
        self._poll_info_action_state()

    def _on_local_delivery_changed(self, enabled):
        """Stop current presentation only after this live workspace saved OFF."""
        workspace = self._settings_workspace
        if (not isValid(self) or not self._search_enabled or workspace is None
                or not isValid(workspace) or self.sender() is not workspace
                or enabled is not False or self.settings.notifications is not False):
            return
        self._notifications.clear()

    def _on_message(self, event) -> None:
        checker = getattr(self.service, 'is_current_message_event', None)
        if callable(checker):
            from ..core.message_delivery import MessageDelivery
            if not isinstance(event, MessageDelivery):
                return
            try:
                if not checker(event):
                    return
            except Exception:
                return
            msg = event.message
        else:
            # Older UI-only test doubles have no account-context contract.
            # The real service always requires its session-bound envelope.
            if not isinstance(event, Message):
                return
            msg = event
        self._notifications.receive(msg)
        self.chat_view.on_incoming(msg)
        self._schedule_reload()

    def _message_change_current(self, event):
        if not isValid(self) or not self._search_enabled:
            return False
        from ..core.message_delivery import MessageChange
        if not isinstance(event, MessageChange):
            return False
        checker = getattr(self.service, 'is_current_message_change', None)
        try:
            return callable(checker) and checker(event) is True
        except (AttributeError, ValueError):
            return False

    def _on_message_changed(self, event):
        if not self._message_change_current(event):
            return
        binding = self.chat_view.message_change_binding(event.chat_id) if isValid(self.chat_view) else None
        if binding is not None:
            try:
                changes = self.service.cached_messages_for_change(event)
            except (AttributeError, ValueError):
                return
            if not self._message_change_current(event):
                return
            if not self.chat_view.apply_message_change(event, changes, binding):
                return
        # No notification/arrival/read/typing path: this event changes cache rows.
        if self._message_change_current(event):
            self._schedule_reload()

    def _on_state(self, state: ConnectionState) -> None:
        self.chat_view.update_call_availability()
        if (self.chat_view._file_context is not None
                and not self.chat_view.file_context_current(self.chat_view._file_context)):
            self.chat_view.close_file_scope()
        self._bind_search_context()
        if self._contacts_workspace is not None:
            self._contacts_workspace.refresh()
        if self._contact_context is not None and not self._contact_current(self._contact_context):
            self._close_contact_profile()
            self._clear_previous_contact_chat()
        if self._info_context is not None and not self._info_current(self._info_context):
            self._close_conversation_info()
        colors = {
            ConnectionState.CONNECTED: ("#23B26D", "онлайн"),
            ConnectionState.CONNECTING: ("#FFB300", "подключение…"),
            ConnectionState.RECONNECTING: ("#FFB300", "переподключение…"),
            ConnectionState.DISCONNECTED: ("#E5484D", "не в сети"),
        }
        color, tip = colors.get(state, ("#8A93A6", ""))
        self.state_dot.setStyleSheet(f"background:transparent;color:{color};font-size:8px;")
        self.state_dot.setToolTip(tip)

    def _on_auth_invalid(self) -> None:
        """Сервер отверг сохранённый токен. Сессия уже очищена сервисом —
        сообщаем и уводим на экран входа, а не виснем в «не в сети»."""
        self._stop_incoming_presentation()
        self._stop_search()
        self._notifications.clear()
        if self._call_window is not None:
            self._call_window.close()
        QMessageBox.information(
            self,
            "Сессия завершена",
            "Токен больше не действителен (истёк или сессия закрыта). "
            "Войдите снова.",
        )
        self.logged_out.emit()

    # ───────────────────────── chat list ─────────────────────────

    def _schedule_reload(self) -> None:
        if not self._reload_timer.isActive():
            self._reload_timer.start(80)

    def _reload_chats(self) -> None:
        self._refresh_contact_profile_state()
        if (self._contacts_workspace is not None
                and self._sidebar_pages.currentWidget() == self._contacts_workspace):
            self._contacts_workspace.refresh()
        metadata_binding = self.chat_view.chat_metadata_binding()
        all_chats = self.service.list_chats()
        if metadata_binding is not None:
            if not self.chat_view.chat_metadata_binding_current(metadata_binding):
                return
            active_chat = next((chat for chat in all_chats if chat.id == metadata_binding[0]), None)
            if active_chat is not None:
                self.chat_view.refresh_chat_metadata(active_chat, metadata_binding)
        # Rail badges count unread conversations, while each row keeps its
        # unread-message count. Use the same qA/JA projection as server folders.
        builtins = {}
        for key, identity, filters in (('all', 'all.chat.folder', []),
                                      ('new', '__desktop_unread_projection__', [0]),
                                      ('channels', 'chat.channel.folder', [2])):
            try:
                projection = self.service.get_folder_view(
                    {'id': identity, 'title': key, 'filters': filters}, all_chats)
                if not isinstance(projection, dict) or not isinstance(projection.get('chatIds'), list):
                    projection = None
            except (AttributeError, ValueError):
                projection = None
            builtins[key] = projection
            if projection is not None:
                count = projection.get('unreadCount')
                self.nav_buttons[key].set_counter(count if type(count) is int and count >= 0 else 0)
            else:
                # Local-only/offline adapters have no canonical folder DTOs.
                # Their known row flags still must not sum message counters.
                self.nav_buttons[key].set_counter(sum(
                    1 for chat in all_chats if chat.unread > 0
                    and (key != 'channels' or _is_channel(chat))
                    and (key != 'all' or chat.id != 0 and not chat.is_muted)))
        chats = list(all_chats)
        self.folder_status.hide()
        folder_views = {}
        for key, folder in self._folders.items():
            try:
                view = self.service.get_folder_view(folder, all_chats)
            except Exception:
                view = {'chatIds': [], 'complete': False, 'unreadCount': None, 'hidden': None}
            folder_views[key] = view
            self.nav_buttons[key].set_counter(view.get('unreadCount') or 0)
            self.nav_buttons[key].setVisible(view.get('hidden') is not True or key == self._section)
        if self._section == 'new':
            projection = builtins['new']
            chats = ([chat for chat in chats if chat.id in set(projection['chatIds'])]
                     if projection is not None else [chat for chat in chats if chat.unread > 0])
        elif self._section == 'channels':
            chats = [chat for chat in chats if _is_channel(chat)]
        elif self._section in folder_views:
            view = folder_views[self._section]
            by_id = {chat.id: chat for chat in all_chats}
            chats = [by_id[identity] for identity in view['chatIds'] if identity in by_id]
            if not view['complete'] or view.get('unsupportedFields'):
                self.folder_status.setText('Показаны доступные чаты. Сведения о части чатов пока не загружены.')
                self.folder_status.show()
        query = self.search.text().strip().lower()
        remote_search = bool(query and callable(getattr(self.service, 'search_all', None)))
        if query and not remote_search:
            chats = [c for c in chats if query in (c.display or "").lower()]
        self.empty_list_hint.setText('Ничего не найдено' if query else {'all': 'Пока нет чатов', 'new': 'Нет непрочитанных чатов', 'channels': 'Пока нет каналов'}.get(self._section, 'В этой папке пока нет доступных чатов'))
        self._bind_search_context()
        self._chat_pages.setCurrentWidget(self.search_panel if remote_search else
                                          self.chat_list if chats else self.empty_list_hint)
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
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, chat.id)
            row = ChatListItem(chat)
            item.setSizeHint(row.sizeHint())
            self.chat_list.addItem(item)
            self.chat_list.setItemWidget(item, row)
            if chat.id == self._current_chat_id:
                self.chat_list.setCurrentItem(item)
        self._chat_order = new_order

    def _filter_chats(self) -> None:
        self._bind_search_context()
        self.search_panel.set_query(self.search.text())
        self._reload_chats()

    def _bind_search_context(self):
        if not isValid(self):
            return None
        context = None
        if self._search_enabled:
            try:
                context = self.service.search_context()
            except (AttributeError, ValueError):
                pass
        self.search_panel.bind_owner(context)
        self._search_context = context
        return context

    def _stop_search(self):
        self._search_enabled = False
        self._close_contact_profile()
        self._search_context = None
        self.search_panel.bind_owner(None)
        self._close_conversation_info()
        self.chat_view.close_file_scope()
        if self._contacts_workspace is not None:
            self._contacts_workspace.deactivate()
        if self._phone_lookup_dialog is not None and isValid(self._phone_lookup_dialog):
            self._phone_lookup_dialog.reject()
        if self._contact_create_dialog is not None and isValid(self._contact_create_dialog):
            self._contact_create_dialog.reject()
        if self._conversation_create_dialog is not None and isValid(self._conversation_create_dialog):
            self._conversation_create_dialog.reject()
        self._cancel_new_menu()
        self._cancel_chat_row_menu()

    def _request_search(self, request):
        context = self._bind_search_context()
        if (context is None or not isinstance(request, dict) or
                request.get('context') is not context):
            return

        def done(page):
            if isValid(self) and self._search_enabled:
                self._bind_search_context()
                self.search_panel.complete(request, page)

        def failed(_error):
            if isValid(self) and self._search_enabled:
                self._bind_search_context()
                self.search_panel.fail(request)

        run_async(self.service.search_all, request['query'], marker=request['marker'],
                  context=context, on_done=done, on_error=failed)

    def _open_search_result(self, row):
        # Contact IDs are not chat IDs. Only a real cached chat (or its explicit
        # peer_user_id relation) can navigate to the conversation here.
        previous = self._search_context
        if previous is None or self._bind_search_context() is not previous:
            return
        chat_id, user_id = row.get('chat_id'), row.get('user_id')
        chat = self._display_chat(chat_id)
        if chat is None and chat_id is None and int64(user_id):
            chat = next((item for item in self.service.list_chats()
                         if not item.is_group and not item.is_channel and
                         item.peer_user_id == user_id), None)
        if self._bind_search_context() is not previous:
            return
        if chat is not None:
            self._show_chat_workspace()
            self._current_chat_id = chat.id
            self._close_conversation_info()
            self.chat_view.open_chat(chat, message_id=row.get('message_id'), time_ms=row.get('time_ms'))
            return
        # Directory hits don't join chats or manufacture an identity in SQLite.
        # A detached preview remains useful until that server flow is supported.
        preview = QMessageBox(QMessageBox.Icon.Information, 'Результат поиска', '', parent=self)
        preview.setTextFormat(Qt.TextFormat.PlainText)
        preview.setText(row.get('title', 'Результат поиска'))
        preview.setInformativeText((row.get('preview') or '') +
                                   '\n\nЭтот результат пока доступен только для просмотра.')
        if self._bind_search_context() is not previous:
            return
        preview.exec()

    def _display_chat(self, chat_id):
        if not int64(chat_id):
            return None
        # Retain the exact cache lookup: callers bind/check the account around
        # this read. Apply the presentation model only to that existing row.
        if self.service.store.get_chat(chat_id) is None:
            return None
        return next((row for row in self.service.list_chats() if row.id == chat_id), None)

    def _on_chat_clicked(self, item: QListWidgetItem) -> None:
        chat_id = item.data(Qt.ItemDataRole.UserRole)
        chat = self._display_chat(chat_id)
        if chat is None:
            return
        self._close_contact_profile()
        self.chat_list.setCurrentItem(item)
        self._current_chat_id = chat_id
        self._close_conversation_info()
        self.chat_view.open_chat(chat)

    def _chat_context_menu(self, pos) -> None:
        item = self.chat_list.itemAt(pos)
        if item is None:
            return
        chat_id = item.data(Qt.ItemDataRole.UserRole)
        menu = self._build_chat_row_menu(chat_id)
        if menu is not None:
            menu.popup(self.chat_list.mapToGlobal(pos))

    def _cancel_chat_row_menu(self):
        transfer = self._owner_transfer_dialog
        if transfer is not None and isValid(transfer) and transfer.context is self._row_menu_context:
            transfer.reject()
        report = self._report_dialog
        if report is not None and isValid(report) and report.context is self._row_menu_context:
            report.reject()
        preview = self._chat_preview_dialog
        self._chat_preview_dialog = None
        if preview is not None and isValid(preview):
            preview.reject()
        if self._row_menu is not None and isValid(self._row_menu):
            self._row_menu.cancel()
            self._row_menu.deleteLater()
        self._row_menu = None
        self._row_menu_context = None
        self._row_menu_section = None
        self._row_menu_folder_id = None
        self._row_read_context = None
        self._row_pin_context = None
        self._row_pin_state = None
        self._row_pin_job = None
        self._row_action_job = None
        self._row_folder_job = None
        self._row_folder_rows = None
        self._row_metadata_job = None
        self._row_metadata_failed = False

    def _selected_row_folder_id(self):
        record = self._folders.get(self._section)
        if isinstance(record, dict) and isinstance(record.get('id'), str) and record['id']:
            return record['id']
        return None  # Local fallback tabs do not prove a canonical folder identity.

    def _chat_row_current(self, context):
        try:
            return (isValid(self) and self._search_enabled and context is not None
                    and context is self._row_menu_context
                    and self._section == self._row_menu_section
                    and self._selected_row_folder_id() == self._row_menu_folder_id
                    and self.service.is_current_conversation(context))
        except (AttributeError, ValueError, RuntimeError):
            return False

    def _chat_row_control_states(self, context):
        read, pin = {}, {}
        if not self._chat_row_current(context):
            return read, pin
        if self._row_read_context is not None:
            try:
                result = self.service.get_conversation_read_state(self._row_read_context)
                if isinstance(result, dict) and result.get('context') is self._row_read_context:
                    read = result
            except (AttributeError, ValueError, RuntimeError):
                pass
        if self._row_pin_context is not None:
            try:
                if self.service.is_current_conversation_pin(self._row_pin_context):
                    status = self.service.conversation_pin_status(self._row_pin_context)
                    if isinstance(status, dict) and status.get('context') is self._row_pin_context:
                        pin = dict(self._row_pin_state or {}, pending=status.get('pending') is True,
                                   available=status.get('available') is True
                                   and (self._row_pin_state or {}).get('available') is True)
            except (AttributeError, ValueError, RuntimeError):
                pass
        return read, pin

    def _chat_row_entries(self, context, *, ignore_busy=False):
        from .chat_row_menu import chat_row_entries
        if not self._chat_row_current(context):
            return []
        snapshot = self.service.conversation_snapshot(context)
        if not self._chat_row_current(context) or snapshot.get('context') is not context:
            return []
        read, pin = self._chat_row_control_states(context)
        if not self._chat_row_current(context):
            return []
        return chat_row_entries(snapshot, folders=self._row_folder_rows,
                                language=self.settings.ui_language,
                                busy=not ignore_busy and self._row_action_job is not None,
                                read_state=read, pin_state=pin,
                                preview_available=callable(getattr(self.service, 'load_conversation_preview', None)))

    def _chat_row_entry_allowed(self, row, context, *, ignore_busy=False):
        from .chat_row_menu import flatten_entries
        try:
            if row.get('action') == 'refresh_row_metadata':
                return (self._chat_row_current(context) and row.get('value') is None
                        and self._row_metadata_failed and self._row_metadata_job is None
                        and self._row_action_job is None
                        and callable(getattr(self.service, 'refresh_conversation_menu_metadata', None)))
            return any(candidate.get('action') == row.get('action')
                       and candidate.get('value') == row.get('value')
                       and candidate.get('available') is True
                       for candidate in flatten_entries(self._chat_row_entries(context, ignore_busy=ignore_busy)))
        except (AttributeError, ValueError, RuntimeError):
            return False

    def _log_chat_row_menu(self, category, error=None):
        """Local branch diagnostics only: never stringify a server exception."""
        categories = {'capture_failed', 'cached_snapshot_failed', 'metadata_loading',
                      'metadata_failed', 'metadata_unsupported', 'metadata_ready', 'async_failed'}
        errors = {'ValueError', 'RuntimeError', 'AttributeError', 'TypeError', 'KeyError',
                  'TimeoutError', 'ConnectionError', 'OSError', 'MaxError',
                  'MaxWriteNotStarted', 'MaxWriteOutcomeUnknown'}
        category = category if category in categories else 'unknown'
        name = type(error).__name__ if error is not None else 'none'
        name = name if name in errors or name == 'none' else 'OtherError'
        try:
            logger = getattr(self.service, 'log', None)
            if callable(logger):
                logger(f'chat-row-menu: category={category}; error={name}')
        except Exception:
            pass

    def _chat_row_metadata_entries(self):
        failed = self._row_metadata_failed
        entries = [{'action': 'metadata_status',
                    'label': tr('Не удалось обновить меню' if failed else 'Обновление меню…',
                                self.settings.ui_language),
                    'available': False, 'write': False,
                    'unavailable_reason': tr('Нужны подтверждённые данные этого чата',
                                             self.settings.ui_language)}]
        if failed and callable(getattr(self.service, 'refresh_conversation_menu_metadata', None)):
            entries.append({'action': 'refresh_row_metadata',
                            'label': tr('Повторить', self.settings.ui_language),
                            'available': self._row_action_job is None, 'write': False})
        return entries

    def _load_chat_row_metadata(self, context, menu):
        """Repair sparse cache with ordinary reads; never grant guessed actions."""
        if (not self._chat_row_current(context) or not isValid(menu)
                or self._row_menu is not menu or menu._revoked
                or self._row_metadata_job is not None or self._row_metadata_failed
                or self._row_action_job is not None):
            return
        try:
            if self._chat_row_entries(context):
                return
        except (AttributeError, ValueError, RuntimeError) as error:
            self._log_chat_row_menu('cached_snapshot_failed', error)
        method = getattr(self.service, 'refresh_conversation_menu_metadata', None)
        if not callable(method):
            self._row_metadata_failed = True
            menu.replace_entries(self._chat_row_metadata_entries())
            return
        request = self._row_metadata_job = object()
        self._log_chat_row_menu('metadata_loading')
        lifetime = {'active': True}

        def revoke(*_args):
            lifetime['active'] = False

        menu.aboutToHide.connect(revoke)
        menu.destroyed.connect(revoke)

        def request_current():
            # Pure Python/session checks in the worker. aboutToShow precedes
            # visible=True, so a fast worker must not test QWidget visibility.
            try:
                return (lifetime['active'] and self._search_enabled
                        and context is self._row_menu_context
                        and self._row_metadata_job is request
                        and self._row_menu is menu and not menu._revoked
                        and self._section == self._row_menu_section
                        and self._selected_row_folder_id() == self._row_menu_folder_id
                        and self.service.is_current_conversation(context))
            except (AttributeError, ValueError, RuntimeError):
                return False

        def current():
            return (request_current() and self._chat_row_current(context)
                    and isValid(menu) and self._row_menu is menu
                    and not menu._revoked and menu.isVisible())

        def work():
            if not request_current():
                return False
            try:
                method(context)  # Captured owner/session, GET48/GET32 only.
                return True
            except Exception as error:
                # async_runner otherwise stringifies exceptions; strip at source.
                self._log_chat_row_menu('metadata_failed', error)
                return False

        def finish(ok=False):
            if not current():
                return
            self._row_metadata_job = None
            entries = []
            if ok is True:
                # Sparse metadata can also prevent read/pin-context capture.
                # Keep successful existing captures; retry only missing ones.
                if self._row_read_context is None:
                    try:
                        self._row_read_context = self.service.capture_conversation_read(context)
                    except (AttributeError, ValueError, RuntimeError):
                        pass
                if self._row_pin_context is None and self._row_menu_folder_id is not None:
                    try:
                        self._row_pin_context = self.service.capture_conversation_pin(context, self._row_menu_folder_id)
                    except (AttributeError, ValueError, RuntimeError):
                        pass
                try:
                    entries = self._chat_row_entries(context)
                except (AttributeError, ValueError, RuntimeError) as error:
                    self._log_chat_row_menu('cached_snapshot_failed', error)
            # Revalidate once more after reads/capture; never resurrect a popup.
            if (not self._chat_row_current(context) or not isValid(menu)
                    or self._row_menu is not menu or menu._revoked or not menu.isVisible()):
                return
            self._row_metadata_failed = not bool(entries)
            menu.replace_entries(entries or self._chat_row_metadata_entries())
            if entries:
                self._log_chat_row_menu('metadata_ready')
                self._load_chat_row_folders(context, menu)
                self._load_chat_row_pin(context, menu)
            elif ok is True:
                self._log_chat_row_menu('metadata_unsupported')

        def failed(_error):
            self._log_chat_row_menu('async_failed')
            finish(False)

        run_async(work, on_done=finish, on_error=failed)

    def _build_chat_row_menu(self, chat_id):
        from .conversation_actions_menu import ConversationActionsMenu
        if not isValid(self) or not self._search_enabled or not int64(chat_id):
            return None
        previous = self._row_menu_context
        if self._row_action_job is not None:
            if not self._chat_row_current(previous) or previous.chat_id != chat_id:
                return None
            context = previous  # A second popup must not replace an inflight binding.
        else:
            try:
                context = self.service.conversation_context(chat_id, activate_files=False)
            except (AttributeError, ValueError, RuntimeError) as error:
                self._log_chat_row_menu('capture_failed', error)
                return None
            self._row_menu_section = self._section
            self._row_menu_folder_id = self._selected_row_folder_id()
            self._row_read_context = None
            self._row_pin_context = None
            self._row_pin_state = None
            self._row_pin_job = None
            try:
                self._row_read_context = self.service.capture_conversation_read(context)
            except (AttributeError, ValueError, RuntimeError):
                pass
            if self._row_menu_folder_id is not None:
                try:
                    self._row_pin_context = self.service.capture_conversation_pin(context, self._row_menu_folder_id)
                except (AttributeError, ValueError, RuntimeError):
                    pass
        if self._row_menu is not None and isValid(self._row_menu):
            self._row_menu.cancel()
            self._row_menu.deleteLater()
        self._row_menu = None
        self._row_menu_context = context
        self._row_folder_job = None
        self._row_folder_rows = None
        self._row_metadata_job = None
        self._row_metadata_failed = False
        try:
            entries = self._chat_row_entries(context)
        except (AttributeError, ValueError, RuntimeError) as error:
            self._log_chat_row_menu('cached_snapshot_failed', error)
            entries = []
        if not entries:
            if not self._chat_row_current(context):
                return None
            self._row_metadata_failed = not callable(getattr(self.service, 'refresh_conversation_menu_metadata', None))
            entries = self._chat_row_metadata_entries()
        menu = ConversationActionsMenu(
            context, entries, self, theme=self.settings.theme, native_rows=True,
            unavailable_label=tr('Действие пока недоступно', self.settings.ui_language),
            allowed=lambda row: self._chat_row_entry_allowed(row, context))
        self._row_menu = menu
        menu.requested.connect(self._chat_row_menu_action)
        menu.aboutToShow.connect(lambda: self._load_chat_row_metadata(context, menu))
        menu.aboutToShow.connect(lambda: self._load_chat_row_folders(context, menu))
        menu.aboutToShow.connect(lambda: self._load_chat_row_pin(context, menu))
        return menu

    def _load_chat_row_pin(self, context, menu):
        pin_context = self._row_pin_context
        if (pin_context is None or not self._chat_row_current(context) or self._row_pin_job is not None
                or self._row_pin_state is not None or self._row_action_job is not None):
            return
        method = getattr(self.service, 'get_conversation_pin_state', None)
        if not callable(method):
            return
        request = self._row_pin_job = object()

        def work():
            if (not self._chat_row_current(context) or self._row_pin_job is not request
                    or pin_context is not self._row_pin_context):
                raise ValueError('The selected folder is no longer current')
            return method(pin_context)

        def finish(result=None):
            if (not self._chat_row_current(context) or self._row_pin_job is not request
                    or pin_context is not self._row_pin_context or not isValid(menu)
                    or self._row_menu is not menu or menu._revoked):
                return
            self._row_pin_job = None
            if not isinstance(result, dict) or result.get('context') is not pin_context:
                return
            self._row_pin_state = result
            self._apply_chat_row_pin_folder(context, pin_context, result)
            try:
                entries = self._chat_row_entries(context)
                if entries:
                    menu.replace_entries(entries)
            except (AttributeError, ValueError, RuntimeError):
                menu.cancel()

        run_async(work, on_done=finish, on_error=lambda _error: finish())

    def _apply_chat_row_pin_folder(self, context, pin_context, result):
        """Install only the captured folder's authoritative readback DTO."""
        if (not self._chat_row_current(context) or pin_context is not self._row_pin_context
                or not isinstance(result, dict) or result.get('context') is not pin_context):
            return
        folder = result.get('folder')
        if (not isinstance(folder, dict) or folder.get('id') != self._row_menu_folder_id
                or result.get('folder_id') != self._row_menu_folder_id
                or not isinstance(folder.get('title'), str) or not isinstance(folder.get('favorites'), list)
                or self._section not in self._folders):
            return
        self._folders[self._section] = copy.deepcopy(folder)
        self._schedule_reload()

    def _load_chat_row_folders(self, context, menu):
        method = getattr(self.service, 'get_conversation_folders', None)
        if (not self._chat_row_current(context) or not isValid(menu) or self._row_menu is not menu
                or not callable(method) or self._row_folder_job is not None
                or self._row_folder_rows is not None):
            return
        try:
            if not any(row.get('action') == 'folder' and row.get('available') is True
                       for row in self._chat_row_entries(context)):
                return
        except (AttributeError, ValueError, RuntimeError):
            return
        request = self._row_folder_job = object()

        def finish(snapshot=None, *, failed=False):
            if (not self._chat_row_current(context) or self._row_folder_job is not request
                    or not isValid(menu) or self._row_menu is not menu or menu._revoked):
                return
            self._row_folder_job = None
            rows = []
            if (failed or not isinstance(snapshot, dict) or snapshot.get('context') is not context
                    or not isinstance(snapshot.get('orderedFolders'), list)):
                rows.append({'action': 'folder_retry', 'label': tr('Обновить папки', self.settings.ui_language),
                             'available': True, 'write': False})
            else:
                if not self._folders:
                    # Reuse this session-bound read after a failed/partial
                    # startup fetch. _on_folders accepts only a complete
                    # navigation snapshot and revokes this fallback-bound
                    # popup; a new popup must capture the actual folder ID.
                    self._on_folders(snapshot, context=context.session_context)
                    if not self._chat_row_current(context):
                        return
                for folder in snapshot['orderedFolders']:
                    if (not isinstance(folder, dict) or not isinstance(folder.get('id'), str)
                            or folder['id'] == 'all.chat.folder' or not isinstance(folder.get('title'), str)):
                        continue
                    try:
                        view = self.service.get_folder_view(folder)
                    except (AttributeError, ValueError, RuntimeError):
                        continue
                    if view.get('hidden') is not False:
                        continue
                    included = context.chat_id in folder.get('include', [])
                    rows.append({'action': 'folder_include', 'label': folder['title'],
                                 'available': snapshot.get('writePending') is False,
                                 'checked': included, 'write': True,
                                 'value': {'folder_id': folder['id'], 'included': not included}})
            if not self._chat_row_current(context):
                return
            self._row_folder_rows = rows
            try:
                folder = next((entry for entry in self._chat_row_entries(context)
                               if entry.get('action') == 'folder'), None)
            except (AttributeError, ValueError, RuntimeError):
                menu.cancel()
                return
            if folder is not None:
                for _action, descriptor, submenu in menu._rows:
                    if descriptor.get('action') == 'folder' and submenu is not None:
                        submenu.replace_entries(folder.get('children', []))
                        break
            menu.revalidate()

        # The service read validates the captured owner/client/session itself;
        # stale callbacks cannot update an already dismissed or replaced menu.
        run_async(method, context, on_done=finish,
                  on_error=lambda _error: finish(failed=True))

    def _bot_delete_current(self, context, *, source, continuation=False):
        current = self._chat_row_current if source == 'row' else self._info_current
        if not current(context):
            return False
        try:
            snapshot = self.service.conversation_snapshot(context)
        except (ValueError, AttributeError, RuntimeError):
            return False
        return current(context) and bot_delete_available(snapshot, context, continuation=continuation)

    def _chat_row_menu_action(self, action, value, context):
        if (not self._chat_row_current(context) or self._row_action_job is not None
                or not self._chat_row_entry_allowed({'action': action, 'value': value}, context)):
            return
        if action == 'preview':
            self._open_chat_preview(context)
            return
        if action == 'report':
            self._open_conversation_report(context, source='row')
            return
        if action in ('transfer_owner_leave', 'continue_owner_leave'):
            self._open_owner_transfer(context, source='row', continuation=action == 'continue_owner_leave')
            return
        if action == 'folder_create':
            self._open_settings()
            self._settings_workspace.select_page('folders')
            return
        if action in ('folder_retry', 'refresh_row_metadata'):
            # A fresh explicit popup restarts only the ordinary folder read.
            menu = self._build_chat_row_menu(context.chat_id)
            if menu is not None:
                menu.popup(self.chat_list.mapToGlobal(self.chat_list.rect().center()))
            return
        if action in ('block', 'unblock'):
            label = 'Заблокировать' if action == 'block' else 'Разблокировать'
            if not confirm_action(self, label + ' контакт?', label + ' контакт в этом чате?'):
                return
            if (not self._chat_row_current(context)
                    or not self._chat_row_entry_allowed({'action': action, 'value': value}, context)):
                return
        options = {}
        bot_operation = False
        if action in ('clear', 'delete', 'continue_bot_delete', 'refresh_actions'):
            try:
                snapshot = self.service.conversation_snapshot(context)
            except (ValueError, AttributeError):
                return
            bot_operation = (action != 'clear' and
                             snapshot.get('action_capabilities', {}).get('kind') == 'bot')
        if bot_operation and action in ('delete', 'continue_bot_delete'):
            continuation = action == 'continue_bot_delete'
            if not confirm_bot_conversation_delete(
                    self, snapshot, context, continuation=continuation,
                    language=self.settings.ui_language,
                    context_is_current=lambda: self._bot_delete_current(
                        context, source='row', continuation=continuation)):
                return
            if (not self._bot_delete_current(context, source='row', continuation=continuation)
                    or not self._chat_row_entry_allowed({'action': action, 'value': value}, context)):
                return
            if not continuation:
                options['for_all'] = False
        elif action == 'continue_bot_delete':
            return
        elif action in ('clear', 'delete'):
            for_all = confirm_conversation_removal(self, action, snapshot['title'],
                                                   snapshot.get('action_capabilities', {}))
            if (for_all is None or not self._chat_row_current(context)
                    or not self._chat_row_entry_allowed({'action': action, 'value': value}, context)):
                return
            options['for_all'] = for_all
        elif action == 'leave':
            try:
                snapshot = self.service.conversation_snapshot(context)
            except (ValueError, AttributeError):
                return
            if snapshot.get('context') is not context or not confirm_conversation_leave(
                    self, snapshot, language=self.settings.ui_language,
                    context_is_current=lambda: self._chat_row_current(context)):
                return
            if (not self._chat_row_current(context)
                    or not self._chat_row_entry_allowed({'action': action, 'value': value}, context)):
                return
        methods = {'mute': ('set_conversation_mute', (value,)),
                   'block': ('set_conversation_contact_blocked', (True,)),
                   'unblock': ('set_conversation_contact_blocked', (False,)),
                   'stop_bot': ('stop_conversation_bot', ()),
                   'refresh_actions': ('refresh_conversation_controls', ()),
                   'clear': ('clear_conversation_history', ()),
                   'delete': ('delete_conversation', ()),
                   'continue_bot_delete': ('continue_bot_conversation_delete', ()),
                   'leave': ('leave_conversation', ())}
        operation_context = context
        if action in ('toggle_read', 'refresh_row_read'):
            operation_context = self._row_read_context
            if operation_context is None or (action == 'toggle_read' and type(value) is not bool):
                return
            methods[action] = (('set_conversation_read', (value,)) if action == 'toggle_read'
                               else ('refresh_conversation_read_state', ()))
        elif action in ('toggle_pin', 'refresh_row_pin'):
            operation_context = self._row_pin_context
            if operation_context is None or (action == 'toggle_pin' and type(value) is not bool):
                return
            methods[action] = (('set_conversation_pinned', (value,)) if action == 'toggle_pin'
                               else ('get_conversation_pin_state', ()))
        if action == 'folder_include':
            if (not isinstance(value, dict) or set(value) != {'folder_id', 'included'}
                    or not isinstance(value['folder_id'], str) or type(value['included']) is not bool):
                return
            methods[action] = ('set_conversation_folder_included', (value['folder_id'], value['included']))
        if action not in methods:
            return
        name, args = methods[action]
        method = getattr(self.service, name, None)
        if not callable(method):
            return
        if action == 'toggle_read' and value is False:
            self._close_chat_for_row_unread(context)
            if not self._chat_row_current(context):
                return
        request = self._row_action_job = object()

        def work():
            if (not self._chat_row_current(context) or self._row_action_job is not request
                    or not self._chat_row_entry_allowed({'action': action, 'value': value}, context,
                                                        ignore_busy=True)):
                raise ValueError('The selected conversation is no longer current')
            if bot_operation and action in ('delete', 'continue_bot_delete') and not self._bot_delete_current(
                    context, source='row', continuation=action == 'continue_bot_delete'):
                raise ValueError('The selected bot deletion is no longer current')
            return method(operation_context, *args, **options)

        def finish(result=None, *, failed=False):
            if not isValid(self) or self._row_action_job is not request:
                return
            self._row_action_job = None
            if not self._chat_row_current(context):
                return
            completed = confirmed_lifecycle_action(result, context, action) if not failed else None
            bot_result = bot_delete_result(result, context, action)
            if bot_operation and completed == 'delete' and bot_result is None:
                completed = None
            if completed == 'leave':
                self._finish_conversation_leave(context)
                transfer_result = owner_transfer_result(result, context, action)
                if transfer_result is not None:
                    self.statusBar().showMessage(owner_transfer_notice(transfer_result, self.settings.ui_language), 5000)
                return
            if completed in ('clear', 'delete'):
                if self.chat_view.chat is not None and self.chat_view.chat.id == context.chat_id:
                    if completed == 'delete':
                        self._close_conversation_info()
                        self._clear_previous_contact_chat()
                    else:
                        self.chat_view.reload()
                self.statusBar().showMessage(bot_delete_notice(bot_result, self.settings.ui_language)
                    if bot_result is not None else 'Чат удалён.' if completed == 'delete' else 'Переписка удалена.', 5000)
            if (action in ('toggle_pin', 'refresh_row_pin') and isinstance(result, dict)
                    and result.get('context') is operation_context):
                self._row_pin_state = result
                self._apply_chat_row_pin_folder(context, operation_context, result)
            try:
                snapshot = self.service.conversation_snapshot(context)
            except (AttributeError, ValueError, RuntimeError):
                return
            if not self._chat_row_current(context):
                return
            if self._row_menu is not None and isValid(self._row_menu):
                self._row_menu.revalidate()
            self._schedule_reload()
            read, pin = self._chat_row_control_states(context)
            bot_status = bot_result or bot_delete_state(snapshot, context)
            transfer_status = owner_transfer_result(result, context, action) or owner_transfer_state(snapshot, context)
            if transfer_status is not None and completed is None:
                from .invite_link import show_menu_notice
                show_menu_notice(self, owner_transfer_notice(transfer_status, self.settings.ui_language))
            elif bot_status is not None and completed is None:
                from .invite_link import show_menu_notice
                show_menu_notice(self, bot_delete_notice(bot_status, self.settings.ui_language))
            elif not failed and accepted_lifecycle_pending_notice(result, context, action):
                from .invite_link import show_menu_notice
                show_menu_notice(self, tr(accepted_lifecycle_pending_notice(result, context, action),
                                          self.settings.ui_language))
            elif (failed or snapshot.get('action_pending') is True or read.get('pending') is True
                    or pin.get('pending') is True):
                from .invite_link import show_menu_notice
                show_menu_notice(self, tr('Не удалось подтвердить действие. Проверьте состояние.',
                                          self.settings.ui_language))

        run_async(work, on_done=finish, on_error=lambda _error: finish(failed=True))

    def _open_conversation_editor(self, context):
        if not self._info_current(context) or self._conversation_edit_dialog is not None:
            return
        try:
            snapshot = self.service.conversation_snapshot(context)
        except (AttributeError, ValueError, RuntimeError):
            return
        if not conversation_edit_available(snapshot, context):
            return
        dialog = ConversationEditDialog(self.service, context, snapshot, self,
            language=self.settings.ui_language, context_is_current=lambda: self._info_current(context))
        self._conversation_edit_dialog = dialog

        def completed(_result):
            if not self._info_current(context) or self._conversation_edit_dialog is not dialog:
                return
            try:
                updated = self.service.conversation_snapshot(context)
            except (AttributeError, ValueError, RuntimeError):
                return
            if not self._info_current(context) or updated.get('context') is not context:
                return
            self._info_panel.set_conversation(self._info_with_avatar(updated))
            self._schedule_reload()

        def closed(_result):
            if isValid(self) and self._conversation_edit_dialog is dialog:
                self._conversation_edit_dialog = None
            if isValid(dialog):
                dialog.deleteLater()

        dialog.completed.connect(completed)
        dialog.finished.connect(closed)
        dialog.open()
        dialog.start_loading()

    def _open_owner_transfer(self, context, *, source, continuation=False):
        if source not in ('row', 'info'):
            return
        current = self._chat_row_current if source == 'row' else self._info_current
        if not current(context) or self._owner_transfer_dialog is not None:
            return
        try:
            snapshot = self.service.conversation_snapshot(context)
        except (AttributeError, ValueError, RuntimeError):
            return
        if not owner_transfer_available(snapshot, context, continuation=continuation):
            return
        dialog = ConversationOwnerTransferDialog(self.service, context, snapshot, self,
            language=self.settings.ui_language, context_is_current=lambda: current(context))
        self._owner_transfer_dialog = dialog

        def completed(result):
            if not current(context) or self._owner_transfer_dialog is not dialog:
                return
            action = 'continue_owner_leave' if continuation else 'transfer_owner_leave'
            if confirmed_lifecycle_action(result, context, action) == 'leave':
                self._finish_conversation_leave(context)
                self.statusBar().showMessage(owner_transfer_notice(result, self.settings.ui_language), 5000)
                return
            try:
                updated = self.service.conversation_snapshot(context)
            except (AttributeError, ValueError, RuntimeError):
                return
            if not current(context) or updated.get('context') is not context:
                return
            if source == 'info':
                self._info_panel.set_conversation(self._info_with_avatar(updated))
            elif self._row_menu is not None and isValid(self._row_menu):
                self._row_menu.revalidate()
            self._schedule_reload()

        def closed(_result):
            if isValid(self) and self._owner_transfer_dialog is dialog:
                self._owner_transfer_dialog = None
            if isValid(dialog):
                dialog.deleteLater()

        dialog.completed.connect(completed)
        dialog.finished.connect(closed)
        dialog.open()
        if not continuation:
            dialog.start_loading()

    def _open_conversation_report(self, context, *, source):
        from .conversation_report_dialog import ConversationReportDialog, report_available
        if source not in ('row', 'info'):
            return
        current = self._chat_row_current if source == 'row' else self._info_current
        if not current(context) or self._report_dialog is not None:
            return
        try:
            snapshot = self.service.conversation_snapshot(context)
        except (AttributeError, ValueError, RuntimeError):
            return
        if snapshot.get('context') is not context or not report_available(snapshot):
            return
        dialog = ConversationReportDialog(self.service, context, snapshot, self,
                                          language=self.settings.ui_language,
                                          context_is_current=lambda: current(context))
        self._report_dialog = dialog

        def completed(_result):
            if not current(context) or self._report_dialog is not dialog:
                return
            try:
                updated = self.service.conversation_snapshot(context)
            except (AttributeError, ValueError, RuntimeError):
                return
            if not current(context) or updated.get('context') is not context:
                return
            if source == 'info':
                self._info_panel.set_conversation(self._info_with_avatar(updated))
            elif self._row_menu is not None and isValid(self._row_menu):
                self._row_menu.revalidate()

        def closed(_result):
            if isValid(self) and self._report_dialog is dialog:
                self._report_dialog = None
            if isValid(dialog):
                dialog.deleteLater()

        dialog.completed.connect(completed)
        dialog.finished.connect(closed)
        dialog.open()
        dialog.start_loading()

    def _finish_conversation_leave(self, context):
        """Close only the departed conversation; its cached history stays intact."""
        if self._info_context is not None and self._info_context.chat_id == context.chat_id:
            self._close_conversation_info()
        if self.chat_view.chat is not None and self.chat_view.chat.id == context.chat_id:
            self._clear_previous_contact_chat()
        self._cancel_chat_row_menu()
        self._schedule_reload()
        self.statusBar().showMessage(tr('Вы вышли из чата.', self.settings.ui_language), 5000)

    def _open_chat_preview(self, context):
        from .chat_preview import ChatPreviewDialog
        if not self._chat_row_current(context):
            return
        previous = self._chat_preview_dialog
        if previous is not None and isValid(previous):
            previous.reject()
        try:
            snapshot = self.service.conversation_snapshot(context)
        except (AttributeError, ValueError, RuntimeError):
            return
        if snapshot.get('context') is not context or not self._chat_row_current(context):
            return
        dialog = ChatPreviewDialog(self.service, self.settings, context, snapshot.get('title', ''),
                                   self, context_is_current=lambda: self._chat_row_current(context))
        self._chat_preview_dialog = dialog
        receipts = self.chat_view.read_receipts
        receipts.suspend_for_overlay()

        def closed(_result):
            if isValid(self) and self._chat_preview_dialog is dialog:
                self._chat_preview_dialog = None
                receipts.schedule()
            if isValid(dialog):
                dialog.deleteLater()

        dialog.finished.connect(closed)
        dialog.open()
        receipts.schedule()  # Modal state is now visible to the foreground guard.
        dialog.start_loading()

    def _close_chat_for_row_unread(self, context):
        """Only explicit unread dispatch detaches this chat and its read timer."""
        if not self._chat_row_current(context):
            return
        view = self.chat_view
        if view.chat is None or view.chat.id != context.chat_id:
            return  # A menu for a different row must not close the active chat.
        view.read_receipts.timer.stop()
        view._drafts[view.chat.id] = view.input.toPlainText()
        if self._info_context is None or self._info_context.chat_id == context.chat_id:
            self._close_conversation_info()
        view.show_placeholder()
        view.input.clear()
        self._current_chat_id = None
        self.chat_list.setCurrentItem(None)
        self.chat_list.clearSelection()

    def _block_contact(self, contact_id, name):
        self._open_settings()
        self._settings_workspace.blocked_contacts.request_block(contact_id, name)
        self._settings_workspace.select_page('blocked')

    def _rename_chat(self, chat_id: int) -> None:
        context = self._bind_search_context()
        if context is None:
            return
        chat = self.service.store.get_chat(chat_id)
        if chat is None or self._bind_search_context() is not context:
            return
        current = chat.title if chat else ""
        new_name, ok = QInputDialog.getText(
            self, "Переименовать", "Имя собеседника / чата:", text=current or ""
        )
        if not ok or not new_name.strip() or self._bind_search_context() is not context:
            return
        try:
            self.service.rename_conversation(chat_id, new_name.strip(), context=context)
        except Exception as e:  # noqa: BLE001
            warning = QMessageBox(QMessageBox.Icon.Warning, 'Не удалось', '', parent=self)
            warning.setTextFormat(Qt.TextFormat.PlainText)
            warning.setText(str(e))
            warning.exec()
            return
        self._reload_chats()

    def _on_chat_touched(self, _chat_id: int) -> None:
        self._schedule_reload()

    def _open_contact_by_id(self, contact_id: int, title: str) -> bool:
        context = self._bind_search_context()
        if context is None or not int64(contact_id):
            return False
        try:
            chats = self.service.list_chats()
            resolver = getattr(self.service, 'dialog_for_contact', None)
            if callable(resolver):
                chat = resolver(contact_id)
            elif contact_id == self.service.my_id:
                chat = next((item for item in chats if item.id == 0 and item.chat_type == 'DIALOG'), None)
            else:
                chat = next((item for item in chats
                             if item.peer_user_id == contact_id and not item.is_group and not item.is_channel), None)
        except (AttributeError, ValueError):
            self.statusBar().showMessage('Сессия изменилась. Откройте контакты заново.', 5000)
            return False
        if self._bind_search_context() is not context:
            return False
        if chat is None:
            self._close_conversation_info()
            self._clear_previous_contact_chat()
            if self._show_contact_profile_by_id(contact_id):
                return True
            self.statusBar().showMessage('Диалог с этим контактом пока не загружен', 5000)
            return False
        self._show_chat_workspace()
        self._close_conversation_info()
        self.search.clear()
        self._current_chat_id = chat.id
        self.chat_view.open_chat(chat)
        return True

    def _info_current(self, context):
        try:
            return (isValid(self) and context is not None and context is self._info_context and
                    self._search_enabled and self.service.is_current_conversation(context))
        except (AttributeError, ValueError):
            return False

    def _close_conversation_info(self):
        editor = self._conversation_edit_dialog
        if editor is not None and isValid(editor) and editor.context is self._info_context:
            editor.reject()
        transfer = self._owner_transfer_dialog
        if transfer is not None and isValid(transfer) and transfer.context is self._info_context:
            transfer.reject()
        report = self._report_dialog
        if report is not None and isValid(report) and report.context is self._info_context:
            report.reject()
        self._info_clock.stop()
        if (self._info_context is not None and self._info_file_controller is not None
                and self._info_file_controller.active
                and self._info_file_controller.descriptor.context is self._info_context):
            self._info_file_controller.cancel()
        clear_files = getattr(self.service, 'clear_conversation_files', None)
        if self._info_context is not None and callable(clear_files):
            clear_files(self._info_context)
        self._info_media_loader.bind(None)
        if self._info_photo_viewer is not None and isValid(self._info_photo_viewer):
            self._info_photo_viewer.close()
        self._info_photo_viewer = None
        self._info_action_job = None
        self._info_folder_rows = None
        self._info_folder_job = None
        self._info_context = None
        self._info_pages = {}
        self._info_snapshot = {}
        if self._info_panel is not None:
            self._info_panel.clear()
        self._conversation_pages.setCurrentWidget(self.chat_view)

    def _open_conversation_info(self, chat):
        from .conversation_info import ConversationInfo
        if self._info_context is not None:
            self._close_conversation_info()
        try:
            context = self.service.conversation_context(chat.id)
            snapshot = self.service.conversation_snapshot(context)
        except (AttributeError, ValueError):
            self.statusBar().showMessage('Информация о чате сейчас недоступна', 5000)
            return
        self._info_context, self._info_pages = context, {}
        self._info_action_job = self._info_folder_job = None
        self._info_folder_rows = None
        if not self._info_current(context):
            return
        self._info_media_loader.bind(context)
        if self._info_panel is None:
            self._info_panel = ConversationInfo(theme=self.settings.theme, language=self.settings.ui_language)
            self._info_panel.back_requested.connect(self._close_conversation_info)
            self._info_panel.action_requested.connect(self._info_action)
            self._info_panel.menu_action_requested.connect(self._info_menu_action)
            self._info_panel.tab_requested.connect(self._request_info_tab)
            self._info_panel.more_requested.connect(lambda tab, owner: self._request_info_tab(tab, owner, more=True))
            self._info_panel.item_requested.connect(self._open_info_item)
            self._info_panel.thumbnail_requested.connect(self._request_info_thumbnail)
            self._info_panel.file_action_requested.connect(self._info_file_action)
            self._conversation_pages.addWidget(self._info_panel)
        self._info_panel.set_conversation(self._info_with_avatar(snapshot))
        self._conversation_pages.setCurrentWidget(self._info_panel)
        self._info_clock.start()
        self._load_info_folders(context)

        def refreshed(updated):
            if self._info_current(context):
                self._info_panel.set_conversation(self._info_with_avatar(updated))
                self._load_info_folders(context)

        def failed(_error):
            if self._info_current(context):
                self._info_panel.set_error(context)

        run_async(self.service.refresh_conversation, context, on_done=refreshed, on_error=failed)

    def _info_with_avatar(self, snapshot):
        result = dict(snapshot)
        result['action_busy'] = self._info_action_job is not None
        bot_state = bot_delete_state(result, result.get('context'))
        if bot_state is not None:
            result.update(_localized_info_status(bot_delete_notice(bot_state, 'ru'), self.settings.ui_language))
        transfer_state = owner_transfer_state(result, result.get('context'))
        if transfer_state is not None:
            source, values, sources = owner_transfer_notice_parts(transfer_state)
            result.update(_localized_info_status(source, self.settings.ui_language, values, sources))
        edit_state = conversation_edit_state(result, result.get('context'))
        if edit_state is not None:
            result.update(_localized_info_status(conversation_edit_notice(edit_state, 'ru'), self.settings.ui_language))
        if self._info_action_job is not None:
            result['action_pending'] = True
            result.update(_localized_info_status('Сохранение…', self.settings.ui_language))
        result['more_actions'] = [dict(row) for row in result.get('more_actions', [])]
        for row in result['more_actions']:
            if row.get('action') == 'folder' and row.get('available') is True:
                row['children'] = list(self._info_folder_rows or [])
                if self._info_folder_rows is None:
                    row['children'].append({'action': 'folder_loading', 'label': 'Загрузка папок…', 'label_source': 'Загрузка папок…',
                                            'available': False, 'write': False})
                row['children'].append({'action': 'folder_create', 'label': 'Создать папку', 'label_source': 'Создать папку',
                                        'available': True, 'write': False, 'separator_before': True})
        if self.chat_view.chat is not None and self.chat_view.chat.id == result.get('chat_id'):
            result['avatar_pixmap'] = self.chat_view.h_avatar.cached_photo()
        self._info_snapshot = result
        return result

    def _load_info_folders(self, context):
        method = getattr(self.service, 'get_conversation_folders', None)
        if (not self._info_current(context) or not callable(method) or
                self._info_folder_job is not None or self._info_folder_rows is not None or
                not any(row.get('action') == 'folder' and row.get('available') is True
                        for row in self._info_snapshot.get('more_actions', []))):
            return
        request = self._info_folder_job = object()

        def done(snapshot):
            if not self._info_current(context) or self._info_folder_job is not request:
                return
            self._info_folder_job = None
            if not isinstance(snapshot, dict) or not isinstance(snapshot.get('orderedFolders'), list):
                self._info_folder_rows = [{'action': 'folder_retry', 'label': 'Обновить папки', 'label_source': 'Обновить папки',
                                          'available': True, 'write': False}]
            else:
                rows = []
                for folder in snapshot['orderedFolders']:
                    if not isinstance(folder, dict) or folder.get('id') == 'all.chat.folder':
                        continue
                    try:
                        view = self.service.get_folder_view(folder)
                    except (ValueError, AttributeError):
                        continue
                    if view.get('hidden') is not False:
                        continue  # Unknown visibility is not a confirmed selectable folder.
                    checked = context.chat_id in folder.get('include', [])
                    rows.append({'action': 'folder_include', 'label': folder['title'],
                                 'available': not snapshot.get('writePending', False), 'write': True,
                                 'checked': checked,
                                 'value': {'folder_id': folder['id'], 'included': not checked}})
                self._info_folder_rows = rows
            if self._info_current(context):
                self._info_panel.set_conversation(self._info_with_avatar(self._info_snapshot))

        def failed(_error):
            if self._info_current(context) and self._info_folder_job is request:
                self._info_folder_job = None
                self._info_folder_rows = [{'action': 'folder_retry', 'label': 'Обновить папки', 'label_source': 'Обновить папки',
                                          'available': True, 'write': False}]
                self._info_panel.set_conversation(self._info_with_avatar(self._info_snapshot))

        run_async(method, context, on_done=done, on_error=failed)

    def _poll_info_action_state(self):
        context = self._info_context
        method = getattr(self.service, 'conversation_action_status', None)
        if not self._info_current(context) or not callable(method):
            return
        try:
            state = method(context)  # Local confirmed cache only, no RPC.
        except (ValueError, AttributeError):
            return
        if not self._info_current(context) or not isinstance(state, dict) or state.get('context') is not context:
            return
        pending = state.get('pending') is True or self._info_action_job is not None
        bot_state = bot_delete_state(state, context)
        status = {'action_status': state.get('action_status', '') if pending else ''}
        if pending and not status['action_status']:
            status = _localized_info_status('MAX пока не подтвердил изменение.', self.settings.ui_language)
        if bot_state is not None:
            status = _localized_info_status(bot_delete_notice(bot_state, 'ru'), self.settings.ui_language)
        transfer_state = owner_transfer_state(state, context)
        if transfer_state is not None:
            source, values, sources = owner_transfer_notice_parts(transfer_state)
            status = _localized_info_status(source, self.settings.ui_language, values, sources)
        edit_state = conversation_edit_state(state, context)
        if edit_state is not None:
            status = _localized_info_status(conversation_edit_notice(edit_state, 'ru'), self.settings.ui_language)
        if self._info_action_job is not None:
            status = _localized_info_status('Сохранение…', self.settings.ui_language)
        available = (self._info_snapshot.get('action_capabilities', {}).get('mute') is True
                     and (state.get('available') is True or state.get('mute_transport_available') is True))
        self._info_panel.apply_action_state(context, muted=state.get('muted'), available=available,
            pending=pending, status=status['action_status'], status_source=status.get('action_status_source'),
            status_values=status.get('action_status_values'), status_values_sources=status.get('action_status_values_sources'))
        self._info_snapshot['muted'] = state.get('muted')
        self._info_snapshot['action_pending'] = pending
        # Lazy folder callbacks reuse this projection: never resurrect an older
        # notice/template or a previous owner's name after a newer status read.
        for key in ('action_status_source', 'action_status_values', 'action_status_values_sources'):
            self._info_snapshot.pop(key, None)
        self._info_snapshot.update(status)
        for key, current in (('bot_delete_state', bot_state), ('owner_transfer_state', transfer_state),
                             ('conversation_edit_state', edit_state)):
            if current is None:
                self._info_snapshot.pop(key, None)
            else:
                self._info_snapshot[key] = dict(current)
        self._info_snapshot.setdefault('availability', {})['mute'] = available

    def _info_menu_action(self, action, value, context):
        if not self._info_current(context):
            return

        def entries(rows):
            for row in rows:
                yield row
                yield from entries(row.get('children', []))

        entry = next((row for row in entries(self._info_snapshot.get('more_actions', []))
                      if row.get('action') == action and row.get('value') == value
                      and row.get('available') is True), None)
        if action == 'mute':
            if self._info_snapshot.get('availability', {}).get('mute') is not True:
                return
        elif entry is None:
            return
        if action == 'search':
            self._info_action('find', context)
            return
        if action == 'folder_create':
            self._open_settings()
            self._settings_workspace.select_page('folders')
            return
        if action == 'folder_retry':
            self._info_folder_rows = None
            self._load_info_folders(context)
            return
        if action in ('transfer_owner_leave', 'continue_owner_leave'):
            if self._info_action_job is None:
                self._open_owner_transfer(context, source='info', continuation=action == 'continue_owner_leave')
            return
        continuation_allowed = (action == 'continue_bot_delete' and
                                bot_delete_available(self._info_snapshot, context, continuation=True))
        if self._info_action_job is not None or (
                self._info_snapshot.get('action_pending') is True
                and action != 'refresh_actions' and not continuation_allowed):
            return
        if action == 'report':
            self._open_conversation_report(context, source='info')
            return
        if action in ('block', 'unblock'):
            label = 'Заблокировать' if action == 'block' else 'Разблокировать'
            if not confirm_action(self, label + ' контакт?', label + ' «' + self._info_snapshot['title'] + '»?'):
                return
            if not self._info_current(context):
                return
        options = {}
        target_context = None
        bot_operation = (action in ('delete', 'continue_bot_delete', 'refresh_actions') and
                         self._info_snapshot.get('action_capabilities', {}).get('kind') == 'bot')
        if bot_operation and action in ('delete', 'continue_bot_delete'):
            continuation = action == 'continue_bot_delete'
            try:
                snapshot = self.service.conversation_snapshot(context)
            except (ValueError, AttributeError, RuntimeError):
                return
            if not confirm_bot_conversation_delete(
                    self, snapshot, context, continuation=continuation,
                    language=self.settings.ui_language,
                    context_is_current=lambda: self._bot_delete_current(
                        context, source='info', continuation=continuation)):
                return
            if not self._bot_delete_current(context, source='info', continuation=continuation):
                return
            if not continuation:
                options['for_all'] = False
        elif action == 'continue_bot_delete':
            return
        elif action in ('clear', 'delete'):
            for_all = confirm_conversation_removal(self, action, self._info_snapshot['title'],
                                                   self._info_snapshot.get('action_capabilities', {}))
            if for_all is None or not self._info_current(context):
                return
            options['for_all'] = for_all
        elif action == 'leave':
            if not confirm_conversation_leave(
                    self, self._info_snapshot, language=self.settings.ui_language,
                    context_is_current=lambda: self._info_current(context)):
                return
            if not self._info_current(context):
                return
        elif action == 'share_contact':
            try:
                destinations = self.service.share_conversation_destinations(context)
            except (ValueError, AttributeError):
                if self._info_current(context):
                    self.statusBar().showMessage('Не удалось подтвердить доступные чаты для отправки.', 5000)
                return
            if not self._info_current(context):
                return
            if not destinations:
                self.statusBar().showMessage('Нет доступных чатов для отправки контакта.', 5000)
                return
            picker = ShareContactDialog(self._info_snapshot['title'], destinations, self)
            accepted = picker.exec() == QDialog.DialogCode.Accepted
            target_id = picker.selected_chat_id
            picker.deleteLater()
            if (not accepted or not int64(target_id) or not self._info_current(context)
                    or not any(chat.id == target_id for chat in destinations)):
                return
            try:
                target_context = self.service.conversation_context(target_id, activate_files=False)
            except (ValueError, AttributeError):
                self.statusBar().showMessage('Выбранный чат сейчас недоступен.', 5000)
                return
        if (not self._info_current(context) or self._info_action_job is not None
                or self._info_snapshot.get('action_pending') is True
                and action != 'refresh_actions' and not continuation_allowed):
            return
        if action != 'mute' and not any(row.get('action') == action and row.get('value') == value
                                       and row.get('available') is True
                                       for row in entries(self._info_snapshot.get('more_actions', []))):
            return
        methods = {'mute': ('set_conversation_mute', (value,)),
                   'hide_stories': ('set_conversation_stories_hidden', (True,)),
                   'show_stories': ('set_conversation_stories_hidden', (False,)),
                   'stop_bot': ('stop_conversation_bot', ()),
                   'block': ('set_conversation_contact_blocked', (True,)),
                   'unblock': ('set_conversation_contact_blocked', (False,)),
                   'refresh_actions': ('refresh_conversation_controls', ()),
                   'clear': ('clear_conversation_history', ()),
                   'delete': ('delete_conversation', ()),
                   'continue_bot_delete': ('continue_bot_conversation_delete', ()),
                   'leave': ('leave_conversation', ()),
                   'share_contact': ('share_conversation_contact', (target_context,))}
        if action == 'folder_include':
            if (not isinstance(value, dict) or set(value) != {'folder_id', 'included'}
                    or not isinstance(value['folder_id'], str) or type(value['included']) is not bool):
                return
            methods[action] = ('set_conversation_folder_included', (value['folder_id'], value['included']))
        if action not in methods:
            return
        name, args = methods[action]
        method = getattr(self.service, name, None)
        if not callable(method):
            return
        request = self._info_action_job = object()
        self._info_snapshot['action_busy'] = True
        self._info_panel._snapshot['action_busy'] = True
        source = 'Проверка состояния…' if action == 'refresh_actions' else 'Сохранение…'
        self._info_panel.set_action_pending(context, True, source, status_source=source)

        def finish(result=None, *, failed=False):
            if not self._info_current(context) or self._info_action_job is not request:
                return
            self._info_action_job = None
            self._info_snapshot['action_busy'] = False
            self._info_panel._snapshot['action_busy'] = False
            completed = confirmed_lifecycle_action(result, context, action) if not failed else None
            bot_result = bot_delete_result(result, context, action)
            if bot_operation and completed == 'delete' and bot_result is None:
                completed = None
            if completed == 'share_contact' and result.get('target_context') is not target_context:
                completed = None
            if completed == 'leave':
                self._finish_conversation_leave(context)
                transfer_result = owner_transfer_result(result, context, action)
                if transfer_result is not None:
                    self.statusBar().showMessage(owner_transfer_notice(transfer_result, self.settings.ui_language), 5000)
                return
            if completed == 'delete':
                self._close_conversation_info()
                if self.chat_view.chat is not None and self.chat_view.chat.id == context.chat_id:
                    self._clear_previous_contact_chat()
                self._schedule_reload()
                self.statusBar().showMessage(bot_delete_notice(bot_result, self.settings.ui_language)
                    if bot_result is not None else 'Чат удалён.', 5000)
                return
            try:
                snapshot = self.service.conversation_snapshot(context)
            except (ValueError, AttributeError):
                source = 'Состояние не подтверждено. Откройте карточку заново.'
                self._info_panel.set_action_pending(context, True, source, status_source=source)
                return
            if not self._info_current(context):
                return
            if action in ('folder_include', 'refresh_actions'):
                self._info_folder_rows = None
            self._info_panel.set_conversation(self._info_with_avatar(snapshot))
            bot_status = bot_result or bot_delete_state(snapshot, context)
            transfer_status = owner_transfer_result(result, context, action) or owner_transfer_state(snapshot, context)
            if transfer_status is not None:
                source, values, sources = owner_transfer_notice_parts(transfer_status)
                self._info_panel.set_action_pending(context, snapshot.get('action_pending') is True,
                    owner_transfer_notice(transfer_status, self.settings.ui_language), status_source=source,
                    status_values=values, status_values_sources=sources)
            elif bot_status is not None:
                source = bot_delete_notice(bot_status, 'ru')
                self._info_panel.set_action_pending(context, snapshot.get('action_pending') is True,
                    bot_delete_notice(bot_status, self.settings.ui_language), status_source=source)
            elif failed:
                source = 'Не удалось подтвердить действие. Проверьте состояние.'
                self._info_panel.set_action_pending(context, snapshot.get('action_pending') is True,
                    source, status_source=source)
            elif accepted_lifecycle_pending_notice(result, context, action):
                source = accepted_lifecycle_pending_notice(result, context, action)
                self._info_panel.set_action_pending(context, True,
                    tr(source, self.settings.ui_language), status_source=source)
            elif completed in ('clear', 'share_contact'):
                self.statusBar().showMessage('Переписка удалена.' if completed == 'clear' else 'Контакт отправлен.', 5000)
                if completed == 'clear' and self.chat_view.chat is not None and self.chat_view.chat.id == context.chat_id:
                    self.chat_view.reload()
            self._load_info_folders(context)
            self._schedule_reload()

        if bot_operation and action in ('delete', 'continue_bot_delete'):
            def bot_delete_work():
                if (self._info_action_job is not request or not self._bot_delete_current(
                        context, source='info', continuation=action == 'continue_bot_delete')):
                    raise ValueError('The selected bot deletion is no longer current')
                return method(context, **options)
            run_async(bot_delete_work, on_done=finish, on_error=lambda _error: finish(failed=True))
        elif action == 'leave':
            def leave_work():
                if not self._info_current(context) or self._info_action_job is not request:
                    raise ValueError('The selected conversation is no longer current')
                snapshot = self.service.conversation_snapshot(context)
                if (snapshot.get('context') is not context
                        or not simple_leave_available(snapshot.get('action_capabilities'))
                        or snapshot.get('action_pending') is True):
                    raise ValueError('The selected conversation cannot be left')
                return method(context)
            run_async(leave_work, on_done=finish, on_error=lambda _error: finish(failed=True))
        else:
            run_async(method, context, *args, **options, on_done=finish,
                      on_error=lambda _error: finish(failed=True))

    def _request_info_tab(self, tab, context, *, more=False):
        if not self._info_current(context):
            return
        state = self._info_pages.setdefault(tab, {'items': [], 'marker': None, 'seen': set(), 'request': None})
        if state['request'] is not None or (more and state['marker'] is None):
            return
        marker = state['marker'] if more else None
        request = object()
        state['request'] = request
        self._info_panel.set_tab_busy(context, tab, True)

        def done(page):
            if not self._info_current(context) or state['request'] is not request:
                return
            state['request'] = None
            if not isinstance(page, dict) or not isinstance(page.get('items'), list):
                self._info_panel.set_tab_error(context, tab)
                return
            rows = dict((item['key'], item) for item in state['items']) if more else {}
            rows.update((item['key'], item) for item in page['items'])
            state['items'] = list(rows.values())
            if more:
                state['seen'].add(marker)
            next_marker = page.get('marker')
            repeated = next_marker is not None and next_marker in state['seen']
            state['marker'] = None if repeated else next_marker
            self._info_panel.set_tab_items(context, tab, state['items'],
                                          complete=state['marker'] is None,
                                          status='Следующая страница недоступна' if repeated else page.get('status', ''),
                                          status_source='Следующая страница недоступна' if repeated else page.get('status_source'))

        def failed(_error):
            if self._info_current(context) and state['request'] is request:
                state['request'] = None
                self._info_panel.set_tab_error(context, tab)

        run_async(self.service.load_conversation_tab, context, tab, marker=marker,
                  on_done=done, on_error=failed)

    def _info_action(self, action, context):
        if not self._info_current(context):
            return
        if action == 'copy_phone':
            phone = self._info_snapshot.get('phone')
            if (self._info_snapshot.get('context') is context and isinstance(phone, str)
                    and phone and self._info_current(context)):
                QApplication.clipboard().setText(phone)
            return
        active = self.chat_view.chat
        if (action in ('chat', 'find') and active is not None
                and int64(active.id) and active.id == context.chat_id):
            # Returning to the bound open conversation is local navigation.
            # Empty/deleted chats can be absent from the sidebar projection.
            chat = active
        else:
            chat = self._display_chat(context.chat_id)
        if not self._info_current(context) or chat is None:
            return
        if action in ('chat', 'find'):
            self._close_conversation_info()
            if self.chat_view.chat is None or self.chat_view.chat.id != chat.id:
                self.chat_view.open_chat(chat)
            if action == 'find':
                self.chat_view.search_button.setChecked(True)
                self.chat_view.history_search.setFocus()
        elif action in ('call', 'video'):
            self._open_call(chat, action == 'video')
        elif action == 'edit':
            if chat.is_group or chat.is_channel:
                self._open_conversation_editor(context)
                return
            if (not chat.is_group and not chat.is_channel and int64(chat.peer_user_id)
                    and chat.peer_user_id != self.service.my_id
                    and self._edit_conversation_contact(chat.peer_user_id)):
                return
            self._rename_chat(chat.id)
            if self._info_current(context):
                self._info_panel.set_conversation(self._info_with_avatar(self.service.conversation_snapshot(context)))

    def _known_info_media_item(self, item, context):
        if not self._info_current(context) or not isinstance(item, dict):
            return None
        return next((row for row in self._info_pages.get('media', {}).get('items', [])
                     if row.get('key') == item.get('key') and row.get('chat_id') == context.chat_id
                     and row.get('available') is True), None)

    def _info_file_action(self, action, item, context):
        if not self._info_current(context) or not isinstance(item, dict):
            return
        known = next((row for row in self._info_pages.get('files', {}).get('items', [])
                      if row.get('key') == item.get('key') and row.get('chat_id') == context.chat_id
                      and row.get('kind') == 'FILE'), None)
        if known is None:
            return
        if action == 'message':
            self._open_info_message(known, context)
            return
        if action != 'save' or known.get('download_available') is not True or known.get('deleted') is True:
            return
        if self._file_download_pending(context):
            return
        try:
            descriptor = self.service.get_conversation_file_descriptor(context, known['key'])
        except (ValueError, AttributeError):
            self.statusBar().showMessage(tr('Файл больше недоступен. Обновите список.',
                                           getattr(self.settings, 'ui_language', 'ru')), 5000)
            return
        self._save_file_descriptor(descriptor, lambda: self._info_current(context))

    def _file_download_pending(self, context):
        controller = self._info_file_controller  # Shared by Info and timeline saves.
        if controller is None or not controller.active:
            return False
        if controller.descriptor.context is context:
            controller.show()
        else:
            self.statusBar().showMessage(tr('Дождитесь завершения предыдущей загрузки.',
                                           getattr(self.settings, 'ui_language', 'ru')), 5000)
        return True

    def _timeline_file_save(self, descriptor):
        context = getattr(descriptor, 'context', None)
        if not self._search_enabled or not self.chat_view.file_context_current(context):
            return
        if self._file_download_pending(context):
            return
        self._save_file_descriptor(descriptor, lambda: self.chat_view.file_context_current(context))

    def _save_file_descriptor(self, descriptor, current):
        from ..core.file_download_transport import capture_target, suggested_filename
        from .conversation_file_controller import ConversationFileController
        context = descriptor.context
        if not current() or self.service.is_current_conversation_file(context, descriptor) is not True:
            return
        language = getattr(self.settings, 'ui_language', 'ru')
        destination, _ = QFileDialog.getSaveFileName(self, tr('Сохранить файл', language),
                                                      suggested_filename(descriptor.file_name),
                                                      tr('Все файлы (*)', language))
        if (not destination or not current()
                or self.service.is_current_conversation_file(context, descriptor) is not True):
            return
        if self._file_download_pending(context):
            return
        try:
            expected_target = capture_target(destination)
        except (OSError, ValueError):
            self.statusBar().showMessage(tr('Не удалось выбрать место для сохранения файла.',
                                           getattr(self.settings, 'ui_language', 'ru')), 5000)
            return
        controller = ConversationFileController(self.service, descriptor, destination, expected_target,
                                               self, theme=self.settings.theme,
                                               language=getattr(self.settings, 'ui_language', 'ru'))
        self._info_file_controller = controller
        controller.message_requested.connect(self._saved_file_message)
        controller.start()

    def _file_scope_closed(self, context):
        controller = self._info_file_controller
        if controller is not None and controller.active and controller.descriptor.context is context:
            controller.cancel()

    def _saved_file_message(self, descriptor):
        if self.chat_view.file_context_current(getattr(descriptor, 'context', None)):
            self._close_conversation_info()
            self.chat_view.show_file_message(descriptor)
        else:
            self._info_file_message(descriptor)

    def _info_file_message(self, descriptor):
        context = getattr(descriptor, 'context', None)
        if not self._info_current(context):
            return
        known = next((row for row in self._info_pages.get('files', {}).get('items', [])
                      if row.get('key') == getattr(descriptor, 'key', None)), None)
        if known is not None and self.service.is_current_conversation_file(context, descriptor):
            self._open_info_message(known, context)

    def _request_info_thumbnail(self, item, context):
        known = self._known_info_media_item(item, context)
        if known is not None:
            self._info_media_loader.request(known, context)

    def _info_thumbnail_ready(self, context, key, url, pixmap):
        known = self._known_info_media_item({'key': key}, context)
        if known is not None and known.get('thumbnail_url') == url and self._info_panel is not None:
            self._info_panel.set_thumbnail(context, key, pixmap)

    def _open_info_item(self, item, context):
        from ..core.media_fetch import image_url
        known = self._known_info_media_item(item, context)
        if known is not None and known.get('kind') == 'PHOTO' and image_url(known.get('open_url')) is not None:
            from .conversation_photo_viewer import ConversationPhotoViewer
            if self._info_photo_viewer is not None and isValid(self._info_photo_viewer):
                self._info_photo_viewer.close()
            viewer = ConversationPhotoViewer(self._info_pages['media']['items'], known['key'], context,
                                              self._info_current, self,
                                              language=getattr(self.settings, 'ui_language', 'ru'))
            self._info_photo_viewer = viewer
            viewer.message_requested.connect(self._open_info_message)
            viewer.show()
            return
        self._open_info_message(item, context)

    def _open_info_message(self, item, context):
        if not self._info_current(context) or not isinstance(item, dict):
            return
        identity = item.get('chat_id')
        chat = self._display_chat(identity)
        if not self._info_current(context):
            return
        if chat is None:
            self.statusBar().showMessage('Этот чат пока не загружен', 5000)
            return
        if item.get('action') == 'message' and identity != context.chat_id:
            return
        self._close_conversation_info()
        self._current_chat_id = chat.id
        if item.get('action') == 'message':
            self.chat_view.open_chat(chat, message_id=item.get('message_id'), time_ms=item.get('time_ms'))
        elif item.get('action') == 'chat':
            self.chat_view.open_chat(chat)

    # ───────────────────────── dialogs ─────────────────────────

    def _open_verification_codes(self) -> None:
        if not isValid(self) or not self._search_enabled:
            return
        if self._code_dialog is not None and isValid(self._code_dialog) and self._code_dialog.isVisible():
            self._code_dialog.raise_()
            self._code_dialog.activateWindow()
            return
        self._close_verification_codes()
        self._code_dialog = VerificationCodesDialog(
            self.service, self, language=getattr(self.settings, 'ui_language', 'ru'))
        self._code_dialog.show()

    def _close_verification_codes(self) -> None:
        dialog, self._code_dialog = self._code_dialog, None
        if dialog is not None and isValid(dialog):
            dialog.close()
            dialog.deleteLater()

    def _open_call_history(self) -> None:
        if self._call_history_dialog is not None and self._call_history_dialog.isVisible():
            self._call_history_dialog.raise_()
            self._call_history_dialog.activateWindow()
            return
        self._call_history_dialog = CallHistoryDialog(self.service, self)
        self._call_history_dialog.show()

    def _open_contacts(self) -> None:
        if not self._search_enabled:
            return
        if self._contacts_workspace is None:
            from .contacts_workspace import ContactsWorkspace
            self._contacts_workspace = ContactsWorkspace(self.service, self)
            self._contacts_workspace.open_chat.connect(self._select_contact)
            self._contacts_workspace.context_requested.connect(self._contact_context_menu)
            self._contacts_workspace.plus_requested.connect(self._open_new_menu)
            self._sidebar_pages.addWidget(self._contacts_workspace)
        self._workspace_pages.setCurrentWidget(self.splitter)
        self._sidebar_pages.setCurrentWidget(self._contacts_workspace)
        self._contacts_workspace.activate_context()
        self.contacts_button.setChecked(True)
        self.settings_button.setChecked(False)
        for button in self.nav_buttons.values():
            button.setChecked(False)

    def _build_new_menu(self, *, contacts=False):
        """Contact creation and lookup use separate confirmed protocol routes."""
        self._cancel_new_menu()
        epoch = self._new_menu_epoch
        context = self._bind_search_context()
        language = self.settings.ui_language
        menu = QMenu(self)
        menu.setToolTipsVisible(True)

        def dispatch(callback):
            def triggered(_checked=False):
                if (not isValid(self) or not isValid(menu) or not self._search_enabled
                        or epoch != self._new_menu_epoch or context is None):
                    return
                try:
                    if self.service.search_context() is not context:
                        return
                except (AttributeError, ValueError):
                    return
                callback()
            return triggered

        if contacts:
            add = menu.addAction(line_icon('plus'), tr('Добавить контакт', language))
            add.setEnabled(self._search_enabled)
            add.triggered.connect(dispatch(self._open_contact_create))
        for kind, icon, label in [('channel', 'channels', 'Создать канал'),
                                  ('group', 'contacts', 'Создать группу')]:
            action = menu.addAction(line_icon(icon), tr(label, language))
            action.setEnabled(self._search_enabled)
            action.triggered.connect(dispatch(lambda kind=kind: self._open_conversation_create(kind)))
        call = menu.addAction(line_icon('phone'), tr('Создать групповой звонок', language))
        call.setEnabled(False)
        call.setToolTip(tr('Пока недоступно', language))
        find = menu.addAction(line_icon('search'), tr('Найти по номеру', language))
        find.setEnabled(self._search_enabled)
        find.triggered.connect(dispatch(self._open_phone_lookup))
        invite = menu.addAction(line_icon('link'), tr('Пригласить по ссылке', language))
        invite.setEnabled(self._search_enabled)
        invite.triggered.connect(dispatch(lambda: self._copy_invitation(context)))
        if contacts:
            menu.addSeparator()
            local = menu.addAction(tr('Управление локальными контактами…', language))
            local.setEnabled(self._search_enabled)
            local.triggered.connect(dispatch(self._open_contact_manager))
        return menu

    def _cancel_new_menu(self):
        self._new_menu_epoch += 1
        if self._new_menu is not None and isValid(self._new_menu):
            self._new_menu.close()
            self._new_menu.deleteLater()
        self._new_menu = None

    def _open_new_menu(self, _checked=False):
        if not self._search_enabled:
            return
        contacts = (self._contacts_workspace is not None
                    and self._sidebar_pages.currentWidget() == self._contacts_workspace)
        self._new_menu = self._build_new_menu(contacts=contacts)
        anchor = self._contacts_workspace.plus_button if contacts else self.new_chat_button
        # Native Contacts+ aligns the popup's right edge with the plus button.
        offset = anchor.width() - self._new_menu.sizeHint().width()
        self._new_menu.popup(anchor.mapToGlobal(QPoint(offset, anchor.height() + 6)))

    def _open_phone_lookup(self):
        if not self._search_enabled:
            return
        if self._phone_lookup_dialog is not None and isValid(self._phone_lookup_dialog):
            self._phone_lookup_dialog.raise_()
            self._phone_lookup_dialog.activateWindow()
            return
        from .phone_lookup_dialog import PhoneLookupDialog
        dialog = PhoneLookupDialog(self.service, self)
        self._phone_lookup_dialog = dialog
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.navigate_contact = self._open_contact_by_id
        dialog.contact_found.connect(self._refresh_contact_sidebar)
        dialog.finished.connect(lambda _result: self._release_phone_lookup(dialog))
        dialog.open()

    def _open_contact_create(self):
        if not self._search_enabled:
            return
        if self._contact_create_dialog is not None and isValid(self._contact_create_dialog):
            self._contact_create_dialog.raise_()
            self._contact_create_dialog.activateWindow()
            return
        from .contact_create_dialog import ContactCreateDialog
        dialog = ContactCreateDialog(self.service, self)
        self._contact_create_dialog = dialog
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.contact_saved.connect(self._contact_saved)
        dialog.save_notice.connect(self._contact_save_notice)
        dialog.finished.connect(lambda _result: self._release_contact_create(dialog))
        dialog.open()

    def _release_contact_create(self, dialog):
        if self._contact_create_dialog is dialog:
            self._contact_create_dialog = None

    def _open_conversation_create(self, kind):
        context = self._bind_search_context()
        if context is None or kind not in ('group', 'channel'):
            return
        if self._conversation_create_dialog is not None and isValid(self._conversation_create_dialog):
            self._conversation_create_dialog.raise_()
            self._conversation_create_dialog.activateWindow()
            return
        from .conversation_create_dialog import ConversationCreateDialog
        dialog = ConversationCreateDialog(self.service, kind=kind, parent=self,
                                          language=getattr(self.settings, 'ui_language', 'ru'))
        self._conversation_create_dialog = dialog
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.conversation_created.connect(
            lambda chat: self._conversation_created(chat, context=context))
        dialog.finished.connect(lambda _result: self._release_conversation_create(dialog))
        dialog.open()

    def _release_conversation_create(self, dialog):
        if self._conversation_create_dialog is dialog:
            self._conversation_create_dialog = None

    def _conversation_created(self, chat, *, context):
        if (not isValid(self) or not self._search_enabled or context is None
                or not isinstance(chat, Chat) or not int64(chat.id)
                or self._bind_search_context() is not context):
            return
        # Only a server-confirmed, cached conversation can replace the view.
        actual = self._display_chat(chat.id)
        if actual is None or self._bind_search_context() is not context:
            return
        self.search.clear()
        self._select_section('all')
        self._close_conversation_info()
        self._current_chat_id = actual.id
        self.chat_view.open_chat(actual)
        for index in range(self.chat_list.count()):
            item = self.chat_list.item(index)
            if item.data(Qt.ItemDataRole.UserRole) == actual.id:
                self.chat_list.setCurrentItem(item)
                self.chat_list.scrollToItem(item)
                break

    def _contact_saved(self, contact):
        if not self._search_enabled:
            return
        if self._contacts_workspace is not None:
            self._contacts_workspace.reveal_contact(contact)
        self.statusBar().showMessage('Контакт сохранён в MAX', 8000)

    def _contact_save_notice(self, notice):
        if self._search_enabled:
            self.statusBar().showMessage(notice, 12000)

    def _release_phone_lookup(self, dialog):
        if self._phone_lookup_dialog is dialog:
            self._phone_lookup_dialog = None

    def _refresh_contact_sidebar(self):
        if not self._search_enabled:
            return
        if self._contacts_workspace is not None:
            self._contacts_workspace.refresh()
        for dialog in self.findChildren(ContactsDialog):
            if isValid(dialog) and dialog.isVisible():
                dialog._refresh()

    def _open_contact_manager(self):
        if not self._search_enabled:
            return
        dlg = ContactsDialog(self.service, self)
        dlg.navigate_contact = self._open_contact_by_id
        dlg.exec()
        if self._contacts_workspace is not None:
            self._contacts_workspace.refresh()
        dlg.deleteLater()

    def _copy_invitation(self, context):
        if not isValid(self) or not self._search_enabled:
            return
        from .invite_link import copy_invitation, show_menu_notice
        try:
            copy_invitation(self.service, context=context)
        except (AttributeError, ValueError, RuntimeError):
            show_menu_notice(self, tr('Не удалось скопировать приглашение. Проверьте соединение с MAX и повторите действие.', self.settings.ui_language))
            return
        show_menu_notice(self, tr('Вы скопировали ссылку', self.settings.ui_language))

    def _focus_chat_search(self):
        if not self._search_enabled or QApplication.activeModalWidget() is not None:
            return
        self._show_chat_workspace()
        self.search.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self.search.selectAll()

    def _focus_history_search(self):
        if (not self._search_enabled or QApplication.activeModalWidget() is not None
                or self._workspace_pages.currentWidget() != self.splitter
                or self.chat_view.chat is None):
            return
        self._close_conversation_info()
        self.chat_view.search_button.setChecked(True)
        self.chat_view._toggle_search(True)
        self.chat_view.history_search.selectAll()

    def _close_current_chat(self):
        """Close only the visible conversation, retaining its in-memory draft."""
        if (QApplication.activeModalWidget() is not None
                or QApplication.activePopupWidget() is not None
                or self._workspace_pages.currentWidget() != self.splitter
                or self._sidebar_pages.currentWidget() != self.sidebar
                or self.chat_view.chat is None):
            return
        view = self.chat_view
        view._drafts[view.chat.id] = view.input.toPlainText()
        # Reply targets already live in the per-chat map. The placeholder
        # invalidates history/file scopes without clearing either draft map.
        self._close_conversation_info()
        view.show_placeholder()
        view.input.clear()
        self._current_chat_id = None
        self.chat_list.setCurrentItem(None)
        self.chat_list.clearSelection()
        self.search.setFocus(Qt.FocusReason.ShortcutFocusReason)

    def _open_call(self, chat, is_video=False):
        if not web_features_available():
            self.statusBar().showMessage('Звонки не включены в эту переносимую сборку MAXIM.', 7000)
            return
        if self._call_window is not None:
            self._call_window.show()
            self._call_window.raise_()
            return
        try:
            from ..core.call_backend import MaxCallBackend
            from ..core.call_runtime import CallRuntime
            from .call_window import CallWindow
            backend = MaxCallBackend(self.service)
            if not self.service.calls_available(chat):
                return
            config = backend.config()
            runtime = CallRuntime(backend, is_authorized=backend.is_authorized)
            window = CallWindow(runtime, config, contact_id=chat.peer_user_id,
                                title=chat.display, parent=self)
            runtime.setParent(window)
            window.setWindowFlag(Qt.WindowType.Window, True)
            self._call_window = window
            window.closure_unknown.connect(self._call_teardown_unknown)
            window.destroyed.connect(lambda _obj=None, owned=window: self._release_call_window(owned))
            window.show()
            (window.video_call if is_video else window.audio_call).setFocus()
        except Exception:
            QMessageBox.information(self, 'Звонок MAX', 'Не удалось подготовить окно звонка.')

    def _on_incoming_call(self, notice):
        """Present an invitation only;137 does not establish an answered call."""
        if not web_features_available():
            self.statusBar().showMessage('Звонки не включены в эту переносимую сборку MAXIM.', 7000)
            return
        checker = getattr(self.service, 'is_current_incoming_call', None)
        if not self._incoming_enabled or not self.isVisible() or not callable(checker):
            return
        runtime = None
        window = None
        try:
            from ..core.incoming_calls import IncomingCallNotice
            if (not isinstance(notice, IncomingCallNotice) or not checker(notice)
                    or notice.call_type not in {'AUDIO', 'VIDEO'}):
                return
            if notice.session_context is not self._incoming_context:
                self._incoming_context = notice.session_context
                self._incoming_seen.clear()
                self._incoming_order.clear()
            if notice.conversation_id in self._incoming_seen:
                return
            self._incoming_seen.add(notice.conversation_id)
            self._incoming_order.append(notice.conversation_id)
            if len(self._incoming_order) > 128:
                self._incoming_seen.discard(self._incoming_order.popleft())
            if self._call_window is not None:
                self.statusBar().showMessage('Входящий звонок: окно другого звонка уже открыто.', 7000)
                return
            from ..core.call_backend import MaxCallBackend
            from ..core.call_runtime import CallRuntime
            from .call_window import CallWindow
            backend = MaxCallBackend(self.service)
            config = backend.config()  # local confirmed configuration, no158/auth/RPC
            runtime = CallRuntime(backend, is_authorized=lambda: backend.is_authorized() and checker(notice))
            title = 'Входящий видеозвонок MAX' if notice.call_type == 'VIDEO' else 'Входящий аудиозвонок MAX'
            window = CallWindow(runtime, config, incoming=notice.sdk_payload(), title=title, parent=self)
            runtime.setParent(window)
            if not self._incoming_enabled or not self.isVisible() or not checker(notice):
                window.close()
                return
            window.setWindowFlag(Qt.WindowType.Window, True)
            self._call_window = window
            window.closure_unknown.connect(self._call_teardown_unknown)
            window.destroyed.connect(lambda _obj=None, owned=window: self._release_call_window(owned))
            window.show()
        except Exception:
            # A notice may contain vcp/TURN credentials: never format/log it.
            if window is not None:
                window.close()
            elif runtime is not None:
                runtime.close()
            self.statusBar().showMessage('Не удалось показать входящий звонок MAX.', 7000)

    def _release_call_window(self, owned):
        if self._call_window is owned:
            self._call_window = None

    def _stop_incoming_presentation(self):
        self._incoming_enabled = False
        self._incoming_context = None
        self._incoming_seen.clear()
        self._incoming_order.clear()

    def _call_teardown_unknown(self, _message):
        self.statusBar().showMessage('Окно звонка закрыто. Подтверждение завершения от MAX не получено.', 15000)

    def _open_settings(self) -> None:
        self._close_contact_profile()
        if self._contacts_workspace is not None:
            self._contacts_workspace.deactivate()
        self.contacts_button.setChecked(False)
        if self._settings_workspace is None:
            self._settings_workspace = SettingsWorkspace(self.service, self.settings, self)
            self._settings_workspace.theme_changed.connect(self._apply_local_theme)
            self._settings_workspace.language_changed.connect(self._apply_local_language)
            self._settings_workspace.logout_requested.connect(self._handle_logout)
            self._settings_workspace.folders_changed.connect(self._on_folders)
            self._settings_workspace.local_delivery_changed.connect(self._on_local_delivery_changed)
            self._workspace_pages.addWidget(self._settings_workspace)
        self._workspace_pages.setCurrentWidget(self._settings_workspace)
        self.settings_button.setChecked(True)
        for button in self.nav_buttons.values():
            button.setChecked(False)

    def _show_chat_workspace(self) -> None:
        self._close_contact_profile()
        if self._contacts_workspace is not None:
            self._contacts_workspace.deactivate()
        self._sidebar_pages.setCurrentWidget(self.sidebar)
        self.contacts_button.setChecked(False)
        self._workspace_pages.setCurrentWidget(self.splitter)
        self.settings_button.setChecked(False)
        for key, button in self.nav_buttons.items():
            button.setChecked(key == self._section)

    def _apply_local_theme(self) -> None:
        for button in self._rail_widgets:
            button.set_dark(resolve_theme(self.settings.theme) == 'dark')
        if self._settings_workspace is not None:
            self._settings_workspace.apply_theme()
        self.chat_view.apply_appearance()
        self.search_panel.apply_theme(self.settings.theme)
        if self._info_panel is not None:
            self._info_panel.apply_theme(self.settings.theme)
        if self._contact_panel is not None:
            self._contact_panel.apply_theme(self.settings.theme)
        if self._contact_edit_panel is not None:
            self._contact_edit_panel.apply_theme(self.settings.theme)
        self.theme_changed.emit()
        self._reload_chats()

    def _handle_logout(self) -> None:
        self._close_verification_codes()
        self._stop_incoming_presentation()
        self._stop_search()
        self._notifications.clear()
        if self._call_window is not None:
            self._call_window.close()
        run_async(self.service.logout, on_done=lambda _r: self.logged_out.emit(),
                  on_error=lambda _e: self.logged_out.emit())

    # ───────────────────────── window ─────────────────────────

    def closeEvent(self, event) -> None:  # noqa: N802
        self._close_verification_codes()
        self._stop_incoming_presentation()
        self._stop_search()
        self._notifications.clear()
        if self._call_window is not None:
            self._call_window.close()
        self.settings.window_w = self.width()
        self.settings.window_h = self.height()
        self.settings.save()
        super().closeEvent(event)


def _is_channel(chat: Chat) -> bool:
    """Only explicit server channel metadata qualifies; ordinary groups do not."""
    return getattr(chat, 'is_channel', False) is True or str(getattr(chat, 'chat_type', '') or '').upper() == 'CHANNEL'
