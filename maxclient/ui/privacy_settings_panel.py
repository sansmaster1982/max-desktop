"""Native security rows backed only by confirmed current-session values."""
from __future__ import annotations

from PySide6.QtCore import QPoint, QSignalBlocker, Qt
from PySide6.QtWidgets import QLabel, QMenu, QPushButton, QVBoxLayout, QWidget
from shiboken6 import isValid

from ..core.i18n import normalize_language, tr
from ..protocol.settings_contracts import privacy_values
from .async_runner import run_async
from .theme import resolve_theme
from .widgets.security_setting_row import SecurityCard, SecuritySettingRow, SecurityChoiceMenu


FIELDS = (
    ('INCOMING_CALL', 'Позвонить', (('могут контакты', 'CONTACTS'), ('могут все', 'ALL'))),
    ('SEARCH_BY_PHONE', 'Найти меня по номеру', (('могут контакты', 'CONTACTS'), ('могут все', 'ALL'))),
    ('CHATS_INVITE', 'Пригласить в чат', (('могут контакты', 'CONTACTS'), ('могут все', 'ALL'))),
    ('CONTENT_LEVEL_ACCESS', 'Показывать контент', (('весь', False), ('безопасный', True))),
    ('HIDDEN', 'Видеть статус «в сети»', (('никто', True), ('могут контакты', False))),
    ('PHONE_NUMBER_PRIVACY', 'Видеть мой номер',
     (('могут все', 'ALL'), ('могут контакты', 'CONTACTS'), ('никто', 'NOBODY'))),
)
GUARDED = frozenset({'SEARCH_BY_PHONE', 'INCOMING_CALL', 'CHATS_INVITE', 'CONTENT_LEVEL_ACCESS'})


def _same(left, right):
    return type(left) is type(right) and left == right


def capture_security_context(service):
    """Pin the existing owner/client/authenticated-session guard, without RPC."""
    try:
        return service._history_read_context()
    except Exception:
        return None


def security_context_current(service, context):
    if context is None:
        return False
    try:
        service._require_history_context(context)
        return True
    except Exception:
        return False


def _same_context(left, right):
    return (isinstance(left, tuple) and isinstance(right, tuple) and len(left) == len(right) == 3
            and left[0] == right[0] and left[1] is right[1] and left[2] is right[2])


