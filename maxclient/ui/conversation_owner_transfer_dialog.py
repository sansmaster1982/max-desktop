"""Explicit successor choice and phase-aware ownership transfer presentation."""
from __future__ import annotations
import math

from PySide6.QtCore import Qt, QTimer, Signal, QSize
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QLineEdit,
                              QListWidget, QListWidgetItem, QPushButton, QVBoxLayout, QWidget)
from shiboken6 import isValid

from ..core.i18n import tr
from ..protocol.folder_contracts import int64
from .async_runner import run_async
from .widgets.avatar import Avatar


def _label(text, parent=None):
    label = QLabel(text, parent)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return label


def owner_transfer_result(result, context, requested=None):
    candidate = (result.get('lifecycle_result') if requested == 'refresh_actions'
                 and isinstance(result, dict) else result)
    if (context is None or not isinstance(candidate, dict) or candidate.get('historical') is True
            or candidate.get('context') is not context or candidate.get('action') != 'leave'
            or candidate.get('lifecycle_kind') != 'owner_transfer'
            or not int64(candidate.get('chat_id')) or candidate['chat_id'] == 0
            or candidate['chat_id'] != getattr(context, 'chat_id', None)
            or candidate.get('peer_id') is not None or getattr(context, 'peer_id', None) is not None
            or not int64(candidate.get('successor_id')) or candidate['successor_id'] <= 0
            or candidate['successor_id'] == getattr(context, 'owner_id', None)):
        return None
    return candidate


def owner_transfer_state(snapshot, context):
    if not isinstance(snapshot, dict) or snapshot.get('context') is not context:
        return None
    return owner_transfer_result(snapshot.get('owner_transfer_state'), context)


def owner_transfer_available(snapshot, context, *, continuation=False):
    if (not isinstance(snapshot, dict) or context is None
            or snapshot.get('context') is not context
            or not int64(snapshot.get('chat_id')) or snapshot['chat_id'] == 0
            or snapshot['chat_id'] != getattr(context, 'chat_id', None)
            or snapshot.get('peer_id') is not None or getattr(context, 'peer_id', None) is not None):
        return False
    caps = snapshot.get('action_capabilities')
    if (not isinstance(caps, dict) or caps.get('supported') is not True
            or caps.get('kind') not in ('group', 'channel') or caps.get('service') is True):
        return False
    action = 'continue_owner_leave' if continuation else 'transfer_owner_leave'
    rows = snapshot.get('more_actions')
    if not isinstance(rows, list) or not any(isinstance(row, dict) and row.get('action') == action
                and row.get('available') is True for row in rows):
        return False
    if continuation:
        state = owner_transfer_state(snapshot, context)
        return (state is not None and state.get('stage') == 'transferred'
                and state.get('can_continue_leave') is True
                and state.get('transfer_confirmed') is True and state.get('leave_confirmed') is False)
    return (snapshot.get('action_pending') is not True and caps.get('owner') is True
            and caps.get('participant') is True)


def owner_transfer_notice_parts(state):
    """Owned template and literal user values, suitable for later retranslation."""
    if not isinstance(state, dict) or state.get('historical') is True:
        return '', {}, {}
    name = state.get('successor_name')
    values = {'name': name} if isinstance(name, str) and name.strip() else {}
    sources = {} if values else {'name': 'новому владельцу'}
    if (state.get('confirmed') is True and state.get('pending') is False
            and state.get('transfer_confirmed') is True and state.get('leave_confirmed') is True):
        return 'Права переданы: {name}. Вы вышли из чата.', values, sources
    if state.get('transfer_confirmed') is True:
        return 'Права переданы: {name}. Выход не подтверждён.', values, sources
    return 'Передача прав не подтверждена. Выход не запускался. Проверьте состояние.', {}, {}


def owner_transfer_notice(state, language='ru'):
    source, values, sources = owner_transfer_notice_parts(state)
    values.update({key: tr(value, language) for key, value in sources.items()})
    return tr(source, language, **values)


