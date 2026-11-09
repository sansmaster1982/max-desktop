"""Explicit server title/description editor; recovery only reads the captured chat."""
from __future__ import annotations

from PySide6.QtCore import QEvent, Qt, QTimer, Signal
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QLabel, QLineEdit,
                              QPlainTextEdit, QPushButton, QVBoxLayout)
from shiboken6 import isValid

from ..core.i18n import tr
from ..protocol.client import MaxError, MaxWriteNotStarted, MaxWriteOutcomeUnknown
from ..protocol.conversation_edit_contracts import normalize_conversation_edit_text, utf16_length
from ..protocol.folder_contracts import int64
from .async_runner import run_async


def _target(value, context):
    return (isinstance(value, dict) and context is not None
            and value.get('context') is context and int64(value.get('chat_id'))
            and value['chat_id'] != 0 and value['chat_id'] == getattr(context, 'chat_id', None)
            and value.get('peer_id') is None and getattr(context, 'peer_id', None) is None)


def conversation_edit_result(value, context, *, refresh=False):
    if refresh:
        if not isinstance(value, dict) or value.get('context') is not context:
            return None
        value = value.get('lifecycle_result')
    if (not _target(value, context) or value.get('action') != 'edit'
            or value.get('lifecycle_kind') != 'conversation_edit'):
        return None
    for key in ('confirmed_fields', 'pending_fields'):
        fields = value.get(key)
        if (not isinstance(fields, list) or any(field not in ('title', 'description') for field in fields)
                or len(fields) != len(set(fields))):
            return None
    if set(value['confirmed_fields']) & set(value['pending_fields']):
        return None
    if value.get('confirmed') is True and (value.get('pending') is not False or value['pending_fields']):
        return None
    return value


def conversation_edit_state(snapshot, context):
    if not isinstance(snapshot, dict) or snapshot.get('context') is not context:
        return None
    return conversation_edit_result(snapshot.get('conversation_edit_state'), context)


def conversation_edit_available(snapshot, context):
    if (not _target(snapshot, context) or snapshot.get('edit_mode') != 'server'
            or not isinstance(snapshot.get('availability'), dict)
            or snapshot['availability'].get('edit') is not True):
        return False
    pending = conversation_edit_state(snapshot, context)
    return snapshot.get('action_pending') is not True or (pending is not None and pending.get('pending') is True)


def conversation_edit_notice(state, language='ru'):
    if isinstance(state, dict):
        if state.get('confirmed') is True and state.get('pending') is False and not state.get('pending_fields'):
            return tr('Сведения обновлены.', language)
        fields = state.get('confirmed_fields', [])
        pending = state.get('pending_fields', [])
        if 'title' in fields and 'description' in pending:
            return tr('Название подтверждено; описание ещё не подтверждено. Повторное сохранение остановлено.', language)
        if 'description' in fields and 'title' in pending:
            return tr('Описание подтверждено; название ещё не подтверждено. Повторное сохранение остановлено.', language)
    return tr('Результат сохранения не подтверждён. Проверьте состояние; повторное сохранение остановлено.', language)


def _label(text=''):
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return label


