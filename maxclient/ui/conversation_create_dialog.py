"""Explicit group/channel creation with session-bound, single-flight writes."""
from __future__ import annotations

from dataclasses import dataclass
from weakref import WeakKeyDictionary

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox,
    QPlainTextEdit, QPushButton, QStackedWidget, QVBoxLayout, QWidget,
)
from shiboken6 import isValid

from ..core.conversation_create import ConversationCreateResult
from ..core.i18n import normalize_language, tr
from ..core.models import Chat, Contact
from ..core.service import _contact_display_name
from ..protocol.client import MaxError, MaxWriteOutcomeUnknown
from ..protocol.conversation_create_contracts import conversation_create_values, conversation_creation_limits
from ..protocol.folder_contracts import int64
from .async_runner import run_async


@dataclass(frozen=True)
class _Failure:
    message: str
    uncertain: bool = False


class _LocalValidationError(ValueError):
    """Owned wording from the pure local validator, never a service error."""

    def __init__(self, source, **values):
        super().__init__(source)
        self.source = source
        self.values = values


def _local_validation_error(error, limits):
    """Adapt exact local messages without recognizing arbitrary server text."""
    if type(error) is not ValueError:
        return error
    message = str(error)
    fixed = ('Неверные ограничения MAX', 'MAX вернул неверные ограничения создания',
             'Укажите тип, название и описание', 'Выберите уникальные контакты MAX',
             'Создание канала не добавляет подписчиков', 'Описание доступно при создании канала')
    if message in fixed:
        return _LocalValidationError(message)
    try:
        normalized = conversation_creation_limits(limits)
    except ValueError:
        return error
    for template, limit in (
        ('Название должно содержать от 1 до {limit} символов', normalized['max-cname-length']),
        ('Описание: не более {limit} символов', normalized['max-description-length']),
        ('Можно выбрать не более {limit} участников',
         min(normalized['max-added-participants'], normalized['max-participants'] - 1)),
    ):
        if message == template.format(limit=limit):
            return _LocalValidationError(template, limit=limit)
    return error


# Keep a queued/uncertain operation bound to its service even if its window closes.
# The service separately persists network writes; this also covers the queue gap.
_UI_WRITES = WeakKeyDictionary()


def _confirm_release(parent, language, title):
    """Explicit warning before forgetting an unconfirmed create; Cancel is default."""
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Warning)
    box.setTextFormat(Qt.TextFormat.PlainText)
    box.setWindowTitle(tr('Снять блокировку создания', language))
    box.setText(tr('MAX не подтвердил предыдущее создание «{title}». Возможно, чат уже создан: '
                   'сначала проверьте список чатов. Снять блокировку? Повторный запрос не отправляется.',
                   language, title=title))
    release = box.addButton(tr('Снять блокировку', language), QMessageBox.ButtonRole.DestructiveRole)
    cancel = box.addButton(tr('Отмена', language), QMessageBox.ButtonRole.RejectRole)
    box.setDefaultButton(cancel)
    box.setEscapeButton(cancel)
    box.exec()
    return box.clickedButton() is release


def _create(service, values, context, verify=False):
    """Keep uncertainty typed across the runner's string-only error signal."""
    try:
        # The worker can start after logout, even if the original click was valid.
        if context is None or service.search_context() is not context:
            return _Failure('Сессия изменилась. Откройте создание заново.')
        if verify:
            return service.verify_conversation_creation(context=context)
        return service.create_conversation(
            values['kind'], values['title'], values['member_ids'],
            values['description'], context=context,
        )
    except MaxWriteOutcomeUnknown:
        return _Failure('Результат создания пока не подтверждён. Повторное создание заблокировано.', True)
    except (ValueError, MaxError):
        # Server errors may contain wire data: use a product-level message here.
        return _Failure('MAX не подтвердил операцию. Проверьте данные и подключение.', verify)
    except Exception:
        return _Failure('Не удалось подтвердить результат. Повторное создание заблокировано.', True)


