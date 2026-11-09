"""Real folder management; requested changes never become optimistic rows."""
from __future__ import annotations
from dataclasses import dataclass
import uuid

from PySide6.QtCore import QMimeData, QPoint, QSize, Qt, Signal, QTimer
from PySide6.QtGui import QDrag, QPainter, QColor
from PySide6.QtWidgets import (
    QCheckBox, QFrame, QGridLayout, QHBoxLayout, QLabel, QLayout, QLineEdit, QListWidget, QListWidgetItem,
    QMenu, QPushButton, QSizePolicy, QVBoxLayout, QWidget,
    QApplication, QStyle, QStyledItemDelegate, QStyleOptionViewItem,
)
from shiboken6 import isValid

from .async_runner import run_async
from .management_widgets import OrderedChatSelection, apply_panel_theme, confirm_action, is_identity, known_chats, label
from .theme import palette_for
from .widgets.folder_chat_picker import FolderChatPicker
from .widgets.line_icon import line_icon
from .widgets.toggle_switch import ToggleSwitch
from ..core.i18n import normalize_language, tr
from ..protocol.client import MaxError, MaxWriteNotStarted, MaxWriteOutcomeUnknown


ALL_FOLDER = 'all.chat.folder'
_DRAG_MIME = 'application/x-maxim-folder-order'
_DRAG_REJECTED = object()
_DRAG_HINT = 'Перетащите папку, чтобы изменить порядок. Можно также использовать кнопки «Вверх» и «Вниз».'
FILTER_LABELS = (
    'Непрочитанные', 'Прочитанные', 'Каналы', 'Группы', 'Личные чаты',
    'Где я владелец', 'Где я администратор', 'Без звука', 'Контакты',
    'Не из контактов', 'Боты', 'Со звуком', 'Отмеченные непрочитанными',
    'Организации', 'С упоминаниями',
)
_CATEGORY_FILTERS = frozenset((8, 9, 3, 2, 10))
_ADVANCED_FILTERS = tuple(index for index in range(15) if index not in _CATEGORY_FILTERS | {0, 11})
_TEMPLATES = {'new': ('Новые', [0]), 'personal': ('Личные', [8, 9])}


def _same_field(key, left, right):
    return set(left) == set(right) if key in ('include', 'filters') else left == right


def _folder_snapshot(value):
    if not isinstance(value, dict) or not is_identity(value.get('folderSync')):
        raise ValueError('Invalid folder snapshot')
    if 'foldersOrder' not in value or 'orderedFolders' not in value:
        return None  # An omitted delta field is not an empty/full folder list.
    if 'writePending' in value and type(value['writePending']) is not bool:
        raise ValueError('Invalid local write state')
    order, records = value['foldersOrder'], value['orderedFolders']
    if (not isinstance(order, list) or any(not isinstance(identity, str) or not identity for identity in order)
            or len(set(order)) != len(order) or not isinstance(records, list) or len(records) != len(order)):
        raise ValueError('Invalid folder order')
    for identity, record in zip(order, records):
        if record is None:
            continue
        if not isinstance(record, dict) or record.get('id') != identity or not isinstance(record.get('title'), str):
            raise ValueError('Invalid folder data')
        for key in ('include', 'favorites'):
            if key in record and (not isinstance(record[key], list) or not all(map(is_identity, record[key]))):
                raise ValueError('Invalid chat identities')
        for key, maximum in (('filters', 14), ('options', 4)):
            if key in record and (not isinstance(record[key], list)
                    or any(type(item) is not int or not 0 <= item <= maximum for item in record[key])):
                raise ValueError('Invalid folder flags')
    return value


@dataclass(frozen=True, eq=False)
class _FolderDragTicket:
    identity: str
    snapshot: dict
    order: tuple
    context: tuple
    epoch: int
    nonce: str


class _FolderGrip(QWidget):
    """Only this handle starts an in-process, session-bound order gesture."""
    def __init__(self, panel, identity, parent=None):
        super().__init__(parent)
        self.panel, self.identity = panel, identity
        self._press = self._ticket = None
        self.color = '#A5A5AB'
        self.setFixedSize(20, 24)
        self.setCursor(Qt.CursorShape.OpenHandCursor)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._ticket = self.panel._begin_folder_drag(self.identity)
            self._press = event.position().toPoint() if self._ticket is not None else None
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if (self._press is None or not event.buttons() & Qt.MouseButton.LeftButton
                or (event.position().toPoint() - self._press).manhattanLength() < QApplication.startDragDistance()):
            return
        ticket = self._ticket
        self._press = None
        if not self.panel._folder_drag_current(ticket):
            return
        drag = QDrag(self)
        mime = QMimeData()
        mime.setData(_DRAG_MIME, ticket.nonce.encode('ascii'))
        drag.setMimeData(mime)
        row = self.parentWidget()
        if row is not None:
            drag.setPixmap(row.grab())
            drag.setHotSpot(self.mapTo(row, event.position().toPoint()))
        self.panel._drag_exec_active = True
        try:
            drag.exec(Qt.DropAction.MoveAction)
        finally:
            if isValid(self.panel):
                self.panel._drag_exec_active = False
                self.panel._cancel_folder_drag(cancel_native=False)

    def mouseReleaseEvent(self, event):
        self._press = None
        if self.panel._drag_ticket is self._ticket:
            self.panel._cancel_folder_drag()
        self._ticket = None
        event.accept()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(self.color))
        for y in (7, 12, 17):
            painter.drawRoundedRect(2, y, 16, 1, .5, .5)