class OwnerTransferConfirmation(QDialog):
    """No service calls: accepting this modal only records an explicit choice."""
    def __init__(self, chat_title, successor, *, continuation=False, channel=False,
                 private=True, language='ru', context_is_current=None, parent=None):
        super().__init__(parent)
        self._scope = context_is_current or (lambda: False)
        self.setObjectName('OwnerTransferConfirmation')
        self.setWindowModality(Qt.WindowModality.WindowModal)
        caption = ('Отписаться от канала' if channel else 'Выйти из чата') if continuation else 'Передать права и выйти'
        self.setWindowTitle(tr(caption, language))
        self.setMinimumWidth(420)
        self.resize(480, 300)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(16)
        layout.addWidget(_label(str(chat_title)))
        person = QHBoxLayout()
        avatar = Avatar(46)
        name = successor.get('name') if isinstance(successor.get('name'), str) else ''
        avatar.set_identity(name, successor.get('id'))  # Initials only; no remote photo request.
        person.addWidget(avatar)
        details = QVBoxLayout()
        details.addWidget(_label(name or tr('Новый владелец', language)))
        role = 'Владелец' if continuation else ('Администратор' if successor.get('role') == 'ADMIN' else 'Участник')
        extras = [tr(role, language)]
        for field in ('username', 'phone'):
            value = successor.get(field)
            if isinstance(value, str) and value.strip():
                extras.append(value)
        details.addWidget(_label(' · '.join(extras)))
        person.addLayout(details, 1)
        layout.addLayout(person)
        explanation = ('Права уже переданы этому участнику. Сейчас будет выполнен только выход.'
                       if continuation else
                       'Этот участник станет владельцем. Вы потеряете права владельца, затем выйдете. '
                       'Передача прав не отменяется при сбое выхода.')
        layout.addWidget(_label(tr(explanation, language)))
        if private:
            layout.addWidget(_label(tr('Вы сможете вернуться только по\xa0ссылке‑приглашению', language)))
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok |
                                        QDialogButtonBox.StandardButton.Cancel)
        self.submit_button = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.submit_button.setText(tr(caption, language))
        self.submit_button.setAutoDefault(False)
        self.submit_button.setDefault(False)
        cancel = self.buttons.button(QDialogButtonBox.StandardButton.Cancel)
        cancel.setText(tr('Отмена', language))
        cancel.setDefault(True)
        cancel.setFocus()
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self._timer = QTimer(self)
        self._timer.setInterval(150)
        self._timer.timeout.connect(self._check_scope)

    def _current(self):
        try:
            return self._scope() is True
        except (AttributeError, ValueError, RuntimeError):
            return False

    def _check_scope(self):
        if not self._current():
            self.reject()

    def showEvent(self, event):
        super().showEvent(event)
        self._timer.start()
        self._check_scope()

    def accept(self):
        if self._current():
            super().accept()
        else:
            self.reject()

    def done(self, result):
        self._timer.stop()
        super().done(result)


