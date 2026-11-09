"""Paint-only Info media grid; owned pixmaps in, guarded navigation signals out.

The parent owns all URLs, fetching, decoding and account context. In particular,
painting never emits a request or performs file/network IO.
"""
from __future__ import annotations

from datetime import datetime
from html import escape
import math

from PySide6.QtCore import QPoint, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QWidget
from shiboken6 import isValid

from ..core.i18n import normalize_language, tr
from .theme import palette_for


_MONTHS = ('Январь', 'Февраль', 'Март', 'Апрель', 'Май', 'Июнь',
           'Июль', 'Август', 'Сентябрь', 'Октябрь', 'Ноябрь', 'Декабрь')


def month_label(timestamp, language='ru'):
    if type(timestamp) not in (int, float) or not math.isfinite(timestamp):
        return ''
    try:
        date = datetime.fromtimestamp(timestamp / 1000)
    except (OverflowError, OSError, ValueError):
        return ''
    return f'{tr(_MONTHS[date.month - 1], language)} {date.year}'


def duration_label(milliseconds):
    """Input is the explicitly normalized source milliseconds, never a guess."""
    if type(milliseconds) not in (int, float) or not math.isfinite(milliseconds) or milliseconds < 0:
        return ''
    seconds = int(milliseconds / 1000)
    hours, rest = divmod(seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    return f'{hours}:{minutes:02}:{seconds:02}' if hours else f'{minutes}:{seconds:02}'


class ConversationMediaGrid(QWidget):
    activated = Signal(object)
    thumbnail_needed = Signal(object)
    current_rect_changed = Signal(object)
    visible_keys_changed = Signal(object)

    def __init__(self, parent=None, *, theme='light', language='ru'):
        super().__init__(parent)
        self._theme = theme
        self._lang = normalize_language(language)
        self._rows = []
        self._cells = []
        self._headers = []
        self._header_times = []
        self._pixmaps = {}
        self._requested = set()
        self._current = -1
        self._hovered = -1
        self._enabled = True
        self._generation = 0
        self._pressed = None
        self.columns = 3
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)
        self.setAccessibleName(tr('Медиа: фотографии и видео', self._lang))
        self._visibility_timer = QTimer(self)
        self._visibility_timer.setSingleShot(True)
        self._visibility_timer.timeout.connect(self.request_visible_thumbnails)

    def retranslate(self, language=None):
        if not isValid(self):
            return
        self._lang = normalize_language(language if language is not None else self._lang)
        self.setAccessibleName(tr('Медиа: фотографии и видео', self._lang))
        self._headers = [(rect, month_label(timestamp, self._lang))
                         for (rect, _caption), timestamp in zip(self._headers, self._header_times)]
        if 0 <= self._current < len(self._rows):
            self.setAccessibleDescription(self._row_title(self._rows[self._current]))
        self.update()  # No set_rows/reflow: keep selection, pixels, requests and geometry.

    def _row_title(self, row):
        return tr('Данные недоступны', self._lang) if row.get('_info_local_title') is True else row.get('title', '')

    def set_rows(self, rows, *, reset=False, enabled=True):
        self._generation += 1
        self._pressed = None
        prior = {row.get('key'): (row.get('kind'), row.get('thumbnail_url')) for row in self._rows}
        self._rows = [dict(row) for row in rows]
        self._enabled = enabled is True
        if reset:
            self._requested.clear()
            self._pixmaps.clear()
        else:
            stable = {row.get('key') for row in self._rows
                      if prior.get(row.get('key')) == (row.get('kind'), row.get('thumbnail_url'))}
            self._requested.intersection_update(stable)
            self._pixmaps = {key: value for key, value in self._pixmaps.items() if key in stable}
        self._current = -1
        self._reflow()

    def set_thumbnail(self, key, pixmap):
        if (not isinstance(key, str) or not isinstance(pixmap, QPixmap) or pixmap.isNull()
                or not any(row.get('key') == key for row in self._rows)):
            return False
        self._pixmaps[key] = QPixmap(pixmap)
        for index, row in enumerate(self._rows):
            if row.get('key') == key:
                self.update(self._cells[index])
        return True

    def drop_thumbnail(self, key):
        self._pixmaps.pop(key, None)
        self._requested.discard(key)
        self.update()

    def visible_keys(self):
        visible = self._visible_rect()
        return {row['key'] for row, rect in zip(self._rows, self._cells)
                if rect.intersects(visible) and isinstance(row.get('key'), str)}

    def apply_theme(self, theme):
        self._theme = theme
        self.update()

    def schedule_visibility(self, *_args):
        if isValid(self) and not self._visibility_timer.isActive():
            self._visibility_timer.start(0)

    def _visible_rect(self):
        if not self.isVisible():
            return QRect()
        rect = self.rect()
        ancestor = self.parentWidget()
        while ancestor is not None:
            if not ancestor.isVisible():
                return QRect()
            rect = rect.intersected(QRect(self.mapFrom(ancestor, QPoint()), ancestor.size()))
            ancestor = ancestor.parentWidget()
        return rect

    def request_visible_thumbnails(self):
        if not isValid(self) or not self._enabled:
            return
        visible = self._visible_rect()
        # Snapshot before emitting: a receiver may clear/change the panel.
        rows, cells = list(self._rows), list(self._cells)
        generation = self._generation
        self.visible_keys_changed.emit(self.visible_keys())
        if not isValid(self) or self._generation != generation:
            return
        for row, rect in zip(rows, cells):
            key = row.get('key')
            if (rect.intersects(visible) and row.get('kind') in ('PHOTO', 'VIDEO')
                    and row.get('available') is True and isinstance(key, str)
                    and isinstance(row.get('thumbnail_url'), str) and row['thumbnail_url']
                    and key not in self._requested and key not in self._pixmaps):
                self._requested.add(key)
                self.thumbnail_needed.emit(dict(row))
                if not isValid(self) or self._generation != generation:
                    return

    def _reflow(self):
        width = max(0, self.width())
        self.columns = 3 if width >= 450 else 2 if width >= 200 else 1
        gap = 4
        side = max(1, (width - (self.columns - 1) * gap) // self.columns)
        self._cells, self._headers, self._header_times = [], [], []
        y, column, previous_month = 0, 0, None
        for row in self._rows:
            month = month_label(row.get('time_ms'), self._lang)
            if month and month != previous_month:
                if column:
                    y += side + gap
                    column = 0
                self._headers.append((QRect(6, y, max(0, width - 12), 36), month))
                self._header_times.append(row.get('time_ms'))
                y += 36
            previous_month = month
            if row.get('kind') in ('PHOTO', 'VIDEO'):
                self._cells.append(QRect(column * (side + gap), y, side, side))
                column += 1
                if column == self.columns:
                    column = 0
                    y += side + gap
            else:
                if column:
                    y += side + gap
                    column = 0
                self._cells.append(QRect(0, y, width, 70))
                y += 74
        if column:
            y += side + gap
        self.setFixedHeight(max(1, y))
        self.updateGeometry()
        self.update()
        self.schedule_visibility()

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        self._reflow()

    def showEvent(self, event):  # noqa: N802
        super().showEvent(event)
        self.schedule_visibility()

    def hideEvent(self, event):  # noqa: N802
        super().hideEvent(event)
        self.schedule_visibility()

    def _index_at(self, point):
        return next((index for index, rect in enumerate(self._cells) if rect.contains(point)), -1)

    def _activate(self, index):
        if 0 <= index < len(self._rows) and self._enabled and self._rows[index].get('available') is True:
            self.activated.emit(dict(self._rows[index]))

    def mousePressEvent(self, event):  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._pressed = (self._generation, self._index_at(event.position().toPoint()))
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._current = self._index_at(event.position().toPoint())
            self.setFocus(Qt.FocusReason.MouseFocusReason)
            self.update()
            if self._pressed == (self._generation, self._current):
                self._activate(self._current)
            self._pressed = None
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseMoveEvent(self, event):  # noqa: N802
        index = self._index_at(event.position().toPoint())
        if index != self._hovered:
            self._hovered = index
            row = self._rows[index] if index >= 0 else {}
            self.setToolTip('<qt>' + escape(row['title']) + '</qt>' if isinstance(row.get('title'), str) else '')
            self.update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):  # noqa: N802
        self._hovered = -1
        self.update()
        super().leaveEvent(event)

    def keyPressEvent(self, event):  # noqa: N802
        key = event.key()
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self._activate(self._current)
            event.accept()
        elif key in (Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_Up, Qt.Key.Key_Down):
            delta = {Qt.Key.Key_Left: -1, Qt.Key.Key_Right: 1,
                     Qt.Key.Key_Up: -self.columns, Qt.Key.Key_Down: self.columns}[key]
            self._current = (0 if self._current < 0 and self._rows else
                             min(len(self._rows) - 1, max(0, self._current + delta)))
            if 0 <= self._current < len(self._rows):
                self.setAccessibleDescription(self._row_title(self._rows[self._current]))
                self.current_rect_changed.emit(QRect(self._cells[self._current]))
            self.update()
            event.accept()
        else:
            super().keyPressEvent(event)

    def paintEvent(self, event):  # noqa: N802
        p = palette_for(self._theme)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        text_font = QFont(self.font())
        text_font.setPixelSize(13)
        painter.setFont(text_font)
        painter.setPen(QColor(p.text))
        for rect, title in self._headers:
            if rect.intersects(event.rect()):
                painter.drawText(rect, Qt.AlignmentFlag.AlignVCenter, title)
        for index, (row, rect) in enumerate(zip(self._rows, self._cells)):
            if not rect.intersects(event.rect()):
                continue
            active = self._enabled and row.get('available') is True
            square = row.get('kind') in ('PHOTO', 'VIDEO')
            painter.save()
            path = QPainterPath()
            path.addRoundedRect(QRectF(rect), 6, 6)
            painter.setClipPath(path)
            painter.fillRect(rect, QColor(p.surface_hi if square else p.surface))
            photo = self._pixmaps.get(row.get('key')) if square else None
            if photo is not None:
                source_side = min(photo.width(), photo.height())
                source = QRect((photo.width() - source_side) // 2,
                               (photo.height() - source_side) // 2, source_side, source_side)
                painter.drawPixmap(rect, photo, source)
            elif square:
                # Crisp geometric image/video glyph, never a fabricated image.
                center = rect.center()
                painter.setPen(QPen(QColor('#737983' if p.name == 'light' else '#A3ADBF'), 1.8))
                if row.get('kind') == 'VIDEO':
                    triangle = QPainterPath()
                    triangle.moveTo(center.x() - 6, center.y() - 9)
                    triangle.lineTo(center.x() + 9, center.y())
                    triangle.lineTo(center.x() - 6, center.y() + 9)
                    triangle.closeSubpath()
                    painter.drawPath(triangle)
                else:
                    painter.drawRoundedRect(QRect(center.x() - 13, center.y() - 10, 26, 20), 3, 3)
                    painter.drawEllipse(QPoint(center.x() - 5, center.y() - 3), 2, 2)
                    painter.drawLine(center.x() - 11, center.y() + 7, center.x(), center.y())
                    painter.drawLine(center.x(), center.y(), center.x() + 11, center.y() + 7)
            else:
                painter.setPen(QColor(p.text))
                title = self._row_title(row)
                subtitle = row.get('subtitle', '')
                painter.drawText(rect.adjusted(12, 8, -12, -35), Qt.AlignmentFlag.AlignVCenter,
                                 painter.fontMetrics().elidedText(title, Qt.TextElideMode.ElideRight, max(0, rect.width() - 24)))
                painter.setPen(QColor('#737983' if p.name == 'light' else '#A3ADBF'))
                painter.drawText(rect.adjusted(12, 35, -12, -8), Qt.AlignmentFlag.AlignVCenter,
                                 painter.fontMetrics().elidedText(subtitle, Qt.TextElideMode.ElideRight, max(0, rect.width() - 24)))
            if square and row.get('kind') == 'VIDEO':
                play_rect = QRect(rect.x() + 7, rect.bottom() - 27, 25, 21)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(0, 0, 0, 185))
                painter.drawRoundedRect(play_rect, 5, 5)
                triangle = QPainterPath()
                triangle.moveTo(play_rect.x() + 10, play_rect.y() + 5)
                triangle.lineTo(play_rect.x() + 17, play_rect.y() + 10.5)
                triangle.lineTo(play_rect.x() + 10, play_rect.y() + 16)
                triangle.closeSubpath()
                painter.setBrush(QColor('white'))
                painter.drawPath(triangle)
                duration = duration_label(row.get('duration_ms'))
                if duration:
                    badge_width = painter.fontMetrics().horizontalAdvance(duration) + 14
                    badge = QRect(rect.right() - badge_width - 6, rect.bottom() - 27, badge_width, 21)
                    painter.setPen(Qt.PenStyle.NoPen)
                    painter.setBrush(QColor(0, 0, 0, 185))
                    painter.drawRoundedRect(badge, 5, 5)
                    painter.setPen(QColor('white'))
                    painter.drawText(badge, Qt.AlignmentFlag.AlignCenter, duration)
            if not active:
                painter.fillRect(rect, QColor(0, 0, 0, 70))
                painter.setPen(QColor('white'))
                painter.drawText(rect.adjusted(4, 4, -4, -4), Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignHCenter, tr('Недоступно', self._lang))
            painter.restore()
            if active and (index == self._hovered or index == self._current and self.hasFocus()):
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.setPen(QPen(QColor('#0085FF'), 2))
                painter.drawRoundedRect(QRectF(rect).adjusted(1, 1, -1, -1), 5, 5)
        painter.end()
