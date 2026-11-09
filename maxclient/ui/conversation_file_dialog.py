"""File-download presentation only. Controller owns network and filesystem IO."""
from __future__ import annotations

from html import escape
import math

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QProgressBar, QPushButton, QVBoxLayout
from shiboken6 import isValid

from ..core.i18n import normalize_language, tr
from .theme import palette_for
from .widgets.chat_list_item import _ElidedLabel


_STATES = {'connecting': 'Подключение…', 'downloading': 'Скачивание…',
           'saving': 'Сохранение файла…', 'done': 'Файл сохранён',
           'error': 'Не удалось скачать файл', 'cancelled': 'Скачивание отменено',
           'cancelling': 'Отмена…'}
_TERMINAL = frozenset({'done', 'error', 'cancelled'})
_PHASES = {'connecting': 0, 'downloading': 1, 'saving': 2}


def _field(descriptor, key):
    # Exact agreed descriptor attributes, no filename/size aliases or asdict()
    # traversal through the account context.
    return descriptor.get(key) if isinstance(descriptor, dict) else getattr(descriptor, key, None)


def _bytes(value):
    return type(value) is int and 0 <= value < 2**63


def format_size(value, language='ru'):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        return ''
    amount = value
    units = ('Б', 'КБ', 'МБ', 'ГБ', 'ТБ')
    index = 0
    while amount >= 1024 and index < len(units) - 1:
        amount /= 1024
        index += 1
    unit = tr(units[index], language)
    return f'{amount:.0f} {unit}' if index == 0 else f'{amount:.1f} {unit}'


def _plain(text='', name=''):
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    if name:
        label.setObjectName(name)
    return label


def _style(theme):
    p = palette_for(theme)
    secondary = '#737983' if p.name == 'light' else '#A3ADBF'
    return f'''
        QDialog#FileDownloadDialog,QDialog#UnsafeFileDownloadDialog {{background:{p.surface};color:{p.text};}}
        QLabel {{background:transparent;color:{p.text};font-size:14px;}}
        #FileHeading {{font-size:18px;font-weight:600;}}
        #FileName {{font-size:15px;font-weight:600;}}
        #FileSize,#FileProgressText {{font-size:13px;color:{secondary};}}
        QPushButton {{background:{p.surface_hi};color:{p.text};border:0;border-radius:10px;padding:10px 16px;min-height:18px;}}
        QPushButton:hover {{background:{p.hover};}}
        QPushButton:focus {{border:1px solid #0085FF;}}
        QPushButton:disabled {{color:{secondary};}}
        #FileMessageButton {{background:transparent;color:#0085FF;}}
        #FileSafeCancelButton {{background:#0085FF;color:white;}}
        QProgressBar {{background:{p.surface_hi};border:0;border-radius:4px;min-height:8px;max-height:8px;}}
        QProgressBar::chunk {{background:#0085FF;border-radius:4px;}}
    '''


