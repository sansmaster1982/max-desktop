"""Local folder include/category draft: no service, RPC or account access.

Categories are folder rules, not a filter that expands to cached chat IDs.
The parent owns server confirmation, the current context, and other filters.
"""
from __future__ import annotations

from collections.abc import Mapping
import html

from PySide6.QtCore import QEvent, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QDialog, QFrame, QHBoxLayout, QLabel, QLayout, QLineEdit, QListWidget,
    QListWidgetItem, QPushButton, QSizePolicy, QStyledItemDelegate, QStyle,
    QToolButton, QVBoxLayout, QWidget,
)
from shiboken6 import isValid

from ...core.i18n import normalize_language, tr
from ..theme import palette_for
from .line_icon import line_icon


CATEGORIES = ((8, 'Контакты', 'contacts'), (9, 'Не контакты', 'add_profile'),
              (3, 'Группы', 'contacts'), (2, 'Каналы', 'channels'), (10, 'Боты', 'bot'))
_CATEGORY_LABELS = {identity: text for identity, text, _icon in CATEGORIES}
_ROLE = Qt.ItemDataRole.UserRole


def _ids(values, *, filters=False):
    if not isinstance(values, (list, tuple)) or any(
            type(value) is not int or not (0 <= value <= 14 if filters else -(2**63) <= value < 2**63)
            for value in values):
        raise ValueError('Invalid folder selection')
    return list(dict.fromkeys(values))


class _ChipFlow(QLayout):
    """Small height-for-width layout; chips never force horizontal scrolling."""
    def __init__(self, parent):
        super().__init__(parent)
        self._items = []
        self.setContentsMargins(0, 0, 0, 0)
        self.setSpacing(8)

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, index):
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index):
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._arrange(QRect(0, 0, width, 0), False)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._arrange(rect, True)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        return QSize(0, 32 if self._items else 0)

    def _arrange(self, rect, apply):
        x, y, line = rect.x(), rect.y(), 0
        width = max(1, rect.width())
        for item in self._items:
            hint = item.sizeHint()
            size = QSize(min(width, hint.width()), hint.height())
            if x > rect.x() and x + size.width() > rect.x() + width:
                x, y, line = rect.x(), y + line + self.spacing(), 0
            if apply:
                item.setGeometry(QRect(x, y, size.width(), size.height()))
            x += size.width() + self.spacing()
            line = max(line, size.height())
        return y - rect.y() + line


class _PickerDelegate(QStyledItemDelegate):
    def __init__(self, theme, parent):
        super().__init__(parent)
        self.palette = palette_for(theme)

    def paint(self, painter, option, index):
        p, rect = self.palette, option.rect
        enabled = bool(index.flags() & Qt.ItemFlag.ItemIsEnabled)
        checked = index.data(Qt.ItemDataRole.CheckStateRole) == Qt.CheckState.Checked.value
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(rect, QColor(p.hover if option.state & QStyle.StateFlag.State_MouseOver else p.sidebar))
        left = QRect(rect.left() + 12, rect.top() + 8, 36, 36)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(p.selected if index.data(_ROLE)[0] == 'filter' else p.surface_hi))
        painter.drawEllipse(left)
        icon = index.data(_ROLE + 1)
        color = '#0085FF' if enabled else p.text_dim
        if icon == 'bot':
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor(color), 1.8))
            face = QRectF(left.left() + 9, left.top() + 11, 18, 15)
            painter.drawRoundedRect(face, 4, 4)
            painter.drawLine(left.center().x(), left.top() + 6, left.center().x(), left.top() + 11)
            for offset in (14, 22):
                painter.drawEllipse(QRectF(left.left() + offset - 1, left.top() + 16, 2, 2))
        elif icon:
            line_icon(icon, color, 24).paint(painter, left.adjusted(6, 6, -6, -6))
        else:
            painter.setPen(QColor(p.text_dim))
            painter.drawText(left, Qt.AlignmentFlag.AlignCenter, str(index.data() or '')[:1].upper())
        check = QRectF(rect.right() - 34, rect.top() + 16, 20, 20)
        painter.setPen(QPen(QColor('#0085FF' if checked and enabled else p.border), 1.5))
        painter.setBrush(QColor('#0085FF') if checked else Qt.BrushStyle.NoBrush)
        painter.drawEllipse(check)
        if checked:
            line_icon('check', '#FFFFFF', 16).paint(painter, check.toRect().adjusted(3, 3, -3, -3))
        text_rect = rect.adjusted(62, 0, -48, 0)
        painter.setPen(QColor(p.text if enabled else p.text_dim))
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter,
                         option.fontMetrics.elidedText(str(index.data()), Qt.TextElideMode.ElideRight, text_rect.width()))
        if option.state & QStyle.StateFlag.State_HasFocus:
            painter.setPen(QPen(QColor('#0085FF'), 1))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(rect.adjusted(2, 2, -2, -2), 6, 6)
        painter.restore()

    def editorEvent(self, event, model, option, index):
        # The whole row has one toggle handler; Qt's default check hit-test must
        # not toggle it a second time, and its invisible default indicator is unused.
        return False


