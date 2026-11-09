"""Embedded, session-bound contact sidebar; opening it never performs a lookup."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal, QSize
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QPushButton, QVBoxLayout, QWidget,
)
from shiboken6 import isValid

from ..core.models import Contact
from ..core.i18n import normalize_language, tr
from ..core.service import MaxService, _contact_display_name
from ..protocol.folder_contracts import int64
from .widgets.avatar import Avatar
from .widgets.chat_list_item import _ElidedLabel
from .widgets.line_icon import line_icon


class _ContactList(QListWidget):
    keyboard_open = Signal(object)

    def keyPressEvent(self, event):  # noqa: N802
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if (not event.isAutoRepeat() and event.modifiers()
                    in (Qt.KeyboardModifier.NoModifier, Qt.KeyboardModifier.KeypadModifier)):
                self.keyboard_open.emit(self.currentItem())
            event.accept()
            return
        super().keyPressEvent(event)


class ContactsWorkspace(QWidget):
    open_chat = Signal(object, str)  # A contact int64, never an invented chat ID.
    context_requested = Signal(object, object)  # Contact int64 and global menu point.
    plus_requested = Signal()
    lookup_requested = Signal()
    import_requested = Signal()

    def __init__(self, service: MaxService, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.service = service
        self._context = None
        self._active = False
        self._rows_snapshot = None
        self._language = 'ru'
        self._language_epoch = 0
        self._empty_source = 'Откройте контакты после подключения к MAX.'
        self.setObjectName('Sidebar')
        self.setMinimumWidth(290)
        self.setMaximumWidth(520)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        header = QFrame()
        header.setObjectName('SidebarHeader')
        header.setFixedHeight(64)
        heading = QHBoxLayout(header)
        heading.setContentsMargins(16, 12, 14, 8)
        self.title = QLabel()
        self.title.setObjectName('SidebarTitle')
        heading.addWidget(self.title, 1)
        self.plus_button = QPushButton()
        self.plus_button.setObjectName('NewChatButton')
        self.plus_button.setIcon(line_icon('plus', '#FFFFFF', 21))
        self.plus_button.setIconSize(QSize(21, 21))
        self.plus_button.setFixedSize(34, 34)
        self.plus_button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.plus_button.clicked.connect(self._request_plus)
        heading.addWidget(self.plus_button)
        root.addWidget(header)
        search_wrap = QWidget()
        search_layout = QVBoxLayout(search_wrap)
        search_layout.setContentsMargins(15, 0, 15, 8)
        self.search = QLineEdit()
        self.search.setObjectName('ChatSearch')
        self.search.setClearButtonEnabled(True)
        self.search.setFixedHeight(36)
        self.search.addAction(line_icon('search', '#AFB4BC', 18), QLineEdit.ActionPosition.LeadingPosition)
        self.search.textChanged.connect(self.refresh)
        search_layout.addWidget(self.search)
        root.addWidget(search_wrap)
        self.list = _ContactList()
        self.list.setObjectName('ChatList')
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.setSpacing(0)
        self.list.itemClicked.connect(self._open_item)
        self.list.keyboard_open.connect(self._open_item)
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._context_menu)
        root.addWidget(self.list, 1)
        self.empty = QLabel()
        self.empty.setObjectName('EmptyHint')
        self.empty.setWordWrap(True)
        self.empty.setTextFormat(Qt.TextFormat.PlainText)
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setContentsMargins(18, 20, 18, 20)
        root.addWidget(self.empty, 1)
        self.list.hide()
        self.plus_button.setEnabled(False)
        self.retranslate()

    @property
    def language_epoch(self):
        """Available to the owner of plus/context popups; this page emits signals."""
        return self._language_epoch

    def retranslate(self, language=None) -> None:
        """Update owned labels only, without reading contacts or changing drafts."""
        if not isValid(self):
            return
        if language is None:
            language = getattr(getattr(self.service, 'settings', None), 'ui_language', 'ru')
        language = normalize_language(language if isinstance(language, str) else 'ru')
        if language != self._language:
            self._language = language
            self._language_epoch += 1
        self.title.setText(tr('Контакты', language))
        self.plus_button.setAccessibleName(tr('Меню контактов', language))
        self.plus_button.setToolTip(tr('Меню контактов', language))
        self.search.setPlaceholderText(tr('Найти', language))
        self.search.setAccessibleName(tr('Поиск контактов по имени или номеру', language))
        self.list.setAccessibleName(tr('Контакты', language))
        self.empty.setText(tr(self._empty_source, language))
        for index in range(self.list.count()):
            row = self.list.itemWidget(self.list.item(index))
            if isinstance(row, ContactWorkspaceRow):
                row.retranslate(language)

    set_language = retranslate

    def _set_empty(self, source: str) -> None:
        self._empty_source = source
        self.empty.setText(tr(source, self._language))

    def activate_context(self) -> bool:
        """Bind an explicit navigation to the currently authenticated session."""
        if not isValid(self):
            return False
        try:
            context = self.service.search_context()
        except (AttributeError, ValueError):
            context = None
        if context is not self._context:
            self.search.blockSignals(True)
            self.search.clear()
            self.search.blockSignals(False)
            self.list.clear()
            self._rows_snapshot = None
        self._context = context
        self._active = context is not None
        return self.refresh()

    def deactivate(self) -> None:
        """Drop account rows and outstanding UI context before leaving the page."""
        self._active = False
        self._context = None
        if not isValid(self):
            return
        self.search.blockSignals(True)
        self.search.clear()
        self.search.blockSignals(False)
        self.list.clear()
        self._rows_snapshot = None
        self.list.hide()
        self._set_empty('Откройте контакты после подключения к MAX.')
        self.empty.show()
        self.plus_button.setEnabled(False)

    def context_current(self) -> bool:
        try:
            return (isValid(self) and self._active and self._context is not None
                    and self.service.search_context() is self._context)
        except (AttributeError, ValueError):
            return False

    def refresh(self, *_args) -> bool:
        if not self.context_current():
            self._expired()
            return False
        query = self.search.text().strip()
        context = self._context
        try:
            contacts = self.service.list_contacts(query)
        except ValueError:
            self._expired()
            return False
        except Exception:  # A local cache failure is not an empty contact book.
            if not self.context_current():
                self._expired()
            else:
                self.list.clear()
                self._rows_snapshot = None
                self.list.hide()
                self._set_empty('Не удалось загрузить контакты. Откройте раздел заново.')
                self.empty.show()
                self.plus_button.setEnabled(True)
            return False
        if not self.context_current() or self._context is not context:
            self._expired()
            return False
        contacts = [contact for contact in contacts if isinstance(contact, Contact)
                    and int64(contact.id)]
        snapshot = tuple((contact.id, _contact_display_name(contact),
                          contact.phone if isinstance(contact.phone, str) else None,
                          contact.avatar_url if isinstance(contact.avatar_url, str) else None,
                          contact.is_verified is True) for contact in contacts)
        if not self.context_current() or self._context is not context:
            self._expired()
            return False
        if snapshot == self._rows_snapshot:
            self._set_result_state(query)
            return True
        previous = self.list.currentItem()
        prior_contact = previous.data(Qt.ItemDataRole.UserRole) if previous is not None else None
        selected = prior_contact.id if isinstance(prior_contact, Contact) else None
        scroll = self.list.verticalScrollBar().value()
        self.list.clear()
        for contact in contacts:
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, contact)
            item.setData(Qt.ItemDataRole.AccessibleTextRole, _contact_display_name(contact))
            row = ContactWorkspaceRow(contact, language=self._language)
            item.setSizeHint(QSize(0, 64))
            self.list.addItem(item)
            self.list.setItemWidget(item, row)
            if contact.id == selected:
                self.list.setCurrentItem(item)
        if not self.context_current() or self._context is not context:
            self._expired()
            return False
        self._rows_snapshot = snapshot
        self.list.verticalScrollBar().setValue(scroll)
        self._set_result_state(query)
        return True

    def _set_result_state(self, query: str) -> None:
        found = self.list.count() > 0
        self.list.setVisible(found)
        self._set_empty('Ничего не найдено' if query else 'Пока нет контактов')
        self.empty.setVisible(not found)
        self.plus_button.setEnabled(True)

    def reveal_contact(self, contact: Contact) -> bool:
        """Reveal a confirmed explicit save even if the previous filter hid it."""
        if not self.context_current() or not isinstance(contact, Contact) or not int64(contact.id):
            return False
        self.search.blockSignals(True)
        self.search.clear()
        self.search.blockSignals(False)
        if not self.refresh():
            return False
        for index in range(self.list.count()):
            item = self.list.item(index)
            row = item.data(Qt.ItemDataRole.UserRole)
            if isinstance(row, Contact) and row.id == contact.id:
                self.list.setCurrentItem(item)
                self.list.scrollToItem(item)
                return True
        return False

    def _expired(self) -> None:
        if not isValid(self):
            return
        self._active = False
        self._context = None
        self.search.blockSignals(True)
        self.search.clear()
        self.search.blockSignals(False)
        self.list.clear()
        self._rows_snapshot = None
        self.list.hide()
        self._set_empty('Сессия изменилась. Откройте контакты заново.')
        self.empty.show()
        self.plus_button.setEnabled(False)

    def _request_plus(self) -> None:
        if self.context_current():
            self.plus_requested.emit()
        else:
            self._expired()

    def _open_item(self, item: QListWidgetItem) -> None:
        if not self.context_current() or item is None or not isValid(item):
            return
        if item.listWidget() is not self.list:
            return
        contact = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(contact, Contact) or not int64(contact.id):
            return
        self.open_chat.emit(contact.id, _contact_display_name(contact))

    def _context_menu(self, point) -> None:
        if not self.context_current():
            return
        item = self.list.itemAt(point)
        if item is None:
            item = self.list.currentItem() if point.x() < 0 or point.y() < 0 else None
        if item is None or not isValid(item) or item.listWidget() is not self.list:
            return
        contact = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(contact, Contact) or not int64(contact.id):
            return
        self.list.setCurrentItem(item)
        anchor = self.list.visualItemRect(item).center() if point.x() < 0 or point.y() < 0 else point
        self.context_requested.emit(contact.id, self.list.viewport().mapToGlobal(anchor))

    def contact_avatar(self, contact_id):
        """Reuse the exact selected account's already-loaded avatar; no new fetch."""
        if not self.context_current() or not int64(contact_id):
            return None
        for index in range(self.list.count()):
            item = self.list.item(index)
            contact = item.data(Qt.ItemDataRole.UserRole)
            if isinstance(contact, Contact) and contact.id == contact_id:
                row = self.list.itemWidget(item)
                if isinstance(row, ContactWorkspaceRow):
                    return row.avatar.cached_photo()
        return None