class PrivacySettingsPanel(QWidget):
    def __init__(self, service, parent=None):
        super().__init__(parent)
        self.service = service
        self._lang = self._service_language()
        self._status_source = ''
        self._values = {}
        self._busy = self._loaded = self._read_failed = self._refresh_queued = False
        self._write_key = self._write_value = None
        self._unconfirmed = {}
        self._intent_owner = None
        self._request = 0
        self._context = capture_security_context(service)
        self._menu = None
        self._theme = 'light'
        self.rows = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.family_card = SecurityCard()
        self.family_row = SecuritySettingRow('Семейная защита', subtitle='Неизвестно', icon='family_shield')
        self.family_row.setEnabled(False)
        self.family_row.setToolTip('Открытие семейной защиты пока недоступно в этом клиенте.')
        self.family_status = self.family_row.subtitle_label
        self.family_card.add_row(self.family_row)
        layout.addWidget(self.family_card)
        layout.addSpacing(40)
        self.privacy_card = SecurityCard()
        self.safe_mode_row = SecuritySettingRow(
            'Безопасный режим', subtitle='Доступно только в мобильном приложении', icon='lock', toggle=True, height=72)
        self.safe_mode_row.setEnabled(False)
        self.safe_mode_status = self.safe_mode_row.value_label
        self.privacy_card.add_row(self.safe_mode_row)
        self.info_card = SecurityCard()
        for key, title, _options in FIELDS:
            row = SecuritySettingRow(title, height=56 if key in ('HIDDEN', 'PHONE_NUMBER_PRIVACY') else 64)
            row.clicked.connect(lambda _checked=False, name=key: self.open_menu(name))
            self.rows[key] = row
            (self.info_card if key in ('HIDDEN', 'PHONE_NUMBER_PRIVACY') else self.privacy_card).add_row(
                row, divider=key == 'INCOMING_CALL')
        self.content_status = self.rows['CONTENT_LEVEL_ACCESS'].value_label
        layout.addWidget(self.privacy_card)
        layout.addSpacing(52)
        self.info_heading = QLabel('Информация')
        self.info_heading.setTextFormat(Qt.TextFormat.PlainText)
        self.info_heading.setObjectName('SettingsSecondary')
        self.info_heading.setContentsMargins(12, 0, 0, 0)
        layout.addWidget(self.info_heading)
        layout.addSpacing(16)
        self.info_card.layout_box.setContentsMargins(0, 4, 0, 4)
        layout.addWidget(self.info_card)
        self.status = QLabel()
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        self.status.setObjectName('SettingsSecondary')
        layout.addWidget(self.status)
        self.refresh_button = QPushButton('Обновить настройки')
        self.refresh_button.setObjectName('SecurityRefresh')
        self.refresh_button.clicked.connect(self.refresh)
        layout.addWidget(self.refresh_button, 0, Qt.AlignmentFlag.AlignRight)
        self._populate({})
        self._show_status('', retry=False)
        self.apply_theme('light')
        self.retranslate()

    def _service_language(self):
        settings = getattr(self.service, 'settings', None)
        value = getattr(settings, 'ui_language', None)
        # Mock services and missing preferences must not become a string locale.
        return normalize_language(value if isinstance(value, str) else 'ru')

    def _tr(self, source):
        return tr(source, self._lang)

    def retranslate(self, language=None):
        """Translate current presentation without reading or changing settings."""
        if not self._ui_alive():
            return
        self._lang = self._service_language() if language is None else normalize_language(language)
        self._close_menu()  # Captured actions from the old menu remain invalid.
        for key, title, _options in FIELDS:
            self.rows[key].title_label.setText(self._tr(title))
            self.rows[key].setAccessibleName(self._tr(title))
        for row, title in ((self.family_row, 'Семейная защита'), (self.safe_mode_row, 'Безопасный режим')):
            row.title_label.setText(self._tr(title))
            row.setAccessibleName(self._tr(title))
            if row.toggle is not None:
                row.toggle.setAccessibleName(self._tr(title))
        family_hint = self._tr('Открытие семейной защиты пока недоступно в этом клиенте.')
        self.family_row.setToolTip(family_hint)
        self.safe_mode_row.set_subtitle(self._tr('Доступно только в мобильном приложении'))
        self.safe_mode_row.setToolTip(self._tr('Доступно только в мобильном приложении'))
        self.info_heading.setText(self._tr('Информация'))
        self.refresh_button.setText(self._tr('Обновить настройки'))
        self.refresh_button.setAccessibleName(self._tr('Обновить настройки'))
        self.status.setAccessibleName(self._tr('Состояние настроек приватности'))
        self.status.setText(self._tr(self._status_source))
        self._render_values()

    def _ui_alive(self):
        return (isValid(self) and all(isValid(item) for item in (self.status, self.refresh_button, self.info_heading))
                and all(row.ui_alive() for row in (*self.rows.values(), self.safe_mode_row, self.family_row)))

    def _current(self):
        return self._ui_alive() and security_context_current(self.service, self._context)

    def _show_status(self, text, *, retry=False):
        if not self._ui_alive():
            return
        self._status_source = text
        self.status.setText(self._tr(text))
        self.status.setVisible(bool(text))
        self.refresh_button.setVisible(bool(retry))

    def _close_menu(self):
        menu, self._menu = self._menu, None
        if menu is not None and isValid(menu):
            menu.close()
            menu.deleteLater()

    def _expired(self):
        if not self._ui_alive():
            return
        self._request += 1
        if self._write_key is not None:
            self._unconfirmed[self._write_key] = self._write_value
        if self._unconfirmed and isinstance(self._context, tuple):
            self._intent_owner = self._context[0]
        self._context = None
        self._busy = self._loaded = self._refresh_queued = False
        self._read_failed = True
        self._write_key = self._write_value = None
        self._populate({})
        self._show_status('Сессия изменилась. Обновите настройки.', retry=True)

    def ensure_loaded(self):
        if self._ui_alive() and not self._loaded:
            self.refresh()

    def refresh(self):
        if not self._ui_alive():
            return
        if self._busy and self._current():
            self._refresh_queued = True
            return
        if self._busy:
            self._expired()
        context = capture_security_context(self.service)
        if not security_context_current(self.service, context):
            self._expired()
            return
        if not _same_context(self._context, context):
            if self._intent_owner != context[0]:
                self._unconfirmed.clear()
                self._intent_owner = None
            self._populate({})
        self._context = context
        self._busy = True
        self._show_status('Загрузка настроек…')
        self._apply_enabled()
        self._close_menu()
        self._run(self.service.get_privacy_settings, self._read_done, self._read_error)

    def _run(self, function, on_done, on_error, *args):
        self._request += 1
        request, context = self._request, self._context

        def deliver(callback, result):
            if not self._ui_alive() or self._request != request or self._context is not context:
                return
            if not security_context_current(self.service, context):
                self._expired()
                return
            callback(result)

        run_async(function, *args, context=context,
                  on_done=lambda result: deliver(on_done, result),
                  on_error=lambda error: deliver(on_error, error))

    def _populate(self, values):
        if not self._ui_alive():
            return
        self._close_menu()
        self._values = dict(values)
        self._render_values()

    def _render_values(self):
        if not self._ui_alive():
            return
        values = self._values
        for key, _title, options in FIELDS:
            current = self._values.get(key)
            display = next((text for text, value in options if key in values and _same(value, current)), None)
            if display is None and key in ('INCOMING_CALL', 'CHATS_INVITE') and current == 'NOBODY':
                display = 'никто'
            self.rows[key].set_value(self._tr(display if display is not None else 'Неизвестно'), current,
                                     confirmed=display is not None)
        family = {'OFF': 'Отключена', 'ADMIN': 'Вы администратор', 'MANAGEABLE': 'Профиль под защитой'}
        self.family_row.set_subtitle(self._tr(family.get(values.get('FAMILY_PROTECTION'), 'Неизвестно')))
        self.family_row.setAccessibleDescription(' · '.join((self.family_row.subtitle_label.text(),
                                                            self.family_row.toolTip())))
        safe = values.get('SAFE_MODE')
        self.safe_mode_row.toggle.setVisible(type(safe) is bool)
        with QSignalBlocker(self.safe_mode_row.toggle):
            self.safe_mode_row.toggle.setChecked(safe is True)
        self.safe_mode_row.set_value('' if type(safe) is bool else self._tr('Неизвестно'), safe,
                                     confirmed=type(safe) is bool)
        self._apply_enabled()

    def _apply_enabled(self):
        if not self._ui_alive():
            return
        locked = self._values.get('SAFE_MODE') is True or self._values.get('FAMILY_PROTECTION') == 'MANAGEABLE'
        for key, row in self.rows.items():
            enabled = (not self._busy and not self._read_failed and row.confirmed and key not in self._unconfirmed)
            row.setEnabled(enabled and not (key in GUARDED and locked))
            row.setToolTip(self._tr('Эта настройка управляется безопасным режимом или семейной защитой.')
                           if key in GUARDED and locked else '')
            row.setAccessibleDescription(' · '.join(text for text in
                (row.value_label.text(), row.toolTip()) if text))
        self.refresh_button.setEnabled(not self._busy)

    def open_menu(self, key):
        if not self._current():
            self._expired()
            return None
        row = self.rows.get(key)
        if row is None or not row.isEnabled() or self._busy:
            return None
        self._close_menu()
        menu = SecurityChoiceMenu(self)
        menu.check_color = '#EAECEF' if resolve_theme(self._theme) == 'dark' else '#17191D'
        menu.setObjectName('SecurityChoiceMenu')
        menu.setAccessibleName(row.accessibleName())
        menu.setMinimumWidth(176)
        menu.setStyleSheet(self._menu_stylesheet())
        self._menu = menu
        request, context = self._request, self._context
        options = next(options for name, _title, options in FIELDS if name == key)
        for title, value in options:
            action = menu.addAction(self._tr(title))
            action.setData(value)
            action.setCheckable(True)
            action.setChecked(_same(value, self._values[key]))
            action.triggered.connect(lambda _checked=False, selected=value: self._menu_choice(
                menu, request, context, key, selected))
        if key in ('INCOMING_CALL', 'CHATS_INVITE') and self._values[key] == 'NOBODY':
            action = menu.addAction(self._tr('никто (текущее значение)'))
            action.setData('NOBODY')
            action.setCheckable(True)
            action.setChecked(True)
            action.setEnabled(False)
        menu.popup(row.mapToGlobal(QPoint(max(0, row.width() - menu.sizeHint().width()), row.height())))
        return menu

    def _menu_choice(self, menu, request, context, key, selected):
        if (not self._ui_alive() or not isValid(menu) or self._menu is not menu
                or not (menu.isVisible() or menu.selecting)
                or self._request != request or self._context is not context):
            return
        self._choose(key, selected)

    def _choose(self, key, selected):
        if not self._current():
            self._expired()
            return
        row = self.rows.get(key)
        if (row is None or self._busy or not row.isEnabled() or key not in self._values
                or _same(self._values[key], selected)):
            return
        options = next(options for name, _title, options in FIELDS if name == key)
        if not any(_same(value, selected) for _title, value in options):
            return
        try:
            privacy_values({key: selected}, writable=True)
        except ValueError:
            return
        self._write_key, self._write_value = key, selected
        self._intent_owner = self._context[0]
        self._busy = True
        self._populate(self._values)
        self._show_status('Сохранение…')
        self._run(self.service.update_privacy_settings, self._write_done, self._write_error, {key: selected})

    def _read_done(self, result):
        if not self._current():
            self._expired()
            return
        try:
            values = privacy_values(result)
        except ValueError:
            self._read_error('invalid settings')
            return
        self._busy = False
        self._loaded = True
        self._read_failed = False
        self._unconfirmed = {key: value for key, value in self._unconfirmed.items()
                             if key not in values or not _same(values[key], value)}
        self._populate(values)
        self._show_status('Изменение ещё не подтверждено MAX. Показаны известные значения.'
                          if self._unconfirmed else 'MAX пока не передал настройки.' if not values else '',
                          retry=bool(self._unconfirmed or not values))
        self._drain_refresh()

    def _read_error(self, _error):
        if not self._current():
            self._expired()
            return
        self._busy = False
        self._read_failed = True
        self._apply_enabled()
        self._show_status('Не удалось загрузить настройки. Попробуйте обновить позже.', retry=True)
        self._refresh_queued = False

    def _write_done(self, result):
        if not self._current():
            self._expired()
            return
        key, value = self._write_key, self._write_value
        try:
            if (key is None or not isinstance(result, dict) or result.get('accepted') is not True
                    or type(result.get('pending')) is not bool):
                raise ValueError('invalid update result')
            values = privacy_values(result.get('settings'))
            if not result['pending'] and (key not in values or not _same(values[key], value)):
                raise ValueError('unconfirmed value')
        except ValueError:
            self._write_error('invalid update result')
            return
        self._busy = False
        if result['pending']:
            self._unconfirmed[key] = value
            self._populate(self._values)
            self._show_status('Запрос принят. Изменение ещё не подтверждено MAX.', retry=True)
        else:
            self._unconfirmed.pop(key, None)
            self._populate(values)
            self._show_status('')
        self._write_key = self._write_value = None
        self._drain_refresh()

    def _write_error(self, _error):
        if not self._current():
            self._expired()
            return
        if self._write_key is not None:
            self._unconfirmed[self._write_key] = self._write_value
        self._write_key = self._write_value = None
        self._busy = False
        self._populate(self._values)
        self._show_status('Не удалось подтвердить изменение. Проверьте состояние перед повтором.', retry=True)
        self._drain_refresh()

    def _drain_refresh(self):
        if self._ui_alive() and self._refresh_queued:
            self._refresh_queued = False
            self.refresh()

    def hideEvent(self, event):  # noqa: N802
        self._close_menu()
        super().hideEvent(event)

    def _menu_stylesheet(self):
        dark = resolve_theme(self._theme) == 'dark'
        surface, text, hover = ('#141821', '#EAECEF', '#23354A') if dark else ('#FFFFFF', '#17191D', '#EAF4FF')
        return (f'QMenu {{background:{surface};color:{text};border:1px solid {hover};border-radius:12px;padding:6px;}}'
                'QMenu::item {padding:9px 36px 9px 12px;border-radius:8px;}'
                'QMenu::indicator {width:0px;height:0px;image:none;}'
                f'QMenu::item:selected {{background:{hover};}}')

    def apply_theme(self, theme):
        if not self._ui_alive():
            return
        self._theme = theme
        for card in (self.family_card, self.privacy_card, self.info_card):
            card.apply_theme(theme)
        color = '#AAB3C2' if resolve_theme(theme) == 'dark' else '#878C95'
        self.refresh_button.setStyleSheet('background:transparent;border:0;color:#0085FF;padding:10px;font-size:14px;')
        self.info_heading.setStyleSheet(f'background:transparent;color:{color};font-size:14px;')
        if self._menu is not None and isValid(self._menu):
            self._menu.check_color = '#EAECEF' if resolve_theme(theme) == 'dark' else '#17191D'
            self._menu.setStyleSheet(self._menu_stylesheet())
