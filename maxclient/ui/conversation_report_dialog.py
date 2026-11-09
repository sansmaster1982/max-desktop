"""Explicit reason selection for a captured channel/bot; no automatic reports."""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (QButtonGroup, QDialog, QDialogButtonBox, QLabel,
                              QPushButton, QRadioButton, QScrollArea, QVBoxLayout, QWidget)
from shiboken6 import isValid

from ..core.i18n import tr
from ..protocol.client import MaxError, MaxWriteNotStarted, MaxWriteOutcomeUnknown
from .async_runner import run_async


def report_available(snapshot):
    if not isinstance(snapshot, dict) or snapshot.get('action_pending') is True:
        return False
    caps = snapshot.get('action_capabilities', {})
    return (isinstance(caps, dict) and caps.get('supported') is True
            and caps.get('kind') in ('channel', 'bot')
            and 'report' in caps.get('more', [])
            and any(isinstance(row, dict) and row.get('action') == 'report'
                    and row.get('available') is True for row in snapshot.get('more_actions', [])))


class ConversationReportDialog(QDialog):
    completed = Signal(object)

    def __init__(self, service, context, snapshot, parent=None, *, language='ru',
                 context_is_current=None):
        super().__init__(parent)
        self.service, self.context, self.language = service, context, language
        self._scope = context_is_current or (lambda: True)
        self._lifetime = {'active': True}
        self._load_job = self._submit_job = None
        self._ticket = None
        self._attempted = False
        self._reasons = {}
        self._type_id = 2 if snapshot['action_capabilities']['kind'] == 'channel' else 7
        self._target_id = context.chat_id if self._type_id == 2 else snapshot.get('peer_id')
        self.setObjectName('ConversationReportDialog')
        self.setWindowTitle(tr('Пожаловаться на канал?' if self._type_id == 2 else 'Пожаловаться на бота?', language))
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.resize(440, 360)
        self.setMinimumWidth(360)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(16)
        self.title = QLabel(str(snapshot.get('title', '')))
        self.title.setTextFormat(Qt.TextFormat.PlainText)
        self.title.setWordWrap(True)
        layout.addWidget(self.title)
        subtitle = QLabel(tr('Вы также можете выбрать причину, чтобы\xa0отправить жалобу'
                              if self._type_id == 2 else 'Жалобу увидят только модераторы', language))
        subtitle.setTextFormat(Qt.TextFormat.PlainText)
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)
        self.status = QLabel(tr('Загрузка причин жалобы…', language))
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        contents = QWidget()
        self.reason_layout = QVBoxLayout(contents)
        self.reason_layout.setContentsMargins(0, 0, 0, 0)
        self.reason_layout.setSpacing(12)
        self.reason_layout.addStretch(1)
        scroll.setWidget(contents)
        layout.addWidget(scroll, 1)
        self.group = QButtonGroup(self)
        self.group.idToggled.connect(lambda *_args: self._update_submit())
        self.retry = QPushButton(tr('Повторить', language))
        self.retry.setAutoDefault(False)
        self.retry.clicked.connect(self.start_loading)
        self.retry.hide()
        layout.addWidget(self.retry)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok |
                                        QDialogButtonBox.StandardButton.Cancel)
        self.submit_button = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.submit_button.setText(tr('Отправить жалобу', language))
        self.submit_button.setAutoDefault(False)
        self.submit_button.setDefault(False)
        self.submit_button.setEnabled(False)
        self.cancel_button = self.buttons.button(QDialogButtonBox.StandardButton.Cancel)
        self.cancel_button.setText(tr('Отмена', language))
        self.cancel_button.setDefault(True)
        self.cancel_button.setFocus()
        self.buttons.accepted.connect(self.submit)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self._scope_timer = QTimer(self)
        self._scope_timer.setInterval(150)
        self._scope_timer.timeout.connect(self._check_scope)

    def _current(self):
        try:
            return (isValid(self) and self._lifetime['active']
                    and self.service.is_current_conversation(self.context) is True
                    and self._scope() is True)
        except (AttributeError, RuntimeError, ValueError):
            return False

    def _check_scope(self):
        if not self._current():
            self.reject()

    def showEvent(self, event):
        super().showEvent(event)
        self._scope_timer.start()
        self._check_scope()

    def start_loading(self):
        if (not self.isVisible() or not self._current() or self._load_job or self._attempted
                or self._ticket is not None):
            return
        request = self._load_job = object()
        self.retry.hide()
        self.status.setText(tr('Загрузка причин жалобы…', self.language))
        context, lifetime, service = self.context, self._lifetime, self.service

        def work():
            # The worker never reads Qt widgets or derives a new target/session.
            if not lifetime['active']:
                return None
            try:
                return service.get_conversation_report_reasons(context)
            except Exception:
                return None  # No raw server/account text reaches async error logs.

        def done(result=None):
            if not self._current() or self._load_job is not request or not self.isVisible():
                return
            self._load_job = None
            if not self._matching(result):
                self.status.setText(tr('Не удалось загрузить причины жалобы.', self.language))
                self.retry.show()
                return
            if result.get('pending') is True or result.get('already_reported') is True:
                phrase = ('Результат отправки не подтверждён. Повторная отправка остановлена.'
                          if result.get('pending') is True else 'Жалоба на этот чат уже отправлена.')
                self._terminal(phrase)
                return
            if result.get('available') is not True:
                self._terminal('Жалоба недоступна для текущих прав или состояния чата.')
                return
            reasons = result.get('reasons')
            if (not isinstance(reasons, list) or not reasons or result.get('ticket') is None
                    or any(not isinstance(row, dict) or type(row.get('reason_id')) is not int
                           or not isinstance(row.get('title'), str) or not row['title'].strip()
                           for row in reasons)
                    or len({row['reason_id'] for row in reasons}) != len(reasons)):
                self.status.setText(tr('Нет доступных причин жалобы.', self.language))
                self.retry.show()
                return
            self._ticket = result['ticket']
            for index, row in enumerate(reasons):
                if row['reason_id'] == 0:  # The reference submit requires a truthy ID.
                    continue
                self._reasons[index] = row['reason_id']
                button = QRadioButton(row['title'].replace('&', '&&'))
                self.group.addButton(button, index)
                self.reason_layout.insertWidget(self.reason_layout.count() - 1, button)
            self.status.setText(tr('Укажите причину жалобы' if self._reasons else
                                   'Нет доступных причин жалобы.', self.language))
            self._update_submit()

        run_async(work, on_done=done, on_error=lambda _error: done())

    def _matching(self, result):
        return (isinstance(result, dict) and result.get('context') is self.context
                and type(result.get('chat_id')) is int and result['chat_id'] == self.context.chat_id
                and type(result.get('type_id')) is int and result['type_id'] == self._type_id
                and type(result.get('target_id')) is int and result['target_id'] == self._target_id)

    def _update_submit(self):
        self.submit_button.setEnabled(not self._attempted and self._ticket is not None
                                      and self.group.checkedId() in self._reasons and self._current())

    def submit(self):
        if (not self.isVisible() or not self._current() or self._attempted
                or self._ticket is None or self.group.checkedId() not in self._reasons):
            return
        reason = self._reasons[self.group.checkedId()]
        self._attempted = True
        request = self._submit_job = object()
        ticket, context, lifetime, service = self._ticket, self.context, self._lifetime, self.service
        self._update_submit()
        for button in self.group.buttons():
            button.setEnabled(False)
        self.status.setText(tr('Отправка жалобы…', self.language))
        self.cancel_button.setText(tr('Закрыть', self.language))

        def work():
            if not lifetime['active']:
                return ('not_sent', None)
            try:
                result = service.submit_conversation_report(context, reason, ticket=ticket)
                return ('result', result)
            except MaxWriteOutcomeUnknown:
                return ('unknown', None)
            except (MaxWriteNotStarted, ValueError):
                return ('not_sent', None)
            except MaxError:
                return ('rejected', None)
            except Exception:
                return ('unknown', None)

        def done(envelope=None):
            if not self._current() or self._submit_job is not request or not self.isVisible():
                return
            self._submit_job = None
            status, result = envelope if isinstance(envelope, tuple) and len(envelope) == 2 else ('unknown', None)
            confirmed = (status == 'result' and self._matching(result)
                         and result.get('action') == 'report' and result.get('confirmed') is True
                         and result.get('pending') is False)
            if confirmed:
                self._terminal('Жалоба отправлена')
                self.completed.emit(result)
            else:
                phrase = ('Жалоба не отправлена. Откройте окно заново и проверьте причину.'
                          if status == 'not_sent' else 'MAX отклонил жалобу.' if status == 'rejected'
                          else 'Результат отправки не подтверждён. Повторная отправка остановлена.')
                self._terminal(phrase)
                self.completed.emit(None)

        run_async(work, on_done=done, on_error=lambda _error: done())

    def _terminal(self, phrase):
        self._attempted = True
        self.status.setText(tr(phrase, self.language))
        self.submit_button.setEnabled(False)
        self.retry.hide()
        self.cancel_button.setText(tr('Закрыть', self.language))

    def accept(self):
        # Never bypass the explicit selected-reason action through QDialog.accept.
        self.submit()

    def done(self, result):
        self._lifetime['active'] = False
        self._scope_timer.stop()
        self._load_job = self._submit_job = None
        self._ticket = None
        super().done(result)
