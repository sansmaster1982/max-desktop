"""Plain-text widgets and local selectors shared by settings management pages."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMessageBox,
    QPushButton, QVBoxLayout, QWidget,
)
from .theme import palette_for


def label(text='', *, secondary=False):
    widget = QLabel(text)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    widget.setWordWrap(True)
    widget.setObjectName('SettingsSecondary' if secondary else 'SettingsLabel')
    return widget


def confirm_action(parent, title, text):
    box = QMessageBox(parent)
    box.setWindowTitle(title)
    box.setIcon(QMessageBox.Icon.Question)
    box.setTextFormat(Qt.TextFormat.PlainText)
    box.setText(text)
    box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
    box.setDefaultButton(QMessageBox.StandardButton.No)
    return box.exec() == QMessageBox.StandardButton.Yes


def apply_panel_theme(panel, theme=None):
    if not isinstance(theme, str):
        settings = getattr(panel.parentWidget(), 'settings', None)
        theme = getattr(settings, 'theme', 'light')
    palette = palette_for(theme)
    panel.setStyleSheet(f'''
        QListWidget {{background:{palette.sidebar};color:{palette.text};border:0;border-radius:12px;}}
        QListWidget::item {{padding:5px 7px;color:{palette.text};}}
        QListWidget::item:selected {{background:{palette.selected};color:{palette.text};}}
    ''')


def is_identity(value):
    return type(value) is int and -(2**63) <= value < 2**63


def known_chats(service):
    """Only current local chat IDs/names; never derive a chat from a user ID."""
    result = {}
    try:
        chats = service.list_chats()
    except Exception:
        return result
    for chat in chats:
        identity = getattr(chat, 'id', None)
        if is_identity(identity):
            result[identity] = str(getattr(chat, 'display', '') or 'Название не передано')
    return result


class ChatSelection(QWidget):
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.items = QListWidget()
        self.items.setMinimumHeight(120)
        self.items.setAccessibleName('Выбранные чаты папки')
        self.items.itemChanged.connect(lambda _item: self.changed.emit())
        layout.addWidget(self.items)

    def set_ids(self, identities, names):
        self.items.blockSignals(True)
        self.items.clear()
        order = list(dict.fromkeys([*identities, *names]))
        for identity in order:
            item = QListWidgetItem(names.get(identity, f'Чат {identity} · данные недоступны'))
            item.setData(Qt.ItemDataRole.UserRole, identity)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if identity in identities else Qt.CheckState.Unchecked)
            self.items.addItem(item)
        self.items.blockSignals(False)

    def values(self):
        return [self.items.item(index).data(Qt.ItemDataRole.UserRole)
                for index in range(self.items.count())
                if self.items.item(index).checkState() == Qt.CheckState.Checked]


class OrderedChatSelection(QWidget):
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._names = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.items = QListWidget()
        self.items.setMinimumHeight(110)
        self.items.setAccessibleName('Закреплённые чаты папки по порядку')
        layout.addWidget(self.items)
        actions = QHBoxLayout()
        self.choice = QComboBox()
        self.choice.setAccessibleName('Добавить закреплённый чат')
        actions.addWidget(self.choice, 1)
        self.add = QPushButton('Добавить')
        self.add.clicked.connect(self._add)
        actions.addWidget(self.add)
        layout.addLayout(actions)
        reorder = QHBoxLayout()
        for title, handler in [('Вверх', lambda: self._move(-1)), ('Вниз', lambda: self._move(1)),
                               ('Убрать', self._remove)]:
            button = QPushButton(title)
            button.clicked.connect(handler)
            reorder.addWidget(button)
        layout.addLayout(reorder)

    def set_ids(self, identities, names):
        self._names = dict(names)
        self.items.clear()
        for identity in identities:
            self._append(identity)
        self._choices()

    def values(self):
        return [self.items.item(index).data(Qt.ItemDataRole.UserRole) for index in range(self.items.count())]

    def _append(self, identity):
        item = QListWidgetItem(self._names.get(identity, f'Чат {identity} · данные недоступны'))
        item.setData(Qt.ItemDataRole.UserRole, identity)
        self.items.addItem(item)

    def _choices(self):
        self.choice.clear()
        existing = self.values()
        for identity, title in self._names.items():
            if identity not in existing:
                self.choice.addItem(title, identity)
        self.add.setEnabled(self.choice.count() > 0)

    def _add(self):
        identity = self.choice.currentData()
        if is_identity(identity) and identity not in self.values():
            self._append(identity)
            self._choices()
            self.changed.emit()

    def _remove(self):
        row = self.items.currentRow()
        if row >= 0:
            self.items.takeItem(row)
            self._choices()
            self.changed.emit()

    def _move(self, delta):
        row = self.items.currentRow()
        target = row + delta
        if row >= 0 and 0 <= target < self.items.count():
            item = self.items.takeItem(row)
            self.items.insertItem(target, item)
            self.items.setCurrentRow(target)
            self.changed.emit()