class _ChoiceList(QListWidget):
    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter):
            item = self.currentItem()
            if item is not None and not item.isHidden() and item.flags() & Qt.ItemFlag.ItemIsEnabled:
                self.itemClicked.emit(item)
            event.accept()
            return
        super().keyPressEvent(event)


class _FolderChatDialog(QDialog):
    def __init__(self, owner):
        super().__init__(owner)
        self.owner, self.generation = owner, owner._generation
        self.selected, self.filters = owner.values(), owner.filter_values()
        self._rows = []
        language = owner._language
        self.setObjectName('FolderChatDialog')
        self.setWindowTitle(tr('Добавить чаты', language))
        self.setModal(True)
        self.resize(400, 644)
        self.setMinimumSize(320, 360)
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 18, 16, 16)
        root.setSpacing(12)
        self.heading = QLabel(self.windowTitle())
        self.heading.setTextFormat(Qt.TextFormat.PlainText)
        self.heading.setObjectName('FolderPickerHeading')
        root.addWidget(self.heading)
        self.search = QLineEdit()
        self.search.setPlaceholderText(tr('Найти по имени', language))
        self.search.setAccessibleName(tr('Найти по имени', language))
        self.search.setClearButtonEnabled(True)
        self.search.setMinimumHeight(40)
        root.addWidget(self.search)
        self.items = _ChoiceList()
        self.items.setAccessibleName(tr('Чаты в папке', language))
        self.items.setFrameShape(QFrame.Shape.NoFrame)
        self.items.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.items.setItemDelegate(_PickerDelegate(owner._theme, self.items))
        self.items.setUniformItemSizes(True)
        self.items.setMouseTracking(True)
        root.addWidget(self.items, 1)
        for identity, text, icon in CATEGORIES:
            self._append('filter', identity, tr(text, language), icon, owner._filters_enabled)
        for identity in dict.fromkeys([*owner._known, *self.selected]):
            self._append('chat', identity, owner._title(identity), '', True)
        self.empty = QLabel(tr('Ничего не найдено', language))
        self.empty.setTextFormat(Qt.TextFormat.PlainText)
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.hide()
        root.addWidget(self.empty)
        buttons = QHBoxLayout()
        self.cancel_button = QPushButton(tr('Отменить', language))
        self.add_button = QPushButton(tr('Добавить', language))
        self.add_button.setObjectName('FolderPickerPrimary')
        for button in (self.add_button, self.cancel_button):
            button.setMinimumHeight(40)
            button.setAutoDefault(False)
            buttons.addWidget(button)
        root.addLayout(buttons)
        self.cancel_button.clicked.connect(self.reject)
        self.add_button.clicked.connect(self.accept)
        self.items.itemClicked.connect(self._toggle)
        self.search.textChanged.connect(self._search)
        self.setStyleSheet(owner._stylesheet())

    def _append(self, kind, identity, title, icon, enabled):
        item = QListWidgetItem(title)
        item.setData(_ROLE, (kind, identity))
        item.setData(_ROLE + 1, icon)
        item.setSizeHint(QSize(0, 52))
        item.setFlags(Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsUserCheckable
                      | (Qt.ItemFlag.ItemIsEnabled if enabled else Qt.ItemFlag.NoItemFlags))
        item.setCheckState(Qt.CheckState.Checked if identity in (self.filters if kind == 'filter' else self.selected)
                           else Qt.CheckState.Unchecked)
        self.items.addItem(item)
        self._rows.append(item)

    def _toggle(self, item):
        if (not self.owner._dialog_current(self) or item.isHidden()
                or not item.flags() & Qt.ItemFlag.ItemIsEnabled):
            return
        kind, identity = item.data(_ROLE)
        values = self.filters if kind == 'filter' else self.selected
        if identity in values:
            values.remove(identity)
            item.setCheckState(Qt.CheckState.Unchecked)
        else:
            values.append(identity)
            item.setCheckState(Qt.CheckState.Checked)

    def _search(self, text):
        query = text.strip().casefold()
        for item in self._rows:
            item.setHidden(query not in item.text().casefold())
        current = self.items.currentItem()
        if current is not None and current.isHidden():
            self.items.setCurrentRow(-1)
        self.empty.setVisible(all(item.isHidden() for item in self._rows))

    def accept(self):
        if self.owner._commit_dialog(self):
            super().accept()
        else:
            self.reject()


