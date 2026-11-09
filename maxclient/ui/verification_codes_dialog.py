"""Read only the text MAX actually makes available in its official code chat."""
from __future__ import annotations

from datetime import datetime, timezone
import math

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication, QDialog, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QVBoxLayout
from shiboken6 import isValid

from ..core.i18n import normalize_language, tr
from .async_runner import run_async


class VerificationCodesDialog(QDialog):
    def __init__(self, service, parent=None, *, language='ru'):
        super().__init__(parent)
        self.service = service
        self._closed = False
        self._stale = False
        self._busy = False
        self._last_text = ''
        self._language = normalize_language(language)
        self._status_source = 'Проверка доступного служебного чата…'
        try:
            self._context = service._history_read_context()
        except (AttributeError, ValueError, RuntimeError):
            self._context = None
        self.resize(620, 430)
        layout = QVBoxLayout(self)
        self.title = QLabel()
        self.title.setTextFormat(Qt.TextFormat.PlainText)
        self.title.setStyleSheet('font-size:18px; font-weight:600;')
        layout.addWidget(self.title)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.status)
        self.messages = QPlainTextEdit()
        self.messages.setReadOnly(True)
        self.messages.setObjectName('AvailableVerificationMessages')
        layout.addWidget(self.messages, 1)
        buttons = QHBoxLayout()
        self.refresh_button = QPushButton()
        self.refresh_button.clicked.connect(self.refresh)
        buttons.addWidget(self.refresh_button)
        self.copy_button = QPushButton()
        self.copy_button.setEnabled(False)
        self.copy_button.clicked.connect(self._copy)
        buttons.addWidget(self.copy_button)
        self.close_button = QPushButton()
        self.close_button.clicked.connect(self.close)
        buttons.addWidget(self.close_button)
        layout.addLayout(buttons)
        self.retranslate(self._language)
        QTimer.singleShot(0, self.refresh)

    def retranslate(self, language):
        """Only app-owned labels; displayed messages and read state stay intact."""
        if not isValid(self) or (self._closed and not (self._stale and self.isVisible())):
            return
        self._language = normalize_language(language)
        self.setWindowTitle(tr('Коды подтверждения — MAX', self._language))
        self.title.setText(tr('Коды подтверждения', self._language))
        self.messages.setPlaceholderText(tr('Здесь появится доступное сообщение с серверным временем.', self._language))
        self.refresh_button.setText(tr('Проверить сообщения', self._language))
        self.copy_button.setText(tr('Копировать последнее сообщение', self._language))
        self.close_button.setText(tr('Закрыть', self._language))
        self.status.setText(tr(self._status_source, self._language))

    def _set_status(self, source):
        self._status_source = source
        self.status.setText(tr(source, self._language))

    def _current(self):
        if not isValid(self) or self._closed or self._context is None:
            return False
        try:
            self.service._require_history_context(self._context)
        except (AttributeError, ValueError, RuntimeError):
            return False
        return True

    def _read_messages(self):
        # This wrapper runs after queueing. The captured account/session must
        # still be current before entering the service's own guarded read.
        if not self._current():
            raise ValueError('verification viewer is no longer current')
        return self.service.verification_code_messages(context=self._context)

    def refresh(self):
        if not self._current():
            self._invalidate(stale=True)
            return
        if self._busy:
            return
        self._busy = True
        self.refresh_button.setEnabled(False)
        self.copy_button.setEnabled(False)
        self.messages.clear()
        self._last_text = ''
        self._set_status('Проверка доступного служебного чата…')
        try:
            run_async(self._read_messages, on_done=self._received, on_error=self._failed)
        except Exception:
            self._failed(None)

    def _received(self, result):
        if not self._current():
            self._invalidate(stale=True)
            return
        self._busy = False
        self.refresh_button.setEnabled(True)
        self._last_text = ''
        self.messages.clear()
        self.copy_button.setEnabled(False)
        status = result.get('status') if isinstance(result, dict) else None
        if status not in {'available', 'empty', 'chat_not_available'}:
            self._failed('invalid_result')
            return
        records = result.get('messages', []) if isinstance(result, dict) else []
        if not isinstance(records, list):
            self._failed('invalid_messages')
            return
        visible = [r for r in records if isinstance(r, dict) and _server_timestamp(r.get('time_ms'))
                   and isinstance(r.get('text'), str) and r['text'].strip()]
        visible.sort(key=lambda r: r['time_ms'])
        if status == 'available' and visible:
            rendered = []
            for message in visible:
                try:
                    stamp = datetime.fromtimestamp(message['time_ms'] / 1000, tz=timezone.utc).astimezone().strftime('%d.%m.%Y %H:%M:%S')
                except (OSError, OverflowError, ValueError):
                    continue
                rendered.append(stamp + '\n' + message['text'])
                self._last_text = message['text']
            self.messages.setPlainText('\n\n────────\n\n'.join(rendered))
            self.copy_button.setEnabled(bool(self._last_text))
            self._set_status('Доступные сообщения. Сверьте время сообщения со своим запросом входа.' if rendered
                                else 'Сообщения с подтверждённым серверным временем не найдены.')
        elif status == 'chat_not_available':
            self._set_status('Чат «Коды подтверждения» не доступен этой сессии. '
                                'Если настроено основное устройство MAX, проверьте код на нём или дождитесь SMS на номер аккаунта.')
        else:
            self._set_status('Служебный чат найден, но доступного сообщения с кодом пока нет. '
                                'Если код скрыт для этого устройства, проверьте основное устройство MAX или SMS.')

    def _failed(self, _message):
        if not self._current():
            self._invalidate(stale=True)
            return
        self._busy = False
        self.refresh_button.setEnabled(True)
        self.copy_button.setEnabled(False)
        self._set_status('Не удалось обновить служебный чат. Проверьте соединение и повторите проверку.')

    def _copy(self):
        if not self._current():
            self._invalidate(stale=True)
            return
        if self._last_text and self.copy_button.isEnabled():
            QApplication.clipboard().setText(self._last_text)

    def _invalidate(self, *, stale=False):
        if self._closed:
            return
        self._closed = True
        self._stale = stale
        self._busy = False
        self._last_text = ''
        if isValid(self):
            self.copy_button.setEnabled(False)
            self.refresh_button.setEnabled(False)
            self.messages.clear()
            if stale:
                self._set_status('Сессия недоступна. Закройте окно и откройте его снова.')

    def done(self, result):
        self._invalidate()
        if isValid(self):
            super().done(result)

    def reject(self):
        self.done(QDialog.DialogCode.Rejected)

    def closeEvent(self, event):
        self._invalidate()
        super().closeEvent(event)


def _server_timestamp(value):
    """Match the service's finite timestamp contract; do not guess an epoch."""
    if type(value) is int:
        return -(2 ** 63) <= value < 2 ** 63
    return type(value) is float and math.isfinite(value)