class ConversationOwnerTransferDialog(QDialog):
    completed = Signal(object)

    def __init__(self, service, context, snapshot, parent=None, *, language='ru',
                 context_is_current=None):
        super().__init__(parent)
        self.service, self.context, self.language = service, context, language
        self._scope = context_is_current or (lambda: False)
        self._lifetime = {'active': True}
        self._kind = snapshot.get('action_capabilities', {}).get('kind')
        self._title = str(snapshot.get('title', ''))
        self._private = snapshot.get('leave_private') is not False
        self._read_job = self._write_job = None
        self._confirmation = None
        self._candidates = {}
        self._ambiguous = set()
        self._query = ''
        self._marker = None
        self._markers = set()
        self._attempted = False
        self._state = owner_transfer_state(snapshot, context)
        if self._state is not None and self._state.get('pending') is not True:
            self._state = None  # A settled prior operation must not replace a new picker.
        self.setObjectName('ConversationOwnerTransferDialog')
        self.setWindowTitle(tr('Передать права владельца', language))
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.setMinimumWidth(440)
        self.resize(500, 540)
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 20)
        root.setSpacing(14)
        root.addWidget(_label(self._title))
        self.status = _label(tr('Выберите нового владельца.', language))
        root.addWidget(self.status)
        self.search_row = QWidget()
        search_layout = QHBoxLayout(self.search_row)
        search_layout.setContentsMargins(0, 0, 0, 0)
        self.search = QLineEdit()
        self.search.setPlaceholderText(tr('Найти по имени', language))
        self.search.setAccessibleName(tr('Найти по имени', language))
        self.search.textChanged.connect(self._query_changed)
        search_layout.addWidget(self.search, 1)
        self.search_button = QPushButton(tr('Найти', language))
        self.search_button.setAutoDefault(False)
        self.search_button.clicked.connect(lambda: self.start_loading())
        search_layout.addWidget(self.search_button)
        root.addWidget(self.search_row)
        self.candidates = QListWidget()
        self.candidates.setAccessibleName(tr('Выберите нового владельца.', language))
        self.candidates.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.candidates.itemSelectionChanged.connect(self._update_buttons)
        root.addWidget(self.candidates, 1)
        self.more_button = QPushButton(tr('Показать ещё', language))
        self.more_button.setAutoDefault(False)
        self.more_button.clicked.connect(lambda: self.start_loading(more=True))
        self.more_button.hide()
        root.addWidget(self.more_button)
        self.verify_button = QPushButton(tr('Проверить состояние', language))
        self.verify_button.setAutoDefault(False)
        self.verify_button.clicked.connect(self.verify)
        self.verify_button.hide()
        root.addWidget(self.verify_button)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok |
                                        QDialogButtonBox.StandardButton.Cancel)
        self.submit_button = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.submit_button.setText(tr('Продолжить', language))
        self.submit_button.setAutoDefault(False)
        self.submit_button.setDefault(False)
        self.submit_button.setEnabled(False)
        self.cancel_button = self.buttons.button(QDialogButtonBox.StandardButton.Cancel)
        self.cancel_button.setText(tr('Отмена', language))
        self.cancel_button.setDefault(True)
        self.cancel_button.setFocus()
        self.buttons.accepted.connect(self.submit)
        self.buttons.rejected.connect(self.reject)
        root.addWidget(self.buttons)
        self._timer = QTimer(self)
        self._timer.setInterval(150)
        self._timer.timeout.connect(self._check_scope)
        if self._state is not None:
            self._present_state(self._state)

    def _current(self):
        try:
            return (isValid(self) and self._lifetime['active']
                    and self.service.is_current_conversation(self.context) is True
                    and self._scope() is True)
        except (AttributeError, ValueError, RuntimeError):
            return False

    def _fresh_available(self, continuation=False):
        if not self._current():
            return False
        try:
            snapshot = self.service.conversation_snapshot(self.context)
        except (AttributeError, ValueError, RuntimeError):
            return False
        if not owner_transfer_available(snapshot, self.context, continuation=continuation):
            return False
        if continuation:
            state = owner_transfer_state(snapshot, self.context)
            return (self._current() and self._state is not None
                    and state['successor_id'] == self._state['successor_id'])
        return self._current()

    def _check_scope(self):
        if not self._current():
            self.reject()

    def showEvent(self, event):
        super().showEvent(event)
        self._timer.start()
        self._check_scope()

    def _query_changed(self):
        if self._attempted or self._state is not None:
            return
        self._read_job = None
        self._candidates.clear()
        self._ambiguous.clear()
        self._marker = None
        self._markers.clear()
        self.candidates.clear()
        self.more_button.hide()
        self.status.setText(tr('Нажмите «Найти», чтобы загрузить участников.', self.language))
        self._update_buttons()

    def start_loading(self, *, more=False):
        if (not self._current() or not self.isVisible() or self._attempted
                or self._state is not None or self._read_job is not None
                or not self._fresh_available()):
            return
        query = self.search.text().strip()
        marker = self._marker if more else None
        if more and (query or marker is None or marker in self._markers):
            return
        if not more:
            self._candidates.clear()
            self._ambiguous.clear()
            self._markers.clear()
            self.candidates.clear()
        self._query = query
        request = self._read_job = object()
        self.status.setText(tr('Загрузка участников…', self.language))
        self._update_buttons()
        service, context, lifetime = self.service, self.context, self._lifetime

        def work():
            try:
                if not lifetime['active'] or not service.is_current_conversation(context):
                    return None
                if not owner_transfer_available(service.conversation_snapshot(context), context):
                    return None
                return service.get_owner_transfer_candidates(context, query=query, marker=marker)
            except Exception:
                return None

        def done(page=None):
            if not self._current() or self._read_job is not request:
                return
            self._read_job = None
            if (not isinstance(page, dict) or page.get('context') is not context
                    or type(page.get('chat_id')) is not int or page['chat_id'] != context.chat_id
                    or page.get('kind') != self._kind or not isinstance(page.get('candidates'), list)
                    or type(page.get('complete')) is not bool):
                self.status.setText(tr('Не удалось загрузить участников. Повторите поиск.', self.language))
                self._update_buttons()
                return
            if marker is not None:
                self._markers.add(marker)
            for row in page['candidates']:
                if (not isinstance(row, dict) or not int64(row.get('id')) or row['id'] <= 0
                        or row['id'] == getattr(context, 'owner_id', None)
                        or not isinstance(row.get('name'), str) or not row['name'].strip()
                        or row.get('role') not in ('ADMIN', 'MEMBER')
                        or self._kind == 'group' and row['role'] != 'ADMIN'
                        or row.get('ticket') is None):
                    continue
                self._candidates[row['id']] = dict(row)
            next_marker = page.get('marker')
            self._marker = (next_marker if not query and type(next_marker) in (int, float)
                            and math.isfinite(next_marker)
                            and next_marker not in self._markers else None)
            self._render_candidates()
            phrase = ('Показана часть списка. Можно уточнить имя.' if page['complete'] is False else
                      'Выберите нового владельца.' if self._candidates else 'Подходящие участники не найдены.')
            self.status.setText(tr(phrase, self.language))
            if self._ambiguous:
                self.status.setText(self.status.text() + '\n' + tr(
                    'Одноимённые записи без различимых данных недоступны для выбора.', self.language))
            self.more_button.setVisible(self._marker is not None)
            self._update_buttons()

        run_async(work, on_done=done, on_error=lambda _error: done())

    def _render_candidates(self):
        self.candidates.clear()
        groups = {}
        for row in self._candidates.values():
            key = tuple(str(row.get(field, '')).strip().casefold()
                        for field in ('name', 'role', 'username', 'phone'))
            groups.setdefault(key, []).append(row['id'])
        self._ambiguous = {identity for values in groups.values() if len(values) > 1 for identity in values}
        self._ambiguous.update(row['id'] for row in self._candidates.values() if row.get('ambiguous') is True)
        for identity, row in self._candidates.items():
            role = tr('Администратор' if row['role'] == 'ADMIN' else 'Участник', self.language)
            extras = [role] + [row[field] for field in ('username', 'phone')
                              if isinstance(row.get(field), str) and row[field].strip()]
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, identity)
            item.setData(Qt.ItemDataRole.AccessibleTextRole, row['name'] + ', ' + ', '.join(extras))
            if identity in self._ambiguous:
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsSelectable & ~Qt.ItemFlag.ItemIsEnabled)
            item.setSizeHint(QSize(400, 72))
            self.candidates.addItem(item)
            widget = QWidget()
            layout = QHBoxLayout(widget)
            layout.setContentsMargins(8, 6, 8, 6)
            avatar = Avatar(42)
            avatar.set_identity(row['name'], identity)
            layout.addWidget(avatar)
            labels = QVBoxLayout()
            labels.setSpacing(2)
            labels.addWidget(_label(row['name']))
            labels.addWidget(_label(' · '.join(extras)))
            layout.addLayout(labels, 1)
            widget.setEnabled(identity not in self._ambiguous)
            self.candidates.setItemWidget(item, widget)
        self.candidates.setCurrentRow(-1)
        self.candidates.clearSelection()

    def _selected(self):
        item = self.candidates.currentItem()
        if item is None or not item.isSelected() or not item.flags() & Qt.ItemFlag.ItemIsEnabled:
            return None
        identity = item.data(Qt.ItemDataRole.UserRole)
        return self._candidates.get(identity) if identity not in self._ambiguous else None

    def _update_buttons(self):
        if not isValid(self):
            return
        busy = self._read_job is not None or self._write_job is not None or self._confirmation is not None
        continuation = self._state is not None
        can_continue = (continuation and self._state.get('stage') == 'transferred'
                        and self._state.get('can_continue_leave') is True)
        self.submit_button.setEnabled(self._current() and not busy and (
            can_continue if continuation else not self._attempted and self._selected() is not None))
        self.search_button.setEnabled(not busy and not self._attempted and not continuation)
        self.more_button.setEnabled(not busy and not self._attempted)
        self.candidates.setEnabled(not busy and not self._attempted and not continuation)
        self.verify_button.setEnabled(not busy and self._current())

    def _present_state(self, state):
        self._state = state
        self._attempted = True
        self.search_row.hide()
        self.candidates.hide()
        self.more_button.hide()
        self.status.setText(owner_transfer_notice(state, self.language))
        self.submit_button.setText(tr('Отписаться от канала' if self._kind == 'channel' else 'Выйти из чата', self.language))
        self.verify_button.setVisible(state.get('pending') is True)
        self.cancel_button.setText(tr('Закрыть', self.language))
        self._update_buttons()

    def submit(self):
        if (not self._current() or not self.isVisible() or self._read_job is not None
                or self._write_job is not None or self._confirmation is not None):
            return
        continuation = self._state is not None
        if not self._fresh_available(continuation):
            return
        if continuation:
            selected = dict(id=self._state['successor_id'], name=self._state.get('successor_name', ''), role='MEMBER')
        else:
            selected = self._selected()
            if self._attempted or selected is None:
                return
            selected = dict(selected)
        successor_id = selected['id']

        def confirmation_current():
            if not self._fresh_available(continuation):
                return False
            current = self._state if continuation else self._selected()
            return (current is not None and
                    current.get('successor_id' if continuation else 'id') == successor_id
                    and (continuation or current.get('ticket') is selected.get('ticket')))

        confirmation = OwnerTransferConfirmation(self._title, selected,
            continuation=continuation, channel=self._kind == 'channel', private=self._private,
            language=self.language, context_is_current=confirmation_current, parent=self)
        self._confirmation = confirmation
        self._update_buttons()
        accepted = confirmation.exec() == QDialog.DialogCode.Accepted and confirmation_current()
        self._confirmation = None
        if isValid(confirmation):
            confirmation.deleteLater()
        if not accepted or not self._current():
            self._update_buttons()
            return
        self._attempted = True
        self._dispatch(continuation=continuation, ticket=selected.get('ticket'), successor_id=successor_id)

    def _dispatch(self, *, continuation=False, ticket=None, successor_id=None, verify=False):
        request = object()
        self._write_job = request
        if self._state is not None:
            self._state = dict(self._state, can_continue_leave=False)
        self.status.setText(tr('Проверка состояния…' if verify else 'Сохранение…', self.language))
        self._update_buttons()
        service, context, lifetime = self.service, self.context, self._lifetime

        def work():
            try:
                if not lifetime['active'] or not service.is_current_conversation(context):
                    return None
                if verify:
                    return service.refresh_conversation_controls(context)
                snapshot = service.conversation_snapshot(context)
                if not owner_transfer_available(snapshot, context, continuation=continuation):
                    return None
                if continuation:
                    current = owner_transfer_state(snapshot, context)
                    if current['successor_id'] != successor_id:
                        return None
                    return service.continue_conversation_owner_leave(context)
                return service.transfer_conversation_owner_and_leave(context, ticket)
            except Exception:
                return None  # Durable state is inspected below; no raw exception text.

        def done(outcome=None):
            if not self._current() or self._write_job is not request:
                return
            self._write_job = None
            state = owner_transfer_result(outcome, context, 'refresh_actions' if verify else None)
            if state is None:
                try:
                    state = owner_transfer_state(service.conversation_snapshot(context), context)
                except (AttributeError, ValueError, RuntimeError):
                    state = None
            if not self._current():
                return
            if state is not None and (successor_id is None or state['successor_id'] == successor_id):
                self._present_state(state)
                self.completed.emit(state)
            else:
                self.status.setText(tr('Результат не подтверждён. Проверьте состояние перед новым действием.', self.language))
                self.verify_button.show()
                self._update_buttons()
                self.completed.emit(None)

        run_async(work, on_done=done, on_error=lambda _error: done())

    def verify(self):
        if (not self._current() or not self.isVisible() or self._read_job is not None
                or self._write_job is not None or not self._attempted):
            return
        self._dispatch(verify=True, successor_id=self._state.get('successor_id') if self._state else None)

    def accept(self):
        self.submit()

    def done(self, result):
        self._lifetime['active'] = False
        self._timer.stop()
        if self._confirmation is not None and isValid(self._confirmation):
            self._confirmation.reject()
        self._read_job = self._write_job = None
        super().done(result)