class FolderChatPicker(QFrame):
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._known, self._selected, self._filters = {}, [], []
        self._language, self._theme = 'ru', 'light'
        self._generation, self._dialog = 0, None
        self._chip_generation = 0
        self._context_guard, self._filters_enabled = None, True
        self.setObjectName('FolderChatPicker')
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 10, 16, 14)
        root.setSpacing(10)
        self.add_button = QPushButton()
        self.add_button.setObjectName('FolderPickerAdd')
        self.add_button.setIconSize(QSize(22, 22))
        self.add_button.setMinimumHeight(40)
        self.add_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.add_button.clicked.connect(self.open_dialog)
        root.addWidget(self.add_button)
        self.chips = QWidget()
        self.chips.setObjectName('FolderPickerChips')
        policy = QSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        policy.setHeightForWidth(True)
        self.chips.setSizePolicy(policy)
        self._flow = _ChipFlow(self.chips)
        root.addWidget(self.chips)
        self.set_language('ru')
        self.apply_theme('light')

    def set_ids(self, known, selected):
        # The existing selector's positional (IDs, names) form remains valid.
        if not isinstance(known, Mapping) and isinstance(selected, Mapping):
            known, selected = selected, known
        if (not isinstance(known, Mapping) or any(type(identity) is not int or not -(2**63) <= identity < 2**63
                or not isinstance(title, str) for identity, title in known.items())):
            raise ValueError('Invalid known chats')
        values = _ids(selected)
        self.invalidate_dialog()
        self._known, self._selected = dict(known), values
        self._render_chips()

    def values(self):
        return list(self._selected)

    def set_filters(self, values):
        filters = _ids(values, filters=True)
        self.invalidate_dialog()
        self._filters = filters
        self._render_chips()

    def filter_values(self):
        return list(self._filters)

    def set_filters_enabled(self, enabled):
        if type(enabled) is not bool:
            raise ValueError('Invalid filter availability')
        if enabled == self._filters_enabled:
            return
        self.invalidate_dialog()
        self._filters_enabled = enabled
        self._render_chips()

    def set_context_guard(self, guard):
        if guard is not None and not callable(guard):
            raise ValueError('Invalid context guard')
        self.invalidate_dialog()
        self._context_guard = guard

    def invalidate_dialog(self):
        self._generation += 1
        dialog, self._dialog = self._dialog, None
        if dialog is not None and isValid(dialog):
            dialog.reject()

    def _can_edit(self):
        try:
            return self.isEnabled() and (self._context_guard is None or self._context_guard() is True)
        except Exception:
            return False

    def _dialog_current(self, dialog):
        return (isValid(self) and self._dialog is dialog and dialog.generation == self._generation
                and self._can_edit())

    def open_dialog(self):
        if not self._can_edit() or self._dialog is not None:
            return None
        dialog = _FolderChatDialog(self)
        self._dialog = dialog
        def finished(_result):
            if isValid(self) and self._dialog is dialog:
                self._dialog = None
            if isValid(dialog):
                dialog.deleteLater()
        dialog.finished.connect(finished)
        dialog.open()
        dialog.search.setFocus()
        return dialog

    def _commit_dialog(self, dialog):
        if not self._dialog_current(dialog):
            return False
        # Accept only identities/rules present in this detached chooser, even
        # if a caller accidentally injects data into its temporary lists.
        try:
            selected, filters = _ids(dialog.selected), _ids(dialog.filters, filters=True)
        except ValueError:
            return False
        if (any(identity not in self._known and identity not in self._selected for identity in selected)
                or set(filters) - set(self._filters) - set(_CATEGORY_LABELS)
                or set(filters) - set(_CATEGORY_LABELS) != set(self._filters) - set(_CATEGORY_LABELS)
                or not self._filters_enabled and set(filters) != set(self._filters)):
            return False
        modified = set(selected) != set(self._selected) or set(filters) != set(self._filters)
        self._selected, self._filters = selected, filters
        self._dialog = None  # Consume before parent changed callbacks repopulate/disable.
        self._render_chips()
        if modified:
            self.changed.emit()
        return True

    def _title(self, identity):
        return self._known.get(identity) or tr('Данные чата недоступны', self._language)

    def _render_chips(self):
        self._chip_generation += 1
        generation = self._chip_generation
        while self._flow.count():
            item = self._flow.takeAt(0)
            widget = item.widget()
            widget.hide()
            widget.deleteLater()
        values = [('filter', identity, tr(_CATEGORY_LABELS[identity], self._language))
                  for identity in self._filters if identity in _CATEGORY_LABELS]
        values += [('chat', identity, self._title(identity)) for identity in self._selected]
        for kind, identity, title in values:
            chip = QFrame()
            chip.setObjectName('FolderPickerChip')
            chip.setProperty('selectionKind', kind)
            chip.setProperty('selectionId', identity)
            row = QHBoxLayout(chip)
            row.setContentsMargins(10, 2, 5, 2)
            row.setSpacing(5)
            text = QLabel(title)
            text.setTextFormat(Qt.TextFormat.PlainText)
            text.setMinimumWidth(0)
            text.setMaximumWidth(210)
            text.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
            text.setToolTip('<span>' + html.escape(title) + '</span>')
            row.addWidget(text)
            remove = QToolButton()
            remove.setFixedSize(24, 24)
            remove.setIcon(line_icon('close', palette_for(self._theme).text_dim, 16))
            remove.setAccessibleName(tr('Убрать', self._language) + ': ' + title)
            remove.setEnabled(kind != 'filter' or self._filters_enabled)
            remove.clicked.connect(lambda _checked=False, k=kind, key=identity, epoch=generation:
                                   self._remove(k, key, epoch))
            row.addWidget(remove)
            chip.setMinimumHeight(32)
            chip.setMaximumWidth(min(270, max(85, text.fontMetrics().horizontalAdvance(title) + 49)))
            self._flow.addWidget(chip)
        self.chips.setVisible(bool(values))
        self._flow.invalidate()
        self.chips.updateGeometry()
        self.updateGeometry()

    def _remove(self, kind, identity, generation):
        if (generation != self._chip_generation or not self._can_edit()
                or kind == 'filter' and not self._filters_enabled):
            return
        values = self._filters if kind == 'filter' else self._selected
        if identity in values:
            self.invalidate_dialog()
            values.remove(identity)
            self._render_chips()
            self.changed.emit()

    def set_language(self, language):
        self.invalidate_dialog()
        self._language = normalize_language(language)
        self.setAccessibleName(tr('Чаты в папке', self._language))
        self.add_button.setText(tr('Добавить чаты', self._language))
        self._render_chips()

    def _stylesheet(self):
        p = palette_for(self._theme)
        return f'''
            QFrame#FolderChatPicker, QDialog#FolderChatDialog {{background:{p.sidebar};border:0;border-radius:14px;color:{p.text};}}
            QWidget#FolderPickerChips, QLabel {{background:transparent;color:{p.text};}}
            QPushButton#FolderPickerAdd {{background:transparent;border:0;color:#0085FF;text-align:left;padding:4px 0;font-size:15px;}}
            QPushButton#FolderPickerAdd:disabled {{color:{p.text_dim};}}
            QFrame#FolderPickerChip {{background:{p.surface_hi};border:0;border-radius:16px;}}
            QToolButton {{background:transparent;border:0;border-radius:12px;}}
            QToolButton:hover {{background:{p.hover};}}
            QLabel#FolderPickerHeading {{font-size:20px;font-weight:600;}}
            QLineEdit {{background:{p.surface_hi};color:{p.text};border:0;border-radius:10px;padding:6px 10px;}}
            QListWidget {{background:{p.sidebar};border:0;color:{p.text};}}
            QListWidget::item {{padding:0;border:0;}}
            QPushButton {{background:{p.surface_hi};color:{p.text};border:0;border-radius:10px;padding:8px 12px;}}
            QPushButton#FolderPickerPrimary {{background:#0085FF;color:white;}}
            QPushButton:focus, QToolButton:focus {{border:1px solid #0085FF;}}
        '''

    def apply_theme(self, theme):
        self.invalidate_dialog()
        self._theme = theme
        self.setStyleSheet(self._stylesheet())
        self.add_button.setIcon(line_icon('plus', '#0085FF', 22))
        self._render_chips()

    def changeEvent(self, event):
        if event.type() == QEvent.Type.EnabledChange and not self.isEnabled():
            self.invalidate_dialog()
        super().changeEvent(event)

    def hideEvent(self, event):
        self.invalidate_dialog()
        super().hideEvent(event)