class _FolderDropList(QListWidget):
    """Paint a drop indicator but never let Qt move the authoritative rows."""
    def __init__(self, panel):
        super().__init__(panel)
        self.panel = panel
        self._drop_y = None
        self._drag_position = None
        self._indicator = QFrame(self.viewport())
        self._indicator.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._indicator.setStyleSheet('background:#0085FF;border:0;')
        self._indicator.hide()
        self._scroll_timer = QTimer(self)
        self._scroll_timer.setInterval(70)
        self._scroll_timer.timeout.connect(self._scroll_drag)
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)

    def clear_drop_indicator(self):
        self._drop_y = None
        self._drag_position = None
        self._scroll_timer.stop()
        self._indicator.hide()
        self.viewport().update()

    def _show_drop_indicator(self, y):
        self._drop_y = y
        self._indicator.setGeometry(8, max(0, y - 1), max(0, self.viewport().width() - 16), 2)
        self._indicator.show()
        self._indicator.raise_()

    def _scroll_drag(self):
        ticket = self.panel._drag_ticket
        if self._drag_position is None or not self.panel._folder_drag_current(ticket):
            self.clear_drop_indicator()
            return
        y, height = self._drag_position.y(), self.viewport().height()
        direction = -1 if y < 28 else 1 if y > height - 28 else 0
        if direction:
            bar = self.verticalScrollBar()
            bar.setValue(bar.value() + direction * max(1, bar.singleStep()))
            target = self._drop_target(self._drag_position, ticket)
            if target is not None:
                self._show_drop_indicator(target[1])

    def _event_ticket(self, event):
        ticket = self.panel._drag_ticket
        source = event.source()
        if (not self.panel._folder_drag_current(ticket) or not isinstance(source, _FolderGrip)
                or source.panel is not self.panel or source.identity != ticket.identity
                or source._ticket is not ticket or not event.mimeData().hasFormat(_DRAG_MIME)
                or bytes(event.mimeData().data(_DRAG_MIME)) != ticket.nonce.encode('ascii')
                or not event.possibleActions() & Qt.DropAction.MoveAction):
            return None
        return ticket

    def _drop_target(self, point, ticket):
        if not self.viewport().rect().contains(point):
            return None
        item = self.itemAt(point)
        if item is None:
            if not self.count() or point.y() < self.visualItemRect(self.item(self.count() - 1)).bottom():
                return None
            boundary, y = self.count(), self.visualItemRect(self.item(self.count() - 1)).bottom()
        else:
            rect = self.visualItemRect(item)
            after = point.y() >= rect.center().y()
            boundary = self.row(item) + int(after)
            y = rect.bottom() if after else rect.top()
        if boundary == 0:
            return None
        source = ticket.order.index(ticket.identity)
        return boundary - int(boundary > source), y

    def dragEnterEvent(self, event):
        if self._event_ticket(event) is not None:
            event.setDropAction(Qt.DropAction.MoveAction)
            event.accept()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        ticket = self._event_ticket(event)
        target = self._drop_target(event.position().toPoint(), ticket) if ticket is not None else None
        if target is None:
            self.clear_drop_indicator()
            event.ignore()
            return
        self._drag_position = event.position().toPoint()
        self._show_drop_indicator(target[1])
        self._scroll_timer.start()
        event.setDropAction(Qt.DropAction.MoveAction)
        event.accept()

    def dragLeaveEvent(self, event):
        self.clear_drop_indicator()
        event.accept()

    def dropEvent(self, event):
        ticket = self._event_ticket(event)
        target = self._drop_target(event.position().toPoint(), ticket) if ticket is not None else None
        self.clear_drop_indicator()
        if target is not None and self.panel._drop_folder_drag(ticket, target[0]):
            event.setDropAction(Qt.DropAction.MoveAction)
            event.accept()
        else:
            event.ignore()

class _FolderBackgroundDelegate(QStyledItemDelegate):
    """Keep selection/focus but let the embedded row be the only text painter."""
    def paint(self, painter, option, index):
        style_option = QStyleOptionViewItem(option)
        self.initStyleOption(style_option, index)
        style_option.text = ''
        style = style_option.widget.style() if style_option.widget else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, style_option, painter, style_option.widget)


class _FolderRow(QWidget):
    def __init__(self, title, panel, identity, parent=None):
        super().__init__(parent)
        self.setObjectName('NativeFolderRow')
        self.setFixedHeight(52)
        self.icon_name = 'folder'
        row = QHBoxLayout(self)
        row.setContentsMargins(16, 0, 12, 0)
        row.setSpacing(16)
        self.icon = QLabel()
        self.icon.setFixedSize(24, 24)
        row.addWidget(self.icon)
        self.title_label = label(title)
        self.title_label.setObjectName('NativeFolderTitle')
        self.title_label.setWordWrap(False)
        self.title_label.setMinimumWidth(0)
        self.title_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        row.addWidget(self.title_label, 1)
        self.grip = _FolderGrip(panel, identity)
        row.addWidget(self.grip)
        self.more = QPushButton()
        self.more.setObjectName('NativeFolderMore')
        self.more.setFixedSize(32, 32)
        self.more.setAccessibleName('Действия папки')
        self.more.setCursor(Qt.CursorShape.PointingHandCursor)
        row.addWidget(self.more)

    def apply_theme(self, palette):
        self.icon.setPixmap(line_icon(self.icon_name, palette.text_dim, 24).pixmap(24, 24))
        self.more.setIcon(line_icon('more_vertical', palette.text_dim, 24))
        self.more.setIconSize(QSize(24, 24))
        self.grip.color = palette.text_dim
        self.grip.update()


