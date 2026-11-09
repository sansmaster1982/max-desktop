"""Explicit phone lookup using the existing CONTACT_INFO_BY_PHONE path.

This is not server contact creation. No request runs on open, typing, or cancel.
"""
from __future__ import annotations

import re

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QDialog, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget
from shiboken6 import isValid

from ..core.models import Contact
from ..core.i18n import normalize_language, tr
from ..core.service import MaxService, _contact_display_name, normalize_phone
from ..protocol.folder_contracts import int64
from .async_runner import run_async


def lookup_phone_input(text: str) -> str | None:
    """Local syntax guard, not an assertion that MAX supports or owns a number."""
    if not isinstance(text, str) or len(text) > 64:
        return None
    value = text.strip()
    if not re.fullmatch(r'\+?[0-9 ()\-.]+', value, flags=re.ASCII):
        return None
    normalized = normalize_phone(value)
    digits = normalized[1:]
    return normalized if 7 <= len(digits) <= 15 and digits[0] != '0' else None


class PhoneLookupDialog(QDialog):
    open_chat = Signal(object, str)
    contact_found = Signal()

    def __init__(self, service: MaxService, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.service = service
        self._language = normalize_language(getattr(getattr(parent, 'settings', None), 'ui_language', 'ru'))
        self._result_source = None
        self._closed = False
        self._request = None
        self._request_phone = None
        self._found_contact = None
        self.navigate_contact = None
        try:
            self._context = self.service.search_context()
        except (AttributeError, ValueError):
            self._context = None
        self.setWindowTitle('Найти по номеру')
        self.setMinimumWidth(440)
        self.setMaximumWidth(520)
        self.setModal(True)
        self.finished.connect(self._finished)
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 22, 24, 24)
        root.setSpacing(18)
        self.heading = QLabel('Найти по номеру')
        self.heading.setStyleSheet('font-size:22px;font-weight:700;')
        root.addWidget(self.heading)
        self.phone = QLineEdit('+7')
        self.phone.setObjectName('LookupPhone')
        self.phone.setAccessibleName('Номер телефона в международном формате')
        self.phone.setPlaceholderText('+7 123 456 78 90')
        self.phone.setToolTip('Введите полный номер с кодом страны, например +7 … или +44 …')
        self.phone.setMaxLength(64)
        self.phone.setMinimumHeight(48)
        self.phone.textChanged.connect(self._input_changed)
        self.phone.returnPressed.connect(self._submit)
        root.addWidget(self.phone)
        self.result_label = QLabel()
        self.result_label.setTextFormat(Qt.TextFormat.PlainText)
        self.result_label.setWordWrap(True)
        self.result_label.hide()
        root.addWidget(self.result_label)
        self.find_button = QPushButton('Найти в MAX')
        self.find_button.setObjectName('Primary')
        self.find_button.setMinimumHeight(48)
        self.find_button.setAutoDefault(False)
        self.find_button.clicked.connect(self._submit)
        root.addWidget(self.find_button)
        self.open_result_btn = QPushButton('Открыть чат')
        self.open_result_btn.setObjectName('Primary')
        self.open_result_btn.setMinimumHeight(48)
        self.open_result_btn.setAutoDefault(False)
        self.open_result_btn.clicked.connect(self._open_found)
        self.open_result_btn.hide()
        root.addWidget(self.open_result_btn)
        self.resize(440, 250)
        self.set_language(self._language)
        self._input_changed()
        if not self._context_current():
            self._expire()

    def _context_current(self) -> bool:
        try:
            return (isValid(self) and not self._closed and self._context is not None
                    and self.service.search_context() is self._context)
        except (AttributeError, ValueError):
            return False

    def set_language(self, language):
        self._language = normalize_language(language)
        self.setWindowTitle(tr('Найти по номеру', self._language))
        self.heading.setText(self.windowTitle())
        self.phone.setAccessibleName(tr('Номер телефона в международном формате', self._language))
        self.phone.setToolTip(tr('Введите полный номер с кодом страны, например +7 … или +44 …', self._language))
        self.find_button.setText(tr('Найти в MAX', self._language))
        own = isinstance(self._found_contact, Contact) and self._found_contact.id == self.service.my_id
        self.open_result_btn.setText(tr('Открыть Избранное' if own else 'Открыть чат', self._language))
        self._render_result()

    retranslate = set_language

    def _message(self, text, *, app_owned=True):
        self._result_source = (text, app_owned)
        self._render_result()
        self.result_label.show()

    def _render_result(self):
        if self._result_source == 'found' and isinstance(self._found_contact, Contact):
            contact = self._found_contact
            own = contact.id == self.service.my_id
            details = [tr('Это ваш профиль' if own else 'Контакт найден', self._language), _contact_display_name(contact)]
            if isinstance(contact.phone, str) and contact.phone:
                details.append(contact.phone)
            if own:
                details.append(tr('Сообщения себе сохраняются в «Избранном».', self._language))
            self.result_label.setText('\n'.join(details))
        elif isinstance(self._result_source, tuple):
            source, owned = self._result_source
            self.result_label.setText(tr(source, self._language) if owned else source)

    def _input_changed(self, *_args) -> None:
        self._found_contact = None
        self._result_source = None
        self.open_result_btn.hide()
        self.result_label.hide()
        self.find_button.setEnabled(self._request is None and self._context_current()
                                    and lookup_phone_input(self.phone.text()) is not None)

    def _submit(self) -> None:
        if not self._context_current():
            self._expire()
            return
        if self._request is not None:
            return
        phone = lookup_phone_input(self.phone.text())
        if phone is None:
            return
        context = self._context
        request = self._request = object()
        self._request_phone = phone
        self._found_contact = None
        self.open_result_btn.hide()
        self._message('Поиск…')
        self.phone.setEnabled(False)
        self.find_button.setEnabled(False)

        def done(contact):
            if not self._request_current(request, context, phone):
                return
            self._request = None
            self._request_phone = None
            self.phone.setEnabled(True)
            self.find_button.setEnabled(True)
            if not isinstance(contact, Contact) or not int64(contact.id):
                self._error('MAX не подтвердил сведения о контакте.', app_owned=True)
                return
            self._found_contact = contact
            self._result_source = 'found'
            self.set_language(self._language)
            self.open_result_btn.show()
            self.contact_found.emit()

        def failed(message):
            if self._request_current(request, context, phone):
                self._request = None
                self._request_phone = None
                self.phone.setEnabled(True)
                self.find_button.setEnabled(True)
                self._error(message)

        run_async(self.service.add_contact_by_phone, phone, context=context,
                  on_done=done, on_error=failed)

    def _request_current(self, request, context, phone) -> bool:
        if not isValid(self) or self._closed or self._request is not request:
            return False
        if not self._context_current() or self._context is not context:
            self._expire()
            return False
        if self._request_phone != phone or lookup_phone_input(self.phone.text()) != phone:
            self._request = None
            self._request_phone = None
            self.phone.setEnabled(True)
            self._input_changed()
            return False
        return True

    def _error(self, message: str, *, app_owned=False) -> None:
        self._found_contact = None
        self._message(message if isinstance(message, str) else 'Поиск не завершён.',
                      app_owned=app_owned or not isinstance(message, str))
        self.open_result_btn.hide()

    def _expire(self) -> None:
        self._request = self._request_phone = self._found_contact = None
        self._context = None
        if not isValid(self) or self._closed:
            return
        self.phone.setEnabled(False)
        self.find_button.setEnabled(False)
        self._error('Сессия изменилась. Откройте поиск номера заново.', app_owned=True)

    def _open_found(self) -> None:
        if not self._context_current():
            self._expire()
            return
        contact = self._found_contact
        if not isinstance(contact, Contact) or not int64(contact.id):
            return
        if callable(self.navigate_contact):
            if not self.navigate_contact(contact.id, _contact_display_name(contact)):
                self._message('Контакт найден. Диалог с ним пока не загружен.\n'
                              'Контакт доступен в списке; новое сообщение не отправлено.')
                return
        else:
            self.open_chat.emit(contact.id, _contact_display_name(contact))
        self.accept()

    def _finished(self, _result) -> None:
        self._closed = True
        self._context = self._request = self._request_phone = self._found_contact = None
