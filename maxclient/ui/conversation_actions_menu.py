"""Scoped native popup presentation; descriptors carry all gates and identities."""
from __future__ import annotations

import math

from PySide6.QtCore import Qt, Signal, QRect
from PySide6.QtGui import QColor, QIcon, QPainter, QPen
from PySide6.QtWidgets import QMenu
from shiboken6 import isValid

from ..core.i18n import normalize_language, tr
from .theme import palette_for
from .widgets.line_icon import line_icon


SOUND_CHOICES = (
    ('На 1 час', 3600000), ('На 4 часа', 14400000),
    ('На 1 день', 86400000), ('Навсегда', -1),
)

_UNAVAILABLE_REASONS = {
    'report': 'Жалоба недоступна для текущих прав или состояния чата.',
    'leave': 'Выход недоступен для текущих прав или состояния чата.',
    'delete': 'Удаление этого вида чата пока не поддерживается.',
    'mute': 'Ожидается подтверждение возможности изменить звук.',
}


def _value_copy(value, depth=0):
    """Copy plain structured data without invoking arbitrary object hooks."""
    if depth > 8:
        raise ValueError('menu value nesting exceeds presentation contract')
    if value is None or type(value) in (str, int, bool):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    if type(value) is list:
        return [_value_copy(child, depth + 1) for child in value]
    if type(value) is dict and all(type(key) is str for key in value):
        return {key: _value_copy(child, depth + 1) for key, child in value.items()}
    raise ValueError('menu value is not plain structured data')


def _entry_shape(entries):
    """Separators and submenu nesting, exactly as _populate builds them."""
    shape = []
    for row in entries if isinstance(entries, list) else []:
        if not isinstance(row, dict) or not isinstance(row.get('label'), str):
            continue
        children = row.get('children')
        shape.append((row.get('separator_before') is True,
                      _entry_shape(children) if isinstance(children, list) else None))
    return tuple(shape)


