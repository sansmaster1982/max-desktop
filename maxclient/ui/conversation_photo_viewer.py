"""Session-bound photo viewer for actual Info attachment URLs only."""
from threading import Event

from PySide6.QtCore import Qt, Signal, QTimer, QSize, QSaveFile, QIODevice
from PySide6.QtGui import QPixmap, QPainter
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QScrollArea, QVBoxLayout, QWidget, QFileDialog
from shiboken6 import isValid

from .. import __app_name__
from ..core.i18n import normalize_language, tr
from ..core.media_fetch import fetch_image, image_url, IMAGE_MAX_BYTES
from .async_runner import run_async
from .conversation_media_loader import decode_image


class _PhotoCanvas(QWidget):
    """Paint only the visible viewport; zoom never allocates an enlarged image."""
    def __init__(self):
        super().__init__()
        self._image = QPixmap()
        self._text = ''

    def setText(self, text):
        self._image = QPixmap()
        self._text = text
        self.update()

    def setPixmap(self, pixmap):
        self._image = pixmap
        self._text = ''
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        if not self._image.isNull():
            painter.drawPixmap(self.rect(), self._image)
        else:
            painter.setPen(Qt.GlobalColor.white)
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self._text)


class ConversationPhotoViewer(QDialog):
    message_requested = Signal(object, object)

    def __init__(self, rows, selected_key, context, is_current, parent=None, *, language='ru'):
        super().__init__(parent)
        self._language = normalize_language(language)
        self._canvas_source = 'Загрузка…'
        self._status_source = None
        self._status_text = ''
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self._context, self._is_current = context, is_current
        self._rows = [dict(row) for row in rows if isinstance(row, dict) and row.get('kind') == 'PHOTO'
                      and row.get('available') is True and image_url(row.get('open_url')) is not None]
        self._index = next((index for index, row in enumerate(self._rows) if row.get('key') == selected_key), -1)
        self._cancel = Event()
        self._closed = False
        self._pixmap = QPixmap()
        self._fit = True
        self._zoom = 1.0
        self.resize(960, 720)
        self.setMinimumSize(420, 320)
        self.setStyleSheet('QDialog{background:#111317;color:#FFFFFF;} QLabel{background:transparent;color:#FFFFFF;}'
                          'QPushButton{background:#292D34;color:white;border:0;border-radius:8px;padding:9px;}'
                          'QPushButton:disabled{color:#858B96;} QScrollArea{background:#111317;border:0;}')
        root = QVBoxLayout(self)
        controls = QHBoxLayout()
        self.previous = QPushButton('‹')
        self.previous.setAccessibleName('Предыдущее фото')
        self.next = QPushButton('›')
        self.next.setAccessibleName('Следующее фото')
        self.counter = QLabel()
        self.counter.setTextFormat(Qt.TextFormat.PlainText)
        self.jump = QPushButton('К сообщению')
        self.retry = QPushButton('Повторить загрузку')
        self.save_button = QPushButton('Сохранить фото')
        self.save_button.setEnabled(False)
        close = self.close_button = QPushButton('Закрыть')
        for widget in (self.previous, self.next, self.counter): controls.addWidget(widget)
        controls.addStretch()
        for widget in (self.retry, self.save_button, self.jump, close): controls.addWidget(widget)
        root.addLayout(controls)
        self.scroller = QScrollArea()
        self.scroller.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.picture = _PhotoCanvas()
        self.picture.setText('Загрузка…')
        self.scroller.setWidget(self.picture)
        root.addWidget(self.scroller, 1)
        footer = QHBoxLayout()
        self.status = QLabel()
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        footer.addWidget(self.status, 1)
        for title, callback in (('−', lambda: self.zoom_by(-0.2)),
                                ('По размеру окна', self.fit_image), ('+', lambda: self.zoom_by(0.2))):
            button = QPushButton(title)
            if title == 'По размеру окна':
                self.fit_button = button
            button.clicked.connect(callback)
            footer.addWidget(button)
        root.addLayout(footer)
        self.previous.clicked.connect(lambda: self.select(self._index - 1))
        self.next.clicked.connect(lambda: self.select(self._index + 1))
        self.jump.clicked.connect(self._jump)
        self.retry.clicked.connect(lambda: self.select(self._index))
        self.save_button.clicked.connect(self.save_image)
        close.clicked.connect(self.close)
        self._clock = QTimer(self)
        self._clock.setInterval(250)
        self._clock.timeout.connect(self._check_session)
        self._clock.start()
        self.destroyed.connect(lambda: self._cancel.set())
        self.retranslate()
        self.select(self._index)

    def retranslate(self, language=None):
        """Relabel the current presentation without selecting/fetching an image."""
        if not isValid(self) or self._closed:
            return
        if language is not None:
            self._language = normalize_language(language)
        self.setWindowTitle(tr('Фото — {app}', self._language, app=__app_name__))
        self.previous.setAccessibleName(tr('Предыдущее фото', self._language))
        self.next.setAccessibleName(tr('Следующее фото', self._language))
        for button, source in ((self.jump, 'К сообщению'), (self.retry, 'Повторить загрузку'),
                               (self.save_button, 'Сохранить фото'), (self.close_button, 'Закрыть'),
                               (self.fit_button, 'По размеру окна')):
            button.setText(tr(source, self._language))
        if self._canvas_source is not None:
            # setText clears canvas pixels: never call it for a loaded image.
            self.picture.setText(tr(self._canvas_source, self._language))
        self._render_status()

    def _set_status(self, *, source=None, text=''):
        self._status_source = source
        self._status_text = text
        self._render_status()

    def _render_status(self):
        self.status.setText(tr(self._status_source, self._language)
                            if self._status_source is not None else self._status_text)

    def _current(self):
        return isValid(self) and not self._closed and self._is_current(self._context)

    def _check_session(self):
        if not self._current():
            self.close()

    def select(self, index):
        if not self._current() or type(index) is not int or not 0 <= index < len(self._rows):
            return
        self._cancel.set()
        cancel = self._cancel = Event()
        self._index = index
        self._pixmap = QPixmap()
        self.save_button.setEnabled(False)
        self._fit = True
        self._zoom = 1.0
        self._canvas_source = 'Загрузка…'
        self.picture.setText(tr(self._canvas_source, self._language))
        self.picture.resize(180, 60)
        self.counter.setText(f'{index + 1} / {len(self._rows)}')
        self.previous.setEnabled(index > 0)
        self.next.setEnabled(index + 1 < len(self._rows))
        self.retry.hide()
        self._set_status()

        def done(data):
            if not self._current() or cancel is not self._cancel:
                return
            pixmap = decode_image(data)
            if pixmap.isNull():
                failed(None)
                return
            self._pixmap = pixmap
            self._canvas_source = None
            self.save_button.setEnabled(True)
            self._set_status(text=f'{pixmap.width()} × {pixmap.height()}')
            self._render()

        def failed(_error):
            if self._current() and cancel is self._cancel:
                self._canvas_source = 'Не удалось открыть фото'
                self.picture.setText(tr(self._canvas_source, self._language))
                self.picture.resize(260, 60)
                self._set_status(source='Повторите загрузку или перейдите к исходному сообщению.')
                self.retry.show()

        run_async(fetch_image, self._rows[index]['open_url'], cancelled=cancel,
                  max_bytes=IMAGE_MAX_BYTES, on_done=done, on_error=failed)

    def fit_image(self):
        self._fit = True
        self._zoom = 1.0
        self._render()

    def save_image(self):
        if not self._current() or self._pixmap.isNull():
            return
        selected = self._index
        path, _ = QFileDialog.getSaveFileName(self, tr('Сохранить фото', self._language), 'photo.png', 'PNG (*.png)')
        if not path or not self._current() or self._index != selected:
            return
        output = QSaveFile(path)
        if not output.open(QIODevice.OpenModeFlag.WriteOnly):
            self._set_status(source='Не удалось сохранить фото.')
            return
        if self._pixmap.save(output, 'PNG') and output.commit():
            self._set_status(source='Фото сохранено.')
        else:
            output.cancelWriting()
            self._set_status(source='Не удалось сохранить фото.')

    def zoom_by(self, delta):
        if self._pixmap.isNull():
            return
        self._fit = False
        self._zoom = max(1.0, min(4.0, self._zoom + delta))
        self._render()

    def _render(self):
        if not self._current() or self._pixmap.isNull():
            return
        base = self._pixmap.size().scaled(self.scroller.viewport().size(), Qt.AspectRatioMode.KeepAspectRatio)
        target = QSize(max(1, round(base.width() * self._zoom)), max(1, round(base.height() * self._zoom)))
        self.picture.setPixmap(self._pixmap)
        self.picture.resize(target)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, '_pixmap'):
            self._render()

    def _jump(self):
        if self._current() and 0 <= self._index < len(self._rows):
            row = dict(self._rows[self._index])
            context = self._context
            self.close()
            self.message_requested.emit(row, context)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_0 and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.fit_image()
        elif event.key() == Qt.Key.Key_Left:
            self.select(self._index - 1)
        elif event.key() == Qt.Key.Key_Right:
            self.select(self._index + 1)
        else:
            super().keyPressEvent(event)

    def closeEvent(self, event):
        self._closed = True
        self._cancel.set()
        self._clock.stop()
        super().closeEvent(event)
