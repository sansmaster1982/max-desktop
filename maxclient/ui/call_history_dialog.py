"""Read-only call log from the official structured 163 response."""
from __future__ import annotations

from datetime import datetime
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QPushButton, QVBoxLayout

from .async_runner import run_async


class CallHistoryDialog(QDialog):
    def __init__(self, service, parent=None):
        super().__init__(parent)
        self.service = service
        self._busy = False
        self._closed = False
        self.setWindowTitle('История звонков — MAX')
        self.resize(520, 540)
        layout = QVBoxLayout(self)
        title = QLabel('История звонков')
        title.setStyleSheet('font-size:18px;font-weight:600;')
        layout.addWidget(title)
        self.status = QLabel('Загрузка…')
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.calls = QListWidget()
        layout.addWidget(self.calls, 1)
        note = QLabel('Аудио- и видеозвонки пока доступны в официальном приложении MAX.')
        note.setWordWrap(True)
        note.setObjectName('ChatSubtitle')
        layout.addWidget(note)
        buttons = QHBoxLayout()
        self.refresh_button = QPushButton('Обновить')
        self.refresh_button.clicked.connect(self.refresh)
        buttons.addWidget(self.refresh_button)
        buttons.addStretch(1)
        close = QPushButton('Закрыть')
        close.clicked.connect(self.close)
        buttons.addWidget(close)
        layout.addLayout(buttons)
        QTimer.singleShot(0, self.refresh)

    def refresh(self):
        if self._closed or self._busy:
            return
        self._busy = True
        self.refresh_button.setEnabled(False)
        self.status.setText('Загрузка…')
        run_async(self.service.list_call_history, on_done=self._received, on_error=self._failed)

    def _received(self, result):
        if self._closed:
            return
        try:
            records = result['callHistoryItems']
            if not isinstance(records, list):
                raise ValueError('invalid history')
            rows = []
            for record in records:
                if not isinstance(record, dict):
                    raise ValueError('invalid item')
                if (type(record.get('time')) is not int or record['time'] <= 0
                        or type(record.get('callerId')) is not int
                        or type(record.get('chatId')) is not int
                        or record.get('callType') not in {'AUDIO', 'VIDEO'}):
                    raise ValueError('invalid fields')
                stamp = datetime.fromtimestamp(record['time'] / 1000).astimezone().strftime('%d.%m.%Y %H:%M')
                outgoing = record['callerId'] == self.service.my_id
                missed = not outgoing and record.get('hangupType', 'HUNGUP') != 'HUNGUP'
                direction = 'Исходящий' if outgoing else ('Пропущенный' if missed else 'Входящий')
                kind = 'Видеозвонок' if record['callType'] == 'VIDEO' else 'Аудиозвонок'
                chat = self.service.store.get_chat(record['chatId'])
                name = record.get('callName') or (chat.display if chat else kind)
                duration = record.get('durationMs')
                duration_text = ''
                if type(duration) in (int, float) and duration >= 0:
                    seconds = int(duration / 1000)
                    duration_text = f' · {seconds // 60}:{seconds % 60:02d}'
                rows.append((record['time'], str(name), f'{direction} · {kind}{duration_text}', stamp, record.get('historyId')))
        except (KeyError, TypeError, ValueError, OverflowError, OSError):
            self._failed('invalid response')
            return
        self.calls.clear()
        for _time, name, details, stamp, identity in sorted(rows, key=lambda row: row[0], reverse=True):
            item = QListWidgetItem(f'{name}\n{details}\n{stamp}')
            item.setData(Qt.ItemDataRole.UserRole, identity)
            self.calls.addItem(item)
        self._busy = False
        self.refresh_button.setEnabled(True)
        self.status.setText(f'Звонков: {len(rows)}' if rows else 'История звонков пуста.')

    def _failed(self, _error):
        if self._closed:
            return
        self._busy = False
        self.refresh_button.setEnabled(True)
        self.status.setText('Не удалось обновить историю звонков. Попробуйте позже.')

    def closeEvent(self, event):
        self._closed = True
        super().closeEvent(event)