class ConversationActionsMenu(QMenu):
    requested = Signal(str, object, object)  # action, value, opaque context

    def __init__(self, context, entries, parent=None, *, theme='light', allowed=None,
                 anchor=None, anchor_requested=None, native_rows=False,
                 unavailable_label=None, language=None):
        super().__init__(parent)
        settings = getattr(parent, 'settings', None)
        if settings is None and parent is not None:
            settings = getattr(parent.window(), 'settings', None)
        self._language = normalize_language(language if language is not None else
                                            getattr(settings, 'ui_language', 'ru'))
        self._context = context
        self._allowed = allowed or (lambda _row: True)
        self._revoked = False
        self._dispatch_depth = 0
        self._rows = []
        self._submenus = []
        self._anchor = anchor
        self._anchor_requested = anchor_requested
        self._anchor_press_consumed = False
        self._native_rows = native_rows
        self._unavailable_label = unavailable_label
        self.setObjectName('ConversationActionsMenu')
        self.setToolTipsVisible(True)
        self._populate(entries if isinstance(entries, list) else [])
        self.apply_theme(theme)

    def _populate(self, entries):
        for value in entries:
            if not isinstance(value, dict) or not isinstance(value.get('label'), str):
                continue
            row = dict(value)
            if row.get('separator_before') is True and self.actions():
                self.addSeparator()
            label = self._display_label(row).replace('&', '&&')  # no implicit mnemonic
            if isinstance(row.get('children'), list):
                submenu = ConversationActionsMenu(
                    self._context, row['children'], self,
                    allowed=lambda child, ancestor=row: (ancestor.get('available') is True and
                                                         self._allowed(ancestor) and self._allowed(child)),
                    native_rows=self._native_rows, unavailable_label=self._unavailable_label,
                    language=self._language)
                submenu.setTitle(label)
                submenu.requested.connect(self.requested.emit)
                action = self.addMenu(submenu)
                self._submenus.append(submenu)
                self._rows.append((action, row, submenu))
                continue
            valid = isinstance(row.get('action'), str) and bool(row['action'])
            try:
                row['value'] = _value_copy(row.get('value'))
            except ValueError:
                valid = False
            row['_valid'] = valid
            action = self.addAction(label)
            if type(row.get('checked')) is bool:
                action.setCheckable(True)
                action.setChecked(row['checked'])
            action.triggered.connect(lambda _checked=False, item=action, descriptor=row:
                                     self._activate(item, descriptor))
            self._rows.append((action, row, None))
        self.revalidate()

    def _display_label(self, row):
        source = row.get('label_source')
        return tr(source, self._language) if isinstance(source, str) else row['label']

    def set_language(self, language):
        """Update presentation only; never recreate actions or reset revocation."""
        if not isValid(self):
            return
        self._language = normalize_language(language)
        for action, row, submenu in self._rows:
            label = self._display_label(row).replace('&', '&&')
            action.setText(label)
            if submenu is not None:
                submenu.setTitle(label)
                submenu.set_language(self._language)
        self.revalidate()
        self.update()

    retranslate = set_language

    def replace_entries(self, entries):
        """Replace an open lazy submenu only; old actions are disconnected."""
        if not isValid(self) or self._revoked:
            return
        # A visible QMenu resizes its native popup on every removed and added
        # action, so a rebuild briefly showed it collapsed to a few pixels.
        # Hold the current size during the rebuild, then resize once.
        held = (self.minimumSize(), self.maximumSize()) if self.isVisible() else None
        if held is not None:
            self.setFixedSize(self.size())
        try:
            for child in self._submenus:
                child.cancel()
                child.deleteLater()
            self.clear()
            self._rows = []
            self._submenus = []
            self._populate(entries if isinstance(entries, list) else [])
            self.apply_theme(self._theme)
        finally:
            if held is not None and isValid(self):
                self.setMinimumSize(held[0])
                self.setMaximumSize(held[1])
                self.resize(self.sizeHint())

    def _shape(self):
        return tuple((row.get('separator_before') is True,
                      submenu._shape() if submenu is not None else None)
                     for _action, row, submenu in self._rows)

    def update_entries(self, entries):
        """Refresh an open popup in place when only labels, values or gates changed.

        Clearing a visible QMenu resizes the native popup once per removed and
        added action, so the open menu collapses to a few pixels and grows back.
        With the same rows and submenus every QAction stays; its descriptor dict
        is updated in place, so the triggered lambda and submenu gates read the
        new values. A different shape still rebuilds.
        """
        if not isValid(self) or self._revoked:
            return
        entries = entries if isinstance(entries, list) else []
        if (_entry_shape(entries) != self._shape()
                or any(child._revoked for child in self._submenus)):
            self.replace_entries(entries)
            return
        self._update_rows(entries)
        self.revalidate()

    def _update_rows(self, entries):
        palette = palette_for(self._theme)
        rows = [value for value in entries
                if isinstance(value, dict) and isinstance(value.get('label'), str)]
        for (action, row, submenu), value in zip(self._rows, rows):
            fresh = dict(value)
            if submenu is None:
                valid = isinstance(fresh.get('action'), str) and bool(fresh['action'])
                try:
                    fresh['value'] = _value_copy(fresh.get('value'))
                except ValueError:
                    valid = False
                fresh['_valid'] = valid
            icon = (row.get('icon'), row.get('danger'))
            row.clear()
            row.update(fresh)
            label = self._display_label(row).replace('&', '&&')
            if submenu is not None:
                submenu.setTitle(label)
                submenu._update_rows(row['children'])
            else:
                action.setText(label)
                checkable = type(row.get('checked')) is bool
                action.setCheckable(checkable)
                if checkable:
                    action.setChecked(row['checked'])
            if (row.get('icon'), row.get('danger')) != icon:
                if isinstance(row.get('icon'), str):
                    action.setIcon(line_icon(row['icon'], '#F15D6B' if row.get('danger') else palette.text_dim, 18))
                    action.setIconVisibleInMenu(True)
                else:
                    action.setIcon(QIcon())

    def revalidate(self):
        if not isValid(self):
            return
        for action, row, submenu in self._rows:
            allowed = (not self._revoked and row.get('available') is True and self._allowed(row))
            if submenu is not None:
                submenu.revalidate()
                allowed = allowed and any(item.isEnabled() and not item.isSeparator()
                                          for item in submenu.actions())
            else:
                allowed = allowed and row.get('_valid') is True
            action.setEnabled(allowed)
            source = row.get('unavailable_reason_source')
            reason = (tr(source, self._language) if isinstance(source, str) else
                      row.get('unavailable_reason'))
            if not isinstance(reason, str) or not reason:
                fallback = _UNAVAILABLE_REASONS.get(row.get('action'))
                reason = tr(fallback, self._language) if fallback else (
                    self._unavailable_label if isinstance(self._unavailable_label, str) else
                    tr('Действие недоступно', self._language))
            action.setToolTip('' if allowed else reason)

    def paintEvent(self, event):  # noqa: N802
        super().paintEvent(event)
        if not self._native_rows:
            return
        # QMenu stylesheets do not expose per-QAction foreground colors. Paint
        # only our opt-in row contents, retaining QMenu sizing, focus, keyboard,
        # submenu and dispatch behavior. Existing Info popups keep native paint.
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        palette = palette_for(self._theme)
        glyphs = any(not action.icon().isNull() or action.isCheckable()
                     for action, _row, _submenu in self._rows)
        for action, row, _submenu in self._rows:
            rect = self.actionGeometry(action)
            if not rect.isValid() or not event.rect().intersects(rect):
                continue
            painter.fillRect(rect, QColor(palette.surface))
            if action.isEnabled() and self.activeAction() is action:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(palette.selected))
                painter.drawRoundedRect(rect, 7, 7)
            color = (palette.text_dim if not action.isEnabled() else
                     '#F15D6B' if row.get('danger') else palette.text)
            painter.setPen(QColor(color))
            painter.setFont(action.font())
            left = rect.left() + (38 if glyphs else 18)
            text_rect = QRect(left, rect.top(), rect.right() - left - 18, rect.height())
            label = painter.fontMetrics().elidedText(self._display_label(row), Qt.TextElideMode.ElideRight,
                                                      text_rect.width())
            painter.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, label)
            if not action.icon().isNull():
                mode = QIcon.Mode.Normal if action.isEnabled() else QIcon.Mode.Disabled
                action.icon().paint(painter, QRect(rect.left() + 1, rect.center().y() - 9, 18, 18),
                                    Qt.AlignmentFlag.AlignCenter, mode)
            elif action.isCheckable() and action.isChecked():
                painter.setPen(QPen(QColor(color), 1.6))
                x, y = rect.left() + 9, rect.center().y()
                painter.drawLine(x - 4, y, x - 1, y + 3)
                painter.drawLine(x - 1, y + 3, x + 5, y - 3)
            if _submenu is not None:
                painter.setPen(QPen(QColor(color), 1.4))
                x, y = rect.right() - 11, rect.center().y()
                painter.drawLine(x - 2, y - 3, x + 1, y)
                painter.drawLine(x + 1, y, x - 2, y + 3)

    def _activate(self, action, row):
        if (not isValid(self) or not isValid(action) or self._revoked or
                not (self.isVisible() or self._dispatch_depth) or
                (not self._dispatch_depth and self._ancestor_revoked()) or
                row.get('_valid') is not True or row.get('available') is not True or
                not self._allowed(row)):
            return
        if action.isCheckable():
            # QAction toggles before triggered; keep the server-confirmed check.
            action.setChecked(row['checked'])
        self._revoked = True
        self.requested.emit(row['action'], _value_copy(row['value']), self._context)
        if isValid(self):
            self.hide()

    def cancel(self):
        self._revoked = True
        for child in self._submenus:
            child.cancel()
        if isValid(self):
            super().close()

    def close(self):
        self.cancel()
        return True

    def hideEvent(self, event):  # noqa: N802
        # Native QMenu hides BEFORE emitting triggered. A real click/Enter is
        # allowed only during that exact dispatch; outside hides revoke it.
        # A submenu also hides whenever the pointer moves to another row of
        # its still open parent; revoking it then left the row dead until the
        # popup was rebuilt. A hidden submenu cannot dispatch anyway (_activate
        # requires visibility or a dispatch, and a live parent outside one).
        if not self._dispatch_depth and not self._parent_menu_open():
            self._revoked = True
        super().hideEvent(event)

    def _parent_menu_open(self):
        parent = self.parent()
        return (isinstance(parent, ConversationActionsMenu) and isValid(parent)
                and not parent._revoked and parent.isVisible())

    def _ancestor_revoked(self):
        parent = self.parent()
        while isinstance(parent, ConversationActionsMenu):
            if not isValid(parent) or parent._revoked:
                return True
            parent = parent.parent()
        return False

    def mouseReleaseEvent(self, event):  # noqa: N802
        if self._anchor_press_consumed:
            self._anchor_press_consumed = False
            event.accept()
            return
        self._dispatch_depth += 1
        try:
            super().mouseReleaseEvent(event)
        finally:
            self._dispatch_depth -= 1
            if isValid(self) and not self.isVisible() and not self._parent_menu_open():
                self._revoked = True

    def mousePressEvent(self, event):  # noqa: N802
        anchor = self._anchor
        if (not self._revoked and self.isVisible() and event.button() == Qt.MouseButton.LeftButton
                and anchor is not None and isValid(anchor) and callable(self._anchor_requested)
                and anchor.rect().contains(anchor.mapFromGlobal(event.globalPosition().toPoint()))):
            # A hovered native popup grabs the pointer. Preserve the actual
            # desktop Sound-button click instead of only dismissing the popup.
            self._anchor_press_consumed = True
            self.setAttribute(Qt.WidgetAttribute.WA_NoMouseReplay, True)
            self.cancel()
            self._anchor_requested()
            event.accept()
            return
        super().mousePressEvent(event)

    def keyPressEvent(self, event):  # noqa: N802
        # Escape in a submenu only steps back to its open parent (native QMenu).
        if event.key() == Qt.Key.Key_Escape and not self._parent_menu_open():
            self.cancel()
            event.accept()
            return
        self._dispatch_depth += 1
        try:
            super().keyPressEvent(event)
        finally:
            self._dispatch_depth -= 1
            if isValid(self) and not self.isVisible() and not self._parent_menu_open():
                self._revoked = True

    def apply_theme(self, theme):
        self._theme = theme
        p = palette_for(theme)
        # QMenu's minimum width does not stretch short QAction rectangles.
        # Reserve the same 208px content width (172 + 18px padding per side)
        # for native-painted rows, so paint and mouse hit areas agree.
        row_min_width = 'min-width:172px;' if self._native_rows else ''
        for action, row, _submenu in self._rows:
            if isinstance(row.get('icon'), str):
                action.setIcon(line_icon(row['icon'], '#F15D6B' if row.get('danger') else p.text_dim, 18))
                action.setIconVisibleInMenu(True)
        self.setStyleSheet(f'''
            QMenu#ConversationActionsMenu {{background:{p.surface};color:{p.text};
                border:1px solid {p.border};border-radius:12px;padding:6px;min-width:208px;}}
            QMenu#ConversationActionsMenu::item {{padding:10px 18px;border-radius:7px;{row_min_width}}}
            QMenu#ConversationActionsMenu::item:selected {{background:{p.selected};}}
            QMenu#ConversationActionsMenu::item:disabled {{color:{p.text_dim};}}
            QMenu#ConversationActionsMenu::separator {{height:1px;background:{p.border};margin:5px 8px;}}
        ''')
        for child in self._submenus:
            child.apply_theme(theme)
