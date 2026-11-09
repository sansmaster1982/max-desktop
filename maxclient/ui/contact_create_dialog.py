"""Explicit contact save, separated from phone lookup and chat navigation."""
from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout
from shiboken6 import isValid

from ..core.contact_create import ContactCreateResult
from ..core.models import Contact
from ..core.i18n import normalize_language, tr
from ..protocol.client import MaxWriteOutcomeUnknown
from ..protocol.contact_create_contracts import contact_create_payload
from ..protocol.folder_contracts import int64
from .async_runner import run_async


@dataclass(frozen=True)
class _Failure:
    message: str
    uncertain: bool = False
    app_owned: bool = True


def _save(service, payload, context, verify=False):
    """Preserve uncertain-write classification across async_runner's string errors."""
    try:
        if verify:
            return service.verify_contact_creation(payload['phone'], context=context)
        return service.create_contact_by_phone(payload['phone'], payload['firstName'],
                                               payload.get('lastName', ''), context=context)
    except MaxWriteOutcomeUnknown:
        return _Failure('Сохранение пока не подтверждено. Проверьте результат перед повторной попыткой.', True)
    except Exception as exc:
        # Do not expose protocol frames, request parameters or credentials in UI.
        from ..protocol.client import MaxError
        if isinstance(exc, (ValueError, MaxError)):
            message = str(exc)
            return _Failure(message or 'Не удалось сохранить контакт.', verify, not bool(message))
        return _Failure('Не удалось завершить операцию. Проверьте сохранение.', True)


