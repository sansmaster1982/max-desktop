"""Native notification navigation with confirmed, session-bound settings only."""
from __future__ import annotations

from PySide6.QtCore import QPointF, QSignalBlocker, Signal, Qt
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QLabel, QPushButton, QStackedWidget, QVBoxLayout, QWidget
from shiboken6 import isValid

from ..core.notification_settings import notification_settings
from ..core.i18n import tr
from ..protocol.settings_contracts import notification_values
from .async_runner import run_async
from .privacy_settings_panel import capture_security_context, security_context_current
from .theme import resolve_theme
from .widgets.security_setting_row import SecurityCard, SecuritySettingRow


PAGE_TITLES = {'main': 'Уведомления и звук', 'personal': 'Личные чаты',
               'groups': 'Группы', 'channels': 'Каналы'}
UNSUPPORTED = {
    'personal': 'Настройки личных чатов пока недоступны в этом клиенте.',
    'channels': 'Настройки уведомлений каналов пока недоступны в этом клиенте.',
    'dnd_call': 'Эта настройка пока недоступна в этом клиенте.',
    'new_contacts': 'Эта настройка пока недоступна в этом клиенте.',
    'reset': 'Сброс настроек уведомлений пока недоступен в этом клиенте.',
    'group_reply': 'Выбор уведомлений об ответах пока недоступен в этом клиенте.',
}
SOUND_FIELDS = {'sound': 'PUSH_SOUND', 'group_sound': 'CHATS_PUSH_SOUND'}
SOUND_DEFAULT_HINT = 'При включении выбирается стандартный звук MAX.'
SOUND_CUSTOM_HINT = ('В MAX сохранена другая настройка звука. Здесь используется стандартный звук; '
                     'повторное включение выберет его и в MAX.')
SOUND_UNKNOWN_HINT = 'Настройка звука пока неизвестна.'


def _label(text='', name='NotificationSecondary'):
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setObjectName(name)
    label.setWordWrap(True)
    return label


