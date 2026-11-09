"""Visible PHOTO previews using supplied URLs and an exact timeline/session binding."""
import os

from PySide6.QtCore import QObject, QPoint, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QPainter, QPixmap
from PySide6.QtWidgets import QWidget
from shiboken6 import isValid

from ..core.media_fetch import image_url
from ..core.i18n import normalize_language, tr
from .conversation_media_loader import ConversationMediaLoader


def photo_source(attach):
    """The Web PHOTO constructor uses the supplied baseUrl as its original URL."""
    if (attach.type != 'PHOTO' or attach.deleted is not False
            or attach.local_path and os.path.isfile(attach.local_path)):
        return None
    return image_url(attach.base_url or attach.url)


def photo_identity(message, index, attach):
    return (id(message), (type(message.chat_id), message.chat_id), (type(message.id), message.id),
            (type(message.time_ms), message.time_ms), message.display_source,
            id(message.forwarded), index, id(attach), attach.type, attach.url,
            attach.base_url, attach.local_path, attach.deleted, attach.width, attach.height)


class PhotoPreview(QWidget):
    """A stable-size preview: late pixels never move the user's scroll position."""
    clicked = Signal()

    def __init__(self, attach, foreground, parent=None, *, language='ru'):
        super().__init__(parent)
        self._language = normalize_language(language)
        self.setObjectName('TimelinePhotoPreview')
        self.setAccessibleName('Фото')
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._pixmap = QPixmap()
        self._foreground = foreground
        self._text_source = 'Загрузка фото…' if photo_source(attach) else 'Фото недоступно'
        self._text = ''
        width, height = attach.width, attach.height
        size = (QSize(width, height).scaled(300, 240, Qt.AspectRatioMode.KeepAspectRatio)
                if type(width) is int and type(height) is int and 0 < width <= 16384
                and 0 < height <= 16384 else QSize(260, 180))
        self.setFixedSize(max(100, size.width()), max(90, size.height()))
        self.retranslate()

    def retranslate(self, language=None):
        if not isValid(self):
            return
        if language is not None:
            self._language = normalize_language(language)
        self.setAccessibleName(tr('Фото', self._language))
        self._text = tr(self._text_source, self._language) if self._text_source is not None else ''
        self.update()

    def set_photo(self, pixmap):
        if isinstance(pixmap, QPixmap) and not pixmap.isNull():
            self._pixmap = pixmap
            self._text_source = None
            self._text = ''
            self.update()

    def set_failed(self):
        self._text_source = 'Не удалось загрузить фото\nНажмите, чтобы открыть'
        self._text = tr(self._text_source, self._language)
        self.update()

    def set_waiting(self, active):
        if self._pixmap.isNull():
            source = 'Загрузка фото…' if active else 'Фото пока недоступно'
            if self._text_source != source:
                self._text_source = source
                self._text = tr(source, self._language)
                self.update()

    def set_unavailable(self):
        self._pixmap = QPixmap()
        self.set_waiting(False)
        self.update()

    def paintEvent(self, event):  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        if not self._pixmap.isNull():
            size = self._pixmap.size().scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio)
            rect = QRect(QPoint((self.width() - size.width()) // 2,
                                (self.height() - size.height()) // 2), size)
            painter.drawPixmap(rect, self._pixmap)
        else:
            from PySide6.QtGui import QColor
            painter.setPen(QColor(self._foreground))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self._text)

    def mouseReleaseEvent(self, event):  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(event.position().toPoint()):
            self.clicked.emit()
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event):  # noqa: N802
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            if not event.isAutoRepeat() and not event.modifiers():
                self.clicked.emit()
            event.accept()
        else:
            super().keyPressEvent(event)


