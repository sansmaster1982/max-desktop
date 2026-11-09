"""Small native vector icons used by the desktop navigation.

The paths are application-owned geometry rather than platform emoji, so the
navigation keeps the same weight and alignment across Windows font versions.
"""
from __future__ import annotations

from functools import lru_cache

from PySide6.QtCore import QByteArray, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QLabel, QPushButton, QVBoxLayout


_PATHS = {
    'rail_chats': '<path d="M20 11.3a7.6 7.6 0 0 1-8 7.2 9.4 9.4 0 0 1-3-.5L4 20l1.2-4A6.8 6.8 0 0 1 4 12a7.6 7.6 0 0 1 8-7.2 7.7 7.7 0 0 1 8 6.5ZM7 11.7a1 1 0 1 0 2 0 1 1 0 1 0-2 0Zm4 0a1 1 0 1 0 2 0 1 1 0 1 0-2 0Zm4 0a1 1 0 1 0 2 0 1 1 0 1 0-2 0Z" fill="currentColor" fill-rule="evenodd" stroke="none"/>',
    'rail_unread': '<path d="M19.5 10.9a7.5 7.5 0 0 1-7.5 7.2c-1.1 0-2.2-.2-3.1-.5l-4.9 2 1.2-4A7 7 0 0 1 4 11.7a7.7 7.7 0 0 1 8-7.3c1.7 0 3.4.6 4.6 1.6Z" fill="currentColor" stroke="none"/><circle cx="18.5" cy="5.5" r="3.3" fill="currentColor" stroke="white" stroke-width="1.3"/>',
    'rail_contacts': '<circle cx="8.6" cy="7.2" r="3.3" fill="currentColor" stroke="none"/><path d="M2.8 19v-1.4a5.8 5.8 0 0 1 11.6 0V19c0 1.6-11.6 1.6-11.6 0Z" fill="currentColor" stroke="none"/><path d="M15.6 4.7a3.2 3.2 0 1 1 0 5.8 5.6 5.6 0 0 0 0-5.8Zm.8 8a5.6 5.6 0 0 1 4.9 5.5v1.1c0 .8-2.4 1.2-5 1a5.8 5.8 0 0 0 .4-2.3 8.2 8.2 0 0 0-1.9-5.2c.6-.1 1.1-.1 1.6-.1Z" fill="currentColor" stroke="none"/>',
    'rail_phone': '<path d="M7 3.5 10 8 8 10a15.1 15.1 0 0 0 6 6l2-2 4.5 3c.6.4.6 1.3.2 1.8l-1.2 1.3c-.7.7-1.8 1-2.8.7A22 22 0 0 1 3.2 7.3c-.3-1 .0-2.1.7-2.8l1.3-1.2c.5-.4 1.4-.4 1.8.2Z" fill="currentColor" stroke="none"/>',
    'rail_settings': '<path d="m10 2-.8 3.1-2 .9-2.8-1.5L2 8l2.3 2.1v2.8L2 15l2.4 3.5 2.8-1.4 2 .8.8 3.1h4l.8-3.1 2-.8 2.8 1.4L22 15l-2.3-2.1v-2.8L22 8l-2.4-3.5L16.8 6l-2-.9L14 2ZM8.9 11.5a3.1 3.1 0 1 0 6.2 0 3.1 3.1 0 1 0-6.2 0Z" fill="currentColor" fill-rule="evenodd" stroke="none"/>',
    'chats': '<path d="M20 11.3a7.6 7.6 0 0 1-8 7.2 9.4 9.4 0 0 1-3-.5L4 20l1.2-4A6.8 6.8 0 0 1 4 12a7.6 7.6 0 0 1 8-7.2 7.7 7.7 0 0 1 8 6.5Z"/><path d="M8 11.7h.1m3.8 0h.1m3.8 0h.1"/>',
    'unread': '<path d="M17 6.1a7.5 7.5 0 0 0-5-1.8 7.7 7.7 0 0 0-8 7.3c0 1.4.4 2.7 1.2 3.9L4 19.5l4.9-2a9.8 9.8 0 0 0 3.1.5 7.5 7.5 0 0 0 8-7"/><circle cx="19" cy="5" r="3.1" fill="currentColor" stroke="none"/>',
    'channels': '<path d="M5 8.5h9l5-3v13l-5-3H5a2 2 0 0 1-2-2v-3a2 2 0 0 1 2-2Z"/><path d="m7 15.5 1.2 4h3.3l-1.2-4m11-7.2a7.8 7.8 0 0 1 0 7.4"/>',
    'contacts': '<circle cx="9" cy="7" r="3"/><path d="M3.2 20v-2a5.8 5.8 0 0 1 11.6 0v2Zm13-10a3 3 0 0 0 0-6m1.2 10a5 5 0 0 1 3.4 4.7V20"/>',
    'phone': '<path d="M7 3.5 10 8 8 10a15.1 15.1 0 0 0 6 6l2-2 4.5 3c.6.4.6 1.3.2 1.8l-1.2 1.3c-.7.7-1.8 1-2.8.7A22 22 0 0 1 3.2 7.3c-.3-1 .0-2.1.7-2.8l1.3-1.2c.5-.4 1.4-.4 1.8.2Z"/>',
    'video': '<rect x="3" y="6" width="12" height="12" rx="3"/><path d="m15 10 6-3v10l-6-3Z"/>',
    'settings': '<path d="m10 2-.8 3.1-2 .9-2.8-1.5L2 8l2.3 2.1v2.8L2 15l2.4 3.5 2.8-1.4 2 .8.8 3.1h4l.8-3.1 2-.8 2.8 1.4L22 15l-2.3-2.1v-2.8L22 8l-2.4-3.5L16.8 6l-2-.9L14 2Z"/><circle cx="12" cy="11.5" r="3.1"/>',
    'preview': '<path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/>',
    'report': '<path d="M8 3h8l5 5v8l-5 5H8l-5-5V8Z"/><path d="M12 7v6m0 4h.01"/>',
    'logout': '<path d="M10 3H4v18h6m4-14 5 5-5 5m-6-5h11"/>',
    'block': '<circle cx="12" cy="12" r="9"/><path d="m6 6 12 12"/>',
    'search': '<circle cx="10.5" cy="10.5" r="6.7"/><path d="m15.5 15.5 5 5"/>',
    'plus': '<path d="M12 4v16M4 12h16"/>',
    'pin': '<path d="m15 3 6 6-3.2.7-3.1 4.7.4 2.4-2.3.7-7-7 .7-2.3 2.4.4 4.7-3.1ZM9 15l-6 6"/>',
    'muted': '<path d="M11 4 6 8H3v8h3l5 4Zm5 5 6 6m0-6-6 6"/>',
    'check': '<path d="m4 12 5 5L20 6"/>',
    'double_check': '<path d="m2 12 4 4L16 6m-4 10 3 3 8-9"/>',
    'verified': '<path d="m12 2 2.5 1.8 3.1-.1.9 3 2.5 1.8-1 3 1 3-2.5 1.8-.9 3-3.1-.1L12 22l-2.5-1.8-3.1.1-.9-3L3 15.5l1-3-1-3L5.5 7l.9-3 3.1.1Z" fill="currentColor" stroke="none"/><path d="m7.5 12 3 3 6-6" stroke="white"/>',
    'codes': '<path d="m12 2 8 3v6c0 5-3.2 8.1-8 11-4.8-2.9-8-6-8-11V5Z"/><path d="m8 12 2.5 2.5L16 9"/>',
    'family_shield': '<path d="m12 2 8 3v6c0 5-3.2 8.1-8 11-4.8-2.9-8-6-8-11V5Z"/><path d="m12 15-3.3-3.2a2.2 2.2 0 0 1 3.3-2.9 2.2 2.2 0 0 1 3.3 2.9Z"/>',
    'more': '<circle cx="5" cy="12" r="1.6" fill="currentColor" stroke="none"/><circle cx="12" cy="12" r="1.6" fill="currentColor" stroke="none"/><circle cx="19" cy="12" r="1.6" fill="currentColor" stroke="none"/>',
    'close': '<path d="m6 6 12 12M18 6 6 18"/>',
    'attach': '<path d="m9 12 6-6a3.3 3.3 0 0 1 4.7 4.7l-8.3 8.4a5 5 0 0 1-7.1-7.1l8.4-8.3a1.7 1.7 0 0 1 2.4 2.4l-8.2 8.2a1.6 1.6 0 0 0 2.3 2.3l7.1-7.1"/>',
    'microphone': '<rect x="8.5" y="2" width="7" height="13" rx="3.5"/><path d="M5.5 10.5v1a6.5 6.5 0 0 0 13 0v-1M12 18v4m-4 0h8"/>',
    'send': '<path d="m3 3 19 9L3 21l2-9Zm2 9h17"/>',
    'stop': '<rect x="5" y="5" width="14" height="14" rx="2"/>',
    'chevron': '<path d="m9 5 7 7-7 7"/>',
    'back': '<path d="m15 5-7 7 7 7"/>',
    'more_vertical': '<circle cx="12" cy="5" r="1.6" fill="currentColor" stroke="none"/><circle cx="12" cy="12" r="1.6" fill="currentColor" stroke="none"/><circle cx="12" cy="19" r="1.6" fill="currentColor" stroke="none"/>',
    'share': '<path d="M12 15V3m-5 5 5-5 5 5M5 12v7a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-7"/>',
    'trash': '<path d="M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7m4-7v7"/>',
    'add_profile': '<circle cx="9" cy="6" r="3"/><path d="M3 19v-2a6 6 0 0 1 12 0v2Zm15-9V4m-3 3h6"/>',
    'link': '<path d="m10 14 4-4m-6 6-2 2a4.2 4.2 0 0 1-6-6l4-4a4.2 4.2 0 0 1 6 0m0 0 2-2a4.2 4.2 0 0 1 6 6l-4 4a4.2 4.2 0 0 1-6 0"/>',
    'bell': '<path d="M5 10a7 7 0 0 1 14 0c0 6 2 6 2 7H3c0-1 2-1 2-7Zm5 11h4M12 1v2"/>',
    'lock': '<rect x="4.5" y="10" width="15" height="11" rx="3"/><path d="M8 10V6a4 4 0 0 1 8 0v4m-4 4v3"/>',
    'devices': '<rect x="3" y="3" width="15" height="12" rx="2"/><path d="M8 20h5m-2.5-5v5"/><rect x="15" y="10" width="6" height="11" rx="1.5"/>',
    'history': '<path d="M4 7a9 9 0 1 1-1 7m0-8v5h5m4-5v6l4 2"/>',
    'folder': '<path d="M3 7V5a2 2 0 0 1 2-2h5l3 3h6a2 2 0 0 1 2 2v1M3 7h18l-2 13H2Z"/>',
    'appearance': '<path d="m7 3 1.4 4.6L13 9l-4.6 1.4L7 15l-1.4-4.6L1 9l4.6-1.4ZM17 10l1.4 4.6L23 16l-4.6 1.4L17 22l-1.4-4.6L11 16l4.6-1.4Z"/>',
    'database': '<ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v14c0 4 16 4 16 0V5M4 12c0 4 16 4 16 0"/>',
    'globe': '<circle cx="12" cy="12" r="9"/><ellipse cx="12" cy="12" rx="4" ry="9"/><path d="M3 12h18"/>',
    'keyboard': '<rect x="2" y="5" width="20" height="14" rx="3"/><path d="M6 9h.1m3.8 0h.1m3.8 0h.1m3.8 0h.1M6 12h.1m3.8 0h.1m3.8 0h.1m3.8 0h.1M7 16h10"/>',
    'camera': '<path d="M4 7h3l2-3h6l2 3h3a1 1 0 0 1 1 1v11H3V8a1 1 0 0 1 1-1Z"/><circle cx="12" cy="13" r="3"/>',
    'business': '<rect x="3" y="7" width="18" height="14" rx="3"/><path d="M8 7V5a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2M3 12a23 23 0 0 0 18 0m-11 0v3h4v-3"/>',
    'help': '<circle cx="12" cy="12" r="9"/><path d="M9 8a3 3 0 0 1 6 0c0 2-3 2-3 4v1m0 4h.01"/>',
    'info': '<circle cx="12" cy="12" r="9"/><path d="M12 11v6m0-10h.01"/>',
}


