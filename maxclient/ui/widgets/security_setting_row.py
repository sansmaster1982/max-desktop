"""Security cards and rows; rendering and ordinary navigation only."""
from PySide6.QtCore import QPointF, QRect, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QPixmap, QWheelEvent
from PySide6.QtWidgets import QApplication, QAbstractScrollArea, QFrame, QHBoxLayout, QLabel, QMenu, QPushButton, QSizePolicy, QVBoxLayout
from shiboken6 import isValid

from ..theme import resolve_theme
from .line_icon import line_icon
from .toggle_switch import ToggleSwitch


def _text_label(text='', name='SecurityRowText'):
    label = QLabel(text)
    label.setObjectName(name)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
    return label


def forward_wheel(widget, event):
    """Rows never interpret scrolling as an account-setting selection."""
    parent = widget.parentWidget()
    while parent is not None:
        if isinstance(parent, QAbstractScrollArea):
            viewport = parent.viewport()
            forwarded = QWheelEvent(
                QPointF(viewport.mapFromGlobal(event.globalPosition().toPoint())),
                event.globalPosition(), event.pixelDelta(), event.angleDelta(),
                event.buttons(), event.modifiers(), event.phase(), event.inverted(),
                event.source(), event.pointingDevice())
            QApplication.sendEvent(viewport, forwarded)
            event.setAccepted(forwarded.isAccepted())
            return
        parent = parent.parentWidget()
    event.ignore()


def _key_pixmap(color):
    pixmap = QPixmap(48, 48)
    pixmap.fill(Qt.GlobalColor.transparent)
    pixmap.setDevicePixelRatio(2)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QPen(QColor(color), 1.8, Qt.PenStyle.SolidLine,
                       Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
    painter.drawEllipse(QPointF(16, 7), 4.5, 4.5)
    painter.drawLine(QPointF(12.8, 10.2), QPointF(3.5, 19.5))
    painter.drawLine(QPointF(5, 18), QPointF(7, 20))
    painter.drawLine(QPointF(8, 15), QPointF(10, 17))
    painter.end()
    return pixmap


class SecuritySettingRow(QPushButton):
    def __init__(self, title, parent=None, *, subtitle='', icon=None, toggle=False, height=64):
        super().__init__(parent)
        self.setObjectName('SecuritySettingRow')
        self.setAccessibleName(title)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setMinimumHeight(height)
        self._minimum_row_height = height
        self._icon_name = icon
        self.current_value = None
        self.confirmed = False
        row = QHBoxLayout(self)
        row.setContentsMargins(20 if icon is not None else 16, 10, 16, 10)
        row.setSpacing(20 if icon is not None else 12)
        self.icon_label = _text_label()
        self.icon_label.setFixedSize(24, 24)
        self.icon_label.setVisible(icon is not None)
        if icon is not None:
            row.addWidget(self.icon_label)
        text_box = QVBoxLayout()
        text_box.setContentsMargins(0, 0, 0, 0)
        text_box.setSpacing(3)
        self.title_label = _text_label(title)
        self.title_label.setWordWrap(True)
        text_box.addWidget(self.title_label)
        self.subtitle_label = _text_label(subtitle, 'SecurityRowSubtitle')
        self.subtitle_label.setWordWrap(True)
        self.subtitle_label.setVisible(bool(subtitle))
        text_box.addWidget(self.subtitle_label)
        row.addLayout(text_box, 1)
        self.value_label = _text_label('', 'SecurityRowValue')
        self.value_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.value_label.setWordWrap(False)
        self.value_label.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Preferred)
        self.value_label.hide()
        row.addWidget(self.value_label)
        self.chevron = _text_label()
        self.chevron.setFixedSize(14, 20)
        row.addWidget(self.chevron)
        self.toggle = None
        if toggle:
            self.toggle = ToggleSwitch()
            self.toggle.setFixedSize(56, 40)
            self.toggle.setEnabled(False)
            self.toggle.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            row.addWidget(self.toggle)
            self.chevron.hide()
        self.apply_theme('light')

    def set_subtitle(self, text):
        if not self.ui_alive():
            return
        self.subtitle_label.setText(text)
        self.subtitle_label.setVisible(bool(text))
        self._description()
        self._adjust_height()

    def set_value(self, text, value=None, *, confirmed=False):
        if not self.ui_alive():
            return
        self.current_value, self.confirmed = value, bool(confirmed)
        self.value_label.setText(text)
        self.value_label.setMinimumWidth(self.value_label.fontMetrics().horizontalAdvance(text) + 2 if text else 0)
        self.value_label.setVisible(bool(text))
        self._description()
        self._adjust_height()

    def _description(self):
        self.setAccessibleDescription(' · '.join(
            text for text in (self.subtitle_label.text(), self.value_label.text()) if text))

    def ui_alive(self):
        return isValid(self) and all(isValid(widget) for widget in (
            self.title_label, self.subtitle_label, self.value_label, self.icon_label, self.chevron,
            *([self.toggle] if self.toggle is not None else [])))

    def wheelEvent(self, event):  # noqa: N802
        forward_wheel(self, event)

    def _adjust_height(self):
        if not self.ui_alive():
            return
        layout = self.layout()
        margins = layout.contentsMargins()
        fixed = [widget.width() for widget in (self.icon_label, self.chevron)
                 if not widget.isHidden()]
        if not self.value_label.isHidden():
            fixed.append(self.value_label.minimumWidth())
        if self.toggle is not None and not self.toggle.isHidden():
            fixed.append(self.toggle.width())
        text_width = max(40, self.width() - margins.left() - margins.right()
                         - sum(fixed) - len(fixed) * layout.spacing())
        text_height = self.title_label.fontMetrics().boundingRect(
            QRect(0, 0, text_width, 10000), Qt.TextFlag.TextWordWrap, self.title_label.text()).height()
        if self.subtitle_label.text():
            text_height += 3 + self.subtitle_label.fontMetrics().boundingRect(
                QRect(0, 0, text_width, 10000), Qt.TextFlag.TextWordWrap, self.subtitle_label.text()).height()
        height = max(self._minimum_row_height, text_height + margins.top() + margins.bottom() + 2)
        if self.minimumHeight() != height:
            self.setMinimumHeight(height)

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        self._adjust_height()
        if self.ui_alive():
            # Changing a minimum height during resize can leave QPushButton's
            # child layout at the preceding height until another event.
            self.layout().setGeometry(self.contentsRect())

    def apply_theme(self, theme):
        if not self.ui_alive():
            return
        dark = resolve_theme(theme) == 'dark'
        text, secondary, hover = (('#EAECEF', '#AAB3C2', '#222B39') if dark
                                  else ('#17191D', '#878C95', '#F4F7FA'))
        self.setStyleSheet(f'''
            #SecuritySettingRow {{background:transparent;border:1px solid transparent;border-radius:12px;padding:0;}}
            #SecuritySettingRow:hover:enabled {{background:{hover};}}
            #SecuritySettingRow:focus {{border:1px solid #0085FF;}}
            #SecurityRowText {{background:transparent;color:{text};font-size:16px;font-weight:600;border:0;padding:0;}}
            #SecurityRowSubtitle, #SecurityRowValue {{background:transparent;color:{secondary};font-size:14px;font-weight:400;border:0;padding:0;}}
        ''')
        title_font = self.title_label.font()
        title_font.setPixelSize(16)
        title_font.setWeight(QFont.Weight.DemiBold)
        self.title_label.setFont(title_font)
        self.title_label.ensurePolished()
        self.value_label.ensurePolished()
        self.value_label.setMinimumWidth(
            self.value_label.fontMetrics().horizontalAdvance(self.value_label.text()) + 2
            if self.value_label.text() else 0)
        self.chevron.setPixmap(line_icon('chevron', secondary, 14).pixmap(14, 14))
        if self._icon_name == 'key':
            self.icon_label.setPixmap(_key_pixmap(text))
        elif self._icon_name:
            self.icon_label.setPixmap(line_icon(self._icon_name, text, 24).pixmap(24, 24))
        if self.toggle is not None:
            self.toggle.set_theme(theme)
        self._adjust_height()