class ContactCreateDialog(QDialog):
    contact_saved = Signal(object)
    save_notice = Signal(str)

    def __init__(self, service, parent=None):
        super().__init__(parent)
        self.service = service
        self._language = normalize_language(getattr(getattr(parent, 'settings', None), 'ui_language', 'ru'))
        self._message_source = None
        self._field_labels = {}
        self._closed = False
        self._request = None
        self._pending_payload = None
        try:
            self._context = service.search_context()
        except (AttributeError, ValueError):
            self._context = None
        self.setWindowTitle('Добавить контакт')
        self.setModal(True)
        self.setMinimumWidth(440)
        self.setMaximumWidth(520)
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 22, 24, 24)
        root.setSpacing(14)
        self.heading = QLabel('Добавить контакт')
        self.heading.setStyleSheet('font-size:22px;font-weight:700;')
        root.addWidget(self.heading)
        self.phone = self._field(root, 'Номер телефона с кодом страны', 'ContactPhoneInput', 64)
        self.phone.setText('+7')
        self.phone.setPlaceholderText('+7 123 456 78 90')
        self.first_name = self._field(root, 'Имя', 'ContactFirstName', 60)
        self.last_name = self._field(root, 'Фамилия (необязательно)', 'ContactLastName', 60)
        self.result_label = QLabel()
        self.result_label.setTextFormat(Qt.TextFormat.PlainText)
        self.result_label.setWordWrap(True)
        self.result_label.hide()
        root.addWidget(self.result_label)
        buttons = QHBoxLayout()
        self.cancel_button = QPushButton('Отмена')
        self.cancel_button.clicked.connect(self.reject)
        self.save_button = QPushButton('Сохранить контакт')
        self.save_button.setObjectName('Primary')
        self.save_button.clicked.connect(self._submit)
        self.verify_button = QPushButton('Проверить сохранение')
        self.verify_button.clicked.connect(self._verify)
        self.verify_button.hide()
        for button in (self.cancel_button, self.save_button, self.verify_button):
            button.setMinimumHeight(44)
            button.setAutoDefault(False)
            buttons.addWidget(button)
        root.addLayout(buttons)
        for field in (self.first_name, self.last_name, self.phone):
            field.textChanged.connect(self._input_changed)
            field.returnPressed.connect(self._submit)
        self.finished.connect(self._finished)
        self.set_language(self._language)
        self._input_changed()
        if not self._current():
            self._expire()

    def _field(self, root, label, name, limit):
        caption = QLabel(label)
        caption.setWordWrap(True)
        root.addWidget(caption)
        field = QLineEdit()
        field.setObjectName(name)
        field.setAccessibleName(label)
        field.setMaxLength(limit)
        field.setMinimumHeight(44)
        root.addWidget(field)
        self._field_labels[label] = (caption, field)
        return field

    def set_language(self, language):
        self._language = normalize_language(language)
        self.setWindowTitle(tr('Добавить контакт', self._language))
        self.heading.setText(self.windowTitle())
        for source, (caption, field) in self._field_labels.items():
            caption.setText(tr(source, self._language))
            field.setAccessibleName(tr(source, self._language))
        for button, source in ((self.cancel_button, 'Отмена'), (self.save_button, 'Сохранить контакт'),
                               (self.verify_button, 'Проверить сохранение')):
            button.setText(tr(source, self._language))
        if self._message_source is not None:
            source, app_owned = self._message_source
            self.result_label.setText(tr(source, self._language) if app_owned else source)

    retranslate = set_language

    def _current(self):
        try:
            return (isValid(self) and not self._closed and self._context is not None
                    and self.service.search_context() is self._context)
        except (AttributeError, ValueError):
            return False

    def _payload(self):
        try:
            return contact_create_payload(self.phone.text(), self.first_name.text(), self.last_name.text())
        except ValueError:
            return None

    def _input_changed(self, *_args):
        self.save_button.setEnabled(self._current() and self._request is None
                                    and self._pending_payload is None and self._payload() is not None)

    def _submit(self):
        if not self._current():
            self._expire()
            return
        payload = self._payload()
        if payload is None or self._request is not None or self._pending_payload is not None:
            return
        self._start(payload, False)

    def _verify(self):
        if not self._current():
            self._expire()
        elif self._request is None and self._pending_payload is not None:
            self._start(self._pending_payload, True)

    def _start(self, payload, verify):
        request = self._request = object()
        context = self._context
        self._enable_fields(False)
        self.save_button.setEnabled(False)
        self.verify_button.setEnabled(False)
        self._message('Проверка сохранения…' if verify else 'Сохранение…')

        def done(result):
            if not isValid(self) or self._closed or self._request is not request:
                return
            if not self._current() or self._context is not context:
                self._expire()
                return
            self._request = None
            if (isinstance(result, ContactCreateResult) and isinstance(result.contact, Contact)
                    and int64(result.contact.id)):
                self.contact_saved.emit(result.contact)
                if result.is_new is False:
                    notice = tr('Контакт уже есть в MAX.', self._language)
                    if not result.name_matches:
                        notice += ' ' + tr('Сохранено прежнее имя; изменить его можно в карточке контакта.', self._language)
                    self.save_notice.emit(notice)
                self.accept()
                return
            failure = result if isinstance(result, _Failure) else _Failure('MAX не подтвердил сохранение контакта.', True)
            self._message(failure.message, app_owned=failure.app_owned)
            if failure.uncertain:
                self._pending_payload = payload
                self.verify_button.show()
                self.verify_button.setEnabled(True)
                self.save_button.hide()
            else:
                self._pending_payload = None
                self._enable_fields(True)
                self._input_changed()

        # An unclassified runner error may follow a write; never offer blind retry.
        try:
            run_async(_save, self.service, payload, context, verify,
                      on_done=done, on_error=lambda _text: done(_Failure('Операция не завершена. Проверьте сохранение.', True)))
        except Exception:
            # Queue setup has not returned a task; preserve a safe recovery path.
            # Do not assume the worker was never scheduled before the exception.
            done(_Failure('Не удалось запустить операцию. Проверьте сохранение.', True))

    def _enable_fields(self, enabled):
        for field in (self.first_name, self.last_name, self.phone):
            field.setEnabled(enabled)

    def _message(self, text, *, app_owned=True):
        self._message_source = (text, app_owned)
        self.result_label.setText(tr(text, self._language) if app_owned else text)
        self.result_label.show()

    def _expire(self):
        self._context = self._request = None
        if not isValid(self) or self._closed:
            return
        self._enable_fields(False)
        self.save_button.setEnabled(False)
        self.verify_button.setEnabled(False)
        self._message('Сессия изменилась. Откройте добавление контакта заново.')

    def _finished(self, _result):
        self._closed = True
        self._context = self._request = None
