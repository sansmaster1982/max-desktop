"""Devices settings cards. Display supplied DTOs only; no service or actions."""
from datetime import datetime

from PySide6.QtCore import QEvent, QRect, Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QVBoxLayout
from shiboken6 import isValid

from ...core.i18n import normalize_language, tr
from ..theme import resolve_theme
from .line_icon import line_icon


INTRO_TITLE = 'Устройства с MAX'
INTRO_SUBTITLE = 'Входите на новых устройствах \nи\u00a0управляйте сессиями'
LOGOUT_TITLE = 'Выйти на всех устройствах, кроме этого'


def _label(name):
    widget = QLabel()
    widget.setObjectName(name)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    widget.setWordWrap(True)
    widget.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
    return widget


def _display_text(value):
    """The core normalizer's display-only empty/null rule, never DTO mutation."""
    if not isinstance(value, str):
        return ''
    parts = value.strip().split(' · ')
    return ' · '.join(part.strip() for part in parts if any(
        token.strip() and token.strip().casefold() != 'null' for token in part.split(',')))


def _time_text(value):
    """Supplied96 timestamp in local time; no current/online age heuristics."""
    if type(value) is not int or not -(2**63) <= value < 2**63:
        return ''
    try:
        return datetime.fromtimestamp(value / 1000).astimezone().strftime('%d.%m.%Y %H:%M')
    except (ValueError, OSError, OverflowError):
        return ''


def _colors(theme):
    dark = resolve_theme(theme) == 'dark'
    return {'card': '#1C2432' if dark else '#FFFFFF',
            'text': '#EEF2F8' if dark else '#14171C',
            'dim': '#A5ADBA' if dark else '#92969D',
            'circle': '#343D4A' if dark else '#EBECF0',
            'glyph': '#A5ADBA' if dark else '#8D9096',
            'hover': '#293241' if dark else '#F5F6F8',
            'red': '#FF5D6C' if dark else '#F04452'}


class DeviceSessionsIntro(QFrame):
    def __init__(self, parent=None, *, theme='light', language='ru'):
        super().__init__(parent)
        self.setObjectName('DeviceSessionsIntro')
        self.setMinimumHeight(200)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._language = normalize_language(language)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 20)
        layout.setSpacing(0)
        self.icon_label = _label('DeviceIntroIcon')
        self.icon_label.setFixedSize(72, 72)
        self.icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.icon_label, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addSpacing(12)
        self.title_label = _label('DeviceIntroTitle')
        self.title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.title_label)
        layout.addSpacing(4)
        self.subtitle_label = _label('DeviceIntroSubtitle')
        self.subtitle_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.subtitle_label)
        layout.addStretch(1)
        self.set_language(self._language)
        self.apply_theme(theme)

    def _alive(self):
        return isValid(self) and all(isValid(widget) for widget in (
            self.icon_label, self.title_label, self.subtitle_label))

    def set_language(self, language):
        if not self._alive():
            return
        self._language = normalize_language(language)
        self.title_label.setText(tr(INTRO_TITLE, self._language))
        self.subtitle_label.setText(tr(INTRO_SUBTITLE, self._language))
        self.setAccessibleName(self.title_label.text())
        self.setAccessibleDescription(self.subtitle_label.text())

    retranslate = set_language

    def apply_theme(self, theme):
        if not self._alive():
            return
        palette = _colors(theme)
        self.setStyleSheet(f'''
            QFrame#DeviceSessionsIntro {{background:{palette['card']};border:0;border-radius:16px;}}
            QLabel {{background:transparent;border:0;}}
            QLabel#DeviceIntroIcon {{background:{palette['circle']};border-radius:36px;}}
            QLabel#DeviceIntroTitle {{color:{palette['text']};font-size:16px;font-weight:600;}}
            QLabel#DeviceIntroSubtitle {{color:{palette['dim']};font-size:14px;}}
        ''')
        font = self.title_label.font()
        font.setPixelSize(16)
        font.setWeight(QFont.Weight.DemiBold)
        self.title_label.setFont(font)
        self.icon_label.setPixmap(line_icon('devices', palette['glyph'], 38).pixmap(38, 38))


