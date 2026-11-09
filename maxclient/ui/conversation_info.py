"""Conversation info presenter: explicit data/availability, no service or media IO."""
from __future__ import annotations
from collections import OrderedDict
import math

from PySide6.QtCore import QByteArray, QEvent, QSignalBlocker, QSize, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPalette, QPixmap
from PySide6.QtSvg import QSvgRenderer
from shiboken6 import isValid
from PySide6.QtWidgets import (
    QFrame, QGridLayout, QHBoxLayout, QLabel, QLayout, QLineEdit, QListView,
    QListWidget, QListWidgetItem, QPushButton, QScrollArea, QSizePolicy,
    QTabBar, QVBoxLayout, QWidget,
)

from ..core.i18n import normalize_language, tr
from .theme import palette_for
from .widgets.avatar import Avatar
from .widgets.chat_list_item import _ElidedLabel
from .widgets.line_icon import line_icon
from .conversation_actions_menu import ConversationActionsMenu, SOUND_CHOICES
from .conversation_media_grid import ConversationMediaGrid
from .conversation_bot_delete_dialog import bot_delete_available
from .conversation_owner_transfer_dialog import owner_transfer_available
from .conversation_edit_dialog import conversation_edit_available, conversation_edit_state


TABS = (('common_chats', 'Общие чаты'), ('media', 'Медиа'), ('files', 'Файлы'),
        ('voice', 'Голосовые'), ('links', 'Ссылки'))
_TAB_KEYS = tuple(key for key, _label in TABS)
_ACTIONS = (('chat', 'Чат', 'chats'), ('call', 'Звонок', 'phone'),
            ('video', 'Видео', 'video'), ('mute', 'Звук', 'bell'),
            ('find', 'Найти', 'search'), ('more', 'Ещё', 'more'))
_WRITE_ACTIONS = frozenset({'call', 'video', 'mute', 'edit'})
_PATHS = {
    'back': '<path d="m14.5 5-7 7 7 7M8 12h13"/>',
    'edit': '<path d="m4 16-1 5 5-1L20 8l-4-4Zm10-10 4 4M4 16l4 4"/>',
    'copy': '<rect x="8" y="8" width="12" height="13" rx="2"/><path d="M15 5V3H3v13h2"/>',
}


def _icon(name, color, size=24):
    if name not in _PATHS:
        return line_icon(name, color, size)
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" '
           f'fill="none" stroke="{color}" stroke-width="1.8" '
           f'stroke-linecap="round" stroke-linejoin="round">{_PATHS[name]}</svg>')
    pixmap = QPixmap(size * 2, size * 2)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    QSvgRenderer(QByteArray(svg.encode('utf-8'))).render(painter)
    painter.end()
    pixmap.setDevicePixelRatio(2)
    return QIcon(pixmap)


def _label(text='', name=None):
    value = QLabel(text)
    value.setTextFormat(Qt.TextFormat.PlainText)
    value.setWordWrap(True)
    if name:
        value.setObjectName(name)
    return value


def _identity(value):
    return value is None or (type(value) is int and -(2**63) <= value < 2**63)


class _ActionTile(QPushButton):
    def __init__(self, title, icon_name, parent=None):
        super().__init__(parent)
        self.icon_name = icon_name
        self.setObjectName('InfoActionTile')
        self.setFixedHeight(68)
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setAccessibleName(title)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 10, 4, 8)
        layout.setSpacing(4)
        self.icon_label = _label()
        self.icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.icon_label.setFixedHeight(25)
        self.icon_label.setStyleSheet('background:transparent;')
        self.title = _label(title)
        self.title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        for value in (self.icon_label, self.title):
            value.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        layout.addWidget(self.icon_label)
        layout.addWidget(self.title)

    def restyle(self, color):
        self.icon_label.setPixmap(_icon(self.icon_name, color, 24).pixmap(24, 24))
        self.title.setStyleSheet(f'background:transparent;color:{color};font-size:12px;')


class _InfoList(QListWidget):
    keyboard_activated = Signal(object)

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        # Nested scroll reflow can leave index widgets at the previous column
        # width until a delayed layout. Fit them to the actual viewport now.
        self.doItemsLayout()

    def keyPressEvent(self, event):  # noqa: N802
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.keyboard_activated.emit(self.currentItem())
            event.accept()
            return
        super().keyPressEvent(event)


class _InfoText(_ElidedLabel):
    def changeEvent(self, event):  # noqa: N802
        super().changeEvent(event)
        if event.type() in (QEvent.Type.FontChange, QEvent.Type.StyleChange):
            self._fit()


