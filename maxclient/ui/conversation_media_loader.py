"""Small per-card thumbnail queue; stale sessions never receive image pixels."""
from collections import deque
from threading import Event

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QObject, Signal, Qt
from PySide6.QtGui import QImageReader, QPixmap
from shiboken6 import isValid

from ..core.media_fetch import fetch_image, image_url
from .async_runner import run_async


def decode_image(data):
    if not isinstance(data, bytes) or not data:
        return QPixmap()
    source = QBuffer()
    source.setData(QByteArray(data))
    source.open(QIODevice.OpenModeFlag.ReadOnly)
    reader = QImageReader(source)
    reader.setAutoTransform(True)
    size = reader.size()
    if (not 0 < size.width() <= 16384 or not 0 < size.height() <= 16384
            or size.width() * size.height() > 16_000_000):
        return QPixmap()
    return QPixmap.fromImage(reader.read())


class ConversationMediaLoader(QObject):
    ready = Signal(object, str, str, object)
    failed = Signal(object, str)

    def __init__(self, is_current, parent=None):
        super().__init__(parent)
        self._is_current = is_current
        self._context = None
        self._cancel = Event()
        self._pending = deque()
        self._seen = set()
        self._active = 0
        self.destroyed.connect(lambda: self._cancel.set())

    def bind(self, context):
        if context is self._context:
            return
        self._cancel.set()
        self._cancel = Event()
        self._context = context
        self._pending.clear()
        self._seen.clear()
        self._active = 0

    def _current(self, context, cancel):
        return (isValid(self) and context is not None and context is self._context
                and cancel is self._cancel and not cancel.is_set() and self._is_current(context))

    def request(self, item, context):
        cancel = self._cancel
        if not self._current(context, cancel) or not isinstance(item, dict):
            return
        key, url = item.get('key'), item.get('thumbnail_url')
        if (not isinstance(key, str) or not key or image_url(url) is None
                or (key, url) in self._seen or len(self._pending) >= 128):
            return
        self._seen.add((key, url))
        self._pending.append((key, url))
        self._pump(context, cancel)

    def _pump(self, context, cancel):
        while self._current(context, cancel) and self._active < 4 and self._pending:
            key, url = self._pending.popleft()
            self._active += 1

            def done(data, key=key, url=url):
                if not self._current(context, cancel):
                    return
                self._active -= 1
                self._seen.discard((key, url))
                pixmap = decode_image(data)
                if pixmap.isNull():
                    self.failed.emit(context, key)
                else:
                    if pixmap.width() > 512 or pixmap.height() > 512:
                        pixmap = pixmap.scaled(512, 512, Qt.AspectRatioMode.KeepAspectRatio,
                                               Qt.TransformationMode.SmoothTransformation)
                    self.ready.emit(context, key, url, pixmap)
                self._pump(context, cancel)

            def failed(_error, key=key, url=url):
                if not self._current(context, cancel):
                    return
                self._active -= 1
                self._seen.discard((key, url))
                self.failed.emit(context, key)
                self._pump(context, cancel)

            run_async(fetch_image, url, cancelled=cancel, on_done=done, on_error=failed)