class NotificationSettingsPanel(QWidget):
    """Body only; the parent owns the common page header and Back button."""

    title_changed = Signal(str)
    back_available_changed = Signal(bool)
    local_delivery_changed = Signal(bool)

    def __init__(self, service, settings, parent=None):
        super().__init__(parent)
        self.setObjectName('NotificationPanel')
        self.service, self.settings = service, settings
        self.adapter = notification_settings(service)
        self._context = capture_security_context(service)
        self._request = 0
        self._loaded = self._busy = self._queued = False
        self._result = {'available': False, 'settings': {}, 'pending': False}
        self._theme = getattr(settings, 'theme', 'light')
        self._lang = getattr(settings, 'ui_language', 'ru')
        self._source_titles, self._captions, self._notes = {}, [], {}
        self._status_source = ''
        self._local_status_source = ''
        self.current_page = 'main'
        self.controls, self.rows, self.navigation_rows = {}, {}, {}
        self.cards, self.page_widgets, self.radio_rows = [], {}, {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.pages = QStackedWidget()
        self.pages.setObjectName('NotificationPages')
        layout.addWidget(self.pages)
        self._build_main()
        for page in ('personal', 'groups', 'channels'):
            self._build_detail(page)
        self.status = _label()
        self.status.setAccessibleName('Состояние настроек уведомлений')
        layout.addWidget(self.status)
        self.refresh_button = QPushButton('Обновить')
        self.refresh_button.setObjectName('NotificationRefresh')
        self.refresh_button.clicked.connect(self.refresh)
        layout.addWidget(self.refresh_button, 0, Qt.AlignmentFlag.AlignRight)
        self._populate()
        self._show_status('')
        self.apply_theme(self._theme)
        self.retranslate(self._lang)

    def _page(self, key):
        page = QWidget()
        page.setObjectName('NotificationPage')
        box = QVBoxLayout(page)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(0)
        self.pages.addWidget(page)
        self.page_widgets[key] = page
        return box

    def _card(self):
        card = SecurityCard()
        self.cards.append(card)
        return card

    def _toggle_row(self, key, title, *, height=48):
        row = SecuritySettingRow(title, toggle=True, height=height)
        row.layout().setContentsMargins(16, 8, 16, 8)
        row.toggle.setFixedSize(56, 32)
        row.toggle.setAccessibleName(title)
        row.clicked.connect(lambda _checked=False, name=key: self.controls[name].click())
        row.toggle.toggled.connect(lambda checked, name=key: self._change(name, checked))
        self.rows[key] = row
        self._source_titles[row] = title
        self.controls[key] = row.toggle
        return row

    def _build_main(self):
        layout = self._page('main')
        self.heading = _label('УВЕДОМЛЕНИЯ', 'NotificationCaption')
        self._captions.append((self.heading, 'УВЕДОМЛЕНИЯ'))
        layout.addWidget(self.heading)
        layout.addSpacing(12)
        card = self._card()
        for key in ('personal', 'groups', 'channels'):
            row = SecuritySettingRow(PAGE_TITLES[key], height=56)
            row.clicked.connect(lambda _checked=False, name=key: self.open_page(name))
            self.navigation_rows[key] = row
            self._source_titles[row] = PAGE_TITLES[key]
            card.add_row(row)
        layout.addWidget(card)
        layout.addSpacing(32)
        card = self._card()
        for key, title in (('preview', 'Показывать текст сообщений'),
                           ('sound', 'Звук уведомлений'),
                           ('dnd_call', 'Не беспокоить во время звонка'),
                           ('new_contacts', 'Уведомлять о новых контактах')):
            card.add_row(self._toggle_row(key, title))
        layout.addWidget(card)
        layout.addSpacing(32)
        card = self._card()
        self.reset_row = SecuritySettingRow('Сбросить настройки уведомлений', icon='trash', height=64)
        self._source_titles[self.reset_row] = 'Сбросить настройки уведомлений'
        self.reset_row.chevron.hide()
        self.reset_row.setEnabled(False)
        self.reset_row.setToolTip(UNSUPPORTED['reset'])
        self.reset_row.setAccessibleDescription(UNSUPPORTED['reset'])
        card.add_row(self.reset_row)
        layout.addWidget(card)
        layout.addSpacing(32)
        # This local delivery gate is independent of all server notification fields.
        card = self._card()
        self.local_delivery_row = SecuritySettingRow('Уведомления на этом компьютере', toggle=True)
        self._source_titles[self.local_delivery_row] = 'Уведомления на этом компьютере'
        self.local_delivery = self.local_delivery_row.toggle
        self.local_delivery.setFixedSize(56, 32)
        self.local_delivery.setChecked(bool(self.settings.notifications))
        self.local_delivery.setEnabled(True)
        self.local_delivery.toggled.connect(self._local_changed)
        self.local_delivery_row.clicked.connect(lambda _checked=False: self.local_delivery.click())
        card.add_row(self.local_delivery_row)
        layout.addWidget(card)
        self.local_status = _label()
        self.local_status.hide()
        layout.addWidget(self.local_status)
        layout.addStretch(1)

    def _build_detail(self, key):
        layout = self._page(key)
        card = self._card()
        card.add_row(self._toggle_row(key, 'Уведомления', height=64))
        if key == 'groups':
            card.add_row(self._toggle_row('group_sound', 'Звук в группах', height=64))
        layout.addWidget(card)
        if key in ('groups', 'channels'):
            layout.addSpacing(32)
            caption = _label('ПОЛУЧАТЬ УВЕДОМЛЕНИЯ', 'NotificationCaption')
            self._captions.append((caption, 'ПОЛУЧАТЬ УВЕДОМЛЕНИЯ'))
            layout.addWidget(caption)
            layout.addSpacing(12)
            choices = (('ON', 'Обо всём'), ('REPLY', 'Об ответах')) if key == 'groups' else (
                ('all', 'Обо всём'), ('posts', 'О новых постах'), ('replies', 'Об ответах на ваши комментарии'))
            card = self._card()
            for value, title in choices:
                row = SecuritySettingRow(title, height=64)
                row.chevron.hide()
                row.setCheckable(True)
                row.clicked.connect(lambda _checked=False, page=key, choice=value: self._radio_choice(page, choice))
                self.radio_rows[key, value] = row
                self._source_titles[row] = title
                card.add_row(row)
            layout.addWidget(card)
        note = _label(UNSUPPORTED[key] if key != 'groups' else UNSUPPORTED['group_reply'], 'NotificationUnavailable')
        self._notes[note] = UNSUPPORTED[key] if key != 'groups' else UNSUPPORTED['group_reply']
        layout.addSpacing(16)
        layout.addWidget(note)
        layout.addStretch(1)

    def _ui_alive(self):
        return (isValid(self) and all(isValid(widget) for widget in (
            self.pages, self.status, self.refresh_button, self.local_delivery,
            self.local_status,
            *self.page_widgets.values(), *(caption for caption, _source in self._captions), *self._notes))
            and all(row.ui_alive() for row in (*self.rows.values(), *self.navigation_rows.values(),
                                                *self.radio_rows.values(), self.reset_row, self.local_delivery_row)))

    def _current(self):
        return self._ui_alive() and security_context_current(self.service, self._context)

    def open_page(self, key):
        if key not in PAGE_TITLES or not self._ui_alive():
            return False
        if not self._current():
            self._expired()
        self.current_page = key
        self.pages.setCurrentWidget(self.page_widgets[key])
        self.title_changed.emit(self._tr(PAGE_TITLES[key]))
        self.back_available_changed.emit(key != 'main')
        return True

    def back(self):
        if self.current_page == 'main':
            return False
        return self.open_page('main')

    def _show_status(self, text, *, retry=False):
        self._status_source = text
        self.status.setText(self._tr(text))
        self.status.setVisible(bool(text))
        self.refresh_button.setVisible(bool(retry))
        self.refresh_button.setEnabled(not self._busy)

    def _expired(self):
        if not self._ui_alive():
            return
        self._request += 1
        self._context = None
        self._loaded = self._busy = self._queued = False
        self._result = {'available': False, 'settings': {}, 'pending': False}
        self._populate()
        self._show_status('Сессия изменилась. Обновите настройки.', retry=True)

    def _local_changed(self, checked):
        if not self._ui_alive() or type(checked) is not bool:
            return
        previous = self.settings.notifications
        if previous == checked:
            return
        persist = getattr(self.settings, 'save_notifications', None)
        try:
            if callable(persist):
                # The production Settings method commits atomically before updating memory.
                persist(checked)
            else:
                # Compatibility for old embedders/test doubles without the scoped method.
                self.settings.notifications = checked
                self.settings.save()
        except Exception:
            if not callable(persist):
                self.settings.notifications = previous
            if self._ui_alive():
                with QSignalBlocker(self.local_delivery):
                    self.local_delivery.setChecked(bool(self.settings.notifications))
                self._local_status_source = 'Не удалось сохранить настройку на этом компьютере.'
                self.local_status.setText(self._tr(self._local_status_source))
                self.local_status.show()
            return
        if self._ui_alive():
            self._local_status_source = ''
            self.local_status.clear()
            self.local_status.hide()
            self.local_delivery_changed.emit(checked)

    def ensure_loaded(self):
        if self._ui_alive() and (not self._loaded or not self._current()):
            self.refresh()

    def refresh(self):
        if not self._ui_alive():
            return
        if self._busy and self._current():
            self._queued = True
            return
        if self._busy:
            self._expired()
        context = capture_security_context(self.service)
        if not security_context_current(self.service, context):
            self._expired()
            return
        previous = self._context
        if (not isinstance(previous, tuple) or previous[0] != context[0]
                or previous[1] is not context[1] or previous[2] is not context[2]):
            self._result = {'available': False, 'settings': {}, 'pending': False}
        self._context = context
        self._busy = True
        self._populate()
        self._show_status('Загрузка настроек…')
        self._run(self.adapter.read)

    def _run(self, function, *args):
        self._request += 1
        request, context = self._request, self._context
        def deliver(callback, result):
            if not self._ui_alive() or request != self._request or context is not self._context:
                return
            if not security_context_current(self.service, context):
                self._expired()
                return
            callback(result)
        run_async(function, *args, context=context,
                  on_done=lambda result: deliver(self._done, result),
                  on_error=lambda error: deliver(self._error, error))

    def _populate(self):
        if not self._ui_alive():
            return
        values = self._result['settings']
        available = self._result['available']
        writable = available and not self._busy and not self._result['pending']
        confirmed = {
            'preview': values.get('PUSH_DETAILS') if available else None,
            'groups': values.get('CHATS_PUSH_NOTIFICATION') != 'OFF'
                      if available and values.get('CHATS_PUSH_NOTIFICATION') in ('ON', 'OFF', 'REPLY') else None,
        }
        for key, field in SOUND_FIELDS.items():
            value = values.get(field) if available else None
            confirmed[key] = value != '_NONE_' if isinstance(value, str) else None
        for key, row in self.rows.items():
            value = confirmed.get(key)
            known = type(value) is bool
            with QSignalBlocker(row.toggle):
                row.toggle.setChecked(value is True)
            row.toggle.setVisible(known)
            row.set_value('' if known else self._tr('Неизвестно'), value, confirmed=known)
            row.setEnabled(writable and known)
            row.toggle.setEnabled(writable and known)
            row.setToolTip(self._tr(UNSUPPORTED.get(key, '')) if not known else '')
            if key in UNSUPPORTED:
                row.setAccessibleDescription(self._tr(UNSUPPORTED[key]))
            if key in SOUND_FIELDS:
                sound = values.get(SOUND_FIELDS[key]) if available else None
                hint = (SOUND_UNKNOWN_HINT if not isinstance(sound, str) else
                        SOUND_DEFAULT_HINT if sound == '_NONE_' else
                        '' if sound in ('DEFAULT', '_DEFAULT_') else SOUND_CUSTOM_HINT)
                row.set_subtitle(self._tr(hint))
                row.setToolTip(self._tr(hint))
        for key, row in self.navigation_rows.items():
            value = confirmed.get(key)
            row.set_value(self._tr('Включены' if value is True else 'Выключены' if value is False else 'Неизвестно'),
                          value, confirmed=type(value) is bool)
        group_mode = values.get('CHATS_PUSH_NOTIFICATION') if available else None
        for (page, value), row in self.radio_rows.items():
            selected = page == 'groups' and value == group_mode
            with QSignalBlocker(row):
                row.setChecked(selected)
            row.set_value('', value if selected else None, confirmed=selected)
            row.value_label.setFixedSize(24, 24)
            row.value_label.setPixmap(self._radio_icon(selected))
            row.value_label.show()
            row.setEnabled(page == 'groups' and value == 'ON' and group_mode in ('ON', 'REPLY') and writable)
            row.setToolTip(self._tr(UNSUPPORTED['group_reply'] if page == 'groups' and value == 'REPLY'
                           else UNSUPPORTED['channels'] if page == 'channels' else ''))
        self.refresh_button.setEnabled(not self._busy)

    def _radio_choice(self, page, value):
        row = self.radio_rows[page, value]
        if page != 'groups' or value != 'ON' or not row.isEnabled():
            self._populate()
            return
        self._submit({'CHATS_PUSH_NOTIFICATION': 'ON'})

    def _change(self, key, checked):
        if not self._ui_alive():
            return
        if key not in self.rows or self._busy or not self.rows[key].isEnabled():
            self._populate()
            return
        patch = ({'PUSH_DETAILS': checked} if key == 'preview' else
                 {'CHATS_PUSH_NOTIFICATION': 'ON' if checked else 'OFF'} if key == 'groups' else
                 {SOUND_FIELDS[key]: 'DEFAULT' if checked else '_NONE_'} if key in SOUND_FIELDS else None)
        if patch is None:
            self._populate()
            return
        self._submit(patch)

    def _submit(self, patch):
        if not self._current():
            self._expired()
            return
        if self._busy or self._result['pending'] or not self._result['available']:
            self._populate()
            return
        if all(type(self._result['settings'].get(key)) is type(value)
               and self._result['settings'].get(key) == value for key, value in patch.items()):
            self._populate()
            return
        notification_values(patch, writable=True)
        self._busy = True
        self._populate()
        self._show_status('Сохранение…')
        self._run(self.adapter.update, patch)

    def _done(self, result):
        if not self._current():
            self._expired()
            return
        if (not isinstance(result, dict) or type(result.get('available')) is not bool
                or type(result.get('pending')) is not bool or not isinstance(result.get('settings'), dict)):
            self._error('invalid result')
            return
        try:
            values = notification_values(result['settings'])
        except ValueError:
            self._error('invalid settings')
            return
        self._busy = False
        self._loaded = True
        self._result = {**result, 'settings': values}
        self._populate()
        self._show_status('Изменение ещё не подтверждено MAX.' if result['pending'] else
                          '' if result['available'] else 'MAX пока не передал настройки уведомлений.',
                          retry=result['pending'] or not result['available'])
        self._drain()

    def _error(self, _error):
        if not self._ui_alive():
            return
        self._busy = False
        self._result = {**self._result, 'pending': True}
        self._populate()
        self._show_status('Не удалось подтвердить настройки. Нажмите «Обновить», чтобы проверить состояние.', retry=True)
        self._drain()

    def _drain(self):
        if self._queued:
            self._queued = False
            self.refresh()

    def _tr(self, source):
        return tr(source, self._lang)

    def retranslate(self, language):
        if not self._ui_alive():
            return
        self._lang = language
        for row, source in self._source_titles.items():
            row.title_label.setText(self._tr(source))
            row.setAccessibleName(self._tr(source))
            if row.toggle is not None:
                row.toggle.setAccessibleName(self._tr(source))
        for caption, source in self._captions:
            caption.setText(self._tr(source))
        for note, source in self._notes.items():
            note.setText(self._tr(source))
        self.refresh_button.setText(self._tr('Обновить'))
        self.reset_row.setToolTip(self._tr(UNSUPPORTED['reset']))
        self.reset_row.setAccessibleDescription(self._tr(UNSUPPORTED['reset']))
        self.status.setAccessibleName(self._tr('Состояние настроек уведомлений'))
        self.status.setText(self._tr(self._status_source))
        self.local_status.setText(self._tr(self._local_status_source))
        self._populate()
        self.title_changed.emit(self._tr(PAGE_TITLES[self.current_page]))

    def _radio_icon(self, selected):
        pixmap = QPixmap(48, 48)
        pixmap.fill(Qt.GlobalColor.transparent)
        pixmap.setDevicePixelRatio(2)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        color = '#0085FF' if selected else '#AAB3C2' if resolve_theme(self._theme) == 'dark' else '#B2B7BF'
        painter.setPen(QPen(QColor(color), 2))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(QPointF(12, 12), 10, 10)
        if selected:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(color))
            painter.drawEllipse(QPointF(12, 12), 6, 6)
        painter.end()
        return pixmap

    def apply_theme(self, theme):
        if not self._ui_alive():
            return
        self._theme = theme
        for card in self.cards:
            card.apply_theme(theme)
        self.local_delivery.set_theme(theme)
        dark = resolve_theme(theme) == 'dark'
        secondary = '#AAB3C2' if dark else '#878C95'
        self.setStyleSheet(f'''
            #NotificationPanel, #NotificationPages, #NotificationPage {{background:transparent;border:0;}}
            #NotificationCaption {{background:transparent;color:{secondary};font-size:14px;padding-left:12px;}}
            #NotificationSecondary, #NotificationUnavailable {{background:transparent;color:{secondary};font-size:14px;}}
            #NotificationRefresh {{background:transparent;color:#0085FF;border:0;padding:10px;font-size:14px;}}
        ''')
        icon = QPixmap(48, 48)
        icon.fill(Qt.GlobalColor.transparent)
        icon.setDevicePixelRatio(2)
        painter = QPainter(icon)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor(secondary), 1.6, Qt.PenStyle.SolidLine,
                            Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        for x1, y1, x2, y2 in ((4, 7, 20, 7), (6, 7, 7, 21), (7, 21, 17, 21),
                                (17, 21, 18, 7), (9, 4, 15, 4), (10, 11, 10, 17), (14, 11, 14, 17)):
            painter.drawLine(x1, y1, x2, y2)
        painter.end()
        self.reset_row.icon_label.setPixmap(icon)
        self._populate()
