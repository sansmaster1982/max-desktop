"""QCheckBox-compatible switch using the captured official52×32 control.

Source: PasswordRecovery.CX7pZAsS.css character14615 and Space scheme tokens.
No settings/network behavior lives here; callers own confirmed checked state.
"""
from PySide6.QtCore import QEvent, QRect, QRectF, QSize, Qt
from PySide6.QtGui import QPainter, QPen
from PySide6.QtWidgets import QCheckBox, QSizePolicy

from ..theme import resolve_theme
from .chat_wallpaper import css_color


_COLORS = {
    'light': {'active': '#007aff', 'active_disabled': '#007aff7a', 'inactive_disabled': '#0909090a',
              'border': '#0c0d0e29', 'hover': '#0c0d0e38', 'pressed': '#0c0d0e5c',
              'knob': '#0607087a', 'muted': '#06070847', 'inverse': '#fff', 'text': '#060708'},
    'dark': {'active': '#007aff', 'active_disabled': '#ffffff1f', 'inactive_disabled': '#ffffff1f',
             'border': '#ffffff1f', 'hover': '#e7e7e738', 'pressed': '#e7e7e75c',
             'knob': '#ffffff85', 'muted': '#ffffff5c', 'inverse': '#fff', 'text': '#fff'},
}


class ToggleSwitch(QCheckBox):
    def __init__(self, text='', parent=None, *, theme='system'):
        super().__init__(text, parent)
        self._theme = theme
        self.setObjectName('ToggleSwitch')
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        policy = QSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)
        self.toggled.connect(lambda _checked: self.update())

    def set_theme(self, theme):
        self._theme = theme
        self.update()

    def sizeHint(self):
        width = min(480, max(200, self.fontMetrics().horizontalAdvance(self.text()) + 80))
        return QSize(width, self.heightForWidth(width))

    def minimumSizeHint(self):
        return QSize(160, self.heightForWidth(max(160, self.width())))

    def heightForWidth(self, width):
        text_height = self.fontMetrics().boundingRect(
            QRect(0, 0, max(1, width - 80), 10000), Qt.TextFlag.TextWordWrap, self.text()).height()
        return max(44, text_height + 12)

    def hitButton(self, position):
        return self.rect().contains(position)

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.Type.EnabledChange:
            self.setCursor(Qt.CursorShape.PointingHandCursor if self.isEnabled() else Qt.CursorShape.ArrowCursor)
        self.update()

    def paintEvent(self, event):  # noqa: N802
        colors = _COLORS[resolve_theme(self._theme)]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(css_color(colors['text'] if self.isEnabled() else colors['muted']))
        painter.drawText(QRectF(0, 0, max(1, self.width() - 80), self.height()),
                         Qt.AlignmentFlag.AlignVCenter | Qt.TextFlag.TextWordWrap, self.text())
        track = QRectF(self.width() - 56, (self.height() - 32) / 2, 52, 32)
        painter.setPen(Qt.PenStyle.NoPen)
        if self.isChecked():
            painter.setBrush(css_color(colors['active'] if self.isEnabled() else colors['active_disabled']))
            painter.drawRoundedRect(track, 16, 16)
        else:
            painter.setBrush(Qt.BrushStyle.NoBrush if self.isEnabled() else css_color(colors['inactive_disabled']))
            border = colors['pressed'] if self.isDown() else colors['hover'] if self.underMouse() and self.isEnabled() else colors['border']
            painter.setPen(QPen(css_color(border), 2))
            painter.drawRoundedRect(track.adjusted(1, 1, -1, -1), 15, 15)
        painter.setPen(Qt.PenStyle.NoPen)
        if self.isChecked():
            painter.setBrush(css_color(colors['inverse']))
            painter.drawEllipse(QRectF(track.left() + 24, track.top() + 4, 24, 24))
        else:
            painter.setBrush(css_color(colors['knob'] if self.isEnabled() else colors['muted']))
            painter.drawEllipse(QRectF(track.left() + 6, track.top() + 6, 20, 20))
        if self.hasFocus():
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(css_color(colors['active']), 2))
            painter.drawRoundedRect(track.adjusted(-3, -3, 3, 3), 19, 19)
        painter.end()
