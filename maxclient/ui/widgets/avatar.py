"""Round contact photos with deterministic initials as the offline fallback."""
from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit
import weakref

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QRectF, QSize, Qt, QUrl
from PySide6.QtGui import QBrush, QColor, QFont, QImageReader, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest
from PySide6.QtWidgets import QApplication, QWidget
from shiboken6 import isValid

from .common import avatar_colors, initials

_NETWORK = None
_PHOTO_CACHE: dict[str, QPixmap] = {}
_MAX_PHOTO_BYTES = 2 * 1024 * 1024


class Avatar(QWidget):
    def __init__(self, size: int = 46, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._size = size
        self._initials = "#"
        self._identity = None
        self._c1 = "#4FC3F7"
        self._c2 = "#2B7FFF"
        self._photo = QPixmap()
        self._photo_source = ''
        self._online: bool | None = None
        self.setFixedSize(size, size)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setObjectName('Avatar')

    def set_identity(self, name: str | None, key=None) -> None:
        identity = key if key is not None else name
        if identity != self._identity:
            self._photo = QPixmap()
            self._photo_source = ''
        self._identity = identity
        self._initials = initials(name)
        self._c1, self._c2 = avatar_colors(identity)
        self.setAccessibleName(name or 'Аватар')
        self.update()

    def set_online(self, online: bool | None) -> None:
        self._online = online
        self.update()

    def cached_photo(self) -> QPixmap:
        """Return the already-loaded image only; never trigger a new request."""
        return QPixmap(self._photo)

    def set_photo(self, source=None) -> None:
        """Load a supplied image; HTTPS requests never carry session credentials."""
        if isinstance(source, QPixmap):
            self._photo = source
            self._photo_source = ''
            self.update()
            return
        source = str(source or '')
        if source == self._photo_source:
            return
        self._photo_source = source
        self._photo = QPixmap()
        if not source:
            self.update()
            return
        cached = _PHOTO_CACHE.get(source)
        if cached is not None:
            self._photo = cached
            self.update()
            return
        parts = urlsplit(source)
        if not parts.scheme or (len(parts.scheme) == 1 and len(source) > 2 and source[1] == ':'):
            path = Path(source)
            try:
                if path.is_file() and path.stat().st_size <= _MAX_PHOTO_BYTES:
                    self._decode_photo(path.read_bytes(), source)
            except OSError:
                pass
            return
        if parts.scheme != 'https' or not parts.hostname or parts.username or parts.password:
            return
        global _NETWORK
        if _NETWORK is None:
            _NETWORK = QNetworkAccessManager(QApplication.instance())
        request = QNetworkRequest(QUrl(source))
        request.setAttribute(QNetworkRequest.Attribute.RedirectPolicyAttribute,
                             QNetworkRequest.RedirectPolicy.NoLessSafeRedirectPolicy)
        request.setTransferTimeout(12000)
        reply = _NETWORK.get(request)
        reference = weakref.ref(self)
        reply.downloadProgress.connect(lambda received, _total: reply.abort() if received > _MAX_PHOTO_BYTES else None)

        def received():
            widget = reference()
            if widget is not None and isValid(widget) and widget._photo_source == source:
                if reply.error() == QNetworkReply.NetworkError.NoError and reply.bytesAvailable() <= _MAX_PHOTO_BYTES:
                    widget._decode_photo(bytes(reply.readAll()), source)
            reply.deleteLater()

        reply.finished.connect(received)
        self.update()

    def _decode_photo(self, data: bytes, source: str) -> None:
        buffer = QBuffer()
        buffer.setData(QByteArray(data))
        buffer.open(QIODevice.OpenModeFlag.ReadOnly)
        reader = QImageReader(buffer)
        reader.setAutoTransform(True)
        if bytes(reader.format()).lower() not in {b'png', b'jpeg', b'jpg', b'webp', b'gif', b'bmp'}:
            return
        original = reader.size()
        if original.isValid():
            reader.setScaledSize(original.scaled(QSize(self._size * 2, self._size * 2), Qt.AspectRatioMode.KeepAspectRatioByExpanding))
        image = reader.read()
        if image.isNull():
            return
        self._photo = QPixmap.fromImage(image)
        if source:
            if len(_PHOTO_CACHE) >= 160:
                _PHOTO_CACHE.pop(next(iter(_PHOTO_CACHE)))
            _PHOTO_CACHE[source] = self._photo
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt API)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(0.5, 0.5, self._size - 1, self._size - 1)

        if not self._photo.isNull():
            path = QPainterPath()
            path.addEllipse(rect)
            painter.save()
            painter.setClipPath(path)
            photo = self._photo.scaled(self._size, self._size, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                                       Qt.TransformationMode.SmoothTransformation)
            painter.drawPixmap((self._size - photo.width()) // 2, (self._size - photo.height()) // 2, photo)
            painter.restore()
        else:
            grad = QLinearGradient(0, 0, 0, self._size)
            grad.setColorAt(0, QColor(self._c1))
            grad.setColorAt(1, QColor(self._c2))
            painter.setBrush(QBrush(grad))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawEllipse(rect)
            painter.setPen(QColor('#FFFFFF'))
            font = QFont(self.font())
            font.setPixelSize(int(self._size * 0.39))
            font.setWeight(QFont.Weight.Medium)
            painter.setFont(font)
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, self._initials)

        if self._online is True:
            radius = max(4.0, self._size * 0.105)
            painter.setBrush(QColor('#21C459'))
            painter.setPen(QPen(QColor('white'), 2))
            painter.drawEllipse(QRectF(self._size - radius * 2 - 1, self._size - radius * 2 - 1, radius * 2, radius * 2))
        painter.end()
