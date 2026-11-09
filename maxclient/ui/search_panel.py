"""Grouped global-search UI; the caller owns all service/network operations.

Requests carry an opaque, token-free context by identity. This widget never
derives chat IDs, makes requests itself, or treats old-query callbacks as current.
"""
from __future__ import annotations

import math

from PySide6.QtCore import QEvent, Qt, QTimer, Signal, QSize, QSignalBlocker
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QListView, QListWidget, QListWidgetItem, QPushButton,
    QSizePolicy, QVBoxLayout, QWidget,
)
from shiboken6 import isValid

from ..core.i18n import normalize_language, tr
from .theme import palette_for
from .widgets.avatar import Avatar
from .widgets.chat_list_item import _ElidedLabel
from .widgets.line_icon import line_icon


_SECTIONS = frozenset({'CHATS', 'MESSAGES', 'PUBLIC_CHATS', 'BOTS', 'CONTACTS'})
_PUBLIC = frozenset({'PUBLIC_CHATS', 'BOTS', 'CONTACTS'})


def _int64(value):
    return type(value) is int and -(2**63) <= value < 2**63


def _label(text=''):
    value = QLabel(text)
    value.setTextFormat(Qt.TextFormat.PlainText)
    value.setWordWrap(True)
    return value


class _ResultList(QListWidget):
    keyboard_activated = Signal(object)

    def keyPressEvent(self, event):  # noqa: N802
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if self.currentItem() is not None:
                self.keyboard_activated.emit(self.currentItem())
            event.accept()
            return
        super().keyPressEvent(event)


class _SearchText(_ElidedLabel):
    def changeEvent(self, event):  # noqa: N802
        super().changeEvent(event)
        if event.type() in (QEvent.Type.FontChange, QEvent.Type.StyleChange):
            self._fit()


class _SearchRow(QWidget):
    def __init__(self, row, parent=None):
        super().__init__(parent)
        self.setObjectName('SearchResultRow')
        self.setMinimumWidth(0)
        self.setFixedHeight(72)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(10)
        self.avatar = Avatar(42)
        self.avatar.set_identity(row['title'], row['key'])
        layout.addWidget(self.avatar)
        text = QVBoxLayout()
        text.setContentsMargins(0, 0, 0, 0)
        text.setSpacing(4)
        self.title = _SearchText()
        self.title.setObjectName('SearchResultTitle')
        self.title.setText(row['title'])
        self.preview = _SearchText()
        self.preview.setObjectName('SearchResultPreview')
        self.preview.setText(row['preview'])
        text.addWidget(self.title)
        text.addWidget(self.preview)
        layout.addLayout(text, 1)
        self.setAccessibleName('\n'.join(value for value in (row['title'], row['preview']) if value))