class FileDownloadDialog(QDialog):
    """A nonmodal progress window; connect controller signals to the Qt slots.

    Queued Qt signal connections to these @Slot methods are disconnected by Qt
    on receiver destruction. A controller must not retain widget-calling lambdas.
    """
    cancel_requested = Signal()
    message_requested = Signal(object)  # exact original descriptor

    def __init__(self, descriptor, parent=None, *, theme='light', language='ru'):
        super().__init__(parent)
        filename = _field(descriptor, 'file_name')
        if not isinstance(filename, str):
            raise ValueError('File descriptor requires file_name')
        self.descriptor = descriptor
        self._language = normalize_language(language)
        self._display_size = _field(descriptor, 'size')
        self._unsafe_dialog = None
        self._state = 'connecting'
        self._closed = False
        self._cancel_pending = False
        self._received = 0
        self._total = None
        self._has_progress = False
        self.setObjectName('FileDownloadDialog')
        self.setWindowTitle('Скачивание файла')
        self.setModal(False)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setMinimumWidth(320)
        self.resize(480, 264)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 20)
        layout.setSpacing(14)
        self.heading = _plain('Скачивание файла', 'FileHeading')
        layout.addWidget(self.heading)
        self.filename_label = _ElidedLabel()
        self.filename_label.setObjectName('FileName')
        self.filename_label.setText(filename)
        self.filename_label.setToolTip('<qt>' + escape(filename) + '</qt>')
        self.filename_label.setAccessibleName(filename)
        layout.addWidget(self.filename_label)
        self.size_label = _plain('', 'FileSize')
        layout.addWidget(self.size_label)
        self.status_label = _plain(_STATES[self._state], 'FileStatus')
        layout.addWidget(self.status_label)
        self.progress_bar = QProgressBar()
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setAccessibleName('Прогресс скачивания файла')
        layout.addWidget(self.progress_bar)
        self.progress_label = _plain('', 'FileProgressText')
        layout.addWidget(self.progress_label)
        buttons = QHBoxLayout()
        self.message_button = QPushButton('К сообщению')
        self.message_button.setObjectName('FileMessageButton')
        self.message_button.setAutoDefault(False)
        self.message_button.setEnabled(all(type(_field(descriptor, key)) is int for key in ('chat_id', 'message_id')))
        self.message_button.clicked.connect(self._message)
        buttons.addWidget(self.message_button)
        buttons.addStretch(1)
        self.cancel_button = QPushButton('Отменить')
        self.cancel_button.setAutoDefault(False)
        self.cancel_button.clicked.connect(self._cancel_or_close)
        buttons.addWidget(self.cancel_button)
        layout.addLayout(buttons)
        self.apply_theme(theme)
        self.set_language(self._language)

    def set_language(self, language):
        """Relabel only: never replay a progress/state transition or cancel."""
        if not isValid(self) or self._closed:
            return False
        self._language = normalize_language(language)
        self.setWindowTitle(tr('Скачивание файла', self._language))
        self.heading.setText(self.windowTitle())
        self.size_label.setText(format_size(self._display_size, self._language)
                                or tr('Размер не передан', self._language))
        self.progress_bar.setAccessibleName(tr('Прогресс скачивания файла', self._language))
        self.message_button.setText(tr('К сообщению', self._language))
        self.cancel_button.setText(tr('Закрыть' if self._state in _TERMINAL else 'Отменить', self._language))
        self._render_status()
        self._render_progress_text()
        warning = self._unsafe_dialog
        if warning is not None and isValid(warning):
            warning.set_language(self._language)
        return True

    retranslate = set_language

    def _render_status(self):
        state = 'cancelling' if self._cancel_pending and self._state not in _TERMINAL else self._state
        self.status_label.setText(tr(_STATES[state], self._language))

    def _render_progress_text(self):
        if not self._has_progress:
            text = ''  # Initial empty is distinct from an observed zero bytes.
        elif self._total is None:
            text = format_size(self._received, self._language)
        else:
            text = tr('{received} из {total}', self._language,
                      received=format_size(self._received, self._language),
                      total=format_size(self._total, self._language))
        self.progress_label.setText(text)

    def apply_theme(self, theme):
        if not isValid(self) or self._closed:
            return False
        self.setStyleSheet(_style(theme))
        self.filename_label._fit()
        # The application font can need more than QSS's 18px content minimum.
        # Respect the polished button hint when a short window compresses rows.
        for button in (self.message_button, self.cancel_button):
            button.ensurePolished()
            button.setMinimumHeight(button.sizeHint().height())
        return True

    @Slot(object, object)
    def set_progress(self, received, total=None):
        if (not isValid(self) or self._closed or self._cancel_pending or self._state in _TERMINAL
                or self._state == 'saving' or not _bytes(received)
                or total is not None and not _bytes(total)
                or received < self._received or total is not None and received > total):
            return False
        self._received, self._total = received, total
        self._has_progress = True
        self._state = 'downloading'
        self._render_status()
        if total is None:
            self.progress_bar.setRange(0, 0)
        else:
            # Native progress controls are int32; bytes remain exact Python ints.
            self.progress_bar.setRange(0, 1000)
            self.progress_bar.setValue(received * 1000 // total if total else 0)
        self._render_progress_text()
        return True

    @Slot(str)
    def set_status(self, state):
        if (not isValid(self) or self._closed or not isinstance(state, str) or state not in _STATES or self._state in _TERMINAL
                or self._cancel_pending and state not in _TERMINAL and state != 'cancelling'
                or state in _PHASES and _PHASES[state] < _PHASES.get(self._state, 0)):
            return False
        self._state = state
        if state == 'cancelling':
            self._cancel_pending = True
            self.cancel_button.setEnabled(False)
        if state in _TERMINAL:
            self.cancel_button.setText(tr('Закрыть', self._language))
            self.cancel_button.setEnabled(True)
            self.progress_bar.setRange(0, 1000)
            if state == 'done':
                self.progress_bar.setValue(1000)
            elif self._total is None:
                self.progress_bar.setValue(0)
            self._cancel_pending = False
        self._render_status()
        return True

    def _message(self):
        if not self._closed and self.message_button.isEnabled():
            self.message_requested.emit(self.descriptor)

    def _request_cancel(self):
        if not self._closed and self._state not in _TERMINAL and not self._cancel_pending:
            self._cancel_pending = True
            self.cancel_button.setEnabled(False)
            self._render_status()
            self.cancel_requested.emit()

    def _cancel_or_close(self):
        if self._state in _TERMINAL:
            self.close()
        else:
            self._request_cancel()

    def _mark_closed(self):
        self._request_cancel()
        self._closed = True

    def closeEvent(self, event):  # noqa: N802
        self._mark_closed()
        if isValid(self):
            super().closeEvent(event)

    def reject(self):
        self._mark_closed()
        if isValid(self):
            super().reject()

    @staticmethod
    def confirm_download(descriptor, parent=None, *, theme='light', language='ru'):
        return confirm_download(descriptor, parent, theme=theme, language=language)


class UnsafeFileDownloadDialog(QDialog):
    """Call only for an actual unsafe ACK. Cancel is both default and Escape."""
    def __init__(self, descriptor, parent=None, *, theme='light', language='ru'):
        super().__init__(parent)
        filename = _field(descriptor, 'file_name')
        if not isinstance(filename, str):
            raise ValueError('File descriptor requires file_name')
        self._language = normalize_language(language)
        self._closed = False
        self.setObjectName('UnsafeFileDownloadDialog')
        self.setWindowTitle('Скачивание файла')
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.setMinimumWidth(320)
        self.resize(480, 255)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 20)
        layout.setSpacing(16)
        self.heading = _plain('Файл может быть небезопасен', 'FileHeading')
        layout.addWidget(self.heading)
        self.filename_label = _ElidedLabel()
        self.filename_label.setObjectName('FileName')
        self.filename_label.setText(filename)
        self.filename_label.setToolTip('<qt>' + escape(filename) + '</qt>')
        self.filename_label.setAccessibleName(filename)
        layout.addWidget(self.filename_label)
        self.body = _plain()
        layout.addWidget(self.body)
        buttons = QHBoxLayout()
        self.proceed_button = QPushButton('Всё равно скачать')
        self.proceed_button.setAutoDefault(False)
        self.proceed_button.clicked.connect(self.accept)
        buttons.addWidget(self.proceed_button)
        self.cancel_button = QPushButton('Отмена')
        self.cancel_button.setObjectName('FileSafeCancelButton')
        self.cancel_button.setDefault(True)
        self.cancel_button.setAutoDefault(True)
        self.cancel_button.clicked.connect(self.reject)
        buttons.addWidget(self.cancel_button)
        layout.addLayout(buttons)
        self.setStyleSheet(_style(theme))
        self.filename_label._fit()
        self.finished.connect(self._finished)
        self.set_language(self._language)
        self.cancel_button.setFocus()

    def set_language(self, language):
        if not isValid(self) or self._closed:
            return False
        self._language = normalize_language(language)
        self.setWindowTitle(tr('Скачивание файла', self._language))
        self.heading.setText(tr('Файл может быть небезопасен', self._language))
        self.body.setText(tr('MAX предупредил о возможной опасности файла. '
                             'Скачивайте его, только если доверяете отправителю.', self._language))
        self.proceed_button.setText(tr('Всё равно скачать', self._language))
        self.cancel_button.setText(tr('Отмена', self._language))
        return True

    retranslate = set_language

    def _finished(self, _result):
        self._closed = True


def confirm_download(descriptor, parent=None, *, theme='light', language='ru'):
    dialog = UnsafeFileDownloadDialog(descriptor, parent, theme=theme, language=language)
    # Explicit ownership permits a live locale change inside the modal loop.
    # No widget traversal and no second confirmation call are needed.
    bound_parent = parent if isinstance(parent, FileDownloadDialog) and isValid(parent) else None
    if bound_parent is not None:
        bound_parent._unsafe_dialog = dialog
    try:
        return dialog.exec() == QDialog.DialogCode.Accepted
    finally:
        if bound_parent is not None and bound_parent._unsafe_dialog is dialog:
            bound_parent._unsafe_dialog = None
        if isValid(dialog):
            dialog.deleteLater()