class ContactWorkspaceRow(QWidget):
    """Only supplied identity fields; no guessed presence or phone number."""
    def __init__(self, contact: Contact, parent: QWidget | None = None, *, language='ru') -> None:
        super().__init__(parent)
        self.setObjectName('ContactWorkspaceRow')
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setStyleSheet('QWidget#ContactWorkspaceRow, QLabel { background: transparent; }')
        self.setMinimumWidth(0)
        self.setFixedHeight(64)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 8, 14, 8)
        layout.setSpacing(12)
        display = _contact_display_name(contact)
        self.avatar = Avatar(46)
        self.avatar.setObjectName('ContactAvatar')
        self.avatar.set_identity(display, contact.id)
        self.avatar.set_photo(contact.avatar_url if isinstance(contact.avatar_url, str) else None)
        layout.addWidget(self.avatar)
        labels = QVBoxLayout()
        labels.setContentsMargins(0, 0, 0, 0)
        labels.setSpacing(3)
        name_row = QHBoxLayout()
        name_row.setSpacing(5)
        self.name = _ElidedLabel()
        self.name.setObjectName('ContactName')
        self.name.setText(display)
        self.name.setToolTip(display)
        self.name.setAccessibleName(display)
        self.name.setStyleSheet('font-weight:600;background:transparent;')
        name_row.addWidget(self.name, 1)
        self.verified_badge = None
        if contact.is_verified is True:
            badge = QLabel()
            self.verified_badge = badge
            badge.setObjectName('ContactVerified')
            badge.setPixmap(line_icon('verified', '#008AFF', 14).pixmap(14, 14))
            name_row.addWidget(badge)
        labels.addLayout(name_row)
        if isinstance(contact.phone, str) and contact.phone and contact.phone != display:
            phone = _ElidedLabel()
            phone.setObjectName('ContactPhone')
            phone.setText(contact.phone)
            phone.setToolTip(contact.phone)
            phone.setAccessibleName(contact.phone)
            phone.setStyleSheet('color:palette(mid);font-size:12px;background:transparent;')
            labels.addWidget(phone)
        layout.addLayout(labels, 1)
        self.retranslate(language)

    def retranslate(self, language) -> None:
        if self.verified_badge is not None and isValid(self.verified_badge):
            self.verified_badge.setAccessibleName(tr('Подтверждённый аккаунт', language))