class ConversationEditDialog(QDialog):
    completed = Signal(object)

    def __init__(self, service, context, snapshot, parent=None, *, language='ru', context_is_current=None):
        super().__init__(parent)
        self.service, self.context, self.language = service, context, language
        self._scope = context_is_current or (lambda: True)
        self._lifetime = {'active': True}
        self._job = self._ticket = None
        self._attempted = False
        self._original = None
        self._limits = None
        self._state = conversation_edit_state(snapshot, context)
        self.setObjectName('ConversationEditDialog')
        self.setWindowTitle(tr('Изменить сведения', language))
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.resize(500, 430)
        self.setMinimumWidth(380)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)
        layout.addWidget(_label(tr('Название и описание будут изменены для всех участников.', language)))
        title_label = _label(tr('Название', language))
        self.title_edit = QLineEdit()
        self.title_edit.setAccessibleName(title_label.text())
        title_label.setBuddy(self.title_edit)
        layout.addWidget(title_label)
        layout.addWidget(self.title_edit)
        self.title_count = _label()
        layout.addWidget(self.title_count)
        description_label = _label(tr('Описание', language))
        self.description_edit = QPlainTextEdit()
        self.description_edit.setAccessibleName(description_label.text())
        self.description_edit.setMinimumHeight(120)
        description_label.setBuddy(self.description_edit)
        layout.addWidget(description_label)
        layout.addWidget(self.description_edit, 1)
        self.description_count = _label()
        layout.addWidget(self.description_count)
        self.validation = _label()
        self.validation.hide()
        layout.addWidget(self.validation)
        self.status = _label(tr('Загрузка сведений…', language))
        layout.addWidget(self.status)
        self.refresh_button = QPushButton(tr('Проверить состояние', language))
        self.refresh_button.setAutoDefault(False)
        self.refresh_button.clicked.connect(self.refresh_state)
        self.refresh_button.hide()
        layout.addWidget(self.refresh_button)
        self.retry_button = QPushButton(tr('Обновить сведения', language))
        self.retry_button.setAutoDefault(False)
        self.retry_button.clicked.connect(self.start_loading)
        self.retry_button.hide()
        layout.addWidget(self.retry_button)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        self.submit_button = self.buttons.button(QDialogButtonBox.StandardButton.Save)
        self.submit_button.setText(tr('Сохранить', language))
        self.submit_button.setAutoDefault(False)
        self.submit_button.setDefault(False)
        self.cancel_button = self.buttons.button(QDialogButtonBox.StandardButton.Cancel)
        self.cancel_button.setText(tr('Отмена', language))
        self.cancel_button.setDefault(True)
        self.cancel_button.setFocus()
        self.buttons.accepted.connect(self.submit)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        for widget in (self.title_edit, self.description_edit):
            widget.installEventFilter(self)
            widget.textChanged.connect(self._update_submit)
            widget.setEnabled(False)
        self.submit_button.setEnabled(False)
        self._scope_timer = QTimer(self)
        self._scope_timer.setInterval(150)
        self._scope_timer.timeout.connect(self._check_scope)

    def _current(self):
        try:
            return (isValid(self) and self._lifetime['active'] and self._scope() is True
                    and self.service.is_current_conversation(self.context) is True)
        except (AttributeError, ValueError, RuntimeError):
            return False

    def _check_scope(self):
        if not self._current():
            self.reject()

    def showEvent(self, event):
        super().showEvent(event)
        self._scope_timer.start()
        self._check_scope()

    def eventFilter(self, watched, event):
        if (watched is self.title_edit and event.type() == QEvent.Type.KeyPress
                and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)):
            self.reject()
            return True
        if event.type() == QEvent.Type.FocusOut and not self._attempted:
            if watched is self.title_edit:
                self.title_edit.setText(normalize_conversation_edit_text(self.title_edit.text()))
            elif watched is self.description_edit:
                self.description_edit.setPlainText(normalize_conversation_edit_text(self.description_edit.toPlainText()))
        return super().eventFilter(watched, event)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.reject()
            return
        super().keyPressEvent(event)

    def _values(self):
        return (normalize_conversation_edit_text(self.title_edit.text()),
                normalize_conversation_edit_text(self.description_edit.toPlainText()))

    def _update_submit(self):
        values = self._values()
        valid = (self._limits is not None and bool(values[0])
                 and all(utf16_length(value) <= limit for value, limit in zip(values, self._limits)))
        self.submit_button.setEnabled(bool(valid and self._ticket is not None and not self._attempted
            and self._job is None and values != self._original and self._current()))
        if self._limits:
            for label, value, limit in zip((self.title_count, self.description_count), values, self._limits):
                label.setText(tr('{count} из {limit} (эмодзи могут занимать два символа)', self.language,
                                 count=utf16_length(value), limit=limit))
        reason = ''
        if self._limits and not self._attempted:
            if not values[0]:
                reason = tr('Введите название.', self.language)
            elif utf16_length(values[0]) > self._limits[0]:
                reason = tr('Название слишком длинное.', self.language)
            elif utf16_length(values[1]) > self._limits[1]:
                reason = tr('Описание слишком длинное.', self.language)
        self.validation.setText(reason)
        self.validation.setVisible(bool(reason))

    def start_loading(self):
        if not self.isVisible() or not self._current() or self._job is not None or self._attempted:
            return
        self._ticket = None
        self.retry_button.hide()
        self.status.setText(tr('Загрузка сведений…', self.language))
        self._dispatch(lambda service, context: service.prepare_conversation_edit(context), self._loaded)

    def _loaded(self, result):
        if (not _target(result, self.context) or result.get('kind') not in ('group', 'channel')
                or not isinstance(result.get('title'), str) or not isinstance(result.get('description'), str)
                or any(type(result.get(key)) is not int or result[key] <= 0
                       for key in ('title_limit', 'description_limit'))):
            self.status.setText(tr('Не удалось загрузить доступные для изменения сведения.', self.language))
            self.retry_button.show()
            return
        self._limits = (result['title_limit'], result['description_limit'])
        self.title_edit.setText(result['title'])
        self.description_edit.setPlainText(result['description'])
        self._original = self._values()
        if result.get('pending') is True:
            self._present_state(conversation_edit_state(result, self.context))
            return
        if result.get('can_submit') is not True or result.get('ticket') is None:
            self.status.setText(tr('Изменение сведений недоступно для текущих прав или состояния чата.', self.language))
            return
        self._ticket = result['ticket']
        self.title_edit.setEnabled(True)
        self.description_edit.setEnabled(True)
        self.status.setText(tr('Пустое описание удалит прежнее описание.', self.language))
        self._update_submit()

    def _dispatch(self, operation, callback):
        request = self._job = object()
        self.refresh_button.setEnabled(False)
        self._update_submit()
        service, context, lifetime = self.service, self.context, self._lifetime

        def work():
            try:
                if not lifetime['active'] or not service.is_current_conversation(context):
                    return None
                return operation(service, context)
            except Exception:
                return None  # Never expose raw server/account text in async logs.

        def done(result=None):
            if not self._current() or self._job is not request or not self.isVisible():
                return
            self._job = None
            self.refresh_button.setEnabled(True)
            callback(result)

        run_async(work, on_done=done, on_error=lambda _error: done())

    def submit(self):
        self._update_submit()
        if not self.isVisible() or not self.submit_button.isEnabled():
            return
        try:
            fresh = self.service.conversation_snapshot(self.context)
        except (AttributeError, ValueError, RuntimeError):
            fresh = None
        if not conversation_edit_available(fresh, self.context) or fresh.get('action_pending') is True:
            self._present_state(conversation_edit_state(fresh, self.context), unavailable=True)
            return
        values, ticket = self._values(), self._ticket
        self.title_edit.setText(values[0])
        self.description_edit.setPlainText(values[1])
        self._attempted = True
        self._ticket = None
        self.title_edit.setEnabled(False)
        self.description_edit.setEnabled(False)
        self.cancel_button.setText(tr('Закрыть', self.language))
        self.status.setText(tr('Сохранение…', self.language))

        def operation(service, context):
            try:
                fresh = service.conversation_snapshot(context)
                if not conversation_edit_available(fresh, context) or fresh.get('action_pending') is True:
                    return ('not_sent', None)
                return ('result', service.submit_conversation_edit(context, ticket=ticket,
                    title=values[0], description=values[1]))
            except MaxWriteOutcomeUnknown:
                return ('unknown', None)
            except (MaxWriteNotStarted, ValueError):
                return ('not_sent', None)
            except MaxError:
                return ('rejected', None)

        def done(envelope):
            status, result = envelope if isinstance(envelope, tuple) and len(envelope) == 2 else ('unknown', None)
            state = conversation_edit_result(result, self.context)
            if state is None:
                try:
                    state = conversation_edit_state(self.service.conversation_snapshot(self.context), self.context)
                except (AttributeError, ValueError, RuntimeError):
                    pass
            self._present_state(state)
            if state is None and status in ('not_sent', 'rejected'):
                self.status.setText(tr('Изменения не отправлены. Откройте форму заново.' if status == 'not_sent'
                    else 'MAX отклонил изменение сведений.', self.language))
                self.refresh_button.hide()
            self.completed.emit(state)

        self._dispatch(operation, done)

    def _present_state(self, state, *, unavailable=False):
        self._state = state
        self._attempted = True
        self._ticket = None
        self.title_edit.setEnabled(False)
        self.description_edit.setEnabled(False)
        self.retry_button.hide()
        self.cancel_button.setText(tr('Закрыть', self.language))
        self.status.setText(tr('Изменение сведений недоступно для текущих прав или состояния чата.', self.language)
                            if unavailable and state is None else conversation_edit_notice(state, self.language))
        self.refresh_button.setVisible(not (state is not None and state.get('confirmed') is True
                                            and state.get('pending') is False)
                                      and not (unavailable and state is None))
        self._update_submit()

    def refresh_state(self):
        if not self.isVisible() or not self._current() or self._job is not None or not self._attempted:
            return
        self.status.setText(tr('Проверка сохранения…', self.language))

        def done(result):
            state = conversation_edit_result(result, self.context, refresh=True)
            if state is None and self._state is not None and self._state.get('pending') is True:
                self._present_state(self._state)
                self.status.setText(tr('Не удалось проверить сохранение. Показан последний известный результат.', self.language)
                                    + '\n' + conversation_edit_notice(self._state, self.language))
                self.completed.emit(None)
                return
            self._present_state(state)
            self.completed.emit(state)

        self._dispatch(lambda service, context: service.refresh_conversation_controls(context), done)

    def accept(self):
        # Only the explicit Save button invokes submit; QDialog.accept is not a write.
        return

    def done(self, result):
        self._lifetime['active'] = False
        self._scope_timer.stop()
        self._job = self._ticket = None
        super().done(result)