class TimelinePhotos(QObject):
    """Read-only HTTP images; no refresh RPC, credential forwarding or URL synthesis."""
    def __init__(self, view):
        super().__init__(view)
        self.view = view
        self._context = None
        self._bindings = {}
        self._viewers = []
        self.loader = ConversationMediaLoader(self._current, self)
        self.loader.ready.connect(self._ready)
        self.loader.failed.connect(self._failed)
        self.timer = QTimer(self)
        self.timer.setInterval(250)
        self.timer.timeout.connect(self.refresh)
        self.timer.start()

    def retranslate(self, language):
        """Update only known presenters; never refresh or change media bindings."""
        if not isValid(self) or not isValid(self.view):
            return
        for row in self.view._rows:
            bubble = row['bubble']
            if isValid(bubble):
                bubble.retranslate_photo_previews(language)
        for viewer in self._viewers:
            if isValid(viewer):
                viewer.retranslate(language)

    def _current(self, context):
        return (isValid(self) and isValid(self.view) and context is self._context
                and self.view.file_context_current(context))

    def reset(self):
        self._context = None
        self.loader.bind(None)
        for binding in self._bindings.values():
            if isValid(binding[4]):
                binding[4].set_waiting(False)
        self._bindings.clear()
        for viewer in self._viewers:
            if isValid(viewer):
                viewer.close()
        self._viewers.clear()

    def refresh(self):
        if not isValid(self.view) or not isValid(self.view.scroll):
            self.timer.stop()
            self.reset()
            return
        context = self.view._file_context
        if not self.view.file_context_current(context):
            self.reset()
            for row in self.view._rows:
                if isValid(row['bubble']):
                    for _attach, preview, _snapshot in row['bubble']._photo_bindings.values():
                        if isValid(preview):
                            preview.set_waiting(False)
            return
        if context is not self._context:
            self.reset()
            self._context = context
            self.loader.bind(context)
        if not self.view.isVisible():
            return
        live = set()
        viewport = self.view.scroll.viewport()
        for row in self.view._rows:
            bubble, message = row['bubble'], row['msg']
            if not isValid(bubble):
                continue
            for index, (attach, preview, snapshot) in bubble._photo_bindings.items():
                key = f'photo:{id(bubble)}:{index}'
                if not isValid(preview):
                    continue
                live.add(key)
                source = photo_source(attach)
                if (source is None or message.display_source != 'outer'
                        or type(message.id) is not int or message.chat_id != context.chat_id
                        or index >= len(message.display_attaches) or message.display_attaches[index] is not attach
                        or photo_identity(message, index, attach) != snapshot):
                    preview.set_unavailable()
                    continue
                position = preview.mapTo(viewport, QPoint(0, 0))
                if (not preview.isVisible() or not viewport.rect().intersects(QRect(position, preview.size()))
                        or key in self._bindings):
                    continue
                self._bindings[key] = (message, bubble, index, attach, preview, snapshot, source)
                preview.set_waiting(True)
                self.loader.request({'key': key, 'thumbnail_url': source}, context)
        self._bindings = {key: value for key, value in self._bindings.items() if key in live}

    def _binding(self, context, key):
        value = self._bindings.get(key)
        if not self._current(context) or value is None:
            return None
        message, bubble, index, attach, preview, snapshot, source = value
        if (not isValid(bubble) or not isValid(preview)
                or type(message.id) is not int or message.chat_id != context.chat_id
                or message.display_source != 'outer'
                or not any(row['msg'] is message and row['bubble'] is bubble for row in self.view._rows)
                or index >= len(message.display_attaches) or message.display_attaches[index] is not attach
                or photo_identity(message, index, attach) != snapshot or photo_source(attach) != source):
            return None
        return value

    def _ready(self, context, key, source, pixmap):
        binding = self._binding(context, key)
        if binding is not None and source == binding[-1]:
            binding[4].set_photo(pixmap)

    def _failed(self, context, key):
        binding = self._binding(context, key)
        if binding is not None:
            binding[4].set_failed()

    def open_photo(self, message, attach):
        self.refresh()
        for key, binding in self._bindings.items():
            if binding[0] is not message or binding[3] is not attach:
                continue
            context = self._context
            if self._binding(context, key) is None:
                return
            from .conversation_photo_viewer import ConversationPhotoViewer
            row = {'key': key, 'kind': 'PHOTO', 'available': True, 'open_url': binding[-1],
                   'message_id': message.id, 'time_ms': message.time_ms, 'chat_id': message.chat_id}
            # Every viewer request/save rechecks the same actual message+attachment.
            viewer = ConversationPhotoViewer([row], key, context,
                lambda owner: self._binding(owner, key) is not None, self.view,
                language=normalize_language(getattr(getattr(self.view, 'settings', None), 'ui_language', 'ru')))
            viewer.jump.hide()  # Already at the source message.
            self._viewers = [value for value in self._viewers if isValid(value)]
            self._viewers.append(viewer)
            viewer.show()
            return