class FoldersPanel(QWidget):
    folders_changed = Signal(object)  # Only an actual structured read snapshot.

    def __init__(self, service, parent=None):
        super().__init__(parent)
        self.service = service
        self._drag_ticket = None
        self._drag_epoch = 0
        self._drag_exec_active = False
        self._read_serial = 0
        self._reading_context = self._capture_folder_context()
        self._snapshot_context = self._reading_context
        self._busy = self._loaded = self._read_ok = self._populating = self._creating = False
        self._refresh_queued = False
        self._snapshot = {}
        self._records = {}
        self._selected = None
        self._baseline = {}
        self._touched = set()
        self._pending = None
        self._language = normalize_language(getattr(getattr(parent, 'settings', None), 'ui_language', 'ru'))
        self._theme = getattr(getattr(parent, 'settings', None), 'theme', 'light')
        self._row_widgets = {}
        self.setMaximumWidth(640)
        layout = QVBoxLayout(self)
        layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)
        self.main_card = QFrame()
        self.main_card.setObjectName('NativeFoldersCard')
        card = QVBoxLayout(self.main_card)
        card.setContentsMargins(0, 24, 0, 8)
        card.setSpacing(0)
        self.intro_icon = QLabel()
        self.intro_icon.setObjectName('NativeFoldersIntroIcon')
        self.intro_icon.setFixedSize(72, 72)
        self.intro_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        card.addWidget(self.intro_icon, 0, Qt.AlignmentFlag.AlignHCenter)
        card.addSpacing(16)
        self.intro_text = label()
        self.intro_text.setObjectName('NativeFoldersIntroText')
        self.intro_text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.intro_text.setContentsMargins(16, 0, 16, 0)
        card.addWidget(self.intro_text)
        card.addSpacing(20)
        self.refresh_button = QPushButton('Обновить')
        self.refresh_button.clicked.connect(self.refresh)
        self.new_button = QPushButton('Создать папку')
        self.new_button.setObjectName('NativeFolderCreate')
        self.new_button.setFixedHeight(40)
        self.new_button.setMinimumWidth(180)
        self.new_button.setIconSize(QSize(20, 20))
        self.new_button.clicked.connect(self._new_folder)
        card.addWidget(self.new_button, 0, Qt.AlignmentFlag.AlignHCenter)
        card.addSpacing(24)
        self.divider = QFrame()
        self.divider.setObjectName('NativeFoldersDivider')
        self.divider.setFixedHeight(1)
        card.addWidget(self.divider)
        self.section_label = label('Ваши папки', secondary=True)
        self.section_label.setObjectName('NativeFoldersSection')
        self.section_label.setContentsMargins(16, 18, 16, 8)
        card.addWidget(self.section_label)
        self.items = _FolderDropList(self)
        self.items.setObjectName('NativeFoldersList')
        self.items.setItemDelegate(_FolderBackgroundDelegate(self.items))
        self.items.setAccessibleName('Папки MAX в текущем порядке')
        self.items.setFrameShape(QFrame.Shape.NoFrame)
        self.items.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.items.setFixedHeight(52)
        self.items.currentItemChanged.connect(self._selection_changed)
        card.addWidget(self.items)
        layout.addWidget(self.main_card)
        self.templates = QFrame()
        self.templates.setObjectName('NativeFolderEditorCard')
        template_layout = QVBoxLayout(self.templates)
        template_layout.setContentsMargins(0, 16, 0, 8)
        template_layout.setSpacing(0)
        self.templates_heading = label('Шаблоны', secondary=True)
        self.templates_heading.setContentsMargins(18, 0, 16, 6)
        template_layout.addWidget(self.templates_heading)
        self.template_cards, self.template_labels, self.template_buttons, self.template_icons = {}, {}, {}, {}
        for key, (title, _filters) in _TEMPLATES.items():
            frame = QWidget()
            row = QHBoxLayout(frame)
            row.setContentsMargins(18, 8, 16, 8)
            row.setSpacing(16)
            icon = QLabel()
            icon.setFixedSize(24, 24)
            row.addWidget(icon)
            title_label = label(title)
            row.addWidget(title_label, 1)
            button = QPushButton()
            button.setObjectName('NativeFolderTemplateAdd')
            button.setFixedSize(36, 36)
            button.setIconSize(QSize(24, 24))
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda _checked=False, kind=key: self._create_template(kind))
            row.addWidget(button)
            template_layout.addWidget(frame)
            self.template_cards[key], self.template_labels[key], self.template_buttons[key] = frame, title_label, button
            self.template_icons[key] = icon
        layout.addWidget(self.templates)
        self.status = label('Обновите список папок.', secondary=True)
        layout.addWidget(self.status)
        layout.addWidget(self.refresh_button, 0, Qt.AlignmentFlag.AlignLeft)
        self.editor = QWidget()
        self.editor.setObjectName('NativeFolderEditor')
        editor_layout = QVBoxLayout(self.editor)
        editor_layout.setContentsMargins(0, 0, 0, 0)
        editor_layout.setSpacing(14)
        self.back_button = QPushButton('Назад')
        self.back_button.clicked.connect(self._close_editor)
        editor_layout.addWidget(self.back_button, 0, Qt.AlignmentFlag.AlignLeft)
        self.title = QLineEdit()
        self.title.setObjectName('NativeFolderName')
        self.title.setMaxLength(20)
        self.title.setMinimumHeight(52)
        self.title.setPlaceholderText('Название папки')
        self.title.setAccessibleName('Название папки')
        self.title.textChanged.connect(lambda _text: self._edited('title'))
        editor_layout.addWidget(self.title)
        self.chats_heading = label('ЧАТЫ В ПАПКЕ', secondary=True)
        editor_layout.addWidget(self.chats_heading)
        self.include = FolderChatPicker()
        self.include.changed.connect(self._picker_changed)
        self.include.set_context_guard(self._picker_current)
        editor_layout.addWidget(self.include)
        self.show_only_heading = label('ПОКАЗЫВАТЬ ТОЛЬКО', secondary=True)
        editor_layout.addWidget(self.show_only_heading)
        self.show_only_card = QFrame()
        self.show_only_card.setObjectName('NativeFolderEditorCard')
        show_only = QVBoxLayout(self.show_only_card)
        show_only.setContentsMargins(18, 8, 16, 8)
        show_only.setSpacing(0)
        self.filters = []
        for index, text in enumerate(FILTER_LABELS):
            checkbox = ToggleSwitch(text, theme=self._theme) if index in (0, 11) else QCheckBox(text)
            checkbox.toggled.connect(self._filters_changed)
            self.filters.append(checkbox)
        show_only.addWidget(self.filters[11])
        show_only.addWidget(self.filters[0])
        editor_layout.addWidget(self.show_only_card)
        self.advanced_button = QPushButton('Дополнительно')
        self.advanced_button.setCheckable(True)
        self.advanced_button.setObjectName('NativeFolderAdvanced')
        editor_layout.addWidget(self.advanced_button, 0, Qt.AlignmentFlag.AlignLeft)
        self.advanced = QWidget()
        self.advanced.setObjectName('NativeFolderAdvancedBody')
        advanced_layout = QVBoxLayout(self.advanced)
        advanced_layout.setContentsMargins(0, 0, 0, 0)
        advanced_layout.setSpacing(12)
        self.advanced_button.toggled.connect(self.advanced.setVisible)
        self.filters_heading = label('Фильтры', secondary=True)
        advanced_layout.addWidget(self.filters_heading)
        self.filter_widget = QWidget()
        self.filter_widget.setObjectName('NativeFolderAdvancedFilters')
        self._filter_layout = QGridLayout(self.filter_widget)
        self._filter_columns = 2
        self._filter_layout.setContentsMargins(0, 0, 0, 0)
        for position, index in enumerate(_ADVANCED_FILTERS):
            self._filter_layout.addWidget(self.filters[index], position // 2, position % 2)
        for index in _CATEGORY_FILTERS:
            self.filters[index].setParent(self.filter_widget)
            self.filters[index].hide()  # Category values are edited by the picker.
        advanced_layout.addWidget(self.filter_widget)
        self.favorites_heading = label('Закреплённые чаты · порядок внутри папки', secondary=True)
        advanced_layout.addWidget(self.favorites_heading)
        self.favorites = OrderedChatSelection()
        self.favorites.changed.connect(lambda: self._edited('favorites'))
        advanced_layout.addWidget(self.favorites)
        self.editor_note = label('', secondary=True)
        advanced_layout.addWidget(self.editor_note)
        order = QHBoxLayout()
        self.up_button, self.down_button = QPushButton('Вверх'), QPushButton('Вниз')
        self.up_button.clicked.connect(lambda: self._move_folder(-1))
        self.down_button.clicked.connect(lambda: self._move_folder(1))
        self.delete_button = QPushButton('Удалить папку')
        self.delete_button.setObjectName('Danger')
        self.delete_button.clicked.connect(self._delete_folder)
        for button in (self.up_button, self.down_button, self.delete_button):
            order.addWidget(button)
        advanced_layout.addLayout(order)
        editor_layout.addWidget(self.advanced)
        self.advanced.hide()
        footer = QHBoxLayout()
        self.save_button = QPushButton('Сохранить изменения')
        self.save_button.setObjectName('Primary')
        self.save_button.setMinimumHeight(44)
        self.save_button.clicked.connect(self._save)
        self.cancel_button = QPushButton('Отменить')
        self.cancel_button.setMinimumHeight(44)
        self.cancel_button.clicked.connect(self._close_editor)
        footer.addWidget(self.save_button, 1)
        footer.addWidget(self.cancel_button)
        editor_layout.addLayout(footer)
        layout.addWidget(self.editor)
        self.editor.hide()
        layout.addStretch(1)
        self._apply_enabled()
        self.set_language(self._language)
        self.apply_theme()

    def apply_theme(self, theme=None):
        if not isValid(self):
            return
        self._cancel_folder_drag()
        if isinstance(theme, str):
            self._theme = theme
        palette = palette_for(self._theme)
        apply_panel_theme(self, self._theme)
        self.setStyleSheet(self.styleSheet() + f'''
            QWidget#NativeFolderEditor, QWidget#NativeFolderAdvancedBody,
            QWidget#NativeFolderAdvancedFilters {{background:transparent;}}
            QWidget#NativeFolderEditor QLabel#SettingsLabel,
            QWidget#NativeFolderEditor QLabel#SettingsSecondary {{background:transparent;}}
            QFrame#NativeFoldersCard {{background:{palette.sidebar};border:0;border-radius:16px;}}
            QLabel#NativeFoldersIntroIcon {{background:{palette.surface_hi};border-radius:36px;}}
            QLabel#NativeFoldersIntroText {{color:{palette.text};font-size:16px;background:transparent;}}
            QLabel#NativeFoldersSection {{color:{palette.text_dim};font-size:14px;background:transparent;}}
            QFrame#NativeFoldersDivider {{background:{palette.border};border:0;}}
            QListWidget#NativeFoldersList {{background:transparent;border:0;border-radius:0;}}
            QListWidget#NativeFoldersList::item {{padding:0;}}
            QLabel#NativeFolderTitle {{color:{palette.text};background:transparent;font-size:16px;}}
            QPushButton#NativeFolderMore {{background:transparent;border:0;border-radius:8px;padding:0;}}
            QPushButton#NativeFolderMore:hover {{background:{palette.hover};}}
            QPushButton#NativeFolderCreate {{background:#0085FF;color:white;border:0;border-radius:10px;padding:0 18px;font-size:14px;font-weight:600;}}
            QPushButton#NativeFolderCreate:disabled {{background:{palette.surface_hi};color:{palette.text_dim};}}
            QFrame#NativeFolderEditorCard {{background:{palette.sidebar};border:0;border-radius:14px;}}
            QLineEdit#NativeFolderName {{background:{palette.sidebar};border:0;border-radius:14px;padding:0 16px;font-size:16px;}}
            QPushButton#NativeFolderAdvanced {{background:transparent;border:0;text-align:left;padding:5px 0;}}
            QPushButton#NativeFolderTemplateAdd {{background:transparent;border:0;border-radius:8px;padding:0;}}
            QPushButton#NativeFolderTemplateAdd:hover {{background:{palette.hover};}}
        ''')
        for key, icon in self.template_icons.items():
            icon.setPixmap(line_icon('folder' if key == 'new' else 'contacts', palette.text_dim, 24).pixmap(24, 24))
            self.template_buttons[key].setIcon(line_icon('plus', '#0085FF', 24))
        self.include.apply_theme(self._theme)
        for index in (0, 11):
            self.filters[index].set_theme(self._theme)
        self.intro_icon.setPixmap(line_icon('folder', palette.text_dim, 36).pixmap(36, 36))
        self.new_button.setIcon(line_icon('plus', '#FFFFFF', 20))
        for row in self._row_widgets.values():
            if isValid(row):
                row.apply_theme(palette)

    def set_language(self, language):
        if not isValid(self):
            return
        self._cancel_folder_drag()
        self._language = normalize_language(language)
        self.intro_text.setText(tr('Создавайте папки для чатов', self._language) + '\n'
                                + tr('и легко переключайтесь между ними', self._language))
        self.section_label.setText(tr('Ваши папки', self._language))
        self.new_button.setText(tr('Создать папку', self._language))
        self.refresh_button.setText(tr('Обновить', self._language))
        self.back_button.setText(tr('Назад', self._language))
        self.cancel_button.setText(tr('Отменить', self._language))
        self.title.setPlaceholderText(tr('Название папки', self._language))
        self.title.setAccessibleName(tr('Название папки', self._language))
        self.chats_heading.setText(tr('ЧАТЫ В ПАПКЕ', self._language))
        self.show_only_heading.setText(tr('ПОКАЗЫВАТЬ ТОЛЬКО', self._language))
        self.advanced_button.setText(tr('Дополнительно', self._language))
        self.filters_heading.setText(tr('Фильтры', self._language))
        self.favorites_heading.setText(tr('Закреплённые чаты · порядок внутри папки', self._language))
        for index, checkbox in enumerate(self.filters):
            source = 'Чаты со звуком' if index == 11 else 'Непрочитанные чаты' if index == 0 else FILTER_LABELS[index]
            checkbox.setText(tr(source, self._language))
            checkbox.setAccessibleName(tr(source, self._language))
        for button, source in ((self.up_button, 'Вверх'), (self.down_button, 'Вниз'),
                               (self.delete_button, 'Удалить папку')):
            button.setText(tr(source, self._language))
        for key, (source, _filters) in _TEMPLATES.items():
            self.template_labels[key].setText(tr(source, self._language))
            accessible = tr('Добавить', self._language) + ': ' + tr(source, self._language)
            self.template_buttons[key].setAccessibleName(accessible)
            self.template_buttons[key].setToolTip(accessible)
        self.templates_heading.setText(tr('Шаблоны', self._language))
        self.include.set_language(self._language)
        for row in self._row_widgets.values():
            row.grip.setAccessibleName(tr('Порядок папок', self._language))
            row.grip.setToolTip(tr(_DRAG_HINT, self._language))
        self._apply_enabled()

    retranslate = set_language

    def _capture_folder_context(self):
        try:
            context = self.service._history_read_context()
            if (not isinstance(context, tuple) or len(context) != 3
                    or type(context[0]) is not int or context[0] <= 0):
                return None
            self.service._require_history_context(context)
            return context
        except Exception:
            return None

    def _folder_context_current(self, context):
        if context is None:
            return False
        try:
            self.service._require_history_context(context)
            current = self._capture_folder_context()
            return (current is not None and current[0] == context[0]
                    and current[1] is context[1] and current[2] is context[2])
        except Exception:
            return False

    def _cancel_folder_drag(self, *, cancel_native=True):
        self._drag_epoch += 1
        self._drag_ticket = None
        if hasattr(self, 'items') and isValid(self.items):
            self.items.clear_drop_indicator()
        if cancel_native and self._drag_exec_active:
            QDrag.cancel()

    def _drag_available(self, identity):
        order = self._snapshot.get('foldersOrder', [])
        return (isValid(self) and self._read_ok and not self._busy and self._pending is None
                and self._snapshot.get('writePending') is not True
                and len(order) > 2 and order[0] == ALL_FOLDER and identity != ALL_FOLDER
                and identity in order and self._records.get(identity) is not None
                and self._folder_context_current(self._snapshot_context))

    def _begin_folder_drag(self, identity):
        self._cancel_folder_drag()
        if (self._snapshot_context is not None and not self._busy
                and not self._folder_context_current(self._snapshot_context)):
            # A live panel can outlast a reconnect without being reopened.
            # Keep the old rows read-only and reveal an explicit refresh.
            self._read_error('stale displayed snapshot')
            return None
        if not self._drag_available(identity):
            return None
        self._drag_ticket = _FolderDragTicket(identity, self._snapshot,
            tuple(self._snapshot['foldersOrder']), self._snapshot_context, self._drag_epoch, uuid.uuid4().hex)
        return self._drag_ticket

    def _folder_drag_current(self, ticket):
        return (isValid(self) and isinstance(ticket, _FolderDragTicket) and self._drag_ticket is ticket
                and ticket.epoch == self._drag_epoch and ticket.snapshot is self._snapshot
                and tuple(self._snapshot.get('foldersOrder', ())) == ticket.order
                and self._snapshot_context is ticket.context and self._drag_available(ticket.identity))

    def _drop_folder_drag(self, ticket, target_index):
        if (not self._folder_drag_current(ticket) or type(target_index) is not int
                or not 1 <= target_index < len(ticket.order)):
            return False
        order = list(ticket.order)
        order.remove(ticket.identity)
        order.insert(target_index, ticket.identity)
        self._cancel_folder_drag(cancel_native=False)
        if order == list(ticket.order):
            return False
        intent = {'kind': 'order', 'id': ticket.identity, 'order': order, '_drag_context': ticket.context}
        self._start_write(intent, self.service.reorder_folders, order,
                          context=ticket.context, expected_order=list(ticket.order))
        return self._pending is intent

    def hideEvent(self, event):
        self._cancel_folder_drag()
        self.include.invalidate_dialog()
        super().hideEvent(event)

    def _close_editor(self):
        if not isValid(self) or self._busy or self._pending is not None:
            return
        self.items.setCurrentRow(-1)
        self._selected = None
        self._creating = False
        self._load_editor(None)

    def _status_visibility(self):
        healthy = self.status.text() in ('Текущие папки MAX.', 'Папок нет.')
        self.status.setVisible(not healthy)
        self.refresh_button.setVisible(not healthy)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        columns = 1 if event.size().width() < 440 else 2
        if columns != self._filter_columns:
            self._filter_columns = columns
            while self._filter_layout.count():
                self._filter_layout.takeAt(0)
            for position, index in enumerate(_ADVANCED_FILTERS):
                self._filter_layout.addWidget(self.filters[index], position // columns, position % columns)

    def ensure_loaded(self):
        if not isValid(self):
            return
        if (not self._loaded or (not self._folder_context_current(self._snapshot_context)
                and self._capture_folder_context() is not None)):
            self.refresh()

    def refresh(self):
        if not isValid(self):
            return
        self._cancel_folder_drag()
        self.include.invalidate_dialog()
        if self._busy:
            self._refresh_queued = True
            return
        self._busy = True
        self._read_serial += 1
        serial = self._read_serial
        context = self._reading_context = self._capture_folder_context()
        self.status.setText('Загрузка папок…')
        self._apply_enabled()
        if context is None:
            # Legacy UI-only doubles cannot authorize the new drag path.
            run_async(self.service.get_folders, on_done=self._read_done, on_error=self._read_error)
            return
        service = self.service
        def work():
            service._require_history_context(context)
            result = service.get_folders()
            service._require_history_context(context)
            return result
        def done(result):
            if not isValid(self) or self._reading_context is not context or self._read_serial != serial:
                return
            if not self._folder_context_current(context):
                self._read_error('stale read')
                return
            self._read_done(result)
        def failed(_error):
            if isValid(self) and self._reading_context is context and self._read_serial == serial:
                self._read_error('read failed')
        run_async(work, on_done=done, on_error=failed)

    def _read_done(self, result, *, confirm_pending=True):
        if not isValid(self):
            return
        self._cancel_folder_drag()
        if self._reading_context is not None and not self._folder_context_current(self._reading_context):
            self._read_error('stale read')
            return
        try:
            snapshot = _folder_snapshot(result)
        except ValueError:
            self._read_error('invalid snapshot')
            return
        self._busy = False
        self._loaded = True
        self._read_ok = snapshot is not None
        if snapshot is None:
            self.status.setText('MAX передал только часть списка. Изменения пока недоступны.')
            self._apply_enabled()
        else:
            self._snapshot_context = self._reading_context
            if (self._pending is not None and '_drag_context' in self._pending
                    and (not confirm_pending or not self._pending_matches(snapshot))):
                self.status.setText('Изменение ещё не подтверждено MAX. Повтор отключён.')
                self._apply_enabled()
                self._drain_refresh()
                return
            self._snapshot = snapshot
            if confirm_pending and self._pending_matches(snapshot):
                if '_drag_context' not in self._pending:
                    self._selected = self._pending.get('id')
                self._pending = None
            self._populate()
            missing = any(record is None for record in snapshot['orderedFolders'])
            self.status.setText('Изменение ещё не подтверждено MAX. Повтор отключён.' if self._pending
                                else 'Некоторые папки пока недоступны.' if missing
                                else 'Папок нет.' if not snapshot['foldersOrder'] else 'Текущие папки MAX.')
            self.folders_changed.emit(snapshot)
        self._status_visibility()
        self._drain_refresh()

    def _read_error(self, _error):
        if not isValid(self):
            return
        self._cancel_folder_drag()
        self._busy = False
        self._read_ok = False
        self._refresh_queued = False
        self.status.setText('Не удалось загрузить папки. Попробуйте обновить позже.')
        self._apply_enabled()

    def _populate(self):
        if not isValid(self):
            return
        self._records = dict(zip(self._snapshot['foldersOrder'], self._snapshot['orderedFolders']))
        self.items.blockSignals(True)
        self.items.clear()
        self._row_widgets.clear()
        for identity, record in self._records.items():
            item = QListWidgetItem(record['title'] if record is not None else 'Данные папки недоступны')
            item.setData(Qt.ItemDataRole.UserRole, identity)
            # The item still holds the real title for selection/accessibility;
            # only the dedicated row paints it, avoiding two overlapping texts.
            item.setForeground(QColor(0, 0, 0, 0))
            item.setSizeHint(QSize(0, 52))
            self.items.addItem(item)
            row = _FolderRow(item.text(), self, identity)
            row.grip.setAccessibleName(tr('Порядок папок', self._language))
            row.grip.setToolTip(tr(_DRAG_HINT, self._language))
            if identity == ALL_FOLDER:
                row.icon_name = 'chats'
                row.more.hide()
                row.grip.hide()
            row.apply_theme(palette_for(self._theme))
            row.more.clicked.connect(lambda _checked=False, key=identity, button=row.more:
                                     self._open_row_menu(key, button))
            self._row_widgets[identity] = row
            self.items.setItemWidget(item, row)
            if identity == self._selected:
                self.items.setCurrentItem(item)
        self.items.setFixedHeight(max(52, min(520, 52 * len(self._records))))
        self.items.blockSignals(False)
        if (self._pending is not None and self._pending['kind'] in ('create', 'update')
                and not self.editor.isHidden()):
            # A partial readback cannot replace the user's unresolved draft.
            self._apply_enabled()
        else:
            self._creating = False
            self._load_editor(self._records.get(self._selected))

    def _open_row_menu(self, identity, anchor):
        if (not isValid(self) or not isValid(anchor) or self._busy
                or identity not in self._records):
            return
        snapshot = self._snapshot
        record = self._records.get(identity)
        menu = QMenu(self)
        edit = menu.addAction(tr('Редактировать', self._language))
        edit.setEnabled(record is not None)
        delete = menu.addAction(tr('Удалить папку', self._language))
        delete.setEnabled(self._read_ok and self._pending is None and record is not None
                          and identity != ALL_FOLDER and 1 not in record.get('options', []))
        chosen = self._exec_folder_menu(menu, anchor)
        if not isValid(self) or not isValid(menu):
            return
        allowed = chosen is not None and isValid(chosen) and chosen.isEnabled()
        menu.deleteLater()
        # A refresh/dismissal while a menu is open cannot authorize an action
        # against a different folder or against now-stale metadata.
        if (not allowed or not isValid(self) or snapshot is not self._snapshot or self._busy
                or self._records.get(identity) is not record):
            return
        for index in range(self.items.count()):
            item = self.items.item(index)
            if item.data(Qt.ItemDataRole.UserRole) == identity:
                self.items.setCurrentItem(item)
                if self._selected == identity:
                    self._load_editor(record)
                break
        if chosen is delete:
            self._delete_folder()

    @staticmethod
    def _exec_folder_menu(menu, anchor):
        return menu.exec(anchor.mapToGlobal(anchor.rect().bottomRight()))

    def _selection_changed(self, current, _previous):
        self._creating = False
        self._selected = current.data(Qt.ItemDataRole.UserRole) if current is not None else None
        self._load_editor(self._records.get(self._selected))

    def _load_editor(self, record):
        if not isValid(self):
            return
        self._cancel_folder_drag()
        self.include.invalidate_dialog()
        self._populating = True
        self._baseline = dict(record or {})
        self.title.setText(self._baseline.get('title', ''))
        names = known_chats(self.service)
        self.include.set_ids(names, self._baseline.get('include', []))
        self.include.set_filters(self._baseline.get('filters', []))
        self._include_draft = self.include.values()
        self.favorites.set_ids(self._baseline.get('favorites', []), names)
        for index, checkbox in enumerate(self.filters):
            checkbox.setChecked(index in self._baseline.get('filters', []))
        self._touched.clear()
        self._populating = False
        self.advanced_button.setChecked(False)
        self.advanced.hide()
        active = record is not None or self._creating
        self.editor.setVisible(active)
        self.main_card.setVisible(not active)
        self.templates.setVisible(not active)
        self._apply_enabled()

    def _picker_current(self):
        return (isValid(self) and not self.editor.isHidden() and self._read_ok
                and not self._busy and self._pending is None
                and self._folder_context_current(self._snapshot_context))

    def _picker_changed(self):
        if self._populating:
            return
        included = self.include.values()
        if set(included) != set(self._include_draft):
            self._include_draft = included
            self._touched.add('include')
        selected = set(self.include.filter_values())
        previous = {index for index, checkbox in enumerate(self.filters) if checkbox.isChecked()}
        if selected != previous:
            self._touched.add('filters')
            for index, checkbox in enumerate(self.filters):
                checkbox.blockSignals(True)
                checkbox.setChecked(index in selected)
                checkbox.blockSignals(False)
        self._apply_enabled()

    def _filters_changed(self, _checked=False):
        if self._populating:
            return
        self.include.set_filters([index for index, checkbox in enumerate(self.filters) if checkbox.isChecked()])
        self._edited('filters')

    def _template_missing(self, kind):
        if not self._read_ok or not self._snapshot or any(record is None for record in self._records.values()):
            return False
        filters = [set(record.get('filters', [])) for record in self._records.values()]
        return not any(0 in values if kind == 'new' else {8, 9} <= values for values in filters)

    def _create_template(self, kind):
        if (kind not in _TEMPLATES or not self.template_buttons[kind].isEnabled()
                or not self.editor.isHidden()
                or not self._template_missing(kind) or self._busy or self._pending is not None
                or not self._folder_context_current(self._snapshot_context)):
            return
        source, flags = _TEMPLATES[kind]
        title, flags = tr(source, self._language), list(flags)
        self._start_write({'kind': 'create', 'id': None, 'patch': {'title': title, 'include': [], 'filters': flags}},
                          self.service.create_folder, title, include=[], filters=flags)

    def _new_folder(self):
        if not self.new_button.isEnabled():
            return
        self.items.setCurrentRow(-1)
        self._selected = None
        self._creating = True
        self._load_editor({'title': '', 'include': [], 'filters': [], 'favorites': []})
        self.title.setFocus()

    def _edited(self, key):
        if not self._populating:
            self._touched.add(key)
            self._apply_enabled()

    def _values(self):
        return {'title': self.title.text().strip(), 'include': self.include.values(),
                'filters': [index for index, checkbox in enumerate(self.filters) if checkbox.isChecked()],
                'favorites': self.favorites.values()}

    def _patch(self):
        return {key: value for key, value in self._values().items() if key in self._touched
                and not _same_field(key, value, self._baseline.get(key, '' if key == 'title' else []))}

    def _apply_enabled(self):
        if not isValid(self):
            return
        current = self._folder_context_current(self._snapshot_context)
        available = self._read_ok and current and not self._busy and self._pending is None
        record = self._records.get(self._selected)
        editable = available and (self._creating or record is not None)
        options = self._baseline.get('options', [])
        self.refresh_button.setEnabled(not self._busy)
        # Disable the list before a focused Create button: otherwise Qt can
        # focus the list, select its first row and open an unrelated editor.
        pending_drag = self._pending is not None and '_drag_context' in self._pending
        self.items.setEnabled(not self._busy and not pending_drag)
        self.new_button.setEnabled(available)
        for identity, row in self._row_widgets.items():
            if isValid(row):
                row.more.setEnabled(not self._busy)
                row.grip.setEnabled(self._drag_available(identity))
        self.title.setEnabled(editable and self._selected != ALL_FOLDER and 2 not in options)
        self.include.setEnabled(editable)
        self.include.set_filters_enabled(editable and 3 not in options)
        self.filter_widget.setEnabled(editable and 3 not in options)
        self.show_only_card.setEnabled(editable and 3 not in options)
        self.favorites.setEnabled(editable and not self._creating)
        self.back_button.setEnabled(not self._busy and self._pending is None)
        self.cancel_button.setEnabled(not self._busy and self._pending is None)
        self.advanced_button.setEnabled(not self._busy)
        self.delete_button.setEnabled(available and record is not None and self._selected != ALL_FOLDER and 1 not in options)
        order = self._snapshot.get('foldersOrder', [])
        position = order.index(self._selected) if self._selected in order else -1
        movable = available and record is not None and bool(order) and order[0] == ALL_FOLDER and position > 0
        self.up_button.setEnabled(movable and position > 1)
        self.down_button.setEnabled(movable and position < len(order) - 1)
        for button in (self.up_button, self.down_button, self.delete_button):
            button.setVisible(not self._creating)
        self.save_button.setEnabled(editable and bool(self.title.text().strip())
                                    and (self._creating or bool(self._patch())))
        self.save_button.setText(tr('Создать папку' if self._creating else 'Сохранить изменения', self._language))
        note = ('Закрепления можно настроить после создания папки.' if self._creating
                                 else 'Название и удаление встроенной папки задаёт MAX.' if self._selected == ALL_FOLDER
                                 else 'Ограничения этой папки задаёт MAX.' if options and record is not None
                                 else 'Выберите папку или создайте новую.' if record is None else 'Изменения сохранятся после подтверждения MAX.')
        self.editor_note.setText(tr(note, self._language))
        for key, frame in self.template_cards.items():
            frame.setVisible(self._template_missing(key))
            self.template_buttons[key].setEnabled(available and self.editor.isHidden() and self._template_missing(key))
        self.templates.setVisible(self.editor.isHidden() and any(self._template_missing(key) for key in _TEMPLATES))
        self._status_visibility()

    def _save(self):
        self._apply_enabled()
        if not self.save_button.isEnabled():
            return
        values = self._values()
        if self._creating:
            self._start_write({'kind': 'create', 'id': None, 'patch': {key: values[key] for key in ('title', 'include', 'filters')}},
                              self.service.create_folder, values['title'], include=values['include'], filters=values['filters'])
        else:
            patch = self._patch()
            # Disabled controls never authorize a programmatically injected edit.
            if not self.title.isEnabled():
                patch.pop('title', None)
            if not self.filter_widget.isEnabled():
                patch.pop('filters', None)
            if patch:
                self._start_write({'kind': 'update', 'id': self._selected, 'patch': patch},
                                  self.service.update_folder, self._selected, **patch)

    def _delete_folder(self):
        if not self.delete_button.isEnabled():
            return
        if confirm_action(self, 'Удалить папку?', 'Удалить папку «' + self._baseline['title'] + '»? Чаты останутся в MAX.'):
            self._start_write({'kind': 'delete', 'id': self._selected}, self.service.delete_folder, self._selected)

    def _move_folder(self, delta):
        button = self.up_button if delta < 0 else self.down_button
        if not button.isEnabled():
            return
        order = list(self._snapshot['foldersOrder'])
        index = order.index(self._selected)
        order[index], order[index + delta] = order[index + delta], order[index]
        self._start_write({'kind': 'order', 'id': self._selected, 'order': order}, self.service.reorder_folders, order)

    def _start_write(self, intent, method, *args, **kwargs):
        if not isValid(self):
            return
        if self._busy or self._pending is not None:
            return
        self._cancel_folder_drag(cancel_native=False)
        context = intent.get('_drag_context')
        if context is None:
            context = self._snapshot_context
            if not self._folder_context_current(context):
                self._read_ok = False
                self._apply_enabled()
                return
            intent['_editor_context'] = context
            kwargs['context'] = context
            if intent['kind'] == 'order':
                kwargs['expected_order'] = list(self._snapshot['foldersOrder'])
        self._pending, self._busy = intent, True
        self.status.setText('Сохранение…')
        self._apply_enabled()
        def deliver(callback, value):
            if not isValid(self) or self._pending is not intent:
                return
            if value is _DRAG_REJECTED:
                self._pending = None
                self._busy = self._read_ok = False
                self.status.setText(tr('Изменение не выполнено. Обновите список перед новой попыткой.', self._language))
                self._apply_enabled()
                self._drain_refresh()
                return
            if not self._folder_context_current(context):
                self._read_ok = False
                self._write_error('stale write')
                return
            if (callback == self._write_done and intent['kind'] != 'order'
                    and (not isinstance(value, dict) or value.get('editor_context') is not context
                         or not isinstance(value.get('snapshot'), dict)
                         or value['snapshot'].get('editor_context') is not context)):
                self._read_ok = False
                self._write_error('unbound editor result')
                return
            callback(value)
        def work():
            if not self._folder_context_current(context):
                return _DRAG_REJECTED  # No method call, therefore no write.
            try:
                result = method(*args, **kwargs)
                if not self._folder_context_current(context):
                    raise MaxWriteOutcomeUnknown('Folder result belongs to an earlier session')
                return result
            except MaxWriteOutcomeUnknown:
                raise
            except (MaxWriteNotStarted, MaxError):
                # Only typed preflight failures or explicit rejection prove that
                # this gesture has no write awaiting reconciliation.
                return _DRAG_REJECTED
        run_async(work, on_done=lambda result: deliver(self._write_done, result),
                  on_error=lambda error: deliver(self._write_error, error))

    def _write_done(self, result):
        if not isValid(self):
            return
        if (not isinstance(result, dict)
                or not (result.get('accepted') is True or (result.get('accepted') is False
                        and result.get('unchanged') is True and result.get('pending') is False))
                or type(result.get('pending')) is not bool or self._pending is None):
            self._write_error('invalid result')
            return
        identity = result.get('folderId')
        if self._pending['kind'] == 'create' and isinstance(identity, str) and identity:
            self._pending['id'] = identity
        elif identity is not None and identity != self._pending.get('id'):
            self._write_error('wrong folder')
            return
        # A pending ACK may supply useful rows, but cannot claim commitment.
        self._read_done(result.get('snapshot'), confirm_pending=not result['pending'])

    def _write_error(self, _error):
        if not isValid(self):
            return
        self._busy = False
        self.status.setText('Результат изменения не подтверждён. Проверьте список; повтор отключён.')
        self._apply_enabled()
        self._drain_refresh()

    def _pending_matches(self, snapshot):
        if self._pending is None:
            return False
        if snapshot.get('writePending') is True:
            return False  # The service has detected an inconsistent/partial readback.
        intent = self._pending
        context = intent.get('_drag_context', intent.get('_editor_context'))
        if context is not None and (self._snapshot_context is None
                or context[0] != self._snapshot_context[0] or snapshot.get('writePending') is not False):
            return False
        if intent['kind'] == 'order':
            return snapshot['foldersOrder'] == intent['order']
        identity = intent.get('id')
        if not isinstance(identity, str) or not identity:
            # This local service flag is set false only after it has compared
            # the actual freshly read UUID/fields to its stored write intent.
            return intent['kind'] == 'create' and snapshot.get('writePending') is False
        if intent['kind'] == 'delete':
            return identity not in snapshot['foldersOrder']
        record = next((record for record in snapshot['orderedFolders']
                       if record is not None and record['id'] == identity), None)
        return record is not None and all(_same_field(key, record.get(key, '' if key == 'title' else []), value)
                                           for key, value in intent['patch'].items())

    def _drain_refresh(self):
        if not isValid(self):
            return
        if self._refresh_queued:
            self._refresh_queued = False
            self.refresh()