class SecurityCard(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('SecurityCard')
        self.rows = []
        self.layout_box = QVBoxLayout(self)
        self.layout_box.setContentsMargins(0, 0, 0, 0)
        self.layout_box.setSpacing(0)
        self.apply_theme('light')

    def add_row(self, row, *, divider=False):
        if self.rows and divider:
            divider = QFrame()
            divider.setObjectName('SecurityCardDivider')
            divider.setFixedHeight(1)
            wrapper = QHBoxLayout()
            wrapper.setContentsMargins(4, 0, 4, 0)
            wrapper.addWidget(divider)
            self.layout_box.addLayout(wrapper)
        self.rows.append(row)
        self.layout_box.addWidget(row)

    def apply_theme(self, theme):
        if not isValid(self):
            return
        dark = resolve_theme(theme) == 'dark'
        surface, border = ('#141821', '#303846') if dark else ('#FFFFFF', '#EEF0F3')
        self.setStyleSheet(f'#SecurityCard {{background:{surface};border:0;border-radius:16px;}}'
                          f'#SecurityCardDivider {{background:{border};border:0;}}')
        for row in self.rows:
            row.apply_theme(theme)


class SecurityChoiceMenu(QMenu):
    """Track a real selection while Qt hides the popup before triggered()."""
    selecting = False
    check_color = '#17191D'

    def paintEvent(self, event):  # noqa: N802
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor(self.check_color), 1.8, Qt.PenStyle.SolidLine,
                            Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        for action in self.actions():
            if action.isVisible() and action.isChecked():
                rect = self.actionGeometry(action)
                x, y = rect.right() - 18, rect.center().y()
                painter.drawLine(QPointF(x - 5, y), QPointF(x - 1, y + 4))
                painter.drawLine(QPointF(x - 1, y + 4), QPointF(x + 6, y - 4))
        painter.end()

    def mouseReleaseEvent(self, event):  # noqa: N802
        action = self.actionAt(event.pos())
        self.selecting = bool(action is not None and action.isEnabled() and not action.isSeparator())
        try:
            super().mouseReleaseEvent(event)
        finally:
            if isValid(self):
                self.selecting = False

    def keyPressEvent(self, event):  # noqa: N802
        action = self.activeAction()
        self.selecting = bool(event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space)
                              and action is not None and action.isEnabled())
        try:
            super().keyPressEvent(event)
        finally:
            if isValid(self):
                self.selecting = False
