"""Contact-owned navigation and controls; no conversation identity is invented."""
from __future__ import annotations
from dataclasses import dataclass

from PySide6.QtCore import Qt, Signal, QSize
from PySide6.QtWidgets import QApplication, QFrame, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QScrollArea, QVBoxLayout, QWidget
from shiboken6 import isValid

from ..protocol.folder_contracts import int64
from ..core.models import Chat
from ..core.i18n import normalize_language, tr
from .async_runner import run_async
from .conversation_actions_menu import ConversationActionsMenu
from .conversation_info import ConversationInfo
from .theme import palette_for
from .widgets.avatar import Avatar
from .widgets.line_icon import line_icon


@dataclass(frozen=True)
class _ContactDialogFailure:
    stage: str
    error_class: str
    category: str


_DIALOG_STAGES = frozenset({'capture', 'read32', 'parse32', 'read48', 'parse48', 'remember', 'render', 'unknown'})
_DIALOG_ERRORS = frozenset({'ValueError', 'MaxError', 'MaxWriteNotStarted', 'MaxWriteOutcomeUnknown',
                          'TimeoutError', 'ConnectionError', 'OSError', 'RuntimeError', 'TypeError',
                          'KeyError', 'AttributeError', 'OtherError'})
_DIALOG_CATEGORIES = frozenset({'operation_failed', 'invalid_result', 'async_delivery_failed'})


def _log_contact_dialog_failure(service, failure):
    """Only fixed vocabulary goes to debug.log; no exception text or identity."""
    stage = failure.stage if failure.stage in _DIALOG_STAGES else 'unknown'
    kind = failure.error_class if failure.error_class in _DIALOG_ERRORS else 'OtherError'
    category = failure.category if failure.category in _DIALOG_CATEGORIES else 'operation_failed'
    logger = getattr(service, 'log', None)
    if callable(logger):
        try:
            logger(f'contact-dialog: stage={stage}; error={kind}; category={category}')
        except Exception:
            pass  # Diagnostics cannot replace the original operation outcome.


def _run_contact_dialog(service, method, context):
    try:
        return method(context)
    except Exception as error:
        stage = getattr(error, 'contact_dialog_stage', None)
        failure = _ContactDialogFailure(stage if isinstance(stage, str) and stage in _DIALOG_STAGES else 'unknown',
                                        type(error).__name__ if type(error).__name__ in _DIALOG_ERRORS else 'OtherError',
                                        'operation_failed')
        _log_contact_dialog_failure(service, failure)
        return failure


def confirm_action(parent, title, text):
    """Contact-only confirmation: explicit localized action and safe default."""
    language = getattr(getattr(parent, 'settings', None), 'ui_language', 'ru')
    box = QMessageBox(parent)
    box.setWindowTitle(title)
    box.setIcon(QMessageBox.Icon.Question)
    box.setTextFormat(Qt.TextFormat.PlainText)
    box.setText(text)
    box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
    box.button(QMessageBox.StandardButton.Yes).setText(title)
    box.button(QMessageBox.StandardButton.No).setText(tr('Отмена', language))
    box.setDefaultButton(QMessageBox.StandardButton.No)
    box.setEscapeButton(QMessageBox.StandardButton.No)
    return box.exec() == QMessageBox.StandardButton.Yes


