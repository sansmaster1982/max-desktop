"""Embedded settings: the navigation and profile stay in the main window.

Only existing service operations are exposed. Incomplete server features have
readable pages instead of controls which pretend to save unsupported changes.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QSize, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from shiboken6 import isValid
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QCheckBox, QFileDialog, QFrame, QHBoxLayout,
    QLabel, QLineEdit, QMenu, QMessageBox, QPlainTextEdit, QPushButton,
    QScrollArea, QSlider, QStackedWidget, QVBoxLayout, QWidget,
)

from .. import __app_name__, __version__
from ..core.models import Attach, A_PHOTO
from ..core.i18n import tr, normalize_language
from ..core.appearance import CHAT_FONT_SCALES, CHAT_THEME_IDS, chat_font_index, chat_message_font_pixels
from .async_runner import run_async
from .settings_read_panels import SettingsReadPanel
from .theme import resolve_theme
from .privacy_settings_panel import PrivacySettingsPanel, capture_security_context, security_context_current
from .notification_settings_panel import NotificationSettingsPanel
from .language_panel import LanguagePanel
from .widgets.settings_device_cards import DeviceSessionsIntro, DeviceSessionRow, DeviceSessionsLogoutRow
from .folders_panel import FoldersPanel
from .blocked_contacts_panel import BlockedContactsPanel
from .media import MediaViewer
from .settings_dialog import Change2FADialog, Enable2FADialog, RecoveryEmailDialog, _ACCENTS, _Swatch
from .widgets.avatar import Avatar
from .widgets.security_setting_row import SecurityCard, SecuritySettingRow
from .widgets.line_icon import line_icon
from .widgets.appearance_preview import (
    AppearanceFontSlider, AppearancePresetButton, AppearancePresetStrip, ConversationAppearancePreview,
)
from .keyboard_shortcuts import KeyboardReference


def _label(text='', *, name='SettingsLabel', wrap=False):
    label = QLabel(str(text or ''))
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(wrap)
    if name:
        label.setObjectName(name)
    return label


class SettingsWorkspace(QWidget):
    theme_changed = Signal()
    language_changed = Signal(str)
    logout_requested = Signal()
    folders_changed = Signal(object)
    local_delivery_changed = Signal(bool)

    def __init__(self, service, settings, parent=None):
        super().__init__(parent)
        self.service, self.settings = service, settings
        self._twofa_enabled = None
        self._security_loaded = False
        self._security_loading = False
        self._security_request = 0
        self._security_context = None
        self._security_display_state = 'unknown'
        self._security_email = ''
        self._sessions_loading = False
        self._sessions_loaded = False
        self._termination_unknown = False
        self._saved_name = ''
        self._loading_profile = False
        self._profile_baseline = {}
        self._avatar_url = ''
        self._translated_widgets = []
        self._build()
        self.refresh_profile()
        self.retranslate()
        self.apply_theme()

    def _bind_text(self, widget, source, setter='setText'):
        # Only application-owned literals opt in; profile and message data never do.
        self._translated_widgets.append((widget, str(source), setter))
        getattr(widget, setter)(self._t(source))
        return widget

    def _t(self, source):
        return tr(source, getattr(self.settings, 'ui_language', 'ru'))

    def _build(self):
        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.navigation = QFrame()
        self.navigation.setObjectName('SettingsNavigation')
        self.navigation.setFixedWidth(360)
        nav = QVBoxLayout(self.navigation)
        nav.setContentsMargins(0, 0, 0, 0)
        nav.setSpacing(0)
        title = _label('Настройки', name='SettingsTitle')
        self._bind_text(title, 'Настройки')
        title.setFixedHeight(50)
        nav.addWidget(title)
        menu_scroll = QScrollArea()
        self._menu_scroll = menu_scroll
        menu_scroll.setWidgetResizable(True)
        menu_scroll.setFrameShape(QFrame.Shape.NoFrame)
        menu_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._menu_body = QWidget()
        self._menu_body.setObjectName('SettingsMenuBody')
        items = QVBoxLayout(self._menu_body)
        items.setContentsMargins(0, 0, 0, 14)
        items.setSpacing(0)
        self.nav_buttons = {}
        self.profile_button = QPushButton()
        self.profile_button.setObjectName('SettingsProfileNav')
        self.profile_button.setAccessibleName('Редактировать профиль')
        self.profile_button.setCheckable(True)
        self.profile_button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.profile_button.setFixedHeight(80)
        self.profile_button.clicked.connect(lambda: self.select_page('profile'))
        profile_row = QHBoxLayout(self.profile_button)
        profile_row.setContentsMargins(16, 8, 16, 8)
        profile_row.setSpacing(12)
        self.nav_avatar = Avatar(54)
        self.nav_avatar.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        profile_row.addWidget(self.nav_avatar)
        ptext = QVBoxLayout()
        ptext.setContentsMargins(0, 0, 0, 0)
        ptext.setSpacing(0)
        ptext.addStretch(1)
        self.profile_name = _label(name='SettingsProfileName')
        self.profile_phone = _label(name='SettingsSecondary')
        for label in (self.profile_name, self.profile_phone):
            label.setFixedHeight(20)
            label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            ptext.addWidget(label)
        ptext.addStretch(1)
        profile_row.addLayout(ptext, 1)
        profile_row.addWidget(self._icon('chevron', 15))
        # The account card stays visible while only the menu below it scrolls.
        nav.addWidget(self.profile_button)
        profile_separator = QWidget()
        profile_separator.setObjectName('SettingsNavGap')
        separator_layout = QVBoxLayout(profile_separator)
        separator_layout.setContentsMargins(16, 8, 16, 8)
        separator = QFrame()
        separator.setObjectName('SettingsDivider')
        separator.setFixedHeight(1)
        separator_layout.addWidget(separator)
        nav.addWidget(profile_separator)
        self.nav_buttons['profile'] = self.profile_button
        entries = [
            ('invite', 'link', 'Пригласить друзей', False),
            ('notifications', 'bell', 'Уведомления', True),
            ('security', 'lock', 'Безопасность', False),
            ('devices', 'devices', 'Устройства', False),
            ('stories', 'history', 'Мои истории', True),
            ('folders', 'folder', 'Папки', False),
            ('appearance', 'appearance', 'Оформление', True),
            ('storage', 'database', 'Данные и память', False),
            ('language', 'globe', 'Язык приложения', False),
            ('keyboard', 'keyboard', 'Сочетания клавиш', True),
            ('add_profile', 'add_profile', 'Добавить профиль', True),
            ('business', 'business', 'MAX для бизнеса', True),
            ('help', 'help', 'Помощь', False),
            ('about', 'info', 'О приложении', False),
        ]
        self._nav_icon_labels = []
        for key, icon, text, divider in entries:
            if divider:
                line = QFrame()
                line.setObjectName('SettingsDivider')
                line.setFixedHeight(1)
                gap = QWidget()
                gap.setObjectName('SettingsNavGap')
                gl = QVBoxLayout(gap)
                gl.setContentsMargins(16, 8, 16, 8)
                gl.addWidget(line)
                items.addWidget(gap)
            button = QPushButton()
            button.setObjectName('SettingsNavRow')
            button.setCheckable(key != 'invite')
            # Mouse selection uses the selected background; keep the focus
            # outline for Tab navigation without taking focus on a mouse click.
            button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
            button.setFixedHeight(52)
            button.setAccessibleName(text)
            self._bind_text(button, text, 'setAccessibleName')
            row = QHBoxLayout(button)
            row.setContentsMargins(16, 0, 16, 0)
            row.setSpacing(14)
            glyph = self._icon(icon)
            self._nav_icon_labels.append((glyph, icon, key))
            row.addWidget(glyph)
            label = _label(text, name='SettingsNavText')
            self._bind_text(label, text)
            if key == 'invite':
                label.setStyleSheet('background:transparent;color:#7C4DFF;')
                self._invite_label = label
            label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            row.addWidget(label, 1)
            if key != 'invite':
                row.addWidget(self._icon('chevron', 15))
            button.clicked.connect(lambda _checked=False, page=key: self.select_page(page))
            items.addWidget(button)
            self.nav_buttons[key] = button
        items.addStretch(1)
        menu_scroll.setWidget(self._menu_body)
        nav.addWidget(menu_scroll, 1)
        outer.addWidget(self.navigation)

        self.content = QWidget()
        content = QVBoxLayout(self.content)
        content.setContentsMargins(0, 0, 0, 0)
        content.setSpacing(0)
        self.page_title = _label('Редактировать профиль', name='SettingsPageTitle')
        self.page_title.setFixedHeight(64)
        self.page_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.page_header = QFrame()
        self.page_header.setObjectName('SettingsPageHeader')
        header = QHBoxLayout(self.page_header)
        header.setContentsMargins(8, 0, 8, 0)
        self.page_back = QPushButton()
        self.page_back.setObjectName('SettingsPageBack')
        self.page_back.setFixedSize(40, 40)
        back_policy = self.page_back.sizePolicy()
        back_policy.setRetainSizeWhenHidden(True)
        self.page_back.setSizePolicy(back_policy)
        self.page_back.setIcon(line_icon('back', '#7D8085', 22))
        self._bind_text(self.page_back, 'Назад', 'setAccessibleName')
        self.page_back.clicked.connect(self._page_go_back)
        header.addWidget(self.page_back)
        header.addWidget(self.page_title, 1)
        header.addSpacing(40)
        content.addWidget(self.page_header)
        self.pages = QStackedWidget()
        self.pages.setObjectName('SettingsPages')
        self.page_widgets = {}
        self._page_layouts = {}
        content.addWidget(self.pages, 1)
        outer.addWidget(self.content, 1)
        self._build_profile()
        self._build_notifications()
        self._build_security()
        self._build_devices()
        self._build_appearance()
        self._build_storage()
        self._build_keyboard()
        self._text_page('invite', 'Пригласить друзей',
                        'Скопируйте ссылку на MAX и отправьте её удобным способом.',
                        'Скопировать ссылку', self._copy_invitation)
        self.story_archive = SettingsReadPanel(self.service, 'stories', self)
        self._page('stories').addWidget(self.story_archive)
        self.folder_list = FoldersPanel(self.service, self)
        self.folder_list.folders_changed.connect(self.folders_changed.emit)
        self._page('folders').addWidget(self.folder_list)
        self.blocked_contacts = BlockedContactsPanel(self.service, self)
        self._page('blocked').addWidget(self.blocked_contacts)
        language_layout = self._page('language')
        self.page_widgets['language'].widget().layout().setContentsMargins(32, 24, 32, 32)
        self.language_panel = LanguagePanel(self.settings, self)
        self.language_panel.language_changed.connect(self._language_selected)
        language_layout.addWidget(self.language_panel)
        language_layout.addStretch(1)
        self._text_page('add_profile', 'Добавить профиль',
                        'Добавление второго профиля пока недоступно. Текущий профиль остаётся подключённым.')
        self._text_page('sferum', 'Войти в Сферум',
                        'Вход в образовательный профиль Сферум пока доступен в официальном MAX. '
                        'Этот клиент не переключает образовательные аккаунты.')
        self._text_page('business', 'MAX для бизнеса',
                        'Работа с бизнес-профилем пока не подключена в этом клиенте. '
                        'Используйте официальный MAX для настройки бизнес-профиля.')
        self._text_page('help', 'Помощь',
                        'Сообщения открываются в разделах «Все», «Новые» и «Каналы». '
                        'Настройки клавиатуры, папка загрузок и оформление находятся в этом меню.\n\n'
                        'Для справки по возможностям MAX откройте официальный сайт.',
                        'Открыть справку MAX', lambda: QDesktopServices.openUrl(QUrl('https://help.max.ru/')))
        self._build_about()
        self.select_page('profile')

    def _build_about(self):
        layout = self._page('about')
        self.page_widgets['about'].widget().layout().setContentsMargins(32, 32, 32, 32)
        layout.setSpacing(24)
        self.about_report_card = SecurityCard()
        self.about_report_row = SecuritySettingRow('Отправить отчёт в поддержку', icon='share')
        self._bind_text(self.about_report_row.title_label, 'Отправить отчёт в поддержку')
        self.about_report_row.setEnabled(False)
        self.about_report_row.setToolTip('Отправка отчётов не подключена в этом клиенте.')
        self.about_report_row.chevron.hide()
        self.about_report_card.add_row(self.about_report_row)
        layout.addWidget(self.about_report_card)
        version_card = QFrame()
        version_card.setObjectName('SettingsChoiceCard')
        row = QHBoxLayout(version_card)
        row.setContentsMargins(12, 12, 24, 12)
        text = QVBoxLayout()
        caption = _label('Версия приложения')
        self._bind_text(caption, 'Версия приложения')
        text.addWidget(caption)
        text.addWidget(_label(f'{__app_name__} {__version__}', name='SettingsSecondary'))
        row.addLayout(text, 1)
        update = QPushButton('Проверить обновления')
        self._bind_text(update, 'Проверить обновления')
        update.setObjectName('SettingsUpdateButton')
        update.setEnabled(False)
        update.setToolTip('Автоматическое обновление этой сборки пока не подключено.')
        row.addWidget(update)
        layout.addWidget(version_card)
        notice = _label('Неофициальный клиент MAX.', name='SettingsSecondary')
        self._bind_text(notice, 'Неофициальный клиент MAX.')
        layout.addWidget(notice)
        layout.addStretch(1)

    def _icon(self, name, size=24):
        label = QLabel()
        label.setFixedSize(size, size)
        label.setPixmap(line_icon(name, '#7D8085', size).pixmap(size, size))
        label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        label.setStyleSheet('background:transparent;')
        return label

    def _page(self, key):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setObjectName('SettingsPageScroll')
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        surface = QWidget()
        surface.setObjectName('SettingsPageSurface')
        centering = QHBoxLayout(surface)
        centering.setContentsMargins(32, 42, 32, 32)
        centering.addStretch(1)
        body = QWidget()
        body.setObjectName('SettingsPageBody')
        body.setMaximumWidth(640)
        body.setMinimumWidth(250)
        layout = QVBoxLayout(body)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(18)
        centering.addWidget(body, 1000)
        centering.addStretch(1)
        scroll.setWidget(surface)
        self.pages.addWidget(scroll)
        self.page_widgets[key] = scroll
        self._page_layouts[key] = layout
        return layout

    def _field(self, layout, placeholder, *, multiline=False):
        if multiline:
            field = QPlainTextEdit()
            field.setObjectName('SettingsAboutField')
            field.setPlaceholderText(placeholder)
            field.setFixedHeight(96)
            field.setProperty('maximumLength', 400)
            field.textChanged.connect(lambda: self._limit_about(field))
            layout.addWidget(field)
            return field
        row = QFrame()
        row.setObjectName('SettingsFieldRow')
        row.setFixedHeight(52)
        box = QHBoxLayout(row)
        box.setContentsMargins(14, 0, 14, 0)
        field = QLineEdit()
        field.setObjectName('SettingsProfileField')
        field.setPlaceholderText(placeholder)
        field.setAccessibleName(placeholder)
        field.setMaxLength(59)
        field.setFixedHeight(40)
        remaining = _label('59', name='SettingsFieldRemaining')
        remaining.setToolTip('Не более 59 символов')
        field.textChanged.connect(lambda text: remaining.setText(str(max(0, 59 - _utf16_length(text)))))
        field.textEdited.connect(self._profile_edited)
        box.addWidget(field, 1)
        box.addWidget(remaining)
        layout.addWidget(row)
        return field

    def _build_profile(self):
        layout = self._page('profile')
        avatar_wrap = QWidget()
        avatar_wrap.setObjectName('SettingsAvatarWrap')
        avatar_wrap.setFixedHeight(96)
        av = QHBoxLayout(avatar_wrap)
        av.setContentsMargins(0, 0, 0, 0)
        photo_host = QWidget()
        photo_host.setObjectName('SettingsAvatarWrap')
        photo_host.setFixedSize(96, 96)
        self.avatar_view_button = QPushButton(photo_host)
        self.avatar_view_button.setObjectName('SettingsAvatarButton')
        self.avatar_view_button.setFixedSize(96, 96)
        self.avatar_view_button.setAccessibleName('Посмотреть фото профиля')
        self.avatar_view_button.setToolTip('Посмотреть фото профиля')
        self.avatar_view_button.clicked.connect(self._view_avatar)
        photo_layout = QVBoxLayout(self.avatar_view_button)
        photo_layout.setContentsMargins(4, 0, 4, 8)
        self.avatar = Avatar(88)
        self.avatar.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        photo_layout.addWidget(self.avatar)
        self.avatar_button = QPushButton(photo_host)
        self.avatar_button.setObjectName('SettingsAvatarCamera')
        self.avatar_button.setGeometry(66, 62, 28, 28)
        self.avatar_button.setIcon(line_icon('camera', '#FFFFFF', 20))
        self.avatar_button.setIconSize(QSize(20, 20))
        self.avatar_button.setAccessibleName('Загрузить фото профиля')
        self.avatar_button.setToolTip('Загрузить фото профиля')
        self.avatar_button.clicked.connect(self._choose_avatar)
        av.addStretch(1)
        av.addWidget(photo_host)
        av.addStretch(1)
        layout.addWidget(avatar_wrap)
        self.first_name = self._field(layout, 'Имя')
        self.last_name = self._field(layout, 'Фамилия')
        self.about = self._field(layout, 'О себе', multiline=True)
        self.about.setAccessibleName('О себе')
        self.about.textChanged.connect(self._profile_edited)
        self.profile_save = QPushButton('Сохранить изменения')
        self.profile_save.setObjectName('Primary')
        self.profile_save.clicked.connect(self._save_profile)
        self.profile_save.hide()
        layout.addWidget(self.profile_save)
        self.profile_status = _label('', name='SettingsSecondary', wrap=True)
        self.profile_status.hide()
        layout.addWidget(self.profile_status)
        actions = QHBoxLayout()
        actions.setSpacing(12)
        logout = QPushButton('Выйти из профиля')
        logout.setObjectName('SettingsProfileAction')
        logout.setFixedHeight(52)
        logout.clicked.connect(self._logout)
        actions.addWidget(logout, 1)
        self.profile_more = QPushButton()
        self.profile_more.setObjectName('SettingsProfileAction')
        self.profile_more.setFixedSize(52, 52)
        self.profile_more.setAccessibleName('Дополнительные действия профиля')
        self.profile_more.setIcon(line_icon('more', '#252932', 24))
        self.profile_more.setIconSize(QSize(24, 24))
        self.profile_more.clicked.connect(self._profile_menu)
        actions.addWidget(self.profile_more)
        layout.addLayout(actions)
        layout.addStretch(1)

    def refresh_profile(self, prof=None):
        prof = prof or self.service.my_profile()
        name = str(prof.name or '')
        self._saved_name = name
        self._profile_baseline = {
            'first_name': getattr(prof, 'first_name', None),
            'last_name': getattr(prof, 'last_name', None),
            'description': getattr(prof, 'description', None),
        }
        self._loading_profile = True
        try:
            self.first_name.setText(str(self._profile_baseline['first_name'] or ''))
            self.last_name.setText(str(self._profile_baseline['last_name'] or ''))
            self.about.setPlainText(str(self._profile_baseline['description'] or ''))
        finally:
            self._loading_profile = False
        self._profile_identity(prof)
        self.profile_save.hide()
        self.profile_status.hide()

    def _profile_identity(self, prof):
        name = str(prof.name or '')
        self.profile_name.setText(name or 'Мой профиль')
        self.profile_phone.setText(str(prof.phone or (f'id {prof.id}' if prof.id else '')))
        self.nav_avatar.set_identity(name or 'MAX', prof.id or 'me')
        self.avatar.set_identity(name or 'MAX', prof.id or 'me')
        photo = getattr(prof, 'avatar_url', None)
        self._avatar_url = str(photo or '')
        self.avatar.set_photo(photo)
        self.nav_avatar.set_photo(photo)
        self.avatar_view_button.setEnabled(bool(self._avatar_url))

    def _profile_edited(self, _text=''):
        if self._loading_profile:
            return
        self.profile_save.setVisible(bool(self._profile_changes()))
        self.profile_status.hide()

    def _profile_changes(self):
        values = {'first_name': self.first_name.text().strip(),
                  'last_name': self.last_name.text().strip(),
                  'description': self.about.toPlainText()}
        return {key: value for key, value in values.items()
                if value != (self._profile_baseline.get(key) or '')}

    def _limit_about(self, field):
        if self._loading_profile:
            return
        text = field.toPlainText()
        if _utf16_length(text) <= 400:
            return
        kept, count = [], 0
        for char in text:
            length = _utf16_length(char)
            if count + length > 400:
                break
            kept.append(char)
            count += length
        cursor = field.textCursor()
        position = cursor.position()
        field.blockSignals(True)
        field.setPlainText(''.join(kept))
        cursor = field.textCursor()
        cursor.setPosition(min(position, count))
        field.setTextCursor(cursor)
        field.blockSignals(False)

    def _save_profile(self):
        changes = self._profile_changes()
        if not changes:
            return
        if 'first_name' in changes and not changes['first_name']:
            self._profile_result('Введите имя.')
            self.first_name.setFocus()
            return
        self._profile_busy(True)
        run_async(self.service.update_my_profile, **changes, on_done=self._profile_saved,
                  on_error=lambda error: self._profile_result(str(error)))

    def _profile_busy(self, busy):
        for widget in (self.first_name, self.last_name, self.about, self.avatar_button, self.profile_save):
            widget.setEnabled(not busy)
        self.avatar_view_button.setEnabled(not busy and bool(self._avatar_url))

    def _profile_saved(self, prof):
        self._profile_busy(False)
        self.refresh_profile(prof)

    def _profile_result(self, text):
        self._profile_busy(False)
        self.profile_status.setText(text)
        self.profile_status.show()

    def _choose_avatar(self):
        path, _filter = QFileDialog.getOpenFileName(self, 'Фото профиля', '',
                                                  'Изображения (*.jpg *.jpeg *.png *.webp);;Все файлы (*)')
        if not path:
            return
        self._profile_busy(True)
        self.profile_status.setText('Загрузка фото профиля…')
        self.profile_status.show()
        run_async(self.service.set_my_avatar, path, on_done=self._avatar_saved,
                  on_error=lambda error: self._profile_result(str(error)))

    def _view_avatar(self):
        if not self._avatar_url:
            return
        attach = Attach(type=A_PHOTO, url=self._avatar_url, file_name='Фото профиля')
        MediaViewer(self._avatar_url, attach, self.settings, self).exec()

    def _avatar_saved(self, prof):
        self._profile_busy(False)
        # A photo upload does not discard name/bio edits still in the form.
        self._profile_identity(prof)
        self.profile_status.hide()

    def _profile_menu(self):
        menu = QMenu(self)
        action = menu.addAction(line_icon('trash', '#F04452'), self._t('Удалить профиль'))
        action.setEnabled(False)
        action.setToolTip('Удаление профиля пока недоступно в этом клиенте.')
        menu.setToolTipsVisible(True)
        menu.exec(self.profile_more.mapToGlobal(self.profile_more.rect().bottomRight()))
        menu.deleteLater()

    def _about_app(self):
        self._message('О приложении', f'{__app_name__} {__version__}\nНеофициальный клиент MAX.')

    def _build_notifications(self):
        layout = self._page('notifications')
        self.page_widgets['notifications'].widget().layout().setContentsMargins(32, 32, 32, 32)
        self.notification_panel = NotificationSettingsPanel(self.service, self.settings, self)
        self.notification_panel.title_changed.connect(self._notification_title)
        self.notification_panel.back_available_changed.connect(self._notification_back)
        self.notification_panel.local_delivery_changed.connect(self.local_delivery_changed.emit)
        self.notifications = self.notification_panel.local_delivery
        layout.addWidget(self.notification_panel)
        layout.addStretch(1)

    def _notification_title(self, title):
        if getattr(self, 'current_page', None) == 'notifications':
            self.page_title.setText(title)

    def _notification_back(self, enabled):
        if getattr(self, 'current_page', None) == 'notifications':
            self.page_back.setVisible(bool(enabled))

    def _page_go_back(self):
        if self.current_page == 'notifications':
            self.notification_panel.back()

    def _language_selected(self, language):
        self.retranslate()
        self.language_changed.emit(language)

    def retranslate(self):
        language = normalize_language(getattr(self.settings, 'ui_language', 'ru'))
        for widget, source, setter in self._translated_widgets:
            if isValid(widget):
                getattr(widget, setter)(tr(source, language))
        self.language_panel.retranslate(language)
        self.keyboard_reference.retranslate(language)
        self.folder_list.set_language(language)
        self.notification_panel.retranslate(language)
        self.privacy_panel.retranslate(language)
        self._retranslate_security()
        self.devices_intro.set_language(language)
        self.terminate_button.set_language(language)
        for index in range(self.devices_list.count()):
            widget = self.devices_list.itemAt(index).widget()
            if isinstance(widget, DeviceSessionRow):
                widget.set_language(language)
        self._update_page_title()

    def _build_keyboard(self):
        layout = self._page('keyboard')
        layout.addWidget(_label('ОТПРАВЛЯТЬ СООБЩЕНИЕ В ЧАТЕ', name='SettingsSectionCaption'))
        card = QFrame()
        card.setObjectName('SettingsChoiceCard')
        rows = QVBoxLayout(card)
        rows.setContentsMargins(0, 0, 0, 0)
        rows.setSpacing(0)
        self.send_key_group = QButtonGroup(self)
        self.send_key_group.setExclusive(True)
        for value, text in ((True, 'Нажав на Enter'), (False, 'Нажав на Ctrl + Enter')):
            button = QPushButton()
            button.setObjectName('SettingsChoiceRow')
            button.setCheckable(True)
            button.setAccessibleName(text)
            button.setMinimumHeight(56)
            row = QHBoxLayout(button)
            row.setContentsMargins(12, 8, 12, 8)
            label = _label(text)
            label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            row.addWidget(label, 1)
            check = self._icon('check', 24)
            check.setObjectName('SettingsChoiceCheck')
            check.setPixmap(line_icon('check', '#FFFFFF', 18).pixmap(18, 18))
            check.setAlignment(Qt.AlignmentFlag.AlignCenter)
            check.setStyleSheet('background:#0085FF;border-radius:12px;')
            row.addWidget(check)
            button.toggled.connect(check.setVisible)
            self.send_key_group.addButton(button, int(value))
            button.setChecked(bool(self.settings.send_on_enter) == value)
            check.setVisible(button.isChecked())
            if value:
                self.send_on_enter = button
            else:
                self.send_on_ctrl_enter = button
            rows.addWidget(button)
        self.send_key_group.idClicked.connect(self._select_send_key)
        layout.addWidget(card)
        layout.addWidget(_label('Shift + Enter — новая строка.', name='SettingsSecondary', wrap=True))
        self.keyboard_reference = KeyboardReference(self)
        layout.addWidget(self.keyboard_reference)
        layout.addStretch(1)

    def _select_send_key(self, identifier):
        value = bool(identifier)
        if self.settings.send_on_enter != value:
            self._save_pref('send_on_enter', value)

    def _save_pref(self, name, value):
        setattr(self.settings, name, value)
        self.settings.save()

    def _build_security(self):
        layout = self._page('security')
        layout.parentWidget().setMaximumWidth(640)
        self.page_widgets['security'].widget().layout().setContentsMargins(32, 32, 32, 32)
        layout.setSpacing(0)
        self.password_card = SecurityCard()
        self.twofa_button = SecuritySettingRow('Пароль для входа', subtitle='Неизвестно', icon='key')
        self.twofa_status = self.twofa_button.subtitle_label
        self.twofa_button.setEnabled(False)
        self.twofa_button.clicked.connect(self._open_password_settings)
        self.password_card.add_row(self.twofa_button)
        layout.addWidget(self.password_card)
        layout.addSpacing(40)
        self.privacy_panel = PrivacySettingsPanel(self.service, self)
        layout.addWidget(self.privacy_panel)
        layout.addSpacing(40)
        self.blacklist_card = SecurityCard()
        self.blacklist_row = SecuritySettingRow(
            'Чёрный список', subtitle='Список тех, кто не может вам писать, звонить и добавлять в чаты')
        self.blacklist_row.clicked.connect(lambda: self.select_page('blocked'))
        self.blacklist_card.add_row(self.blacklist_row)
        layout.addWidget(self.blacklist_card)
        self.security_retry = QPushButton('Обновить статус пароля')
        self.security_retry.setObjectName('SettingsContentAction')
        self.security_retry.clicked.connect(self._load_security)
        self.security_retry.hide()
        layout.addWidget(self.security_retry)
        layout.addStretch(1)

        password_layout = self._page('password')
        self.password_back = QPushButton('‹ Безопасность')
        self.password_back.setObjectName('SettingsContentAction')
        self.password_back.clicked.connect(lambda: self.select_page('security'))
        password_layout.addWidget(self.password_back)
        self.password_details = _label('Состояние пароля неизвестно.', name='SettingsSecondary', wrap=True)
        password_layout.addWidget(self.password_details)
        self.password_actions_card = SecurityCard()
        self.password_change_button = SecuritySettingRow(
            'Изменить пароль', subtitle='Изменение подтверждается текущим паролем')
        self.password_change_button.setEnabled(False)
        self.password_change_button.clicked.connect(self._twofa_primary)
        self.password_actions_card.add_row(self.password_change_button)
        self.email_button = SecuritySettingRow('Изменить почту', subtitle='Почта для восстановления')
        self.email_button.setEnabled(False)
        self.email_button.clicked.connect(self._recovery_email)
        self.password_actions_card.add_row(self.email_button)
        password_layout.addWidget(self.password_actions_card)
        password_layout.addStretch(1)

    def _retranslate_security(self):
        """Render owned text without a read, write or change to authorization state."""
        if not self._security_ui_alive():
            return
        for row, source in (
                (self.twofa_button, 'Пароль для входа'),
                (self.blacklist_row, 'Чёрный список'),
                (self.password_change_button, 'Включить пароль' if self._twofa_enabled is False else 'Изменить пароль'),
                (self.email_button, 'Изменить почту')):
            if row.ui_alive():
                title = self._t(source)
                row.title_label.setText(title)
                row.setAccessibleName(title)
                row._adjust_height()
        self.blacklist_row.set_subtitle(self._t('Список тех, кто не может вам писать, звонить и добавлять в чаты'))
        self.email_button.set_subtitle(self._t('Почта для восстановления'))
        self.password_change_button.set_subtitle(
            '' if self._twofa_enabled is False else self._t('Изменение подтверждается текущим паролем'))
        self.security_retry.setText(self._t('Обновить статус пароля'))
        self.password_back.setText('‹ ' + self._t('Безопасность'))
        state = self._security_display_state
        subtitle = {'unknown': 'Неизвестно', 'loading': 'Загрузка…',
                    'error': 'Состояние неизвестно'}.get(state)
        if state == 'ready':
            subtitle = 'Включён' if self._twofa_enabled else 'Отключён'
            source = 'Пароль для входа включён.' if self._twofa_enabled else 'Пароль для входа отключён.'
        elif state == 'error':
            source = 'Не удалось загрузить состояние пароля. Обновите статус в разделе «Безопасность».'
        elif state == 'loading':
            source = 'Загрузка…'
        else:
            source = 'Состояние пароля неизвестно.'
        self.twofa_button.set_subtitle(self._t(subtitle or 'Неизвестно'))
        text = self._t(source)
        if state == 'ready' and self._security_email:
            text += '\n' + tr('Почта: {email}', getattr(self.settings, 'ui_language', 'ru'),
                               email=self._security_email)
        self.password_details.setText(text)

    def _security_ui_alive(self):
        return (isValid(self) and all(isValid(widget) for widget in (self.password_details, self.security_retry))
                and all(row.ui_alive() for row in (self.twofa_button, self.email_button, self.password_change_button)))

    def _security_is_current(self):
        return self._security_ui_alive() and security_context_current(self.service, self._security_context)

    def _load_security(self):
        if not self._security_ui_alive() or self._security_loading:
            return
        context = capture_security_context(self.service)
        if not security_context_current(self.service, context):
            self._security_error(None)
            return
        self._security_loaded = True
        self._security_loading = True
        self._security_context = context
        self._security_request += 1
        request = self._security_request
        self._twofa_enabled = None
        self._security_display_state = 'loading'
        self._security_email = ''
        self.twofa_button.setEnabled(False)
        self.password_change_button.setEnabled(False)
        self.email_button.setEnabled(False)
        self._retranslate_security()
        self.security_retry.hide()

        def deliver(callback, result):
            if (not self._security_ui_alive() or request != self._security_request
                    or self._security_context is not context):
                return
            if not security_context_current(self.service, context):
                self._security_error(None)
                return
            callback(result)

        run_async(self.service.get_2fa_status, context=context,
                  on_done=lambda result: deliver(self._security_status, result),
                  on_error=lambda error: deliver(self._security_error, error))

    def _security_status(self, status):
        if not self._security_is_current():
            self._security_error(None)
            return
        enabled = status.get('enabled') if isinstance(status, dict) else None
        if not isinstance(enabled, bool):
            self._security_error(None)
            return
        self._security_loading = False
        self._twofa_enabled = enabled
        self._security_display_state = 'ready'
        self._security_email = str(status['email']) if status.get('email') else ''
        self.twofa_button.setEnabled(True)
        self.password_change_button.setEnabled(True)
        self._retranslate_security()
        self.email_button.setEnabled(enabled)
        self.security_retry.hide()

    def _security_error(self, _error):
        if not self._security_ui_alive():
            return
        self._security_request += 1
        self._security_context = None
        self._security_loading = False
        self._security_loaded = False
        self._twofa_enabled = None
        self._security_display_state = 'error'
        self._security_email = ''
        self.twofa_button.setEnabled(False)
        self.password_change_button.setEnabled(False)
        self.email_button.setEnabled(False)
        self._retranslate_security()
        self.security_retry.show()

    def _open_password_settings(self):
        if self._twofa_enabled is None or not self._security_is_current():
            self._security_error(None)
            return
        self.select_page('password')

    def _twofa_primary(self):
        if self._twofa_enabled is None or not self._security_is_current():
            return
        dialog_type = Change2FADialog if self._twofa_enabled else Enable2FADialog
        dialog_type(self.service, self).exec()
        self._load_security()

    def _recovery_email(self):
        if self._twofa_enabled is not True or not self._security_is_current():
            return
        RecoveryEmailDialog(self.service, self).exec()
        self._load_security()

    def _build_devices(self):
        layout = self._page('devices')
        self.page_widgets['devices'].widget().layout().setContentsMargins(32, 32, 32, 32)
        layout.setSpacing(24)
        self.devices_intro = DeviceSessionsIntro(theme=self.settings.theme,
                                                language=getattr(self.settings, 'ui_language', 'ru'))
        layout.addWidget(self.devices_intro)
        self.devices_status = _label('Устройства ещё не загружены.', name='SettingsSecondary', wrap=True)
        layout.addWidget(self.devices_status)
        self.devices_card = QFrame()
        self.devices_card.setObjectName('SettingsChoiceCard')
        device_card_layout = QVBoxLayout(self.devices_card)
        device_card_layout.setContentsMargins(0, 0, 0, 0)
        device_card_layout.setSpacing(0)
        self.devices_list = QVBoxLayout()
        self.devices_list.setSpacing(0)
        device_card_layout.addLayout(self.devices_list)
        layout.addWidget(self.devices_card)
        self.devices_refresh = QPushButton('Обновить устройства')
        self._bind_text(self.devices_refresh, 'Обновить устройства')
        self.devices_refresh.setObjectName('SettingsContentAction')
        self.devices_refresh.clicked.connect(self._load_devices)
        layout.addWidget(self.devices_refresh)
        self.terminate_button = DeviceSessionsLogoutRow(theme=self.settings.theme,
                                                       language=getattr(self.settings, 'ui_language', 'ru'))
        self.terminate_button.setEnabled(False)
        self.terminate_button.clicked.connect(self._terminate_other_devices)
        device_card_layout.addWidget(self.terminate_button)
        layout.addStretch(1)

    def _load_devices(self):
        if self._sessions_loading:
            return
        self._sessions_loading = True
        self.devices_refresh.setEnabled(False)
        self.terminate_button.setEnabled(False)
        self.devices_status.setText('Загрузка устройств…')
        self.devices_status.show()
        run_async(self.service.list_sessions, on_done=self._show_devices, on_error=self._devices_error)

    def _show_devices(self, sessions):
        self._sessions_loading = False
        self._sessions_loaded = True
        self.devices_refresh.setEnabled(True)
        self._update_termination_enabled()
        while self.devices_list.count():
            item = self.devices_list.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.devices_status.setText('Активные устройства' if sessions else 'Сервер не вернул данные об устройствах.')
        self.devices_status.setVisible(not bool(sessions))
        self.devices_refresh.setVisible(not bool(sessions))
        for session in sessions:
            self.devices_list.addWidget(DeviceSessionRow(
                session, theme=self.settings.theme, language=getattr(self.settings, 'ui_language', 'ru')))

    def _devices_error(self, error):
        self._sessions_loading = False
        self._sessions_loaded = False
        self.devices_refresh.setEnabled(True)
        self.devices_refresh.show()
        self.devices_status.show()
        self.terminate_button.setEnabled(False)
        self.devices_status.setText('Не удалось загрузить устройства. ' + str(error))

    def _update_termination_enabled(self):
        try:
            allowed = self.service.can_terminate_other_sessions()
        except Exception:
            allowed = False
        self.terminate_button.setEnabled(bool(allowed and self._sessions_loaded
                                               and not self._sessions_loading
                                               and not self._termination_unknown))

    def _terminate_other_devices(self):
        if not self.terminate_button.isEnabled() or self._sessions_loading:
            return
        answer = QMessageBox.question(
            self, 'Завершить другие сеансы?',
            'MAX завершит все остальные сеансы этого профиля. На других устройствах потребуется войти снова.',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._sessions_loading = True
        self.devices_refresh.setEnabled(False)
        self.terminate_button.setEnabled(False)
        self.devices_status.setText('Завершение других сеансов…')
        self.devices_status.show()
        run_async(self.service.terminate_other_sessions,
                  on_done=self._other_devices_terminated, on_error=self._termination_error)

    def _other_devices_terminated(self, confirmed):
        if confirmed is not True:
            self._termination_error('MAX не подтвердил результат операции.')
            return
        self._sessions_loading = False
        self._load_devices()

    def _termination_error(self, _error):
        self._sessions_loading = False
        self._termination_unknown = True
        self.devices_refresh.setEnabled(True)
        self.terminate_button.setEnabled(False)
        self.devices_status.show()
        self.devices_refresh.show()
        self.devices_status.setText('Результат завершения сеансов не подтверждён. '
                                    'Проверьте список устройств; повторная команда в этом окне отключена.')

    def on_config_changed(self):
        if self.current_page == 'notifications' or self.notification_panel._loaded:
            self.notification_panel.refresh()
        if self.current_page == 'security' or self.privacy_panel._loaded or self.privacy_panel._busy:
            self.privacy_panel.refresh()
        self._update_termination_enabled()

    def _build_appearance(self):
        layout = self._page('appearance')
        layout.parentWidget().setMaximumWidth(640)
        layout.setSpacing(12)
        layout.addWidget(_label('РАЗМЕР ТЕКСТА', name='AppearanceSectionCaption'))
        font_card = QFrame()
        font_card.setObjectName('AppearanceFontCard')
        font_card.setFixedHeight(48)
        font_row = QHBoxLayout(font_card)
        font_row.setContentsMargins(16, 0, 16, 0)
        font_row.setSpacing(16)
        small_a = _label('А', name='AppearanceSmallA')
        font_row.addWidget(small_a)
        self.font_slider = AppearanceFontSlider()
        self.font_slider.setObjectName('AppearanceFontSlider')
        self.font_slider.setRange(0, 7)
        self.font_slider.setSingleStep(1)
        self.font_slider.setPageStep(1)
        self.font_slider.setTickInterval(1)
        self.font_slider.setTickPosition(QSlider.TickPosition.TicksBelow)
        self.font_slider.setValue(chat_font_index(self.settings.chat_font_scale))
        self.font_slider.setAccessibleName('Размер текста в чате')
        self.font_slider.setAccessibleDescription(f'{chat_message_font_pixels(self.settings.chat_font_scale)} пикселей')
        self.font_slider.setToolTip(self.font_slider.accessibleDescription())
        self.font_slider.valueChanged.connect(self._set_chat_font)
        font_row.addWidget(self.font_slider, 1)
        font_row.addWidget(_label('А', name='AppearanceLargeA'))
        layout.addWidget(font_card)
        layout.addSpacing(16)

        layout.addWidget(_label('ТЕМА', name='AppearanceSectionCaption'))
        self.theme_segments = QFrame()
        self.theme_segments.setObjectName('AppearanceSegments')
        segments = QHBoxLayout(self.theme_segments)
        segments.setContentsMargins(3, 3, 3, 3)
        segments.setSpacing(2)
        self.theme_group = QButtonGroup(self)
        self.theme_group.setExclusive(True)
        self.theme_buttons = {}
        for value, text in (('system', 'Системная'), ('light', 'Светлая'), ('dark', 'Тёмная')):
            button = QPushButton(text)
            button.setObjectName('AppearanceScheme')
            button.setCheckable(True)
            button.setChecked(value == self.settings.theme)
            button.setAccessibleName('Тема: ' + text)
            button.setMinimumHeight(32)
            self.theme_group.addButton(button)
            button.clicked.connect(lambda _checked=False, scheme=value: self._set_theme(scheme))
            self.theme_buttons[value] = button
            segments.addWidget(button, 1)
        layout.addWidget(self.theme_segments)
        layout.addSpacing(16)

        layout.addWidget(_label('ТЕМА ОФОРМЛЕНИЯ', name='AppearanceSectionCaption'))
        theme_panel = QFrame()
        theme_panel.setObjectName('AppearanceThemePanel')
        panel_layout = QVBoxLayout(theme_panel)
        panel_layout.setContentsMargins(0, 0, 0, 0)
        panel_layout.setSpacing(0)
        self.appearance_preview = ConversationAppearancePreview(
            self.settings.theme, self.settings.chat_theme, self.settings.chat_font_scale)
        panel_layout.addWidget(self.appearance_preview)
        self.preset_scroll = AppearancePresetStrip()
        self.preset_scroll.setObjectName('AppearancePresetScroll')
        self.preset_scroll.setWidgetResizable(True)
        self.preset_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.preset_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.preset_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.preset_scroll.setFixedHeight(156)
        self.preset_scroll.setAccessibleName('Темы оформления — прокручиваемая лента')
        strip = QWidget()
        strip.setObjectName('AppearancePresetStrip')
        presets = QHBoxLayout(strip)
        presets.setContentsMargins(18, 8, 18, 8)
        presets.setSpacing(16)
        self.preset_group = QButtonGroup(self)
        self.preset_group.setExclusive(True)
        self.preset_buttons = {}
        for preset in CHAT_THEME_IDS:
            button = AppearancePresetButton(preset, self.settings.theme)
            self.preset_group.addButton(button)
            button.setChecked(preset == self.settings.chat_theme)
            button.clicked.connect(lambda _checked=False, value=preset: self._set_chat_theme(value))
            self.preset_buttons[preset] = button
            presets.addWidget(button)
        self.preset_scroll.setWidget(strip)
        panel_layout.addWidget(self.preset_scroll)
        layout.addWidget(theme_panel)
        layout.addWidget(_label(
            'При выборе темы изменяются основные цвета, чаты и системные элементы. '
            'Изменения темы не синхронизируются между устройствами.',
            name='SettingsSecondary', wrap=True))
        layout.addSpacing(8)
        layout.addWidget(_label('Дополнительно: цвет акцента', name='SettingsSecondary'))
        swatches = QHBoxLayout()
        self._swatches = []
        for color in _ACCENTS:
            swatch = _Swatch(color, color == self.settings.accent)
            swatch.setAccessibleName('Цвет ' + color)
            swatch.clicked.connect(lambda _checked=False, value=color: self._set_accent(value))
            self._swatches.append(swatch)
            swatches.addWidget(swatch)
        swatches.addStretch(1)
        layout.addLayout(swatches)
        layout.addStretch(1)

    def _set_theme(self, scheme):
        if scheme not in self.theme_buttons or scheme == self.settings.theme:
            return
        self._save_pref('theme', scheme)
        self.apply_theme()
        self.theme_changed.emit()

    def _set_chat_font(self, index):
        scale = CHAT_FONT_SCALES[index]
        self._save_pref('chat_font_scale', scale)
        self.font_slider.setAccessibleDescription(f'{chat_message_font_pixels(scale)} пикселей')
        self.font_slider.setToolTip(self.font_slider.accessibleDescription())
        self.appearance_preview.apply_appearance(self.settings.theme, self.settings.chat_theme, scale)
        self.theme_changed.emit()

    def _set_chat_theme(self, preset):
        if preset != self.settings.chat_theme:
            self._save_pref('chat_theme', preset)
            self.appearance_preview.apply_appearance(self.settings.theme, preset, self.settings.chat_font_scale)
            self.theme_changed.emit()

    def _set_accent(self, value):
        self._save_pref('accent', value)
        for swatch in self._swatches:
            swatch.set_selected(swatch.color == value)
        self.theme_changed.emit()

    def _build_storage(self):
        layout = self._page('storage')
        self.page_widgets['storage'].widget().layout().setContentsMargins(32, 32, 32, 32)
        layout.setSpacing(8)
        caption = _label('ПУТЬ ДЛЯ СОХРАНЕНИЯ', name='SettingsSectionCaption')
        self._bind_text(caption, 'ПУТЬ ДЛЯ СОХРАНЕНИЯ')
        layout.addWidget(caption)
        self.download_path_row = SecuritySettingRow(str(self.settings.download_dir or ''), icon='folder')
        self.download_path_row.clicked.connect(self._choose_download_folder)
        self.download_path = self.download_path_row.title_label
        self.download_path.setAccessibleName('Папка загрузок')
        self.storage_card = SecurityCard()
        self.storage_card.add_row(self.download_path_row)
        layout.addWidget(self.storage_card)
        self.storage_info = _label('', name='SettingsSecondary', wrap=True)
        self.storage_info.hide()
        layout.addStretch(1)

    def _choose_download_folder(self):
        folder = QFileDialog.getExistingDirectory(self, 'Папка загрузок', self.settings.download_dir)
        if folder:
            self._save_pref('download_dir', folder)
            self.download_path.setText(folder)

    def _open_download_folder(self):
        folder = Path(self.settings.download_dir) if self.settings.download_dir else None
        if folder is None or not folder.is_dir():
            self._message('Папка загрузок', 'Выберите существующую папку загрузок.')
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def _storage_summary(self):
        count = len(self.service.list_chats())
        self.storage_info.setText(f'В локальной истории: {count} чатов.\n'
                                  'Загруженные файлы сохраняются в выбранную папку.')

    def _text_page(self, key, title, text, action=None, callback=None):
        layout = self._page(key)
        label = _label(text, name='SettingsSecondary', wrap=True)
        self._bind_text(label, text)
        layout.addWidget(label)
        if action:
            button = QPushButton(action)
            self._bind_text(button, action)
            button.setObjectName('SettingsContentAction')
            button.clicked.connect(callback)
            layout.addWidget(button)
        layout.addStretch(1)

    def select_page(self, key):
        if key == 'invite':
            self._copy_invitation()
            return
        if key not in self.page_widgets:
            return
        self.current_page = key
        self.page_back.hide()
        if key == 'notifications':
            self.notification_panel.open_page('main')
            self.notification_panel.ensure_loaded()
        elif key == 'blocked':
            self.blocked_contacts.ensure_loaded()
        if key == 'folders':
            self.folder_list.ensure_loaded()
        elif key == 'stories':
            self.story_archive.ensure_loaded()
        navigation_key = 'about' if key == 'sferum' else 'security' if key in ('blocked', 'password') else key
        for name, button in self.nav_buttons.items():
            button.setChecked(name == navigation_key)
        self.pages.setCurrentWidget(self.page_widgets[key])
        self._update_page_title()
        if key == 'security' and not self._security_loaded:
            self._load_security()
        elif key == 'devices':
            self._load_devices()
        elif key == 'storage':
            self._storage_summary()
        elif key == 'appearance':
            self.preset_scroll.ensureWidgetVisible(self.preset_buttons[self.settings.chat_theme], 12, 0)
        if key == 'security':
            self.privacy_panel.ensure_loaded()

    def _update_page_title(self):
        key = self.current_page
        if key == 'notifications':
            from .notification_settings_panel import PAGE_TITLES
            self.page_title.setText(self._t(PAGE_TITLES[self.notification_panel.current_page]))
            return
        self.page_title.setText(self._t({
            'profile': 'Редактировать профиль', 'invite': 'Пригласить друзей',
            'notifications': 'Уведомления', 'security': 'Безопасность', 'devices': 'Устройства',
            'password': 'Пароль для входа',
            'blocked': 'Заблокированные контакты',
            'stories': 'Мои истории', 'folders': 'Папки', 'appearance': 'Оформление',
            'storage': 'Данные и память', 'language': 'Язык приложения', 'keyboard': 'Сочетания клавиш',
            'sferum': 'Войти в Сферум', 'business': 'MAX для бизнеса', 'help': 'Помощь', 'about': 'О приложении',
            'add_profile': 'Добавить профиль',
        }[key]))

    def _logout(self):
        answer = QMessageBox.question(self, 'Выйти из профиля?',
                                      'Для продолжения потребуется снова войти в MAX.',
                                      QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if answer == QMessageBox.StandardButton.Yes:
            self.logout_requested.emit()

    def _message(self, title, text):
        box = QMessageBox(QMessageBox.Icon.Information, title, '', parent=self)
        box.setTextFormat(Qt.TextFormat.PlainText)
        box.setText(str(text))
        box.exec()

    def _copy(self, text):
        QApplication.clipboard().setText(text)

    def _copy_invitation(self):
        from .invite_link import copy_invitation, show_menu_notice
        try:
            context = self.service.search_context()
            copy_invitation(self.service, context=context)
        except (AttributeError, ValueError, RuntimeError):
            show_menu_notice(self, self._t('Не удалось скопировать приглашение. Проверьте соединение с MAX и повторите действие.'))
            return
        show_menu_notice(self, self._t('Вы скопировали ссылку'))

    def apply_theme(self):
        self.language_panel.apply_theme(self.settings.theme)
        self.devices_intro.apply_theme(self.settings.theme)
        self.terminate_button.apply_theme(self.settings.theme)
        self.storage_card.apply_theme(self.settings.theme)
        self.about_report_card.apply_theme(self.settings.theme)
        for index in range(self.devices_list.count()):
            widget = self.devices_list.itemAt(index).widget()
            if isinstance(widget, DeviceSessionRow):
                widget.apply_theme(self.settings.theme)
        self.keyboard_reference.apply_theme(self.settings.theme)
        self.notification_panel.apply_theme(self.settings.theme)
        self.privacy_panel.apply_theme(self.settings.theme)
        for card in (self.password_card, self.password_actions_card, self.blacklist_card):
            card.apply_theme(self.settings.theme)
        self.folder_list.apply_theme(self.settings.theme)
        self.blocked_contacts.apply_theme(self.settings.theme)
        self.story_archive.apply_theme(self.settings.theme)
        dark = resolve_theme(self.settings.theme) == 'dark'
        bg = '#1B202A' if dark else '#EDEEF2'
        white = '#141821' if dark else '#FFFFFF'
        text = '#EAECEF' if dark else '#17191D'
        secondary = '#AAB3C2' if dark else '#656C77'
        border = '#303846' if dark else '#E1E3E8'
        hover = '#222B39' if dark else '#F4F7FA'
        selected = '#23354A' if dark else '#EAF4FF'
        action = '#303845' if dark else '#DDE0E7'
        self.setStyleSheet(f'''
            #SettingsNavigation {{background:{white};border-right:1px solid {border};}}
            #SettingsMenuBody, #SettingsNavGap {{background:{white};}}
            #SettingsTitle {{background:{white};font-size:20px;font-weight:700;padding-left:16px;}}
            #SettingsPageTitle {{background:{white};font-size:16px;font-weight:600;}}
            #SettingsPageHeader {{background:{white};}}
            #SettingsPageBack {{background:transparent;border:0;border-radius:20px;}}
            #SettingsPageBack:hover {{background:{hover};}}
            #SettingsPages, #SettingsPageScroll, #SettingsPageSurface, #SettingsPageBody {{background:{bg};border:0;}}
            #SettingsLabel, #SettingsAvatarWrap, #SettingsPageBody QCheckBox {{background:transparent;}}
            #SettingsProfileNav, #SettingsNavRow {{background:{white};border:0;border-radius:0;padding:0;text-align:left;}}
            #SettingsProfileNav:hover, #SettingsNavRow:hover {{background:{hover};}}
            #SettingsProfileNav:checked, #SettingsNavRow:checked {{background:{selected};}}
            #SettingsProfileNav QLabel, #SettingsNavRow QLabel {{background:transparent;}}
            #SettingsProfileName, #SettingsNavText {{color:{text};font-size:15px;font-weight:600;}}
            #SettingsSecondary {{color:{secondary};font-size:14px;background:transparent;}}
            #SettingsDivider {{background:{border};border:0;}}
            #SettingsSectionCaption {{color:{secondary};font-size:12px;background:transparent;padding-left:12px;}}
            #SettingsChoiceCard {{background:{white};border:0;border-radius:12px;}}
            #SettingsChoiceRow {{background:transparent;border:0;border-radius:12px;text-align:left;padding:0;}}
            #SettingsChoiceRow:hover {{background:{hover};}}
            #SettingsChoiceRow:focus {{border:1px solid #0085FF;}}
            #SettingsChoiceRow QLabel {{background:transparent;color:{text};font-size:15px;font-weight:600;}}
            #SettingsFieldRow, #SettingsAboutField {{background:{white};border:0;border-radius:16px;}}
            #SettingsAvatarButton {{background:transparent;border:0;padding:0;}}
            #SettingsAvatarButton:focus {{border:1px solid #0085FF;border-radius:48px;}}
            #SettingsAvatarCamera {{background:#0085FF;border:0;padding:0;border-radius:8px;}}
            #SettingsAvatarCamera:focus {{border:1px solid {text};}}
            #SettingsProfileField {{background:transparent;border:0;padding:0;font-size:15px;color:{text};}}
            #SettingsProfileField:focus {{border:1px solid #0085FF;border-radius:5px;}}
            #SettingsFieldRemaining {{background:transparent;color:{secondary};font-size:11px;}}
            #SettingsAboutField {{padding:14px;font-size:16px;color:{text};}}
            #SettingsAboutField:focus {{border:1px solid #0085FF;}}
            #SettingsProfileAction {{background:{action};border:0;border-radius:16px;color:{text};font-size:15px;font-weight:600;}}
            #SettingsProfileAction:hover {{background:{border};}}
            #SettingsContentAction {{background:{white};border:0;border-radius:12px;text-align:left;padding:16px;color:{text};}}
            #SettingsContentAction:hover {{background:{hover};}}
            #SettingsContentAction:disabled {{color:{secondary};}}
            #SettingsUpdateButton {{background:{action};border:0;border-radius:10px;padding:8px 12px;color:{secondary};}}
            #SettingsReadRow {{background:{white};border-radius:12px;padding:16px;color:{text};}}
            #SettingsNavRow:focus, #SettingsProfileNav:focus {{border:1px solid #0085FF;}}
            #AppearanceSectionCaption {{background:transparent;color:{secondary};font-size:12px;}}
            #AppearanceFontCard, #AppearanceThemePanel, #AppearancePresetScroll, #AppearancePresetStrip {{background:{white};border:0;border-radius:12px;}}
            #AppearanceSmallA, #AppearanceLargeA {{background:transparent;color:{secondary};padding:0;}}
            #AppearanceSmallA {{font-size:14px;}}
            #AppearanceLargeA {{font-size:22px;}}
            #AppearanceSegments {{background:{action};border:0;border-radius:10px;}}
            #AppearanceScheme {{background:transparent;color:{text};border:1px solid transparent;border-radius:8px;padding:0 4px;font-size:14px;}}
            #AppearanceScheme:checked {{background:{white};color:#0085FF;font-weight:600;}}
            #AppearanceScheme:hover {{background:{hover};}}
            #AppearanceScheme:focus {{border:1px solid #0085FF;}}
            #AppearancePreset {{background:{white};border:2px solid transparent;border-radius:12px;padding:0;}}
            #AppearancePreset:hover {{background:{hover};}}
            #AppearancePreset:focus {{border:2px solid {text};}}
            #AppearancePresetName {{background:transparent;color:{text};font-size:12px;}}
            #AppearancePresetName[selected="true"] {{color:#0085FF;font-weight:600;}}
            #AppearanceFontSlider {{background:transparent;border:0;min-height:28px;}}
            #AppearanceFontSlider::groove:horizontal {{height:4px;background:{border};border-radius:2px;}}
            #AppearanceFontSlider::sub-page:horizontal {{background:#0085FF;border-radius:2px;}}
            #AppearanceFontSlider::handle:horizontal {{background:{white};border:2px solid #0085FF;width:16px;margin:-7px 0;border-radius:9px;}}
            #AppearanceFontSlider::handle:horizontal:focus {{border:2px solid {text};}}
        ''')
        invite_color = '#B39AFF' if dark else '#7C4DFF'
        self._invite_label.setStyleSheet(f'background:transparent;color:{invite_color};')
        self.profile_more.setIcon(line_icon('more', text, 24))
        for scheme, button in self.theme_buttons.items():
            button.setChecked(scheme == self.settings.theme)
        self.appearance_preview.apply_appearance(
            self.settings.theme, self.settings.chat_theme, self.settings.chat_font_scale)
        for preset, button in self.preset_buttons.items():
            button.setChecked(preset == self.settings.chat_theme)
            button.preview.set_theme(self.settings.theme)
        for label, icon, key in self._nav_icon_labels:
            color = invite_color if key == 'invite' else ('#AAB3C2' if dark else '#7D8085')
            label.setPixmap(line_icon(icon, color, 24).pixmap(24, 24))


def _utf16_length(text):
    """Match the official HTML input/textarea maxlength string units."""
    return len(text.encode('utf-16-le', errors='surrogatepass')) // 2