class SearchPanel(QWidget):
    """Service-agnostic sidebar results for a single current query.

    Connect ``search_requested(request)`` to an async read adapter, then call
    ``complete(request, page)`` or ``fail(request)``. The request contains only
    query/marker/generation/context; context must be an opaque token-free object.
    A result activation emits a copy of a validated normalized row.
    """

    search_requested = Signal(object)
    result_activated = Signal(object)
    clear_requested = Signal()

    def __init__(self, parent=None, *, debounce_ms=300, theme='light', language='ru'):
        super().__init__(parent)
        if type(debounce_ms) is not int or debounce_ms < 0:
            raise ValueError('debounce_ms must be a nonnegative integer')
        self._language = normalize_language(language)
        self._status_source = 'Введите текст для поиска'
        self._headers = []
        self._expand_button = None
        self._context = None
        self._query = ''
        self._generation = 0
        self._request = None
        self._items = []
        self._keys = set()
        self._marker = None
        self._used_markers = set()
        self._retry_marker = None
        self._retry_available = False
        self._public_expanded = False
        self._partial = False
        self._paging_blocked = False
        self._theme = theme
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(debounce_ms)
        self._timer.timeout.connect(lambda: self._start(None))
        self.setObjectName('GlobalSearchPanel')
        self.setMinimumWidth(0)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        heading = QHBoxLayout()
        heading.setContentsMargins(15, 4, 12, 8)
        self.title = _label('Результаты поиска')
        self.title.setObjectName('SearchPanelTitle')
        heading.addWidget(self.title, 1)
        self.close_button = QPushButton()
        self.close_button.setFixedSize(30, 30)
        self.close_button.setAccessibleName('Закрыть поиск')
        self.close_button.setToolTip('Закрыть поиск')
        self.close_button.clicked.connect(self.clear)
        heading.addWidget(self.close_button)
        root.addLayout(heading)
        self.status = _label('Введите текст для поиска')
        self.status.setObjectName('SearchStatus')
        self.status.setContentsMargins(15, 8, 15, 8)
        root.addWidget(self.status)
        self.results = _ResultList()
        self.results.setObjectName('SearchResults')
        self.results.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.results.setResizeMode(QListView.ResizeMode.Adjust)
        self.results.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.results.setAccessibleName('Результаты поиска')
        self.results.itemClicked.connect(self._activate)
        self.results.keyboard_activated.connect(self._activate)
        root.addWidget(self.results, 1)
        self.actions = QFrame()
        actions = QHBoxLayout(self.actions)
        actions.setContentsMargins(12, 8, 12, 10)
        self.retry_button = QPushButton('Повторить')
        self.retry_button.clicked.connect(self.retry)
        actions.addWidget(self.retry_button)
        self.more_button = QPushButton('Загрузить ещё')
        self.more_button.clicked.connect(self.load_more)
        actions.addWidget(self.more_button)
        self.apply_theme(theme)
        root.addWidget(self.actions)
        self._update_actions()
        self.retranslate(self._language)

    def retranslate(self, language):
        """Relabel existing app-owned presenters without touching search state."""
        if not isValid(self):
            return
        self._language = normalize_language(language)
        self.title.setText(tr('Результаты поиска', self._language))
        close = tr('Закрыть поиск', self._language)
        self.close_button.setAccessibleName(close)
        self.close_button.setToolTip(close)
        self.results.setAccessibleName(tr('Результаты поиска', self._language))
        self.retry_button.setText(tr('Повторить', self._language))
        self.more_button.setText(tr('Загрузить ещё', self._language))
        self.status.setText(tr(self._status_source, self._language))
        # Do not rebuild rows: their data, selection, widgets and scroll position
        # belong to the current response, not to the presentation language.
        with QSignalBlocker(self.results):
            for item, source in self._headers:
                if isValid(item):
                    item.setText(tr(source, self._language))
        button = self._expand_button
        if button is not None and isValid(button):
            button.setText(tr('Показать ещё', self._language))
            button.setAccessibleName(tr('Показать остальные результаты глобального поиска', self._language))

    def bind_owner(self, context):
        """Invalidate callbacks and visible data whenever the bound owner changes."""
        if context is self._context:
            return
        self._context = context
        self._invalidate()
        self._reset_rows()
        if self._query and context is not None:
            self._set_status('Ищем…')
            self._timer.start()
        else:
            self._set_status('Введите текст для поиска' if context is not None else 'Поиск доступен после входа')

    def set_query(self, text):
        query = text.strip() if isinstance(text, str) else ''
        if query == self._query:
            return
        self._query = query
        self._invalidate()
        self._reset_rows()
        if not query:
            self._set_status('Введите текст для поиска')
        elif self._context is None:
            self._set_status('Поиск доступен после входа')
        else:
            self._set_status('Ищем…')
            self._timer.start()

    def clear(self):
        self.set_query('')
        self.clear_requested.emit()

    def _invalidate(self):
        self._generation += 1
        self._timer.stop()
        self._request = None
        self._retry_available = False
        self._retry_marker = None

    def _reset_rows(self):
        self._items = []
        self._keys = set()
        self._marker = None
        self._used_markers = set()
        self._partial = False
        self._paging_blocked = False
        self._public_expanded = False
        self._headers = []
        self._expand_button = None
        self.results.clear()
        self._update_actions()

    def _start(self, marker):
        if self._request is not None or not self._query or self._context is None:
            return
        request = {'query': self._query, 'marker': marker,
                   'generation': self._generation, 'context': self._context}
        self._request = dict(request)
        if marker is not None:
            self._used_markers.add(marker)
        self._retry_available = False
        self._set_status('Загружаем ещё…' if marker is not None else 'Ищем…')
        self._update_actions()
        self.search_requested.emit(request)

    def _matches(self, request):
        current = self._request
        return (isinstance(request, dict) and current is not None and
                request.get('context') is self._context and
                request.get('context') is current['context'] and
                type(request.get('generation')) is int and
                request['generation'] == self._generation == current['generation'] and
                request.get('query') == self._query == current['query'] and
                request.get('marker') == current['marker'])

    @staticmethod
    def _row(value):
        if (not isinstance(value, dict) or not isinstance(value.get('key'), str) or
                not value['key'] or not isinstance(value.get('section'), str) or
                value['section'] not in _SECTIONS or
                not isinstance(value.get('title'), str) or
                not isinstance(value.get('preview'), str)):
            return None
        row = {key: value.get(key) for key in
               ('key', 'section', 'title', 'preview', 'chat_id', 'message_id', 'user_id')}
        if any(identity is not None and not _int64(identity)
               for identity in (row['chat_id'], row['message_id'], row['user_id'])):
            return None
        if 'time_ms' in value:
            timestamp = value['time_ms']
            if timestamp is not None and (type(timestamp) not in (int, float) or
                                          (type(timestamp) is float and not math.isfinite(timestamp))):
                return None
            row['time_ms'] = timestamp
        if row['section'] == 'MESSAGES':
            if row['chat_id'] is None or row['message_id'] is None:
                return None
        elif row['section'] in ('CONTACTS', 'BOTS'):
            if row['user_id'] is None and row['chat_id'] is None:
                return None
        elif row['chat_id'] is None:
            return None
        return row

    def complete(self, request, page):
        """Accept one current normalized page; ignore stale/foreign callbacks."""
        if not self._matches(request):
            return False
        if (not isinstance(page, dict) or page.get('context') is not self._context or
                page.get('query') != self._query or not isinstance(page.get('items'), list) or
                type(page.get('total')) is not int or page['total'] < 0 or
                not isinstance(page.get('counters'), dict) or
                (page.get('marker') is not None and not isinstance(page['marker'], str))):
            self.fail(request)
            return False
        if request['marker'] is None:
            self._items = []
            self._keys = set()
            self._partial = False
        if type(page.get('unavailable_count')) is int and page['unavailable_count'] > 0:
            self._partial = True
        for value in page['items']:
            row = self._row(value)
            if row is None:
                self._partial = True
            elif row['key'] not in self._keys:
                self._keys.add(row['key'])
                self._items.append(row)
        marker = page.get('marker')
        self._paging_blocked = marker is not None and marker in self._used_markers
        self._marker = None if self._paging_blocked else marker
        self._request = None
        self._retry_available = False
        self._render()
        if self._partial:
            self._set_status('Часть результатов недоступна. Показаны полученные данные.')
        elif self._paging_blocked:
            self._set_status('Получены доступные результаты. Следующая страница недоступна.')
        else:
            self._set_status('' if self._items else 'Ничего не найдено')
        self._update_actions()
        return True

    def fail(self, request, _error=None):
        """No raw exception/account text is displayed. Reads require manual retry."""
        if not self._matches(request):
            return False
        self._request = None
        self._retry_marker = request['marker']
        self._retry_available = True
        self._set_status('Не удалось загрузить ещё результаты.' if self._items else 'Не удалось выполнить поиск.')
        self._update_actions()
        return True

    def retry(self):
        if not self._retry_available or self._request is not None:
            return
        marker = self._retry_marker
        self._generation += 1
        self._start(marker)

    def load_more(self):
        if self._marker is not None and not self._retry_available and self._request is None:
            self._start(self._marker)

    def _update_actions(self):
        busy = self._request is not None or self._timer.isActive()
        self.retry_button.setVisible(self._retry_available)
        self.retry_button.setEnabled(self._retry_available and not busy)
        self.more_button.setVisible(self._marker is not None and not self._retry_available)
        self.more_button.setEnabled(self._marker is not None and not busy)
        self.actions.setVisible(self._retry_available or self._marker is not None)

    def _set_status(self, source):
        self._status_source = source
        self.status.setText(tr(source, self._language))
        self.status.setVisible(bool(source))

    def _render(self):
        self._headers = []
        self._expand_button = None
        self.results.clear()
        groups = [('Чаты', [row for row in self._items if row['section'] == 'CHATS']),
                  ('Глобальный поиск', [row for row in self._items if row['section'] in _PUBLIC]),
                  ('Сообщения', [row for row in self._items if row['section'] == 'MESSAGES'])]
        for title, rows in groups:
            if not rows:
                continue
            header = QListWidgetItem(tr(title, self._language))
            header.setFlags(Qt.ItemFlag.NoItemFlags)
            header.setData(Qt.ItemDataRole.UserRole, None)
            header.setSizeHint(QSize(0, 34))
            self.results.addItem(header)
            self._headers.append((header, title))
            shown = rows if title != 'Глобальный поиск' or self._public_expanded else rows[:5]
            for row in shown:
                item = QListWidgetItem()
                item.setData(Qt.ItemDataRole.UserRole, row)
                item.setSizeHint(QSize(0, 72))
                item.setData(Qt.ItemDataRole.AccessibleTextRole, row['title'])
                self.results.addItem(item)
                self.results.setItemWidget(item, _SearchRow(row))
            if title == 'Глобальный поиск' and len(shown) < len(rows):
                item = QListWidgetItem()
                item.setFlags(Qt.ItemFlag.ItemIsEnabled)
                item.setSizeHint(QSize(0, 42))
                button = QPushButton(tr('Показать ещё', self._language))
                button.setAccessibleName(tr('Показать остальные результаты глобального поиска', self._language))
                button.clicked.connect(self.expand_public)
                self.results.addItem(item)
                self.results.setItemWidget(item, button)
                self._expand_button = button

    def expand_public(self):
        self._public_expanded = True
        self._render()

    def _activate(self, item):
        row = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        if isinstance(row, dict) and row.get('key') in self._keys:
            self.result_activated.emit(dict(row))

    def apply_theme(self, theme):
        self._theme = theme
        p = palette_for(theme)
        secondary = '#737983' if p.name == 'light' else '#A3ADBF'
        self.close_button.setIcon(line_icon('close', secondary, 18))
        self.setStyleSheet(f'''
            #GlobalSearchPanel {{background:{p.sidebar};color:{p.text};}}
            #SearchPanelTitle {{font-weight:600;color:{p.text};background:transparent;}}
            #SearchStatus,#SearchResultPreview {{color:{secondary};background:transparent;}}
            #SearchResultTitle {{color:{p.text};background:transparent;font-weight:600;}}
            #SearchResultRow {{background:transparent;}}
            #SearchResults {{background:{p.sidebar};color:{secondary};border:0;outline:0;}}
            #SearchResults::item {{border:0;padding:0;}}
            #SearchResults::item:selected {{background:{p.selected};color:{p.text};}}
            #SearchResults::item:hover {{background:{p.hover};}}
            #SearchResults::item:focus {{border:1px solid #0085FF;}}
            QScrollBar:vertical {{background:{p.sidebar};}}
            QPushButton:focus {{border:2px solid #0085FF;}}
        ''')