@lru_cache(maxsize=160)
def line_icon(name: str, color: str = '#777D85', size: int = 24) -> QIcon:
    paths = _PATHS.get(name, _PATHS['more']).replace('currentColor', color)
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" '
        f'viewBox="0 0 24 24" fill="none" stroke="{color}" '
        'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">'
        + paths + '</svg>'
    )
    pixmap = QPixmap(size * 2, size * 2)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    QSvgRenderer(QByteArray(svg.encode('utf-8'))).render(painter)
    painter.end()
    pixmap.setDevicePixelRatio(2)
    return QIcon(pixmap)


class RailButton(QPushButton):
    """The icon, short label and real unread counter form one keyboard target."""

    def __init__(self, icon_name: str, text: str, *, checkable: bool = False, parent=None):
        super().__init__(parent)
        self.icon_name = icon_name
        self._counter = 0
        self._dark = False
        self.setObjectName('RailButton')
        self.setCheckable(checkable)
        self.setFixedSize(64, 66)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(text)
        self.setAccessibleName(text)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 9, 2, 7)
        layout.setSpacing(4)
        self.icon_label = QLabel()
        self.icon_label.setFixedHeight(25)
        self.icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.label = QLabel(text)
        self.label.setObjectName('RailLabel')
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.icon_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        layout.addWidget(self.icon_label)
        layout.addWidget(self.label)
        self.badge = QLabel(self)
        self.badge.setObjectName('RailBadge')
        self.badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.badge.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.toggled.connect(self._refresh)
        self._refresh()

    def set_counter(self, count: int) -> None:
        self._counter = max(0, int(count or 0))
        self._refresh()

    def set_dark(self, dark: bool) -> None:
        self._dark = dark
        self._refresh()

    def _refresh(self, *_args) -> None:
        selected = self.isChecked()
        color = '#0085FF' if selected else ('#9CA8BA' if self._dark else '#90959B')
        if selected and self.icon_name == 'settings':
            color = '#EAECEF' if self._dark else '#17191D'
        rail_icon = {'chats': 'rail_chats', 'unread': 'rail_unread',
                     'contacts': 'rail_contacts', 'phone': 'rail_phone'}.get(self.icon_name, self.icon_name)
        if selected and self.icon_name == 'settings':
            rail_icon = 'rail_settings'
        self.icon_label.setPixmap(line_icon(rail_icon, color, 25).pixmap(25, 25))
        self.label.setStyleSheet(f'background:transparent;color:{color};font-size:10px;font-weight:400;')
        self.badge.setVisible(self._counter > 0)
        if self._counter:
            text = str(self._counter) if self._counter < 1000 else '999+'
            self.badge.setText(text)
            width = max(18, self.badge.fontMetrics().horizontalAdvance(text) + 8)
            self.badge.setGeometry(min(43, 61 - width), 4, width, 17)
            self.badge.setStyleSheet(f'background:{color};color:white;border:1px solid white;border-radius:8px;font-size:10px;padding:0;')