class ConversationInfo(QWidget):
    action_requested = Signal(str, object)
    menu_action_requested = Signal(str, object, object)  # action, value, context
    tab_requested = Signal(str, object)
    item_requested = Signal(object, object)
    file_action_requested = Signal(str, object, object)  # save/message, row, context
    thumbnail_requested = Signal(object, object)  # normalized row, opaque context
    more_requested = Signal(str, object)
    back_requested = Signal()

    def __init__(self, parent=None, *, theme='light', language='ru'):
        super().__init__(parent)
        self._lang = normalize_language(language)
        self._status_mode = 'unavailable'
        self._list_labels = []
        self._context = None
        self._snapshot = {}
        self._availability = {}
        self._tabs_available = {}
        self._tab_data = {}
        self._tab_state = {}
        self._tab_complete = {}
        self._tab_status_text = {}
        self._tab_status_sources = {}
        self._tab_busy = {}
        self._media_pixmaps = OrderedDict()
        self._media_thumbnail_requests = set()
        self._media_deferred_thumbnails = set()
        self._thumbnail_cache_limit_entries = 64
        self._thumbnail_cache_limit_bytes = 64 * 1024 * 1024
        self._selected_tab = None
        self._busy = False
        self._read_only = False
        self._theme = theme
        self._columns = None
        self._action_pending = False
        self._menu = None
        self._menu_epoch = 0
        self._file_menu_row = None
        self._build()
        self.apply_theme(theme)
        self.set_conversation(None)
        self.retranslate(self._lang)

    def _tr(self, source, **values):
        return tr(source, self._lang, **values)

    def retranslate(self, language=None):
        """Change owned captions only, retaining rows, focus and read/write state."""
        if not isValid(self):
            return
        language = normalize_language(language if language is not None else self._lang)
        if language != self._lang:
            self._dismiss_menus()
        self._lang = language
        self.header_title.setText(self._tr('Инфо'))
        self.back_button.setAccessibleName(self._tr('Назад'))
        self.back_button.setToolTip(self._tr('Назад'))
        self.edit_button.setAccessibleName(self._tr('Изменить сведения'))
        self.copy_button.setAccessibleName(self._tr('Скопировать номер телефона'))
        self.copy_button.setToolTip(self._tr('Скопировать номер телефона'))
        for key, source, _icon_name in _ACTIONS:
            self.buttons[key].title.setText(self._tr(source))
            self.buttons[key].setAccessibleName(self._tr(source))
        with QSignalBlocker(self.tabs):
            for index, (_key, source) in enumerate(TABS):
                self.tabs.setTabText(index, self._tr(source))
                self.tabs.setTabToolTip(index, self._tr(source))
        self.search.setPlaceholderText(self._tr('Найти'))
        self.search.setToolTip(self._tr('Поиск среди загруженных элементов раздела'))
        self.search.setAccessibleName(self._tr('Найти среди загруженных элементов'))
        self.retry_button.setText(self._tr('Повторить'))
        self.more_button.setText(self._tr('Загрузить ещё'))
        if not self._snapshot.get('title'):
            self.name.setText(self._tr('Сведения недоступны'))
        if not isinstance(self._snapshot.get('phone_label'), str) or not self._snapshot['phone_label']:
            self.phone_label.setText(self._tr('Телефон'))
        if not isinstance(self._snapshot.get('phone'), str) or not self._snapshot['phone']:
            self.phone.setText(self._tr('Номер не передан'))
        self._render_status()
        self._render_action_captions()
        self._render_tab_status()
        for item, title, subtitle, value in self._list_labels:
            if not isValid(title) or not isValid(subtitle):
                continue
            if value.get('_info_local_title') is True:
                text = self._tr('Данные недоступны')
                title.setText(text)
                item.setData(Qt.ItemDataRole.AccessibleTextRole, text)
            if not value.get('subtitle') and value.get('available') is not True:
                subtitle.setText(self._tr('Недоступно'))
        self.media_grid.retranslate(self._lang)

    def _build(self):
        self.setObjectName('ConversationInfo')
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.header = QFrame()
        self.header.setObjectName('InfoHeader')
        self.header.setFixedHeight(62)
        heading = QHBoxLayout(self.header)
        heading.setContentsMargins(18, 8, 18, 8)
        self.back_button = QPushButton()
        self.back_button.setAccessibleName('Назад')
        self.back_button.setToolTip('Назад')
        self.back_button.clicked.connect(lambda: self.back_requested.emit())
        heading.addWidget(self.back_button)
        self.header_title = _label(self._tr('Инфо'), 'InfoHeaderTitle')
        self.header_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        heading.addWidget(self.header_title, 1)
        self.edit_button = QPushButton()
        self.edit_button.setAccessibleName('Изменить сведения')
        self.edit_button.clicked.connect(lambda: self._action('edit'))
        heading.addWidget(self.edit_button)
        for button in (self.back_button, self.edit_button):
            button.setObjectName('InfoHeaderButton')
            button.setFixedSize(36, 36)
            button.setIconSize(QSize(24, 24))
        root.addWidget(self.header)

        self.scroll = QScrollArea()
        self.scroll.setObjectName('InfoScroll')
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.viewport().installEventFilter(self)
        body = QWidget()
        body.setObjectName('InfoBody')
        outer = QVBoxLayout(body)
        outer.setContentsMargins(24, 24, 24, 24)
        outer.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        self.content = QWidget()
        self.content.setObjectName('InfoContent')
        outer.addWidget(self.content, 0, Qt.AlignmentFlag.AlignHCenter)
        outer.addStretch(1)
        content = QVBoxLayout(self.content)
        content.setContentsMargins(0, 0, 0, 0)
        content.setSpacing(0)
        self.status = _label('', 'InfoSecondary')
        content.addWidget(self.status)
        self.avatar = Avatar(92)
        content.addWidget(self.avatar, 0, Qt.AlignmentFlag.AlignHCenter)
        content.addSpacing(12)
        self.name = _label('', 'InfoName')
        self.name.setAlignment(Qt.AlignmentFlag.AlignCenter)
        content.addWidget(self.name)
        content.addSpacing(4)
        self.profile_name = _label('', 'InfoSecondary')
        self.profile_name.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.profile_name.setWordWrap(True)
        self.profile_name.hide()
        content.addWidget(self.profile_name)
        self.presence = _label('', 'InfoSecondary')
        self.presence.setAlignment(Qt.AlignmentFlag.AlignCenter)
        content.addWidget(self.presence)
        content.addSpacing(24)
        self.actions = QWidget()
        self.actions.setObjectName('InfoActions')
        self.action_layout = QGridLayout(self.actions)
        self.action_layout.setContentsMargins(0, 0, 0, 0)
        self.action_layout.setHorizontalSpacing(12)
        self.action_layout.setVerticalSpacing(12)
        self.buttons = {}
        for key, title, icon_name in _ACTIONS:
            button = _ActionTile(self._tr(title), icon_name)
            button.clicked.connect(lambda _checked=False, action=key: self._action(action))
            self.buttons[key] = button
            if key == 'mute':
                button.installEventFilter(self)
        content.addWidget(self.actions)
        content.addSpacing(24)
        self.description = _label('', 'InfoDescription')
        content.addWidget(self.description)
        self.phone_card = QFrame()
        self.phone_card.setObjectName('InfoPhoneCard')
        self.phone_card.setMinimumHeight(86)
        phone = QHBoxLayout(self.phone_card)
        phone.setContentsMargins(20, 14, 18, 14)
        phone_text = QVBoxLayout()
        phone_text.setContentsMargins(0, 0, 0, 0)
        phone_text.setSpacing(5)
        self.phone_label = _label('Телефон', 'InfoPhoneLabel')
        self.phone = _label('', 'InfoPhone')
        phone_text.addWidget(self.phone_label)
        phone_text.addWidget(self.phone)
        phone.addLayout(phone_text, 1)
        self.copy_button = QPushButton()
        self.copy_button.setObjectName('InfoCopyButton')
        self.copy_button.setFixedSize(36, 36)
        self.copy_button.setAccessibleName('Скопировать номер телефона')
        self.copy_button.setToolTip('Скопировать номер телефона')
        self.copy_button.clicked.connect(lambda: self._action('copy_phone'))
        phone.addWidget(self.copy_button)
        content.addWidget(self.phone_card)
        content.addSpacing(24)
        self.tabs_card = QFrame()
        self.tabs_card.setObjectName('InfoTabsCard')
        tabs_layout = QVBoxLayout(self.tabs_card)
        tabs_layout.setContentsMargins(8, 5, 8, 8)
        tabs_layout.setSpacing(8)
        self.tabs = QTabBar()
        self.tabs.setObjectName('InfoTabs')
        self.tabs.setMinimumWidth(0)
        self.tabs.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.tabs.setExpanding(True)
        self.tabs.setElideMode(Qt.TextElideMode.ElideRight)
        self.tabs.setUsesScrollButtons(False)
        self.tabs.setDrawBase(False)
        for key, title in TABS:
            index = self.tabs.addTab(self._tr(title))
            self.tabs.setTabData(index, key)
            self.tabs.setTabToolTip(index, self._tr(title))
        self.tabs.currentChanged.connect(self._select_tab)
        tabs_layout.addWidget(self.tabs)
        self.search = QLineEdit()
        self.search.setObjectName('InfoItemSearch')
        self.search.setFixedHeight(36)
        self.search.setPlaceholderText('Найти')
        self.search.setToolTip('Поиск среди загруженных элементов раздела')
        self.search.setAccessibleName('Найти среди загруженных элементов')
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._render_tab)
        tabs_layout.addWidget(self.search)
        self.tab_status = _label('', 'InfoSecondary')
        self.tab_status.setContentsMargins(8, 4, 8, 4)
        tabs_layout.addWidget(self.tab_status)
        self.items = _InfoList()
        self.items.setObjectName('InfoItems')
        self.items.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.items.setResizeMode(QListView.ResizeMode.Adjust)
        self.items.itemClicked.connect(self._activate_item)
        self.items.keyboard_activated.connect(self._activate_item)
        self.items.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.items.customContextMenuRequested.connect(self._open_file_item_menu)
        tabs_layout.addWidget(self.items)
        self.media_grid = ConversationMediaGrid(theme=self._theme, language=self._lang)
        self.media_grid.activated.connect(self._activate_media)
        self.media_grid.thumbnail_needed.connect(self._request_thumbnail)
        self.media_grid.current_rect_changed.connect(self._reveal_media)
        self.media_grid.visible_keys_changed.connect(self._media_visible)
        self.scroll.verticalScrollBar().valueChanged.connect(self.media_grid.schedule_visibility)
        tabs_layout.addWidget(self.media_grid)
        self.retry_button = QPushButton('Повторить')
        self.retry_button.setObjectName('InfoPageButton')
        self.retry_button.clicked.connect(self._retry_tab)
        tabs_layout.addWidget(self.retry_button)
        self.more_button = QPushButton('Загрузить ещё')
        self.more_button.setObjectName('InfoPageButton')
        self.more_button.clicked.connect(self._more_tab)
        tabs_layout.addWidget(self.more_button)
        content.addWidget(self.tabs_card)
        self.scroll.setWidget(body)
        root.addWidget(self.scroll, 1)
        self._fit_content(self.scroll.viewport().width())

    def eventFilter(self, watched, event):  # noqa: N802
        if not isValid(self):
            return False
        if isValid(self.scroll) and watched is self.scroll.viewport() and event.type() == QEvent.Type.Resize:
            self._fit_content(event.size().width())
            self.media_grid.schedule_visibility()
        if (watched is getattr(self, 'buttons', {}).get('mute') and event.type() == QEvent.Type.Enter
                and self._allowed('mute') and self._snapshot.get('muted') is not True):
            self.open_sound_menu()
        return super().eventFilter(watched, event)

    def _fit_content(self, viewport_width):
        width = max(0, min(624, viewport_width - 48))
        self.content.setFixedWidth(width)
        columns = 6 if width >= 624 else 3 if width >= 270 else 2
        if columns != self._columns:
            while self.action_layout.count():
                self.action_layout.takeAt(0)
            for index, (key, _title, _icon_name) in enumerate(_ACTIONS):
                self.action_layout.addWidget(self.buttons[key], index // columns, index % columns)
            for column in range(6):
                self.action_layout.setColumnStretch(column, 1 if column < columns else 0)
            self._columns = columns

    def set_conversation(self, snapshot):
        self._dismiss_menus()
        valid = (isinstance(snapshot, dict) and snapshot.get('context') is not None and
                 isinstance(snapshot.get('title'), str) and
                 _identity(snapshot.get('chat_id')) and _identity(snapshot.get('peer_id')) and
                 (snapshot.get('chat_id') is not None or snapshot.get('peer_id') is not None))
        if not valid:
            snapshot = {}
        context = snapshot.get('context')
        same = context is self._context and context is not None
        if (same and any(snapshot.get(key) != self._snapshot.get(key) for key in ('chat_id', 'peer_id'))):
            snapshot, context, same = {}, None, False
        if not same:
            self._tab_data = {}
            self._tab_state = {}
            self._tab_complete = {}
            self._tab_status_text = {}
            self._tab_status_sources = {}
            self._tab_busy = {}
            self._media_pixmaps = OrderedDict()
            self._media_thumbnail_requests = set()
            self._media_deferred_thumbnails = set()
            self.media_grid.set_rows([], reset=True)
            self._selected_tab = None
            self.search.clear()
        self._context = context
        self._snapshot = dict(snapshot)
        self._availability = dict(snapshot['availability']) if isinstance(snapshot.get('availability'), dict) else {}
        self._tabs_available = dict(snapshot['tabs']) if isinstance(snapshot.get('tabs'), dict) else {}
        self._busy = snapshot.get('busy') is True
        self._read_only = snapshot.get('read_only') is True
        self._action_pending = snapshot.get('action_pending') is True
        self.name.setText(snapshot.get('title') or self._tr('Сведения недоступны'))
        self.presence.setText(snapshot.get('presence') if isinstance(snapshot.get('presence'), str) else '')
        self.presence.setVisible(bool(self.presence.text()))
        self.profile_name.setText(snapshot.get('profile_name') if isinstance(snapshot.get('profile_name'), str) else '')
        self.profile_name.setVisible(bool(self.profile_name.text()))
        self.description.setText(snapshot.get('description') if isinstance(snapshot.get('description'), str) else '')
        self.description.setVisible(bool(self.description.text()))
        identity = snapshot.get('chat_id') if snapshot.get('chat_id') is not None else snapshot.get('peer_id')
        self.avatar.set_identity(snapshot.get('title'), identity)
        # No URL/path is passed to Avatar: only an explicit already-owned pixmap.
        pixmap = snapshot.get('avatar_pixmap')
        self.avatar.set_photo(pixmap if isinstance(pixmap, QPixmap) else QPixmap())
        self.phone_label.setText(snapshot.get('phone_label') if isinstance(snapshot.get('phone_label'), str)
                                 and snapshot['phone_label'] else self._tr('Телефон'))
        self.phone.setText(snapshot.get('phone') if isinstance(snapshot.get('phone'), str)
                           and snapshot['phone'] else self._tr('Номер не передан'))
        self.phone.setProperty('availablePhone', bool(isinstance(snapshot.get('phone'), str) and snapshot['phone']))
        self.phone.style().unpolish(self.phone)
        self.phone.style().polish(self.phone)
        self._status_mode = 'action' if context is not None else 'unavailable'
        self._render_status()
        self.tabs.blockSignals(True)
        for index, key in enumerate(_TAB_KEYS):
            available = context is not None and self._tabs_available.get(key) is True
            self.tabs.setTabEnabled(index, available and not self._busy)
            if not available:
                self._tab_data.pop(key, None)
                self._tab_state.pop(key, None)
                self._tab_complete.pop(key, None)
                self._tab_status_text.pop(key, None)
                self._tab_status_sources.pop(key, None)
                self._tab_busy.pop(key, None)
        key = self._selected_tab if self._tabs_available.get(self._selected_tab) is True else next(
            (key for key in _TAB_KEYS if self._tabs_available.get(key) is True), None)
        self.tabs.setCurrentIndex(_TAB_KEYS.index(key) if key is not None else -1)
        self.tabs.blockSignals(False)
        self._selected_tab = key
        self._update_actions()
        self._render_tab()
        if key is not None:
            self._request_tab(key)
        return valid and context is not None

    def clear(self):
        """Drop the bound context/data so deferred parent replies are rejected."""
        self.set_conversation(None)

    def _allowed(self, action):
        if self._context is None or self._busy:
            return False
        if action == 'copy_phone':
            return isinstance(self._snapshot.get('phone'), str) and bool(self._snapshot['phone'])
        if action == 'more':
            return (self._availability.get('more') is True and
                    isinstance(self._snapshot.get('more_actions'), list) and
                    bool(self._snapshot['more_actions']))
        if action == 'edit' and self._snapshot.get('edit_mode') == 'server':
            state = conversation_edit_state(self._snapshot, self._context)
            return (conversation_edit_available(self._snapshot, self._context)
                    and (not self._read_only or (state is not None and state.get('pending') is True)))
        return (self._availability.get(action) is True and
                not (self._read_only and action in _WRITE_ACTIONS) and
                not (self._action_pending and action in _WRITE_ACTIONS))

    def _action(self, name):
        if self._allowed(name):
            if name == 'more':
                self.open_more_menu()
            elif name == 'mute':
                muted = self._snapshot.get('muted')
                if type(muted) is bool:
                    self._dismiss_menus()
                    self.menu_action_requested.emit('mute', 0 if muted else -1, self._context)
                else:
                    self.open_sound_menu()
            else:
                self.action_requested.emit(name, self._context)

    def _action_status(self):
        source = self._snapshot.get('action_status_source')
        if isinstance(source, str) and source:
            values = self._snapshot.get('action_status_values')
            values = dict(values) if isinstance(values, dict) and all(isinstance(k, str) for k in values) else {}
            sources = self._snapshot.get('action_status_values_sources')
            if isinstance(sources, dict):
                for key, fallback in sources.items():
                    if isinstance(key, str) and key not in values and isinstance(fallback, str):
                        values[key] = self._tr(fallback)
            return self._tr(source, **values)
        status = self._snapshot.get('action_status')
        return status if isinstance(status, str) and status else (self._tr('Ожидаем подтверждения…') if self._action_pending else '')

    def _render_status(self):
        source = {'unavailable': 'Сведения о выбранном чате недоступны',
                  'refreshing': 'Обновление…', 'error': 'Не удалось обновить сведения'}.get(self._status_mode)
        self.status.setText(self._tr(source) if source else self._action_status())
        self.status.setVisible(bool(self.status.text()))

    def set_action_pending(self, context, pending, status='', *, status_source=None, status_values=None,
                           status_values_sources=None):
        if context is not self._context or context is None:
            return False
        self._action_pending = pending is True
        self._snapshot['action_pending'] = self._action_pending
        self._snapshot['action_status'] = status if isinstance(status, str) else ''
        # Every delivery replaces provenance, so an old owned key cannot translate a later raw value.
        self._snapshot['action_status_source'] = status_source if isinstance(status_source, str) else None
        self._snapshot['action_status_values'] = dict(status_values) if isinstance(status_values, dict) else {}
        self._snapshot['action_status_values_sources'] = dict(status_values_sources) if isinstance(status_values_sources, dict) else {}
        self._status_mode = 'action'
        self._render_status()
        self._update_actions()
        return True

    def apply_action_state(self, context, *, muted=None, available=None, pending=False, status='',
                           status_source=None, status_values=None, status_values_sources=None):
        """Apply cached action state without a profile/tab refresh or popup dismissal."""
        if (not isValid(self) or context is not self._context or context is None or
                (muted is not None and type(muted) is not bool) or
                (available is not None and type(available) is not bool) or
                type(pending) is not bool or not isinstance(status, str)):
            return False
        self._snapshot['muted'] = muted
        if available is not None:
            self._availability['mute'] = available
            self._snapshot['availability'] = dict(self._availability)
        return self.set_action_pending(context, pending, status, status_source=status_source,
                                       status_values=status_values, status_values_sources=status_values_sources)

    def _dismiss_menus(self):
        self._menu_epoch += 1
        self._file_menu_row = None
        menu = self._menu
        self._menu = None
        if menu is not None and isValid(menu):
            menu.cancel()
            menu.deleteLater()

    def _menu_allowed(self, row):
        if row.get('action') == 'continue_owner_leave':
            return (isValid(self) and self._context is not None and not self._busy
                    and not self._read_only and self._snapshot.get('action_busy') is not True
                    and owner_transfer_available(self._snapshot, self._context, continuation=True))
        if row.get('action') == 'continue_bot_delete':
            return (isValid(self) and self._context is not None and not self._busy
                    and not self._read_only and self._snapshot.get('action_busy') is not True
                    and bot_delete_available(self._snapshot, self._context, continuation=True))
        return (isValid(self) and self._context is not None and not self._busy and
                (row.get('action') != 'mute' or self._availability.get('mute') is True) and
                (row.get('write') is False or not (self._action_pending or self._read_only)))

    def _show_action_menu(self, entries, anchor, *, sound=False):
        self._dismiss_menus()
        context, epoch = self._context, self._menu_epoch
        menu = ConversationActionsMenu(
            context, entries, self, theme=self._theme, allowed=self._menu_allowed,
            language=self._lang,
            anchor=anchor if sound else None,
            anchor_requested=lambda: self._sound_anchor_click(epoch, context))
        self._menu = menu
        menu.requested.connect(lambda action, value, bound: self._menu_request(epoch, action, value, bound))
        menu.popup(anchor.mapToGlobal(anchor.rect().bottomLeft()))
        return menu

    def _sound_anchor_click(self, epoch, context):
        if isValid(self) and epoch == self._menu_epoch and context is self._context and context is not None:
            self._action('mute')

    def _menu_request(self, epoch, action, value, context):
        if isValid(self) and context is self._context and context is not None and epoch == self._menu_epoch:
            self.menu_action_requested.emit(action, value, context)

    def open_sound_menu(self):
        if not self._allowed('mute'):
            return None
        return self._show_action_menu([
            {'action': 'mute', 'label': label, 'label_source': label, 'value': value, 'available': True, 'write': True}
            for label, value in SOUND_CHOICES], self.buttons['mute'], sound=True)

    def open_more_menu(self):
        if not self._allowed('more'):
            return None
        return self._show_action_menu(self._snapshot['more_actions'], self.buttons['more'])

    def closeEvent(self, event):  # noqa: N802
        self._dismiss_menus()
        super().closeEvent(event)

    def _update_actions(self):
        p = palette_for(self._theme)
        for key, button in self.buttons.items():
            enabled = self._allowed(key)
            button.setEnabled(enabled)
            if key == 'mute':
                button.icon_name = 'muted' if self._snapshot.get('muted') is True else 'bell'
            button.restyle('#0085FF' if enabled else ('#A8AEB8' if p.name == 'light' else '#657085'))
        self.edit_button.setEnabled(self._allowed('edit'))
        self.copy_button.setEnabled(self._allowed('copy_phone'))
        self._render_action_captions()
        if self._menu is not None and isValid(self._menu):
            self._menu.revalidate()

    def _render_action_captions(self):
        for key, button in self.buttons.items():
            enabled = button.isEnabled()
            button.setToolTip(button.accessibleName() if enabled else self._tr('Действие пока недоступно'))
            if not enabled and key in ('call', 'video'):
                button.setToolTip(self._tr('Звонки и видеозвонки в этой сборке пока не реализованы.'))
            if key == 'mute':
                muted = self._snapshot.get('muted')
                source = (('Включить звук' if muted is True else 'Выключить звук' if muted is False
                           else 'Выбрать время без звука') if enabled else 'Состояние звука недоступно')
                button.setToolTip(self._tr(source))
        self.edit_button.setToolTip(self._tr('Изменить сведения' if self.edit_button.isEnabled() else 'Изменение сведений недоступно'))

    def set_busy(self, context, busy):
        if context is not self._context or context is None:
            return False
        self._busy = busy is True
        self._status_mode = 'refreshing' if self._busy else 'action'
        self._render_status()
        for index, key in enumerate(_TAB_KEYS):
            self.tabs.setTabEnabled(index, self._tabs_available.get(key) is True and not self._busy)
        self._update_actions()
        self._render_tab()
        if not self._busy and self._selected_tab is not None:
            self._request_tab(self._selected_tab)
        return True

    def set_read_only(self, context, value):
        if context is not self._context or context is None:
            return False
        self._read_only = value is True
        self._update_actions()
        return True

    def set_error(self, context, _error=None):
        if not self.set_busy(context, False):
            return False
        self._status_mode = 'error'
        self._render_status()
        return True

    def _select_tab(self, index):
        key = _TAB_KEYS[index] if 0 <= index < len(_TAB_KEYS) else None
        if key is None or self._tabs_available.get(key) is not True or self._busy:
            return
        self._selected_tab = key
        self.search.clear()
        self._render_tab()
        self._request_tab(key)

    def _request_tab(self, key):
        if (self._context is not None and not self._busy and self._tabs_available.get(key) is True
                and key not in self._tab_state):
            self._tab_state[key] = 'loading'
            self._render_tab()
            self.tab_requested.emit(key, self._context)

    def set_tab_items(self, context, tab, items, *, complete=True, status='', append=False, status_source=None):
        if (context is not self._context or context is None or tab not in _TAB_KEYS or
                self._tabs_available.get(tab) is not True):
            return False
        if not isinstance(items, list) or type(complete) is not bool or not isinstance(status, str):
            self.set_tab_error(context, tab)
            return False
        normalized, supplied_thumbnails = [], []
        for item in items:
            if (not isinstance(item, dict) or not isinstance(item.get('key'), str) or
                    not item['key'] or not isinstance(item.get('title'), str) or
                    not isinstance(item.get('subtitle', ''), str)):
                normalized.append({'title': 'Данные недоступны', 'subtitle': '', 'available': False,
                                   '_info_local_title': True})
            else:
                row = dict(item)
                row.pop('_info_local_title', None)  # Never infer ownership from a caller-supplied title.
                pixmap = row.pop('thumbnail_pixmap', None)
                if isinstance(pixmap, QPixmap) and not pixmap.isNull():
                    supplied_thumbnails.append((dict(row), pixmap))
                normalized.append(row)
        if append:
            previous = list(self._tab_data.get(tab, []))
            known = {value['key'] for value in previous if isinstance(value.get('key'), str)}
            normalized = previous + [value for value in normalized if value.get('key') not in known]
        if tab == 'media':
            previous = {row.get('key'): (row.get('kind'), row.get('thumbnail_url'))
                        for row in self._tab_data.get(tab, [])}
            keys = {row.get('key') for row in normalized if isinstance(row.get('key'), str)
                    and previous.get(row.get('key')) == (row.get('kind'), row.get('thumbnail_url'))}
            self._media_pixmaps = OrderedDict((key, value) for key, value in self._media_pixmaps.items() if key in keys)
            self._media_thumbnail_requests.intersection_update(keys)
            self._media_deferred_thumbnails.intersection_update(keys)
        self._tab_data[tab] = normalized
        self._tab_state[tab] = 'ready'
        self._tab_complete[tab] = complete
        self._tab_status_text[tab] = status
        self._tab_status_sources[tab] = status_source if isinstance(status_source, str) else None
        self._tab_busy.pop(tab, None)
        if tab == self._selected_tab:
            self._render_tab()
        if tab == 'media':
            for row, pixmap in supplied_thumbnails:
                if any(all(value.get(field) == row.get(field) for field in ('key', 'kind', 'thumbnail_url'))
                       for value in self._tab_data[tab]):
                    self.set_thumbnail(context, row['key'], pixmap)
        return True

    def set_thumbnail(self, context, key, pixmap):
        """Accept decoded, caller-owned pixels only for a currently loaded key."""
        if (not isValid(self) or context is not self._context or context is None
                or not isinstance(key, str) or not isinstance(pixmap, QPixmap) or pixmap.isNull()
                or self._tabs_available.get('media') is not True
                or not any(row.get('key') == key and row.get('kind') in ('PHOTO', 'VIDEO')
                           for row in self._tab_data.get('media', []))):
            return False
        cost = lambda image: image.width() * image.height() * max(1, (image.depth() + 7) // 8)
        limit = self._thumbnail_cache_limit_bytes
        visible = self.media_grid.visible_keys()
        candidate = [(old_key, old) for old_key, old in self._media_pixmaps.items() if old_key != key]
        estimated = sum(cost(old) for _old_key, old in candidate) + cost(pixmap)
        victims = []
        for old_key, old in candidate:
            if (estimated <= limit and len(candidate) + 1 - len(victims) <= self._thumbnail_cache_limit_entries):
                break
            if old_key not in visible:
                victims.append(old_key)
                estimated -= cost(old)
        if estimated > limit or len(candidate) + 1 - len(victims) > self._thumbnail_cache_limit_entries:
            # Keep current visible cells stable at the hard cap. No immediate
            # request/evict loop; a new visibility visit may request this again.
            self._media_deferred_thumbnails.add(key)
            return False
        for old_key in victims:
            self._media_pixmaps.pop(old_key, None)
            self._media_thumbnail_requests.discard(old_key)
            self.media_grid.drop_thumbnail(old_key)
        self._media_pixmaps[key] = QPixmap(pixmap)
        self._media_pixmaps.move_to_end(key)
        self._media_deferred_thumbnails.discard(key)
        self.media_grid.set_thumbnail(key, pixmap)
        return True

    def _media_visible(self, keys):
        for key in keys:
            if key in self._media_pixmaps:
                self._media_pixmaps.move_to_end(key)
        for key in self._media_deferred_thumbnails - keys:
            self._media_thumbnail_requests.discard(key)
            self.media_grid.drop_thumbnail(key)
        self._media_deferred_thumbnails.intersection_update(keys)

    def _request_thumbnail(self, row):
        key = row.get('key') if isinstance(row, dict) else None
        if (self._context is not None and self._selected_tab == 'media' and not self._busy
                and not self._tab_busy.get('media') and self._tabs_available.get('media') is True
                and row.get('available') is True and key not in self._media_thumbnail_requests
                and key not in self._media_pixmaps
                and any(value.get('key') == key for value in self._tab_data.get('media', []))):
            self._media_thumbnail_requests.add(key)
            self.thumbnail_requested.emit(dict(row), self._context)

    def _activate_media(self, row):
        if (self._context is not None and self._selected_tab == 'media' and not self._busy
                and not self._tab_busy.get('media') and self._tabs_available.get('media') is True
                and row.get('available') is True
                and any(value.get('key') == row.get('key') for value in self._tab_data.get('media', []))):
            self.item_requested.emit(dict(row), self._context)

    def _reveal_media(self, rect):
        if self._context is not None and self._selected_tab == 'media':
            point = self.media_grid.mapTo(self.scroll.widget(), rect.center())
            self.scroll.ensureVisible(point.x(), point.y(), 12, min(rect.height() // 2 + 8, 120))

    def set_tab_error(self, context, tab, _error=None):
        if (context is not self._context or context is None or tab not in _TAB_KEYS or
                self._tabs_available.get(tab) is not True):
            return False
        self._tab_state[tab] = 'more_error' if self._tab_state.get(tab) == 'loading_more' else 'error'
        self._tab_busy.pop(tab, None)
        if tab == self._selected_tab:
            self._render_tab()
        return True

    def set_tab_busy(self, context, tab, busy):
        if (context is not self._context or context is None or tab not in _TAB_KEYS or
                self._tabs_available.get(tab) is not True):
            return False
        self._tab_busy[tab] = busy is True
        if busy is not True:
            state = self._tab_state.get(tab)
            if state == 'loading':
                self._tab_state[tab] = 'ready' if tab in self._tab_data else 'waiting'
            elif state == 'loading_more':
                self._tab_state[tab] = 'ready'
        if tab == self._selected_tab:
            self._render_tab()
        return True

    def _retry_tab(self):
        key = self._selected_tab
        if self._tab_busy.get(key):
            return
        if not self._busy and self._tab_state.get(key) == 'more_error':
            self._tab_state[key] = 'ready'
            self._more_tab()
        elif not self._busy and self._tab_state.get(key) in ('error', 'waiting'):
            self._tab_state.pop(key, None)
            self._request_tab(key)

    def _more_tab(self):
        key = self._selected_tab
        if (self._context is not None and not self._busy and not self._tab_busy.get(key)
                and self._tabs_available.get(key) is True
                and self._tab_state.get(key) == 'ready' and self._tab_complete.get(key) is False):
            self._tab_state[key] = 'loading_more'
            self._render_tab()
            self.more_requested.emit(key, self._context)

    def _render_tab(self, *_args):
        if self._file_menu_row is not None:
            self._dismiss_menus()
        key = self._selected_tab
        state = self._tab_state.get(key)
        busy = self._busy or self._tab_busy.get(key) is True
        self._list_labels = []
        self.items.clear()
        media = key == 'media'
        self.items.setVisible(not media)
        self.media_grid.setVisible(media)
        available = self._context is not None and self._tabs_available.get(key) is True
        self.search.setEnabled(available and not busy and state == 'ready')
        self.retry_button.setVisible(available and state in ('error', 'more_error', 'waiting'))
        self.retry_button.setEnabled(available and not busy)
        self.more_button.setVisible(available and self._tab_complete.get(key) is False and
                                    state in ('ready', 'loading_more'))
        self.more_button.setEnabled(available and not busy and state == 'ready')
        query = self.search.text().strip().casefold()
        values = self._tab_data.get(key, [])
        visible = [value for value in values if not query or query in
                   (value.get('title', '') + '\n' + value.get('subtitle', '')).casefold()]
        self._render_tab_status()
        if media:
            self.media_grid.set_rows(visible, enabled=available and not busy and state not in ('loading', 'loading_more'))
            for thumbnail_key, pixmap in self._media_pixmaps.items():
                self.media_grid.set_thumbnail(thumbnail_key, pixmap)
            return
        for value in visible:
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, value)
            display_title = self._tr('Данные недоступны') if value.get('_info_local_title') is True else value['title']
            item.setData(Qt.ItemDataRole.AccessibleTextRole, display_title)
            if value.get('available') is not True:
                item.setFlags(Qt.ItemFlag.NoItemFlags)
            item.setSizeHint(QSize(0, 70))
            self.items.addItem(item)
            widget = QWidget()
            widget.setObjectName('InfoItemRow')
            widget.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            layout = QVBoxLayout(widget)
            layout.setContentsMargins(12, 10, 12, 10)
            layout.setSpacing(5)
            title = _InfoText()
            title.setText(display_title)
            title.setObjectName('InfoItemTitle' if value.get('available') is True else 'InfoItemUnavailable')
            subtitle = _InfoText()
            subtitle.setObjectName('InfoItemSubtitle')
            subtitle.setText(value.get('subtitle') or (self._tr('Недоступно') if value.get('available') is not True else ''))
            layout.addWidget(title)
            layout.addWidget(subtitle)
            self.items.setItemWidget(item, widget)
            self._list_labels.append((item, title, subtitle, value))
        self.items.setFixedHeight(min(350, max(140, len(visible) * 70)))

    def _render_tab_status(self):
        """Paint a status without rebuilding rows or scheduling a tab/thumbnail read."""
        key = self._selected_tab
        state = self._tab_state.get(key)
        busy = self._busy or self._tab_busy.get(key) is True
        available = self._context is not None and self._tabs_available.get(key) is True
        query = self.search.text().strip().casefold()
        visible = any(not query or query in (value.get('title', '') + '\n' + value.get('subtitle', '')).casefold()
                      for value in self._tab_data.get(key, []))
        source = ('Раздел пока недоступен' if not available else
                  'Загрузка…' if busy else
                  'Не удалось загрузить раздел' if state == 'error' else
                  'Не удалось загрузить ещё элементы' if state == 'more_error' else
                  'Загрузка…' if state in (None, 'loading', 'loading_more') else
                  'Данные ещё не получены' if state == 'waiting' else
                  'Ничего не найдено среди загруженных элементов' if query and not visible else
                  'Нет данных для отображения' if not visible else self._tab_status_sources.get(key))
        text = self._tr(source) if isinstance(source, str) and source else self._tab_status_text.get(key, '')
        self.tab_status.setText(text)
        self.tab_status.setVisible(bool(text))

    def _activate_item(self, item):
        value = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        if (self._context is not None and not self._busy and isinstance(value, dict)
                and not self._tab_busy.get(self._selected_tab)
                and value.get('available') is True and self._tabs_available.get(self._selected_tab) is True):
            if self._selected_tab == 'files' and self._file_download_available(value):
                self.file_action_requested.emit('save', dict(value), self._context)
            else:
                self.item_requested.emit(dict(value), self._context)

    @staticmethod
    def _file_download_available(row):
        return (isinstance(row, dict) and row.get('kind') == 'FILE'
                and row.get('available') is True and row.get('download_available') is True
                and row.get('deleted') is not True and type(row.get('file_id')) in (int, float)
                and math.isfinite(row['file_id']))

    def _current_file_row(self, row, context):
        if (not isValid(self) or context is None or context is not self._context
                or self._selected_tab != 'files' or self._busy or self._tab_busy.get('files')
                or self._tabs_available.get('files') is not True or not isinstance(row, dict)
                or row.get('kind') != 'FILE' or row.get('available') is not True):
            return None
        fields = ('key', 'kind', 'file_id', 'file_name', 'item_type', 'deleted',
                  'download_available', 'chat_id', 'message_id', 'time_ms')
        return next((value for value in self._tab_data.get('files', [])
                     if all(value.get(key) == row.get(key) for key in fields)), None)

    def _open_file_item_menu(self, position):
        item = self.items.itemAt(position) if position.x() >= 0 else self.items.currentItem()
        row = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        return self.open_file_menu(row, self.items.viewport().mapToGlobal(position))

    def open_file_menu(self, row, global_position=None):
        context = self._context
        if self._current_file_row(row, context) is None:
            return None
        self._dismiss_menus()
        epoch = self._menu_epoch
        bound_row = dict(row)
        self._file_menu_row = bound_row
        def allowed(entry):
            return (epoch == self._menu_epoch and self._current_file_row(bound_row, context) is not None
                    and (entry.get('action') != 'save' or self._file_download_available(bound_row)))
        menu = ConversationActionsMenu(context, [
            {'action': 'save', 'label': 'Сохранить файл', 'label_source': 'Сохранить файл', 'available': True, 'write': False},
            {'action': 'message', 'label': 'Перейти к сообщению', 'label_source': 'Перейти к сообщению', 'available': True, 'write': False},
        ], self, theme=self._theme, allowed=allowed, language=self._lang)
        self._menu = menu
        menu.requested.connect(lambda action, _value, bound:
                               self._file_menu_request(epoch, action, bound_row, bound))
        menu.popup(global_position if global_position is not None else self.items.mapToGlobal(self.items.rect().center()))
        return menu

    def _file_menu_request(self, epoch, action, row, context):
        if (epoch == self._menu_epoch and self._current_file_row(row, context) is not None
                and (action == 'message' or action == 'save' and self._file_download_available(row))):
            self.file_action_requested.emit(action, dict(row), context)

    def apply_theme(self, theme):
        self._theme = theme
        p = palette_for(theme)
        background = '#EDEEF3' if p.name == 'light' else p.window
        secondary = '#737983' if p.name == 'light' else '#A3ADBF'
        self.setStyleSheet(f'''
            #ConversationInfo,#InfoBody,#InfoContent,#InfoActions {{background:{background};}}
            #InfoHeader {{background:{p.header};border-bottom:1px solid {p.border};}}
            #InfoHeaderTitle {{font-size:16px;font-weight:600;background:transparent;color:{p.text};}}
            #InfoHeaderButton,#InfoCopyButton {{background:transparent;border:0;padding:5px;}}
            #InfoName {{font-size:18px;font-weight:600;color:{p.text};background:transparent;}}
            #InfoSecondary {{font-size:13px;color:{secondary};background:transparent;}}
            #InfoDescription {{font-size:14px;color:{p.text};background:transparent;padding:0 0 16px 0;}}
            #InfoPhoneCard,#InfoTabsCard {{background:{p.surface};border-radius:18px;}}
            #InfoPhoneLabel {{font-size:12px;color:{secondary};background:transparent;}}
            #InfoPhone {{font-size:15px;color:#0085FF;background:transparent;}}
            #InfoPhone[availablePhone="false"] {{color:{secondary};}}
            #InfoActionTile {{background:{p.surface};border:0;border-radius:18px;padding:0;}}
            #InfoActionTile:hover {{background:{p.hover};}}
            #InfoActionTile:focus,#InfoHeaderButton:focus,#InfoCopyButton:focus {{border:2px solid #0085FF;}}
            #InfoTabs {{background:transparent;}}
            #InfoTabs::tab {{font-size:12px;background:transparent;color:{secondary};padding:10px 5px;border:0;}}
            #InfoTabs::tab:selected {{color:#0085FF;border-bottom:2px solid #0085FF;}}
            #InfoTabs::tab:disabled {{color:{secondary};}}
            #InfoItemSearch {{background:{p.surface_hi};color:{p.text};border:1px solid transparent;border-radius:10px;}}
            #InfoItemSearch:focus {{border:1px solid #0085FF;}}
            #InfoItems {{background:{p.surface};border:0;color:{p.text};}}
            #InfoItems::item:selected {{background:{p.selected};}}
            #InfoItems::item:focus {{border:1px solid #0085FF;}}
            #InfoItemRow,#InfoItemTitle {{background:transparent;color:{p.text};}}
            #InfoItemSubtitle,#InfoItemUnavailable {{background:transparent;color:{secondary};}}
            #InfoPageButton {{background:{p.surface_hi};color:#0085FF;border:0;border-radius:10px;padding:8px;min-height:20px;}}
            #InfoPageButton:hover {{background:{p.hover};}}
            #InfoPageButton:focus {{border:1px solid #0085FF;}}
            #InfoPageButton:disabled {{color:{secondary};}}
            #InfoScroll QScrollBar:vertical {{background:transparent;width:8px;margin:0;}}
            #InfoScroll QScrollBar::handle:vertical {{background:{p.scrollbar};border-radius:4px;min-height:28px;}}
            #InfoScroll QScrollBar::add-line:vertical,#InfoScroll QScrollBar::sub-line:vertical {{height:0;}}
            #InfoScroll QScrollBar::add-page:vertical,#InfoScroll QScrollBar::sub-page:vertical {{background:transparent;}}
        ''')
        self.back_button.setIcon(_icon('back', p.text))
        self.edit_button.setIcon(_icon('edit', p.text))
        self.copy_button.setIcon(_icon('copy', '#0085FF'))
        search_palette = self.search.palette()
        search_palette.setColor(QPalette.ColorRole.PlaceholderText, QColor(secondary))
        self.search.setPalette(search_palette)
        self.media_grid.apply_theme(theme)
        self._update_actions()
        if self._menu is not None and isValid(self._menu):
            self._menu.apply_theme(theme)