class DeviceSessionRow(QFrame):
    """A read-only row for core.list_sessions output. time is never a session ID."""
    def __init__(self, session, parent=None, *, theme='light', language='ru'):
        super().__init__(parent)
        self.setObjectName('DeviceSessionRow')
        self.setMinimumHeight(88)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._language = normalize_language(language)
        # Only the known normalized fields are retained. No raw session object,
        # unrelated credentials or optional guessed online status is kept.
        source = session if isinstance(session, dict) else {}
        self._title = _display_text(source.get('client')) or _display_text(source.get('label'))
        self._info = _display_text(source.get('info'))
        self._location = _display_text(source.get('location'))
        self._current = source.get('current') is True
        self._time = _time_text(source.get('time'))
        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(16)
        body = QVBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(3)
        self.title_label = _label('DeviceSessionTitle')
        self.info_label = _label('DeviceSessionMetadata')
        self.location_label = _label('DeviceSessionMetadata')
        body.addWidget(self.title_label)
        body.addWidget(self.info_label)
        body.addWidget(self.location_label)
        layout.addLayout(body, 1)
        self.time_label = _label('DeviceSessionTime')
        self.time_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
        self.time_label.setFixedWidth(104)
        layout.addWidget(self.time_label, 0, Qt.AlignmentFlag.AlignTop)
        self.info_label.setText(self._info)
        self.info_label.setVisible(bool(self._info))
        self.location_label.setText(self._location)
        self.location_label.setVisible(bool(self._location))
        self.time_label.setText(self._time)
        self.time_label.setVisible(bool(self._time))
        self.set_language(self._language)
        self.apply_theme(theme)

    def _alive(self):
        return isValid(self) and all(isValid(widget) for widget in (
            self.title_label, self.info_label, self.location_label, self.time_label))

    def set_language(self, language):
        if not self._alive():
            return
        self._language = normalize_language(language)
        title = self._title or tr('Устройство', self._language)
        if self._current:
            title += ' (' + tr('Текущая', self._language) + ')'
        self.title_label.setText(title)
        self.setAccessibleName(title)
        self.setAccessibleDescription(' · '.join(text for text in (
            self._info, self._location, self._time) if text))
        self._adjust_height()

    retranslate = set_language

    def text(self):
        """Human-readable display compatibility, without serializing the DTO."""
        if not self._alive():
            return ''
        return '\n'.join(widget.text() for widget in (
            self.title_label, self.info_label, self.location_label, self.time_label) if widget.text())

    def _adjust_height(self):
        if not self._alive():
            return
        margins = self.layout().contentsMargins()
        width = max(40, self.width() - margins.left() - margins.right()
                    - (self.time_label.width() + self.layout().spacing() if self._time else 0))
        labels = [widget for widget in (self.title_label, self.info_label, self.location_label) if widget.text()]
        height = sum(widget.fontMetrics().boundingRect(
            QRect(0, 0, width, 10000), Qt.TextFlag.TextWordWrap, widget.text()).height() for widget in labels)
        height += max(0, len(labels) - 1) * 3
        self.setMinimumHeight(max(88, height + margins.top() + margins.bottom() + 2))

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        self._adjust_height()
        if self._alive():
            self.layout().setGeometry(self.contentsRect())

    def apply_theme(self, theme):
        if not self._alive():
            return
        palette = _colors(theme)
        self.setStyleSheet(f'''
            QFrame#DeviceSessionRow {{background:{palette['card']};border:0;border-radius:16px;}}
            QLabel {{background:transparent;border:0;}}
            QLabel#DeviceSessionTitle {{color:{palette['text']};font-size:16px;font-weight:600;}}
            QLabel#DeviceSessionMetadata {{color:{palette['dim']};font-size:14px;}}
            QLabel#DeviceSessionTime {{color:{palette['dim']};font-size:12px;}}
        ''')
        font = self.title_label.font()
        font.setPixelSize(16)
        font.setWeight(QFont.Weight.DemiBold)
        self.title_label.setFont(font)
        self._adjust_height()


class DeviceSessionsLogoutRow(QPushButton):
    """An explicit action surface; the owner connects clicked to its existing guard."""
    def __init__(self, parent=None, *, theme='light', language='ru'):
        super().__init__(parent)
        self.setObjectName('DeviceSessionsLogoutRow')
        self.setMinimumHeight(64)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        self.title_label = _label('DeviceLogoutTitle')
        self.title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.title_label)
        self.set_language(language)
        self.apply_theme(theme)

    def set_language(self, language):
        if not isValid(self) or not isValid(self.title_label):
            return
        text = tr(LOGOUT_TITLE, normalize_language(language))
        self.title_label.setText(text)
        self.setAccessibleName(text)
        self._adjust_height()

    retranslate = set_language

    def text(self):
        return self.title_label.text() if isValid(self) and isValid(self.title_label) else ''

    def setText(self, text):  # noqa: N802
        # Keep the standard QPushButton setter compatible with the workspace's
        # opt-in translations without painting a duplicate native button label.
        if not isValid(self) or not hasattr(self, 'title_label') or not isValid(self.title_label):
            return
        self.title_label.setText(text)
        self.setAccessibleName(text)
        self._adjust_height()

    def _adjust_height(self):
        if not isValid(self) or not isValid(self.title_label):
            return
        margins = self.layout().contentsMargins()
        width = max(40, self.width() - margins.left() - margins.right())
        height = self.title_label.fontMetrics().boundingRect(
            QRect(0, 0, width, 10000), Qt.TextFlag.TextWordWrap, self.title_label.text()).height()
        self.setMinimumHeight(max(64, height + margins.top() + margins.bottom() + 2))
        self.layout().setGeometry(self.contentsRect())

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        self._adjust_height()

    def apply_theme(self, theme):
        if not isValid(self) or not isValid(self.title_label):
            return
        self._theme = theme
        palette = _colors(theme)
        self.setStyleSheet(f'''
            QPushButton#DeviceSessionsLogoutRow {{background:{palette['card']};border:0;border-radius:16px;}}
            QPushButton#DeviceSessionsLogoutRow:hover {{background:{palette['hover']};}}
            QPushButton#DeviceSessionsLogoutRow:focus {{border:1px solid #0085FF;}}
            QLabel#DeviceLogoutTitle {{background:transparent;color:{palette['red']};font-size:16px;font-weight:600;}}
        ''')
        # Qt's ancestor-pseudostate descendant selector does not reliably track
        # the child label's enabled state. Apply its actual color explicitly.
        self.title_label.setStyleSheet(
            'color:' + (palette['red'] if self.isEnabled() else palette['dim']) + ';background:transparent;')
        self._adjust_height()

    def changeEvent(self, event):  # noqa: N802
        super().changeEvent(event)
        if event.type() == QEvent.Type.EnabledChange and hasattr(self, 'title_label'):
            self.apply_theme(getattr(self, '_theme', 'light'))
