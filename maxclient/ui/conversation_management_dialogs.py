"""Explicit destination and removal choices; these dialogs perform no RPC."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox, QLabel,
                              QLineEdit, QListWidget, QListWidgetItem, QVBoxLayout)
from shiboken6 import isValid

from ..core.i18n import normalize_language, tr
from ..protocol.folder_contracts import int64
from .conversation_bot_delete_dialog import bot_delete_result
from .conversation_owner_transfer_dialog import owner_transfer_result


def accepted_lifecycle_pending_notice(result, context, requested):
    """An acknowledged removal is waiting for readback, not a server refusal."""
    candidate = result.get('lifecycle_result') if requested == 'refresh_actions' and isinstance(result, dict) else result
    if (not isinstance(candidate, dict) or candidate.get('context') is not context
            or candidate.get('accepted') is not True or candidate.get('confirmed') is not False
            or candidate.get('pending') is not True or candidate.get('lifecycle_kind') is not None
            or type(candidate.get('chat_id')) is not int or candidate['chat_id'] != context.chat_id
            or candidate.get('action') not in ('clear', 'delete')
            or requested not in (candidate.get('action'), 'refresh_actions')):
        return None
    return ('MAX принял команду. Результат удаления ещё проверяется. '
            'Выберите «Проверить состояние»; повторное удаление не отправляется.')


def confirmed_lifecycle_action(result, context, requested):
    candidate = result.get('lifecycle_result') if requested == 'refresh_actions' and isinstance(result, dict) else result
    if (not isinstance(candidate, dict) or candidate.get('context') is not context
            or candidate.get('confirmed') is not True):
        return None
    action = candidate.get('action')
    if candidate.get('lifecycle_kind') == 'owner_transfer':
        transfer = owner_transfer_result(candidate, context)
        return ('leave' if transfer is not None and transfer.get('pending') is False
                and transfer.get('transfer_confirmed') is True and transfer.get('leave_confirmed') is True
                and requested in ('transfer_owner_leave', 'continue_owner_leave', 'refresh_actions') else None)
    if candidate.get('lifecycle_kind') == 'bot_delete':
        bot = bot_delete_result(candidate, context)
        return ('delete' if bot is not None and bot.get('pending') is False
                and bot.get('stop_confirmed') is True and bot.get('delete_confirmed') is True
                and requested in ('delete', 'continue_bot_delete', 'refresh_actions') else None)
    if action == 'leave' and (candidate.get('pending') is not False
                              or candidate.get('chat_id') != context.chat_id):
        return None
    return action if action in ('clear', 'delete', 'leave', 'share_contact') and requested in (action, 'refresh_actions') else None


def _dialog_language(parent, language):
    settings = getattr(parent, 'settings', None)
    if settings is None and parent is not None:
        settings = getattr(parent.window(), 'settings', None)
    return normalize_language(language if language is not None else
                              getattr(settings, 'ui_language', 'ru'))


class ShareContactDialog(QDialog):
    def __init__(self, title, chats, parent=None, *, language=None):
        super().__init__(parent)
        self._language = _dialog_language(parent, language)
        self._title = title
        self.selected_chat_id = None
        self.resize(420, 510)
        layout = QVBoxLayout(self)
        self.heading = QLabel()
        self.heading.setTextFormat(Qt.TextFormat.PlainText)
        self.heading.setWordWrap(True)
        layout.addWidget(self.heading)
        self.search = QLineEdit()
        layout.addWidget(self.search)
        self.chats = QListWidget()
        seen = set()
        for chat in chats:
            if int64(chat.id) and chat.id not in seen:
                seen.add(chat.id)
                item = QListWidgetItem(chat.display)
                item.setData(Qt.ItemDataRole.UserRole, chat.id)
                self.chats.addItem(item)
        layout.addWidget(self.chats, 1)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.send_button = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.send_button.setEnabled(False)
        self.send_button.setAutoDefault(False)
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setDefault(True)
        self.buttons.accepted.connect(self._choose)
        self.buttons.rejected.connect(self.reject)
        self.chats.itemSelectionChanged.connect(self._selection_changed)
        self.search.textChanged.connect(self._filter)
        layout.addWidget(self.buttons)
        self.set_language(self._language)

    def set_language(self, language):
        if not isValid(self):
            return
        self._language = normalize_language(language)
        self.setWindowTitle(tr('Поделиться контактом', self._language))
        self.heading.setText(tr('Кому отправить контакт «{title}»?', self._language, title=self._title))
        self.search.setPlaceholderText(tr('Найти чат', self._language))
        self.search.setAccessibleName(tr('Найти чат', self._language))
        self.send_button.setText(tr('Отправить', self._language))
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText(tr('Отмена', self._language))

    retranslate = set_language

    def _selection_changed(self):
        item = self.chats.currentItem()
        self.send_button.setEnabled(item is not None and item.isSelected() and not item.isHidden())

    def _filter(self, text):
        for index in range(self.chats.count()):
            item = self.chats.item(index)
            item.setHidden(text.casefold() not in item.text().casefold())
        self._selection_changed()

    def _choose(self):
        item = self.chats.currentItem()
        if item is not None and item.isSelected() and not item.isHidden():
            self.selected_chat_id = item.data(Qt.ItemDataRole.UserRole)
            self.accept()


class _ConversationRemovalDialog(QDialog):
    def __init__(self, parent, action, title, kind, forced, optional, language):
        super().__init__(parent)
        self._language = _dialog_language(parent, language)
        self._title = title
        self._caption_source = 'Стереть переписку' if action == 'clear' else 'Удалить чат'
        self._body_source = {
            ('clear', True): 'Стереть переписку «{title}» для всех участников?',
            ('clear', False): 'Стереть переписку «{title}» только у вас?',
            ('delete', True): 'Удалить чат «{title}» для всех участников?',
            ('delete', False): 'Удалить чат «{title}» только у вас?',
        }[action, forced]
        self._choice_source = ('Также удалить для всех участников' if kind in ('group', 'channel', 'video')
                               else 'Также удалить у собеседника')
        self.resize(420, 180)
        layout = QVBoxLayout(self)
        self.body = QLabel()
        self.body.setTextFormat(Qt.TextFormat.PlainText)
        self.body.setWordWrap(True)
        layout.addWidget(self.body)
        self.choice = QCheckBox()
        self.choice.setChecked(False)
        self.choice.setVisible(optional)
        layout.addWidget(self.choice)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setAutoDefault(False)
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setDefault(True)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.set_language(self._language)

    def set_language(self, language):
        if not isValid(self):
            return
        self._language = normalize_language(language)
        caption = tr(self._caption_source, self._language)
        self.setWindowTitle(caption)
        self.body.setText(tr(self._body_source, self._language, title=self._title))
        self.choice.setText(tr(self._choice_source, self._language))
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText(caption)
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText(tr('Отмена', self._language))

    retranslate = set_language


def confirm_conversation_removal(parent, action, title, capabilities, *, language=None):
    """Return selected for_all, or None on cancel. No destructive default key."""
    if action not in ('clear', 'delete'):
        return None
    kind = capabilities.get('kind')
    forced = (capabilities.get('clear_for_all') is True if action == 'clear'
              else kind in ('group', 'channel', 'video'))
    optional = capabilities.get(f'{action}_for_all_optional') is True and not forced
    dialog = _ConversationRemovalDialog(parent, action, title, kind, forced, optional, language)
    result = dialog.exec()
    selected = forced or (optional and dialog.choice.isChecked())
    dialog.deleteLater()
    return selected if result == QDialog.DialogCode.Accepted else None