class ContactEditPanel(QWidget):
    """Embedded native-style editor; submitting is always explicit."""
    submitted = Signal(str, str, object)
    delete_requested = Signal(object)
    back_requested = Signal(object)

    def __init__(self, parent=None, *, theme='light', language='ru'):
        super().__init__(parent)
        self._language = normalize_language(language)
        self._status_text = ''
        self._status_source = None
        self._context = None
        self._initial = ('', '')
        self._busy = False
        self._can_edit = False
        self._can_delete = False
        self.setObjectName('ContactEditPanel')
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        header = QFrame()
        header.setObjectName('ContactEditHeader')
        header.setFixedHeight(62)
        row = QHBoxLayout(header)
        row.setContentsMargins(18, 8, 18, 8)
        back = self.back_button = QPushButton()
        back.setIcon(line_icon('back', '#80848A', 24))
        back.setIconSize(QSize(24, 24))
        back.setFixedSize(36, 36)
        back.setAccessibleName('Назад')
        back.clicked.connect(lambda: self.back_requested.emit(self._context))
        row.addWidget(back)
        heading = self.heading = QLabel('Настройки контакта')
        heading.setTextFormat(Qt.TextFormat.PlainText)
        heading.setAlignment(Qt.AlignmentFlag.AlignCenter)
        heading.setStyleSheet('font-size:16px;font-weight:600;')
        row.addWidget(heading, 1)
        self.save_button = QPushButton('Сохранить')
        self.save_button.setObjectName('Primary')
        self.save_button.clicked.connect(self._save)
        self.save_button.setAutoDefault(False)
        row.addWidget(self.save_button)
        root.addWidget(header)
        scroll = self._scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        body = QWidget()
        body.setObjectName('ContactEditBody')
        body.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        outer = QVBoxLayout(body)
        outer.setContentsMargins(24, 46, 24, 36)
        content = self._content = QWidget()
        content.setObjectName('ContactEditContent')
        content.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        content.setMaximumWidth(624)
        outer.addWidget(content, 0, Qt.AlignmentFlag.AlignHCenter)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)
        self.avatar = Avatar(92)
        layout.addWidget(self.avatar, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addSpacing(12)
        self.first_name = self._field(layout, 'Имя', 'ContactEditFirstName')
        self.last_name = self._field(layout, 'Фамилия', 'ContactEditLastName')
        self.delete_button = QPushButton('Удалить контакт')
        self.delete_button.setObjectName('ContactEditDelete')
        self.delete_button.setIcon(line_icon('trash', '#F04452', 22))
        self.delete_button.setMinimumHeight(56)
        self.delete_button.clicked.connect(lambda: self.delete_requested.emit(self._context))
        layout.addWidget(self.delete_button)
        self.status = QLabel()
        self.status.setObjectName('ContactEditStatus')
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        outer.addStretch(1)
        scroll.setWidget(body)
        root.addWidget(scroll, 1)
        self.first_name.textChanged.connect(self._changed)
        self.last_name.textChanged.connect(self._changed)
        self.retranslate()
        self.apply_theme(theme)
        self._changed()

    def retranslate(self, language=None):
        """Relabel controls without touching the draft, capabilities or request."""
        if not isValid(self):
            return
        if language is not None:
            self._language = normalize_language(language)
        self.back_button.setAccessibleName(tr('Назад', self._language))
        self.back_button.setToolTip(tr('Назад', self._language))
        self.heading.setText(tr('Настройки контакта', self._language))
        self.save_button.setText(tr('Сохранить', self._language))
        self.delete_button.setText(tr('Удалить контакт', self._language))
        for field, source in ((self.first_name, 'Имя'), (self.last_name, 'Фамилия')):
            field.setAccessibleName(tr(source, self._language))
            field.setPlaceholderText(tr(source, self._language))
        self._render_status()

    def _render_status(self):
        self.status.setText(tr(self._status_source, self._language)
                            if self._status_source is not None else self._status_text)

    @staticmethod
    def _field(layout, caption, name):
        field = QLineEdit()
        field.setObjectName(name)
        field.setAccessibleName(caption)
        field.setPlaceholderText(caption)
        field.setMaxLength(60)
        field.setMinimumHeight(52)
        field.setMinimumWidth(260)
        layout.addWidget(field)
        return field

    def apply_theme(self, theme):
        p = palette_for(theme)
        background = '#EDEEF3' if p.name == 'light' else p.window
        self.setStyleSheet(f'''
            #ContactEditPanel,#ContactEditBody,#ContactEditContent {{background:{background};}}
            #ContactEditHeader {{background:{p.surface};}}
            #ContactEditStatus {{background:transparent;color:{p.text_dim};}}
            QLineEdit {{background:{p.surface};border:0;border-radius:16px;padding:0 14px;}}
            #ContactEditDelete {{background:{p.surface};color:#F04452;border:0;border-radius:16px;text-align:left;padding:0 18px;}}
        ''')

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        self._content.setFixedWidth(max(260, min(624, self._scroll.viewport().width() - 48)))

    def set_contact(self, snapshot, *, pixmap=None):
        self._context = snapshot.get('context')
        self._initial = tuple(snapshot.get(key) if isinstance(snapshot.get(key), str) else ''
                              for key in ('first_name', 'last_name'))
        self.first_name.setText(self._initial[0])
        self.last_name.setText(self._initial[1])
        self.avatar.set_identity(snapshot.get('title'), snapshot.get('contact_id'))
        self.avatar.set_photo(pixmap)
        self.update_capabilities(snapshot)
        self.set_busy(False)

    def update_capabilities(self, snapshot):
        self._can_edit = snapshot.get('availability', {}).get('edit') is True
        self._can_delete = snapshot.get('availability', {}).get('delete') is True
        self.first_name.setEnabled(self._can_edit and not self._busy)
        self.last_name.setEnabled(self._can_edit and not self._busy)
        self.delete_button.setEnabled(self._can_delete and not self._busy)
        self._changed()

    def set_busy(self, busy, status='', *, status_source=None):
        self._busy = busy
        self.first_name.setEnabled(not busy and self._can_edit)
        self.last_name.setEnabled(not busy and self._can_edit)
        self.delete_button.setEnabled(not busy and self._can_delete)
        self._status_text = status if isinstance(status, str) else ''
        self._status_source = status_source if isinstance(status_source, str) else None
        self._render_status()
        self._changed()

    def clear(self):
        self._context = None
        self._can_edit = self._can_delete = False
        self.first_name.clear()
        self.last_name.clear()
        self.avatar.set_photo(None)
        self.delete_button.setEnabled(False)

    def _changed(self, *_args):
        values = (self.first_name.text().strip(), self.last_name.text().strip())
        changed = values != self._initial
        self.save_button.setVisible(changed)
        self.save_button.setEnabled(self._context is not None and self._can_edit
                                    and not self._busy and changed and bool(values[0]))

    def _save(self):
        if self.save_button.isEnabled():
            self.submitted.emit(self.first_name.text().strip(), self.last_name.text().strip(), self._context)


class ContactProfilePanel(ConversationInfo):
    """A directory profile has an explicit add route, distinct from editing it."""
    add_requested = Signal(object)

    def __init__(self, parent=None, *, theme='light', language='ru'):
        super().__init__(parent, theme=theme, language=language)
        self._membership_reasons = {}
        self.add_button = QPushButton('Добавить в контакты')
        self.add_button.setObjectName('Primary')
        self.add_button.setMinimumHeight(44)
        self.add_button.clicked.connect(lambda: self.add_requested.emit(self._context))
        self.membership_hint = QLabel('Этот человек пока не добавлен в ваши контакты MAX.')
        self.membership_hint.setTextFormat(Qt.TextFormat.PlainText)
        self.membership_hint.setWordWrap(True)
        self.membership_hint.setObjectName('InfoSecondary')
        layout = self.content.layout()
        index = layout.indexOf(self.phone_card) + 1
        layout.insertWidget(index, self.membership_hint)
        layout.insertWidget(index + 1, self.add_button)
        self.membership_hint.hide()
        self.add_button.hide()
        self.retranslate(language)

    def retranslate(self, language=None):
        if not isValid(self):
            return
        super().retranslate(language)
        # ConversationInfo.__init__ calls this override before child controls exist.
        if not hasattr(self, 'add_button'):
            return
        self.add_button.setText(self._tr('Добавить в контакты'))
        self.add_button.setAccessibleName(self._tr('Добавить в контакты'))
        self.membership_hint.setText(self._tr('Этот человек пока не добавлен в ваши контакты MAX.'))
        if self._snapshot.get('phone_label_source') == 'Телефон (сохранённый)':
            self.phone_label.setText(self._tr('Телефон (сохранённый)'))
        self._render_membership_tooltips()

    def set_membership(self, snapshot, *, busy=False):
        unadded = (snapshot.get('fresh') is True and snapshot.get('is_added') is False
                   and snapshot.get('is_self') is not True)
        self.add_button.setVisible(unadded)
        self.membership_hint.setVisible(unadded)
        self.add_button.setEnabled(unadded and not busy and snapshot.get('availability', {}).get('add') is True
                                   and snapshot.get('pending') is not True)
        self._membership_reasons = contact_unavailable_reasons(snapshot, busy=busy)
        self._render_membership_tooltips()

    def _render_membership_tooltips(self):
        reasons = self._membership_reasons
        for action, button in self.buttons.items():
            if not button.isEnabled() and action in reasons:
                button.setToolTip(self._tr(reasons[action]))
        if not self.edit_button.isEnabled() and 'edit' in reasons:
            self.edit_button.setToolTip(self._tr(reasons['edit']))

    def clear(self):
        super().clear()
        self._membership_reasons = {}
        self.add_button.hide()
        self.membership_hint.hide()


def contact_unavailable_reasons(snapshot, *, busy=False):
    waiting = ('Дождитесь завершения текущего действия.' if busy else
               'Сначала проверьте состояние предыдущего действия.' if snapshot.get('pending') is True else
               'Обновите сведения о контакте.' if snapshot.get('fresh') is not True else
               'Это действие недоступно для данного контакта.')
    membership = ('Сначала добавьте человека в контакты MAX кнопкой «Добавить в контакты».'
                  if snapshot.get('fresh') is True and snapshot.get('is_added') is False else waiting)
    return {'call': 'Звонки в этой сборке пока не реализованы.',
            'video': 'Видеозвонки в этой сборке пока не реализованы.',
            'edit': membership, 'delete': membership, 'hide_local': waiting,
            'block': waiting, 'unblock': waiting,
            'chat': waiting, 'mute': 'Настройка звука доступна в карточке открытого чата.',
            'find': 'Откройте чат, чтобы искать в его переписке.',
            'share_contact': 'Откройте чат и выберите «Ещё» → «Поделиться контактом».',
            'clear_history': 'Откройте существующий чат для управления перепиской.',
            'hide_stories': 'Управление историями доступно в карточке существующего чата.',
            'delete_chat': 'Откройте существующий чат для его удаления.',
            'folder': 'Добавление в папку доступно в карточке существующего чата.'}


def contact_menu_entries(snapshot, *, profile=False, busy=False, language='ru'):
    """Native contact-row order, with the supported profile actions separated."""
    availability = snapshot.get('availability', {})
    available = lambda key: availability.get(key) is True and not busy and snapshot.get('pending') is not True
    blocked = snapshot.get('blocked') is True
    rows = [
        {'action': 'chat', 'label': 'Написать', 'icon': 'chats', 'write': False,
         'available': availability.get('chat') is True and not busy},
        {'action': 'call', 'label': 'Позвонить', 'icon': 'phone', 'write': False, 'available': False},
        {'action': 'video', 'label': 'Видеозвонок', 'icon': 'video', 'write': False, 'available': False},
        {'action': 'unblock' if blocked else 'block',
         'label': 'Разблокировать' if blocked else 'Заблокировать', 'icon': 'block',
         'write': True, 'danger': not blocked, 'separator_before': True,
         'available': available('unblock' if blocked else 'block')},
        {'action': 'delete', 'label': 'Удалить контакт', 'icon': 'trash', 'write': True,
         'danger': True, 'available': available('delete')},
    ]
    if profile:
        block = dict(rows[3])
        rows = [
            {'action': 'share_contact', 'label': 'Поделиться контактом', 'available': False, 'write': False},
            {'action': 'clear_history', 'label': 'Стереть переписку', 'available': False,
             'write': True, 'separator_before': True},
            {'action': 'hide_stories', 'label': 'Скрыть истории автора', 'available': False,
             'write': True, 'separator_before': True},
            block,
            {'action': 'delete_chat', 'label': 'Удалить чат', 'available': False, 'write': True},
            {'action': 'folder', 'label': 'Добавить в папку', 'available': False, 'write': True},
        ]
    if (snapshot.get('fresh') is True and snapshot.get('is_added') is False
            and snapshot.get('is_self') is not True and snapshot.get('is_local_hidden') is not True):
        rows.append({'action': 'hide_local', 'label': 'Убрать из локального списка',
                     'icon': 'trash', 'write': True, 'separator_before': True,
                     'available': available('hide_local')})
    if snapshot.get('pending') is True:
        rows.append({'action': 'refresh', 'label': 'Проверить состояние', 'icon': 'refresh',
                     'available': not busy, 'write': False, 'separator_before': True})
    reasons = contact_unavailable_reasons(snapshot, busy=busy)
    for row in rows:
        row['label_source'] = row['label']
        row['label'] = tr(row['label_source'], language)
        if row.get('available') is not True:
            source = reasons.get(row['action'], 'Действие сейчас недоступно.')
            row['unavailable_reason_source'] = source
            row['unavailable_reason'] = tr(source, language)
    return rows


class ContactProfileMixin:
    """All callbacks retain one exact contact/account/session, even without a chat."""
    def _init_contact_profile(self):
        self._contact_context = None
        self._contact_snapshot = {}
        self._contact_panel = None
        self._contact_job = None
        self._contact_row_menu = None
        self._contact_edit_panel = None
        self._contact_edit_after_refresh = False
        self._contact_local_draft_id = None

    def _contact_current(self, context):
        try:
            return (isValid(self) and self._search_enabled and context is not None
                    and context is self._contact_context and self.service.is_current_contact(context) is True)
        except (AttributeError, ValueError):
            return False

    def _close_contact_profile(self):
        self._contact_context = self._contact_job = None
        self._contact_edit_after_refresh = False
        self._contact_local_draft_id = None
        self._contact_snapshot = {}
        if self._contact_row_menu is not None and isValid(self._contact_row_menu):
            self._contact_row_menu.cancel()
            self._contact_row_menu.deleteLater()
        self._contact_row_menu = None
        if self._contact_edit_panel is not None and isValid(self._contact_edit_panel):
            self._contact_edit_panel.clear()
            if self._conversation_pages.currentWidget() is self._contact_edit_panel:
                self._conversation_pages.setCurrentWidget(self.chat_view)
        if self._contact_panel is not None and isValid(self._contact_panel):
            self._contact_panel.clear()
            if self._conversation_pages.currentWidget() is self._contact_panel:
                self._conversation_pages.setCurrentWidget(self.chat_view)

    def _clear_previous_contact_chat(self):
        # Keep the draft before invalidating pending history/read receipt callbacks.
        if self.chat_view.chat is not None:
            self.chat_view._drafts[self.chat_view.chat.id] = self.chat_view.input.toPlainText()
        self._current_chat_id = None
        self.chat_view.show_placeholder()

    def _capture_selected_contact(self, contact_id):
        self._close_contact_profile()
        try:
            context = self.service.contact_context(contact_id)
            snapshot = self.service.contact_snapshot(context)
        except (AttributeError, ValueError):
            return None
        self._contact_context = context
        if (not self._contact_current(context) or not self._accept_contact_snapshot(snapshot, context)):
            self._close_contact_profile()
            return None
        return context

    def _accept_contact_snapshot(self, snapshot, context):
        if (not self._contact_current(context) or not isinstance(snapshot, dict)
                or snapshot.get('context') is not context
                or snapshot.get('contact_id') != context.contact_id
                or not int64(snapshot.get('contact_id')) or not isinstance(snapshot.get('title'), str)):
            return False
        self._contact_snapshot = dict(snapshot)
        return True

    def _select_contact(self, contact_id, title):
        """A Contacts click always targets that row, never the previous chat."""
        if not self._search_enabled or not int64(contact_id):
            return False
        context = self._capture_selected_contact(contact_id)
        if context is None:
            self._close_conversation_info()
            self._clear_previous_contact_chat()
            # A failed authenticated capture is not permission to retry under a
            # replacement account. Only legacy synthetic clients lack this API.
            if callable(getattr(self.service, 'contact_context', None)):
                self.statusBar().showMessage(tr('Сведения о контакте недоступны. Откройте контакты заново.',
                                               self.settings.ui_language), 5000)
                return False
            return self._open_contact_by_id(contact_id, title)
        self._close_conversation_info()
        try:
            chat = self.service.dialog_for_contact(contact_id)
        except (AttributeError, ValueError):
            chat = None
        if not self._contact_current(context):
            self._clear_previous_contact_chat()
            return False
        if chat is not None:
            self._current_chat_id = chat.id
            self.chat_view.open_chat(chat)
        else:
            self._clear_previous_contact_chat()
            self._show_contact_profile(context)
        return True

    def _show_contact_profile_by_id(self, contact_id):
        context = self._capture_selected_contact(contact_id)
        if context is None:
            return False
        self._close_conversation_info()
        self._show_contact_profile(context)
        return True

    def _show_contact_profile(self, context, *, refresh=True):
        if not self._contact_current(context):
            return
        if self._contact_panel is None:
            self._contact_panel = ContactProfilePanel(theme=self.settings.theme, language=self.settings.ui_language)
            self._contact_panel.setObjectName('ContactProfile')
            self._contact_panel.back_requested.connect(self._contact_profile_back)
            self._contact_panel.action_requested.connect(self._contact_action)
            self._contact_panel.add_requested.connect(lambda owner: self._contact_action('add', owner))
            self._contact_panel.menu_action_requested.connect(
                lambda action, _value, owner: self._contact_action(action, owner))
            self._conversation_pages.addWidget(self._contact_panel)
        self._render_contact_profile()
        self._conversation_pages.setCurrentWidget(self._contact_panel)
        if refresh:
            self._start_contact_operation('refresh', context)

    def _render_contact_profile(self, *, status=None, status_source=None):
        context = self._contact_context
        if not self._contact_current(context) or self._contact_panel is None:
            return
        snapshot = dict(self._contact_snapshot)
        availability = snapshot.get('availability', {})
        snapshot.update(peer_id=context.contact_id, chat_id=None,
                        action_pending=snapshot.get('pending') is True or self._contact_job is not None,
                        more_actions=contact_menu_entries(snapshot, profile=True, busy=self._contact_job is not None,
                                                          language=self.settings.ui_language),
                        tabs={}, availability={'edit': availability.get('edit') is True,
                          'chat': availability.get('chat') is True, 'more': True,
                          'call': False, 'video': False, 'mute': False, 'find': False})
        if status is not None or status_source is not None:
            snapshot['action_status'] = status if isinstance(status, str) else ''
            snapshot['action_status_source'] = status_source if isinstance(status_source, str) else None
            snapshot['action_status_values'] = {}
            snapshot['action_status_values_sources'] = {}
        if snapshot.get('phone_source') == 'local':
            snapshot['phone_label_source'] = 'Телефон (сохранённый)'
            snapshot['phone_label'] = tr(snapshot['phone_label_source'], self.settings.ui_language)
        self._contact_panel.set_conversation(snapshot)
        self._contact_panel.set_membership(self._contact_snapshot, busy=self._contact_job is not None)
        # No conversation exists to query media from. The contact card remains useful.
        self._contact_panel.tabs_card.hide()
        workspace = self._contacts_workspace
        if workspace is not None and workspace.context_current():
            pixmap = workspace.contact_avatar(context.contact_id)
            if pixmap is not None:
                self._contact_panel.avatar.set_photo(pixmap)

    def _contact_profile_back(self):
        self._conversation_pages.setCurrentWidget(self.chat_view)

    def _refresh_contact_profile_state(self):
        """Push/cache updates can revoke buttons without resetting typed names."""
        context = self._contact_context
        if context is None or self._contact_job is not None:
            return
        if not self._contact_current(context):
            self._close_contact_profile()
            return
        try:
            snapshot = self.service.contact_snapshot(context)
        except (AttributeError, ValueError):
            return
        if snapshot == self._contact_snapshot:
            return
        if not self._accept_contact_snapshot(snapshot, context):
            return
        self._render_contact_profile()
        menu = self._contact_row_menu
        if menu is not None and isValid(menu) and menu.isVisible():
            menu.replace_entries(contact_menu_entries(self._contact_snapshot, language=self.settings.ui_language))
        panel = self._contact_edit_panel
        if panel is not None and panel._context is context:
            panel.update_capabilities(self._contact_snapshot)

    def _open_chat_header_info(self, chat):
        # Existing conversations keep their full media/common-chats presenter.
        if (self._contact_current(self._contact_context)
                and chat.id == self._contact_local_draft_id
                and chat.peer_user_id == self._contact_context.contact_id):
            self._show_contact_profile(self._contact_context)
            return
        self._open_conversation_info(chat)

    def _edit_conversation_contact(self, contact_id):
        context = self._capture_selected_contact(contact_id)
        if context is None:
            return False
        self._close_conversation_info()
        self._contact_edit_after_refresh = True
        self._show_contact_profile(context)
        return True

    def _contact_context_menu(self, contact_id, point):
        if (self._contacts_workspace is None or not self._contacts_workspace.context_current()
                or not int64(contact_id)):
            return
        # Right-click selects the recipient but does not open/read their conversation.
        context = self._capture_selected_contact(contact_id)
        if context is None:
            return
        self._close_conversation_info()
        self._clear_previous_contact_chat()
        self._show_contact_profile(context, refresh=False)
        menu = ConversationActionsMenu(context, contact_menu_entries(self._contact_snapshot,
                                       language=self.settings.ui_language), self,
             theme=self.settings.theme, language=self.settings.ui_language, native_rows=True,
             allowed=lambda row: self._contact_menu_allowed(context, row))
        self._contact_row_menu = menu
        menu.requested.connect(lambda action, _value, owner: self._contact_action(action, owner))
        menu.popup(point)
        self._start_contact_operation('refresh', context)

    def _contact_menu_allowed(self, context, row):
        if not self._contact_current(context) or self._contact_job is not None:
            return False
        if row.get('write') is not False and self._contact_snapshot.get('pending') is True:
            return False
        if row.get('action') in ('edit', 'add'):
            return self._contact_snapshot.get('availability', {}).get(row['action']) is True
        return any(item['action'] == row.get('action') and item['available'] is True
                   for item in contact_menu_entries(self._contact_snapshot))

    def _contact_action(self, action, context):
        if not self._contact_current(context) or self._contact_job is not None:
            return
        if action == 'copy_phone':
            phone = self._contact_snapshot.get('phone')
            if isinstance(phone, str) and phone:
                QApplication.clipboard().setText(phone)
            return
        if not self._contact_menu_allowed(context, {'action': action, 'write': action not in ('chat', 'refresh')}):
            return
        if action == 'chat':
            self._open_contact_chat(context)
            return
        if action == 'edit':
            self._edit_contact_profile(context)
            return
        if action == 'hide_local':
            language = self.settings.ui_language
            label = tr('Убрать из локального списка', language)
            text = tr('Убрать «{name}» из локального списка этого приложения? Переписка сохранится.',
                      language, name=self._contact_snapshot['title'])
            if not confirm_action(self, label, text) or not self._contact_current(context):
                return
        if action in ('block', 'unblock', 'delete'):
            language = self.settings.ui_language
            label = tr({'block': 'Заблокировать', 'unblock': 'Разблокировать',
                        'delete': 'Удалить контакт'}[action], language)
            source = {'block': 'Заблокировать «{name}»?', 'unblock': 'Разблокировать «{name}»?',
                      'delete': 'Удалить контакт «{name}»? Переписка останется.'}[action]
            text = tr(source, language, name=self._contact_snapshot['title'])
            if not confirm_action(self, label, text) or not self._contact_current(context):
                return
        self._start_contact_operation(action, context)

    def _open_contact_chat(self, context):
        """Resolve or open the local empty dialog; never send or add a contact."""
        if not self._contact_current(context) or self._contact_job is not None:
            return
        method = getattr(self.service, 'open_contact_dialog', None)
        if not callable(method):
            return
        request = self._contact_job = object()
        self._render_contact_profile(status_source='Открытие чата…')

        def failed(_error, *, diagnostic=False):
            if diagnostic:
                _log_contact_dialog_failure(self.service, _ContactDialogFailure('unknown', 'OtherError',
                                                                               'async_delivery_failed'))
            if self._contact_current(context) and self._contact_job is request:
                self._contact_job = None
                self._render_contact_profile(status_source='Не удалось открыть чат. Попробуйте ещё раз.')

        def done(result):
            if not self._contact_current(context) or self._contact_job is not request:
                return
            if isinstance(result, _ContactDialogFailure):
                failed(None)
                return
            chat = result.get('chat') if isinstance(result, dict) else None
            if (not isinstance(result, dict) or result.get('context') is not context
                    or not isinstance(chat, Chat) or not int64(chat.id)
                    or chat.peer_user_id != context.contact_id
                    or type(result.get('local_draft')) is not bool):
                _log_contact_dialog_failure(self.service, _ContactDialogFailure('render', 'ValueError', 'invalid_result'))
                failed(None)
                return
            self._contact_job = None
            self._contact_local_draft_id = chat.id if result['local_draft'] else None
            self._conversation_pages.setCurrentWidget(self.chat_view)
            self._current_chat_id = chat.id
            self.chat_view.open_chat(chat)
            self.chat_view.input.setFocus()

        run_async(_run_contact_dialog, self.service, method, context,
                  on_done=done, on_error=lambda _error: failed(None, diagnostic=True))

    def _edit_contact_profile(self, context):
        if not self._contact_current(context):
            return
        if self._contact_edit_panel is None:
            self._contact_edit_panel = ContactEditPanel(theme=self.settings.theme, language=self.settings.ui_language)
            self._contact_edit_panel.submitted.connect(
                lambda first, last, owner: self._start_contact_operation('edit', owner, first, last))
            self._contact_edit_panel.delete_requested.connect(lambda owner: self._contact_action('delete', owner))
            self._contact_edit_panel.back_requested.connect(
                lambda owner: self._show_contact_profile(owner, refresh=False))
            self._conversation_pages.addWidget(self._contact_edit_panel)
        pixmap = self._contact_panel.avatar.cached_photo() if self._contact_panel is not None else None
        self._contact_edit_panel.set_contact(self._contact_snapshot, pixmap=pixmap)
        self._conversation_pages.setCurrentWidget(self._contact_edit_panel)

    def _start_contact_operation(self, action, context, *values):
        if not self._contact_current(context) or self._contact_job is not None:
            return
        if action != 'refresh' and not self._contact_menu_allowed(context, {'action': action, 'write': True}):
            return
        methods = {'refresh': ('refresh_contact_profile', ()),
                   'add': ('add_contact_profile', ()),
                   'edit': ('rename_contact_profile', values),
                   'delete': ('delete_contact_profile', ()),
                   'hide_local': ('hide_contact_profile', ()),
                   'block': ('set_contact_profile_blocked', (True,)),
                   'unblock': ('set_contact_profile_blocked', (False,))}
        if action not in methods:
            return
        name, args = methods[action]
        method = getattr(self.service, name, None)
        if not callable(method):
            return
        request = self._contact_job = object()
        editing = (self._contact_edit_panel is not None
                   and self._conversation_pages.currentWidget() is self._contact_edit_panel)
        if editing:
            self._contact_edit_panel.set_busy(True, status_source='Сохранение…')
        self._render_contact_profile(status_source='Обновление…' if action == 'refresh' else 'Сохранение…')
        if self._contact_row_menu is not None and isValid(self._contact_row_menu):
            self._contact_row_menu.revalidate()

        def done(result=None, *, failed=False):
            if not self._contact_current(context) or self._contact_job is not request:
                return
            self._contact_job = None
            if not self._accept_contact_snapshot(result, context):
                try:
                    result = self.service.contact_snapshot(context)
                except (AttributeError, ValueError):
                    result = None
                if not self._accept_contact_snapshot(result, context):
                    self._close_contact_profile()
                    return
            status_source = 'Не удалось подтвердить действие. Обновите сведения.' if failed else None
            if action == 'hide_local':
                hidden = self._contact_snapshot.get('is_local_hidden') is True
                status_source = ('Контакт убран из локального списка. Переписка сохранена.' if hidden and not failed else
                                 'Не удалось убрать контакт из локального списка. Попробуйте ещё раз.')
            self._render_contact_profile(status_source=status_source)
            if editing:
                self._contact_edit_panel.update_capabilities(self._contact_snapshot)
                if failed and self._contact_snapshot.get('pending') is not True:
                    self._contact_edit_panel.set_busy(False, status_source='Не удалось сохранить изменения. Введённые данные сохранены в форме.')
                else:
                    self._conversation_pages.setCurrentWidget(self._contact_panel)
            if self._contact_edit_after_refresh:
                self._contact_edit_after_refresh = False
                if not failed and self._contact_snapshot.get('availability', {}).get('edit') is True:
                    self._edit_contact_profile(context)
            menu = self._contact_row_menu
            if menu is not None and isValid(menu) and menu.isVisible():
                menu.replace_entries(contact_menu_entries(self._contact_snapshot, language=self.settings.ui_language))
            self._refresh_contact_sidebar()
            self._schedule_reload()

        run_async(method, context, *args, on_done=done, on_error=lambda _error: done(failed=True))