class ConversationCreateDialog(QDialog):
    """Group: members then title; channel: title and optional description.

    Opening, filtering, navigating and cancelling the initial form are local.
    Only the explicit final button schedules a create request. An uncertain
    create is never retried by this dialog, including after it is reopened.
    """

    conversation_created = Signal(object)

    def __init__(self, service, kind='group', parent=None, language='ru'):
        if kind not in ('group', 'channel'):
            raise ValueError('Unknown conversation kind')
        super().__init__(parent)
        self.service = service
        self.kind = kind
        self.language = normalize_language(language)
        self._message_source = None
        self._details_hint_state = ('', {}, True)
        self._member_hint_source = ('Выбрано участников: {count}', {'count': 0})
        self._cancel_source = 'Отменить' if kind == 'group' else 'Не создавать'
        self._closed = False
        self._request = None
        self._uncertain = False
        self._confirmed_chat = None
        self._member_ids = set()
        self._contacts_loaded = kind == 'channel'
        self._context = self._service_context()
        self.setWindowTitle(self._tr('Создать группу' if kind == 'group' else 'Создать канал'))
        self.setModal(True)
        self.setMinimumWidth(470)
        self.setMaximumWidth(620)
        self.resize(500, 570 if kind == 'group' else 410)
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 22, 24, 24)
        root.setSpacing(14)
        self.heading = QLabel()
        self.heading.setStyleSheet('font-size:22px;font-weight:700;')
        root.addWidget(self.heading)
        self.pages = QStackedWidget()
        root.addWidget(self.pages, 1)
        self._build_members()
        self._build_details()
        self.result_label = QLabel()
        self.result_label.setTextFormat(Qt.TextFormat.PlainText)
        self.result_label.setWordWrap(True)
        self.result_label.setAccessibleName(self._tr('Результат создания'))
        self.result_label.hide()
        root.addWidget(self.result_label)
        buttons = QHBoxLayout()
        self.cancel_button = QPushButton(self._tr('Отменить' if kind == 'group' else 'Не создавать'))
        self.cancel_button.clicked.connect(self.reject)
        self.back_button = QPushButton(self._tr('Назад'))
        self.back_button.clicked.connect(self._back)
        self.next_button = QPushButton(self._tr('Далее'))
        self.next_button.clicked.connect(self._next)
        self.create_button = QPushButton(self._tr('Создать группу' if kind == 'group' else 'Создать канал'))
        self.create_button.clicked.connect(self._submit)
        self.verify_button = QPushButton(self._tr('Проверить результат'))
        self.verify_button.clicked.connect(self._verify)
        self.open_button = QPushButton(self._tr('Открыть созданный чат'))
        self.open_button.clicked.connect(self._open_confirmed)
        # Only for a journal without a server chat ID; never resends anything.
        self.release_button = QPushButton(self._tr('Снять блокировку…'))
        self.release_button.clicked.connect(self._release)
        for button in (self.cancel_button, self.back_button, self.next_button,
                       self.create_button, self.verify_button, self.open_button, self.release_button):
            button.setMinimumHeight(44)
            button.setAutoDefault(False)
            buttons.addWidget(button)
        self.next_button.setObjectName('Primary')
        self.create_button.setObjectName('Primary')
        self.open_button.setObjectName('Primary')
        self.verify_button.hide()
        self.open_button.hide()
        self.release_button.hide()
        root.addLayout(buttons)
        self.finished.connect(self._finished)
        if kind == 'group' and self._current():
            self._load_contacts()
        self._show_page(0 if kind == 'group' else 1)
        if not self._current():
            self._expire()
        else:
            self._restore_pending()

    def _tr(self, source, **values):
        return tr(source, self.language, **values)

    def set_language(self, language):
        """Relabel owned text only; retain drafts, requests and recovery state."""
        if not isValid(self) or self._closed:
            return
        self.language = normalize_language(language)
        title = 'Создать группу' if self.kind == 'group' else 'Создать канал'
        self.setWindowTitle(self._tr(title))
        self.heading.setText(self._tr('Выберите участников') if self.pages.currentIndex() == 0
                             else self.windowTitle())
        label = self._tr('Название группы' if self.kind == 'group' else 'Название канала')
        self._title_label.setText(label)
        self.title_input.setAccessibleName(label)
        self.title_input.setPlaceholderText(label)
        self.description_input.setAccessibleName(self._tr('Описание'))
        self.description_input.setPlaceholderText(self._tr('Описание'))
        self.member_search.setPlaceholderText(self._tr('Найти'))
        self.member_search.setAccessibleName(self._tr('Поиск участников по имени или номеру'))
        self.member_list.setAccessibleName(self._tr('Выберите участников'))
        self.result_label.setAccessibleName(self._tr('Результат создания'))
        for button, source in ((self.cancel_button, self._cancel_source),
                               (self.back_button, 'Назад'), (self.next_button, 'Далее'),
                               (self.create_button, title), (self.verify_button, 'Проверить результат'),
                               (self.open_button, 'Открыть созданный чат'),
                               (self.release_button, 'Снять блокировку…')):
            button.setText(self._tr(source))
        source, values = self._member_hint_source
        self.member_hint.setText(self._tr(source, **values))
        self._render_details_hint()
        if self._message_source is not None:
            self.result_label.setText(self._tr(self._message_source))

    retranslate = set_language

    def _render_details_hint(self):
        text, values, owned = self._details_hint_state
        self.details_hint.setText(self._tr(text, **values) if owned and text else text)

    def _close_caption(self):
        self._cancel_source = 'Закрыть'
        self.cancel_button.setText(self._tr(self._cancel_source))

    def _build_members(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        self.member_search = QLineEdit()
        self.member_search.setObjectName('ConversationMemberSearch')
        self.member_search.setPlaceholderText(self._tr('Найти'))
        self.member_search.setAccessibleName(self._tr('Поиск участников по имени или номеру'))
        self.member_search.setClearButtonEnabled(True)
        self.member_search.setMinimumHeight(40)
        self.member_search.textChanged.connect(self._filter_members)
        layout.addWidget(self.member_search)
        self.member_list = QListWidget()
        self.member_list.setObjectName('ConversationMemberList')
        self.member_list.setAccessibleName(self._tr('Выберите участников'))
        self.member_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.member_list.setStyleSheet(
            'QListWidget::item { padding: 12px 8px; min-height: 30px; }'
            'QListWidget::indicator { width: 20px; height: 20px; }'
        )
        self.member_list.itemChanged.connect(self._selection_changed)
        layout.addWidget(self.member_list, 1)
        self.member_hint = QLabel()
        self.member_hint.setWordWrap(True)
        self.member_hint.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.member_hint)
        self.pages.addWidget(page)

    def _build_details(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)
        label = self._tr('Название группы' if self.kind == 'group' else 'Название канала')
        self._title_label = QLabel(label)
        self._title_label.setWordWrap(True)
        layout.addWidget(self._title_label)
        self.title_input = QLineEdit()
        self.title_input.setObjectName('ConversationTitleInput')
        self.title_input.setAccessibleName(label)
        self.title_input.setPlaceholderText(label)
        self.title_input.setMinimumHeight(44)
        self.title_input.textChanged.connect(self._input_changed)
        layout.addWidget(self.title_input)
        self.description_input = QPlainTextEdit()
        self.description_input.setObjectName('ConversationDescriptionInput')
        self.description_input.setAccessibleName(self._tr('Описание'))
        self.description_input.setPlaceholderText(self._tr('Описание'))
        self.description_input.setMaximumHeight(145)
        self.description_input.textChanged.connect(self._input_changed)
        if self.kind == 'channel':
            layout.addWidget(self.description_input)
        else:
            self.description_input.hide()
        self.details_hint = QLabel()
        self.details_hint.setTextFormat(Qt.TextFormat.PlainText)
        self.details_hint.setWordWrap(True)
        layout.addWidget(self.details_hint)
        layout.addStretch(1)
        self.pages.addWidget(page)

    def _service_context(self):
        try:
            return self.service.search_context()
        except (AttributeError, ValueError):
            return None

    def _current(self):
        return (isValid(self) and not self._closed and self._context is not None
                and self._service_context() is self._context)

    def _load_contacts(self):
        try:
            contacts = self.service.list_contacts()
            if not isinstance(contacts, (list, tuple)):
                raise ValueError('Invalid local contacts')
        except Exception:
            self._contacts_loaded = False
            self._member_hint_source = ('Не удалось загрузить контакты. Откройте создание группы заново.', {})
            self.member_hint.setText(self._tr(self._member_hint_source[0]))
            return
        if not self._current():
            return
        self._contacts_loaded = True
        own_id = getattr(self.service, 'my_id', None)
        if not int64(own_id):
            own_id = None
        seen = set()
        for contact in contacts:
            if not isinstance(contact, Contact):
                continue
            member_id = contact.id
            if not int64(member_id) or member_id <= 0 or member_id == own_id or member_id in seen:
                continue
            seen.add(member_id)
            name = _contact_display_name(contact) or contact.phone or str(member_id)
            phone = contact.phone if isinstance(contact.phone, str) else ''
            text = f'{name}\n{phone}' if phone and phone != name else name
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, member_id)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked)
            self.member_list.addItem(item)
        self._update_member_hint()

    def _selection_changed(self, item):
        if not self._current():
            self._expire()
            return
        if self._request is not None or self._uncertain:
            return
        member_id = item.data(Qt.ItemDataRole.UserRole)
        if not int64(member_id) or member_id <= 0:
            return
        if item.checkState() == Qt.CheckState.Checked:
            self._member_ids.add(member_id)
        else:
            self._member_ids.discard(member_id)
        self._update_member_hint()
        self._input_changed()

    def _update_member_hint(self):
        self._member_hint_source = ('Выбрано участников: {count}', {'count': len(self._member_ids)})
        self.member_hint.setText(self._tr(self._member_hint_source[0], **self._member_hint_source[1]))

    def _filter_members(self, text):
        if not self._current():
            self._expire()
            return
        query = text.strip().casefold()
        for index in range(self.member_list.count()):
            item = self.member_list.item(index)
            item.setHidden(bool(query) and query not in item.text().casefold())

    def _show_page(self, index):
        self.pages.setCurrentIndex(index)
        self.heading.setText(self._tr('Выберите участников') if index == 0 else self.windowTitle())
        self.back_button.setVisible(self.kind == 'group' and index == 1)
        self.next_button.setVisible(index == 0)
        self.create_button.setVisible(index == 1)
        self._input_changed()

    def _next(self):
        if not self._current():
            self._expire()
        elif self._request is None and not self._uncertain and self._contacts_loaded:
            self._show_page(1)
            self.title_input.setFocus()

    def _back(self):
        if not self._current():
            self._expire()
        elif self._request is None and not self._uncertain:
            self._show_page(0)

    def _values(self):
        # Service exceptions have no local-text provenance, even if their text
        # happens to match a validator message. Keep this call outside the adapter.
        limits = self.service.conversation_creation_limits(context=self._context)
        try:
            return conversation_create_values(
                self.kind, self.title_input.text(), sorted(self._member_ids),
                self.description_input.toPlainText() if self.kind == 'channel' else '',
                limits=limits,
            )
        except ValueError as error:
            adapted = _local_validation_error(error, limits)
            if adapted is error:
                raise
            raise adapted from error

    def _input_changed(self, *_args):
        # Signals can fire while the page and its buttons are still being built.
        if not hasattr(self, 'create_button'):
            return
        allowed = self._current() and self._request is None and not self._uncertain
        allowed = allowed and self._confirmed_chat is None
        try:
            self._values()
            valid = True
            hint = ('', {}, True)
        except _LocalValidationError as exc:
            valid = False
            hint = (exc.source, exc.values, True) if self.title_input.text().strip() else ('', {}, True)
        except ValueError as exc:
            valid = False
            hint = (str(exc), {}, False) if self.title_input.text().strip() else ('', {}, False)
        except Exception:
            valid = False
            hint = ('Не удалось проверить параметры создания. Откройте создание заново.', {}, True)
        self._details_hint_state = hint
        self._render_details_hint()
        self.next_button.setEnabled(allowed and self._contacts_loaded)
        self.back_button.setEnabled(allowed)
        self.create_button.setEnabled(allowed and valid and self.pages.currentIndex() == 1)

    def _pending(self):
        try:
            pending = self.service.pending_conversation_creation(context=self._context)
            lease = _UI_WRITES.get(self.service)
            if lease is not None and lease['context'] is self._context:
                # Never inspect while a create worker is still running.
                if lease['running']:
                    return {'has_chat_id': False}
                return pending if pending is not None else {'has_chat_id': False}
            return pending
        except Exception:
            # A failed pending check cannot safely authorize a new create.
            return {'has_chat_id': False}

    def _restore_pending(self):
        pending = self._pending()
        if pending is not None:
            self._uncertain = True
            self._enable_fields(False)
            self._recovery(pending)
            self._message('Предыдущая операция создания ещё не подтверждена. Новое создание заблокировано.')

    def _submit(self):
        if not self._current():
            self._expire()
            return
        if (self._request is not None or self._uncertain or self._confirmed_chat is not None
                or self.pages.currentIndex() != 1):
            return
        pending = self._pending()
        if pending is not None:
            self._restore_pending()
            return
        try:
            values = self._values()
        except (ValueError, AttributeError):
            self._input_changed()
            return
        self._start(values, False)

    def _verify(self):
        if not self._current():
            self._expire()
        elif self._request is None and self._uncertain:
            pending = self._pending()
            if isinstance(pending, dict) and pending.get('has_chat_id') is True:
                self._start(None, True)

    def _start(self, values, verify):
        request = self._request = object()
        context = self._context
        lease = _UI_WRITES.get(self.service)
        if not verify:
            lease = {'context': context, 'running': True}
            _UI_WRITES[self.service] = lease
        self._enable_fields(False)
        self.release_button.hide()
        for button in (self.create_button, self.next_button, self.back_button,
                       self.verify_button, self.open_button):
            button.setEnabled(False)
        self._close_caption()
        self._message('Проверка результата…' if verify else 'Создание…')

        def done(result):
            if lease is not None and _UI_WRITES.get(self.service) is lease:
                lease['running'] = False
                if (self._valid_result(result) or
                        isinstance(result, _Failure) and not result.uncertain):
                    del _UI_WRITES[self.service]
            if not isValid(self) or self._closed or self._request is not request:
                return
            if not self._current() or self._context is not context:
                self._expire()
                return
            self._request = None
            if self._valid_result(result):
                self._confirmed_chat = result.chat
                if result.description_confirmed:
                    self._open_confirmed()
                    return
                self._uncertain = True
                pending = self._pending()
                if pending is None:
                    self._message('Канал создан, но описание не сохранено.')
                elif isinstance(pending, dict) and pending.get('has_chat_id') is True:
                    self._message('Канал создан, но сохранение описания не подтверждено. Можно открыть канал или проверить результат.')
                else:
                    self._message('Канал создан, но сохранение описания не подтверждено.')
                self._recovery(pending)
                self.open_button.show()
                self.open_button.setEnabled(True)
                return
            failure = result if isinstance(result, _Failure) else _Failure('MAX не подтвердил создание.', True)
            pending = self._pending()
            if failure.uncertain or pending is not None:
                self._uncertain = True
                self._recovery(pending)
            else:
                self._enable_fields(True)
                self._input_changed()
            self._message(failure.message)

        try:
            run_async(_create, self.service, values, context, verify, on_done=done,
                      on_error=lambda _text: done(_Failure('Результат операции неизвестен. Повторное создание заблокировано.', True)))
        except Exception:
            # The queue may have scheduled before throwing: never assume no write.
            done(_Failure('Не удалось подтвердить запуск операции. Повторное создание заблокировано.', True))

    def _valid_result(self, result):
        if not isinstance(result, ConversationCreateResult) or not isinstance(result.chat, Chat):
            return False
        if not int64(result.chat.id) or result.chat.id == 0:
            return False
        # A dialog reopened for another kind may be resolving the previous create.
        # The core owns that pending identity; never infer IDs or names in UI.
        return isinstance(result.description_confirmed, bool)

    def _recovery(self, pending):
        self.create_button.hide()
        self.next_button.hide()
        self.back_button.hide()
        self._close_caption()
        can_verify = isinstance(pending, dict) and pending.get('has_chat_id') is True
        self.verify_button.setVisible(can_verify)
        self.verify_button.setEnabled(can_verify and self._current() and self._request is None)
        can_release = self._releasable(pending)
        self.release_button.setVisible(can_release)
        self.release_button.setEnabled(can_release)

    def _releasable(self, pending):
        # A synthesized UI lease or a running worker has no journal to release.
        lease = _UI_WRITES.get(self.service)
        return (isinstance(pending, dict) and pending.get('has_chat_id') is False
                and type(pending.get('cid')) is int and self._request is None
                and self._confirmed_chat is None and self._current()
                and not (lease is not None and lease['running']))

    def _release(self):
        """Forget only the local no-ID journal after an explicit warning. No RPC."""
        if not self._current():
            self._expire()
            return
        pending = self._pending()
        if not self._releasable(pending):
            return
        if not _confirm_release(self, self.language, pending.get('title') or ''):
            return
        if not self._current() or not self._releasable(self._pending()):
            return
        try:
            self.service.release_unconfirmed_conversation_creation(context=self._context, cid=pending['cid'])
        except Exception:
            self._message('Не удалось снять блокировку. Откройте создание заново.')
            return
        lease = _UI_WRITES.get(self.service)
        if lease is not None and lease['context'] is self._context and not lease['running']:
            del _UI_WRITES[self.service]
        if self._pending() is not None:
            self._restore_pending()
            return
        self._uncertain = False
        self.release_button.hide()
        self.verify_button.hide()
        self._cancel_source = 'Отменить' if self.kind == 'group' else 'Не создавать'
        self.cancel_button.setText(self._tr(self._cancel_source))
        self._enable_fields(True)
        self._show_page(0 if self.kind == 'group' else 1)
        self._message('Блокировка снята. Повторный запрос не отправлялся; можно создать новый чат.')

    def _open_confirmed(self):
        if not self._current():
            self._expire()
        elif self._confirmed_chat is not None and self._request is None:
            self.conversation_created.emit(self._confirmed_chat)
            if isValid(self) and not self._closed:
                self.accept()

    def _enable_fields(self, enabled):
        for widget in (self.member_search, self.member_list, self.title_input, self.description_input):
            widget.setEnabled(enabled)

    def _message(self, source):
        self._message_source = source
        self.result_label.setText(self._tr(source))
        self.result_label.show()

    def _expire(self):
        self._context = self._request = None
        if not isValid(self) or self._closed:
            return
        self._enable_fields(False)
        self.member_list.blockSignals(True)
        self.member_list.clear()
        self.member_list.blockSignals(False)
        self._member_ids.clear()
        for button in (self.next_button, self.back_button, self.create_button,
                       self.verify_button, self.open_button, self.release_button):
            button.setEnabled(False)
        self._message('Сессия изменилась. Откройте создание заново.')

    def _finished(self, _result):
        self._closed = True
        self._context = self._request = self._confirmed_chat = None
