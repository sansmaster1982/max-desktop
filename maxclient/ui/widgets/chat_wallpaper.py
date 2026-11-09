"""The official public eight-preset CSS background consumer, rendered by Qt."""
from __future__ import annotations

import math
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPainterPath, QPixmap, QRadialGradient
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QWidget

from ...core.appearance import chat_theme, theme_scheme
from ..appearance_tokens import BACKGROUND_TOKENS, PATTERN_ASSETS, PATTERN_SIZES
from ..theme import resolve_theme

ASSET_DIR = Path(__file__).resolve().parents[2] / 'assets'
PATTERN_FILE = ASSET_DIR / 'pattern_space.svg'
RADIAL_GEOMETRIES = ((.5757, .5361, .944, .1438), (1.4037, .5138, 0., .8005))


def css_color(value: str) -> QColor:
    """CSS #RGBA/#RRGGBBAA put alpha last; QColor's eight-digit form does not."""
    digits = value.lstrip('#')
    if len(digits) in (3, 4):
        digits = ''.join(char * 2 for char in digits)
    if len(digits) == 6:
        digits += 'ff'
    if not value.startswith('#') or len(digits) != 8:
        raise ValueError('A captured CSS hexadecimal color is required')
    return QColor(*(int(digits[index:index + 2], 16) for index in (0, 2, 4, 6)))


def centered_mask_offset(width: float, height: float, tile_width: float, tile_height: float) -> QPointF:
    """CSS mask-position:50% with intrinsic mask-size:auto, repeated both ways."""
    return QPointF(((tile_width - width) / 2) % tile_width,
                   ((tile_height - height) / 2) % tile_height)


def _linear(rect, colors):
    angle = math.radians(28)
    direction = QPointF(math.sin(angle), -math.cos(angle))
    length = rect.width() * abs(direction.x()) + rect.height() * abs(direction.y())
    center = rect.center()
    gradient = QLinearGradient(center - direction * length / 2, center + direction * length / 2)
    gradient.setColorAt(.0803, css_color(colors[0]))
    gradient.setColorAt(.9151, css_color(colors[1]))
    return gradient


def _radials(painter, rect, colors):
    # CSS lists the upper layer first; source-over painting works bottom first.
    for index in (1, 0):
        triplet = colors[index * 3:index * 3 + 3]
        if all(css_color(color).alpha() == 0 for color in triplet):
            continue
        x_radius, y_radius, center_x, center_y = RADIAL_GEOMETRIES[index]
        rx, ry = rect.width() * x_radius, rect.height() * y_radius
        cx, cy = rect.width() * center_x, rect.height() * center_y
        painter.save()
        painter.translate(cx, cy)
        painter.scale(rx, ry)
        gradient = QRadialGradient(QPointF(0, 0), 1)
        for stop, color in zip((0., .5, 1.), triplet):
            gradient.setColorAt(stop, css_color(color))
        painter.fillRect(QRectF(-cx / rx, -cy / ry, rect.width() / rx, rect.height() / ry), gradient)
        painter.restore()


class ChatWallpaper(QWidget):
    def __init__(self, theme='light', parent=None, *, preset='space', corner_radius=0):
        super().__init__(parent)
        self.setObjectName('ChatArea')
        self.theme = theme_scheme(theme)
        self.preset = chat_theme(preset)
        self.corner_radius = corner_radius
        self._tile = None
        self._canvas = None
        self._canvas_key = None

    def set_theme(self, theme):
        self.set_appearance(theme, self.preset)

    def set_appearance(self, theme, preset):
        theme, preset = theme_scheme(theme), chat_theme(preset)
        if (theme, preset) != (self.theme, self.preset):
            self._tile = None
            self._canvas_key = None
        self.theme, self.preset = theme, preset
        self.update()

    def _make_tile(self, dpr):
        name = PATTERN_ASSETS[self.preset]
        if name is None:
            return None
        renderer = QSvgRenderer(str(ASSET_DIR / name))
        if not renderer.isValid():
            return None
        width, height = PATTERN_SIZES[self.preset]
        tile = QPixmap(round(width * dpr), round(height * dpr))
        tile.setDevicePixelRatio(dpr)
        tile.fill(Qt.GlobalColor.transparent)
        painter = QPainter(tile)
        renderer.render(painter, QRectF(0, 0, width, height))
        painter.end()
        return tile

    def _render_canvas(self, scheme, dpr):
        width, height = self.width(), self.height()
        rect = QRectF(0, 0, width, height)
        tokens = BACKGROUND_TOKENS[self.preset][scheme]
        canvas = QPixmap(round(width * dpr), round(height * dpr))
        canvas.setDevicePixelRatio(dpr)
        canvas.fill(Qt.GlobalColor.transparent)
        painter = QPainter(canvas)
        painter.fillRect(rect, _linear(rect, tokens['base']))
        _radials(painter, rect, tokens['additional'])
        if PATTERN_ASSETS[self.preset] is not None:
            if self._tile is None or self._tile.devicePixelRatio() != dpr:
                self._tile = self._make_tile(dpr)
            if self._tile is not None:
                pattern = QPixmap(canvas.size())
                pattern.setDevicePixelRatio(dpr)
                pattern.fill(Qt.GlobalColor.transparent)
                pattern_painter = QPainter(pattern)
                pattern_painter.fillRect(rect, _linear(rect, tokens['pattern_gradient']))
                _radials(pattern_painter, rect, tokens['pattern_radials'])
                mask = QPixmap(canvas.size())
                mask.setDevicePixelRatio(dpr)
                mask.fill(Qt.GlobalColor.transparent)
                mask_painter = QPainter(mask)
                tw, th = PATTERN_SIZES[self.preset]
                mask_painter.drawTiledPixmap(rect, self._tile, centered_mask_offset(width, height, tw, th))
                mask_painter.end()
                pattern_painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
                pattern_painter.drawPixmap(0, 0, mask)
                pattern_painter.end()
                painter.drawPixmap(0, 0, pattern)
        painter.end()
        return canvas

    def paintEvent(self, event):  # noqa: N802
        if self.width() <= 0 or self.height() <= 0:
            return
        scheme, dpr = resolve_theme(self.theme), self.devicePixelRatioF()
        key = (self.width(), self.height(), dpr, scheme, self.preset)
        if key != self._canvas_key:
            self._canvas = self._render_canvas(scheme, dpr)
            self._canvas_key = key
        painter = QPainter(self)
        if self.corner_radius:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            clip = QPainterPath()
            clip.addRoundedRect(QRectF(self.rect()), self.corner_radius, self.corner_radius)
            painter.setClipPath(clip)
        painter.drawPixmap(0, 0, self._canvas)
        painter.end()
